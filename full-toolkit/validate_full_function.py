"""Validate derive_expr.py against the warm-up RTL and named netlist.

Checks every DFF next-state expression literally, resolves the five formerly
isolated XOR inputs through the structural correlation, unrolls eight enabled
shift cycles with derive_expr's own Expr DAG, and exhaustively compares S with
the RTL predicate a_reg + b_reg == 496 for all 2^16 serial sample histories.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import correlate_netlist as cn
import derive_expr as de
import render_diagram as rd


REGISTER_NET_RE = re.compile(r"^([ab])_reg\[(\d+)]$")
SUSPECT_XORS = (631, 632, 633, 634, 972)


def expression_text(expr, name):
    return de.render_expr(expr, name)[-1].partition("=")[2].strip()


def find_output_driver(verilog, gate_map, net):
    drivers = []
    for inst_idx, pin in verilog.net_endpoints.get(net, []):
        inst = verilog.insts[inst_idx]
        info = gate_map[inst.cell_type]
        if pin in info.get("outputs", []):
            drivers.append((inst_idx, inst, pin))
    if len(drivers) != 1:
        raise AssertionError(f"Verilog net {net!r} has {len(drivers)} output drivers")
    return drivers[0]


def collect_leaves(root):
    variables = set()
    states = set()
    unknowns = set()
    seen = set()
    stack = [root]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        if isinstance(node, de.Var):
            variables.add(node.name)
        elif isinstance(node, de.StateRef):
            states.add(node.idx)
        elif isinstance(node, de.Unknown):
            unknowns.add(node.label)
        stack.extend(de.children(node))
    return variables, states, unknowns, len(seen)


def validate_registers(correlation, design, gate_map, node_budget):
    reverse_mapping = {v_idx: g_idx for g_idx, v_idx in correlation.mapping.items()}
    q_net_to_gidx = {}
    for g_idx, v_idx in correlation.mapping.items():
        inst = correlation.verilog.insts[v_idx]
        if inst.cell_type == "dfrtp":
            q_net_to_gidx[inst.pins["Q"]] = g_idx

    results = []
    one_hop = {}
    failures = []
    for g_idx in design.state_insts:
        v_idx = correlation.mapping[g_idx]
        dff = correlation.verilog.insts[v_idx]
        match = REGISTER_NET_RE.match(dff.pins["Q"])
        if not match:
            failures.append(f"Q{g_idx}: unexpected register Q net {dff.pins['Q']!r}")
            continue
        bank, bit_text = match.groups()
        bit = int(bit_text)

        d_net = dff.pins["D"]
        mux_idx, mux, mux_output_pin = find_output_driver(
            correlation.verilog, gate_map, d_net
        )
        literal_errors = []
        if mux.cell_type != "mux2" or mux_output_pin != "X":
            literal_errors.append(f"D driver is {mux.cell_type}.{mux_output_pin}, not mux2.X")
        expected_serial_net = bank.upper() if bit == 0 else f"{bank}_reg[{bit - 1}]"
        if mux.pins.get("A0") != dff.pins["Q"]:
            literal_errors.append(
                f"mux A0={mux.pins.get('A0')}, expected self Q={dff.pins['Q']}"
            )
        if mux.pins.get("A1") != expected_serial_net:
            literal_errors.append(
                f"mux A1={mux.pins.get('A1')}, expected {expected_serial_net}"
            )
        if mux.pins.get("S") != "en":
            literal_errors.append(f"mux S={mux.pins.get('S')}, expected en")
        if dff.pins.get("RESET_B") != "rst_n":
            literal_errors.append(
                f"RESET_B={dff.pins.get('RESET_B')}, expected rst_n"
            )

        new_value = (
            de.Var(bank.upper())
            if bit == 0
            else de.StateRef(q_net_to_gidx[expected_serial_net])
        )
        expected = de.mk_ite(
            de.Var("rst_n"),
            de.mk_ite(de.Var("en"), new_value, de.StateRef(g_idx)),
            de.ZERO,
        )
        actual, _, unresolved, _ = de.state_element_expr(design, g_idx, node_budget)
        one_hop[g_idx] = actual
        if unresolved:
            literal_errors.append(f"derived expression has unresolved nets {sorted(unresolved)}")
        if actual != expected:
            literal_errors.append(
                f"derived {expression_text(actual, f'Q{g_idx}')} != "
                f"expected {expression_text(expected, f'Q{g_idx}')}"
            )

        status = "PASS" if not literal_errors else "FAIL"
        results.append(
            (
                g_idx,
                dff,
                mux,
                bit,
                actual,
                status,
                literal_errors,
                reverse_mapping.get(mux_idx),
            )
        )
        failures.extend(f"Q{g_idx}: {error}" for error in literal_errors)
    return results, one_hop, failures


def unroll_and_bruteforce(design, one_hop, node_budget):
    # The RTL reset state is zero.  Each transition below is one enabled,
    # deasserted-reset rising edge, with fresh serial A/B samples.
    states = {idx: de.ZERO for idx in design.state_insts}
    for cycle in range(8):
        var_env = {
            "rst_n": de.ONE,
            "en": de.ONE,
            "A": de.Var(f"A{cycle}"),
            "B": de.Var(f"B{cycle}"),
        }
        states = {
            idx: de.substitute_expr(expr, vars=var_env, states=states)
            for idx, expr in one_hop.items()
        }

    s_net = next(net for net, name in design.io_output_name.items() if name == "S")
    s_one_hop, _, unresolved = de.compute_expr(design, s_net, node_budget)
    if unresolved:
        raise AssertionError(f"S still contains unresolved nets: {sorted(unresolved)}")
    closed_s = de.substitute_expr(s_one_hop, states=states)
    variables, state_refs, unknowns, node_count = collect_leaves(closed_s)
    expected_variables = {f"{bank}{cycle}" for bank in "AB" for cycle in range(8)}
    if variables != expected_variables or state_refs or unknowns:
        raise AssertionError(
            "incomplete S unroll: "
            f"vars={sorted(variables)}, states={sorted(state_refs)}, unknowns={sorted(unknowns)}"
        )

    mismatches = []
    derived_true_count = 0
    expected_true_count = 0
    for assignment in range(1 << 16):
        values = {
            f"A{cycle}": (assignment >> cycle) & 1 for cycle in range(8)
        }
        values.update(
            {f"B{cycle}": (assignment >> (8 + cycle)) & 1 for cycle in range(8)}
        )
        # From {parallel_out[6:0], serial_in}, the first sample advances to
        # bit 7 after eight shifts and the eighth (newest) sample is bit 0.
        a_value = sum(values[f"A{cycle}"] << (7 - cycle) for cycle in range(8))
        b_value = sum(values[f"B{cycle}"] << (7 - cycle) for cycle in range(8))
        actual = de.evaluate_expr(closed_s, values)
        expected = int(a_value + b_value == 496)
        derived_true_count += actual
        expected_true_count += expected
        if actual != expected and len(mismatches) < 20:
            mismatches.append((assignment, a_value, b_value, actual, expected))
    return closed_s, node_count, mismatches, derived_true_count, expected_true_count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gds", nargs="?", default="gds/04_final.gds")
    parser.add_argument("--netlist", default="warmup/01_netlist.v")
    parser.add_argument("--source", default="warmup/00_source.v")
    parser.add_argument("--gate-map", default="docs/gate-name-map.yaml")
    parser.add_argument("--max-nodes", type=int, default=200_000)
    args = parser.parse_args()

    source = Path(args.source).read_text()
    required_rtl = (
        "parallel_out <= {parallel_out[6:0], serial_in};",
        "assign sum = a + b;",
        "assign eq = (val == 9'd496);",
    )
    missing = [line for line in required_rtl if line not in source]
    if missing:
        raise SystemExit(f"source-semantics check failed; missing literal RTL: {missing}")

    gate_map = de.build_and_validate_gate_map(args.gate_map)
    correlation = cn.correlate(args.gds, args.netlist, args.gate_map)
    if (
        correlation.unmatched_gds
        or correlation.unmatched_verilog
        or correlation.partition_mismatches
    ):
        raise SystemExit("correlation is incomplete or topologically inconsistent")
    design = de.Design(correlation.gds, gate_map)

    results, one_hop, register_failures = validate_registers(
        correlation, design, gate_map, args.max_nodes
    )
    print("per-register validation:")
    for g_idx, dff, mux, bit, actual, status, errors, _ in results:
        print(
            f"  {status} Q{g_idx:<3} -> {dff.name:<9} {dff.pins['Q']:<8} "
            f"D={mux.name}({mux.pins['S']} ? {mux.pins['A1']} : {mux.pins['A0']}); "
            f"derived {expression_text(actual, f'Q{g_idx}')}"
        )
        for error in errors:
            print(f"       {error}")

    print("formerly unresolved xor2.B ground truth:")
    for g_idx in SUSPECT_XORS:
        v_idx = correlation.mapping[g_idx]
        inst = correlation.verilog.insts[v_idx]
        print(f"  GDS {g_idx} -> {inst.name}.B = {inst.pins['B']}")

    if register_failures:
        print("per-register verdict: FAIL")
        raise SystemExit(1)
    print(f"per-register verdict: PASS ({len(results)}/{len(design.state_insts)})")

    closed_s, node_count, mismatches, actual_true, expected_true = unroll_and_bruteforce(
        design, one_hop, args.max_nodes
    )
    print(
        "bit order: cycle 0 (first shifted sample) -> bit 7; "
        "cycle 7 (eighth/newest sample) -> bit 0"
    )
    print(f"fully unrolled S DAG: {node_count} distinct nodes, 16 A/B sample variables")
    if mismatches:
        print(f"full-function verdict: FAIL (showing {len(mismatches)} mismatches)")
        for assignment, a_value, b_value, actual, expected in mismatches:
            print(
                f"  assignment=0x{assignment:04x} a={a_value} b={b_value} "
                f"derived={actual} expected={expected}"
            )
        raise SystemExit(1)
    print(
        "full-function verdict: PASS (65,536/65,536 assignments; "
        f"derived true cases={actual_true}, expected true cases={expected_true})"
    )


if __name__ == "__main__":
    main()

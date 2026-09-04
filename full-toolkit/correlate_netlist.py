"""Correlate name-stripped GDS instances with a named flat Verilog netlist.

The matcher never uses instance order or placement.  It seeds a bijection
from unique cell types and named top-level I/O pins, then propagates through
pin-labelled shared nets.  A new pair is accepted only when it is the unique
best match in both directions among the remaining instances of that cell
type.  Finally, the complete pin/net partitions are checked in both
directions so a plausible but topologically inconsistent assignment fails.
"""
from __future__ import annotations

import argparse
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import gdsconn
import render_diagram as rd


INSTANCE_RE = re.compile(
    r"(?ms)^\s*(sky130_fd_sc_hd__\w+)\s+"
    r"((?:\\\S+)|(?:[A-Za-z_][\w$]*))\s*\((.*?)\);"
)
PIN_RE = re.compile(r"\.(\w+)\s*\(\s*([^)]*?)\s*\)")
PORT_RE = re.compile(
    r"(?m)^\s*(input|output)\s+(?:wire\s+)?(?:\[[^]]+\]\s+)?(\\?\S+?)\s*;"
)


def clean_identifier(value: str) -> str:
    value = value.strip()
    return value[1:] if value.startswith("\\") else value


@dataclass(frozen=True)
class VerilogInstance:
    name: str
    raw_cell: str
    cell_type: str
    pins: dict[str, str]


@dataclass
class VerilogNetlist:
    insts: list[VerilogInstance]
    net_endpoints: dict[str, list[tuple[int, str]]]
    inputs: set[str]
    outputs: set[str]


@dataclass
class Correlation:
    gds: gdsconn.Netlist
    verilog: VerilogNetlist
    mapping: dict[int, int]
    reasons: dict[int, str]
    unmatched_gds: list[int]
    unmatched_verilog: list[int]
    partition_mismatches: list[str]
    checked_signal_pins: int


def parse_verilog_netlist(path: str | Path, gate_map) -> VerilogNetlist:
    text = Path(path).read_text()
    insts = []
    for match in INSTANCE_RE.finditer(text):
        raw_cell, raw_name, body = match.groups()
        cell_type = rd.strip_cell_name(raw_cell)
        info = gate_map.get(cell_type)
        if not info or info.get("category") == "physical_only":
            continue
        pins = {pin: clean_identifier(net) for pin, net in PIN_RE.findall(body)}
        insts.append(
            VerilogInstance(clean_identifier(raw_name), raw_cell, cell_type, pins)
        )

    net_endpoints = defaultdict(list)
    for idx, inst in enumerate(insts):
        for pin, net in inst.pins.items():
            net_endpoints[net].append((idx, pin))

    ports = defaultdict(set)
    for direction, raw_name in PORT_RE.findall(text):
        ports[direction].add(clean_identifier(raw_name))
    return VerilogNetlist(insts, dict(net_endpoints), ports["input"], ports["output"])


def _signal_pins(gate_map, cell_type):
    info = gate_map[cell_type]
    return tuple(info.get("inputs", [])) + tuple(info.get("outputs", []))


def correlate(gds_path: str, verilog_path: str, gate_map_path: str) -> Correlation:
    gate_map = rd.load_gate_map(gate_map_path)
    gds = gdsconn.extract(gds_path)
    verilog = parse_verilog_netlist(verilog_path, gate_map)

    gds_logic = [
        idx
        for idx, inst in enumerate(gds.insts)
        if (info := rd.gate_info(inst.cell.name, gate_map))
        and info.get("category") != "physical_only"
    ]
    g_type = {idx: rd.strip_cell_name(gds.insts[idx].cell.name) for idx in gds_logic}
    v_type = {idx: inst.cell_type for idx, inst in enumerate(verilog.insts)}
    by_g_type = defaultdict(list)
    by_v_type = defaultdict(list)
    for idx in gds_logic:
        by_g_type[g_type[idx]].append(idx)
    for idx, inst in enumerate(verilog.insts):
        by_v_type[inst.cell_type].append(idx)

    mapping = {}
    reverse = {}
    reasons = {}

    def add_pair(g_idx, v_idx, reason):
        if g_idx in mapping or v_idx in reverse:
            return False
        mapping[g_idx] = v_idx
        reverse[v_idx] = g_idx
        reasons[g_idx] = reason
        return True

    # A cell type occurring once in each representation is an unambiguous
    # structural seed even before any neighbouring nets have been matched.
    for cell_type in sorted(set(by_g_type) | set(by_v_type)):
        gs, vs = by_g_type[cell_type], by_v_type[cell_type]
        if len(gs) == len(vs) == 1:
            add_pair(gs[0], vs[0], "unique cell type")

    io_names_by_gnet = defaultdict(set)
    for name, net in gds.io_net.items():
        if name not in ("VPWR", "VGND"):
            io_names_by_gnet[net].add(name)

    # Positive named-I/O anchors identify A/B serial muxes, the S driver, and
    # the root clock buffer.  Shared ports such as en/rst_n constrain but do
    # not by themselves force one of several otherwise-equivalent cells.
    for g_idx in gds_logic:
        if g_idx in mapping:
            continue
        anchored = False
        candidates = []
        for v_idx in by_v_type[g_type[g_idx]]:
            if v_idx in reverse:
                continue
            valid = True
            for pin in _signal_pins(gate_map, g_type[g_idx]):
                names = io_names_by_gnet.get(gds.pin_net.get((g_idx, pin)), set())
                if not names:
                    continue
                anchored = True
                if verilog.insts[v_idx].pins.get(pin) not in names:
                    valid = False
                    break
            if valid:
                candidates.append(v_idx)
        if anchored and len(candidates) == 1:
            add_pair(g_idx, candidates[0], "top-level I/O anchor")

    def pair_score(g_idx, v_idx):
        agreements = 0
        conflicts = 0
        for other_g, other_v in mapping.items():
            for pin in _signal_pins(gate_map, g_type[g_idx]):
                g_net = gds.pin_net.get((g_idx, pin))
                if g_net is None:
                    continue
                for other_pin in _signal_pins(gate_map, g_type[other_g]):
                    if g_net != gds.pin_net.get((other_g, other_pin)):
                        continue
                    if (
                        verilog.insts[v_idx].pins.get(pin)
                        == verilog.insts[other_v].pins.get(other_pin)
                    ):
                        agreements += 1
                    else:
                        conflicts += 1
        return agreements, -conflicts

    # Mutually unique best matches prevent a locally attractive pair from
    # stealing a candidate needed by a stronger match elsewhere.
    while True:
        proposals = []
        for g_idx in gds_logic:
            if g_idx in mapping:
                continue
            scored = sorted(
                (
                    (pair_score(g_idx, v_idx), v_idx)
                    for v_idx in by_v_type[g_type[g_idx]]
                    if v_idx not in reverse
                ),
                reverse=True,
            )
            if not scored or scored[0][0][0] == 0:
                continue
            if len(scored) > 1 and scored[0][0] == scored[1][0]:
                continue
            best_score, v_idx = scored[0]
            reciprocal = sorted(
                (
                    (pair_score(other_g, v_idx), other_g)
                    for other_g in by_g_type[g_type[g_idx]]
                    if other_g not in mapping
                ),
                reverse=True,
            )
            if reciprocal[0][1] != g_idx:
                continue
            if len(reciprocal) > 1 and reciprocal[0][0] == reciprocal[1][0]:
                continue
            proposals.append((best_score, g_idx, v_idx))

        changed = False
        for score, g_idx, v_idx in sorted(proposals, reverse=True):
            changed |= add_pair(
                g_idx,
                v_idx,
                f"unique topology propagation ({score[0]} matched pin-neighbours, {-score[1]} conflicts)",
            )
        if not changed:
            break

    # Safe final elimination only when one instance of a type remains on each
    # side.  The partition audit below still has to prove its connectivity.
    for cell_type in sorted(set(by_g_type) | set(by_v_type)):
        gs = [idx for idx in by_g_type[cell_type] if idx not in mapping]
        vs = [idx for idx in by_v_type[cell_type] if idx not in reverse]
        if len(gs) == len(vs) == 1:
            add_pair(gs[0], vs[0], "last remaining instance of cell type")

    unmatched_gds = sorted(idx for idx in gds_logic if idx not in mapping)
    unmatched_verilog = sorted(idx for idx in range(len(verilog.insts)) if idx not in reverse)

    # Audit equivalence of net partitions in both directions.  This is
    # stronger than checking the propagation edges: every declared signal pin
    # and every named I/O endpoint must group identically in both graphs.
    vnets_by_gnet = defaultdict(set)
    gnets_by_vnet = defaultdict(set)
    checked_signal_pins = 0
    for g_idx, v_idx in mapping.items():
        for pin in _signal_pins(gate_map, g_type[g_idx]):
            g_net = gds.pin_net.get((g_idx, pin))
            v_net = verilog.insts[v_idx].pins.get(pin)
            if g_net is None or v_net is None:
                continue
            checked_signal_pins += 1
            vnets_by_gnet[g_net].add(v_net)
            gnets_by_vnet[v_net].add(g_net)
    for name, g_net in gds.io_net.items():
        if name in verilog.inputs | verilog.outputs:
            vnets_by_gnet[g_net].add(name)
            gnets_by_vnet[name].add(g_net)

    partition_mismatches = []
    for g_net, v_nets in vnets_by_gnet.items():
        if len(v_nets) != 1:
            partition_mismatches.append(f"GDS net {g_net} maps to Verilog nets {sorted(v_nets)}")
    for v_net, g_nets in gnets_by_vnet.items():
        if len(g_nets) != 1:
            partition_mismatches.append(f"Verilog net {v_net} maps to GDS nets {sorted(g_nets)}")

    return Correlation(
        gds,
        verilog,
        mapping,
        reasons,
        unmatched_gds,
        unmatched_verilog,
        partition_mismatches,
        checked_signal_pins,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gds", nargs="?", default="gds/04_final.gds")
    parser.add_argument("--netlist", default="warmup/01_netlist.v")
    parser.add_argument("--gate-map", default="docs/gate-name-map.yaml")
    parser.add_argument("--only-state", action="store_true")
    args = parser.parse_args()

    result = correlate(args.gds, args.netlist, args.gate_map)
    print(
        f"matched {len(result.mapping)}/{len(result.mapping) + len(result.unmatched_gds)} "
        f"GDS logic instances; checked {result.checked_signal_pins} signal pins"
    )
    for g_idx in sorted(result.mapping):
        v_idx = result.mapping[g_idx]
        inst = result.verilog.insts[v_idx]
        is_state = inst.cell_type == "dfrtp"
        if args.only_state and not is_state:
            continue
        prefix = f"Q{g_idx}" if is_state else str(g_idx)
        q_net = f" Q={inst.pins['Q']}" if is_state else ""
        print(f"{prefix:>5} -> {inst.name:<20} [{inst.cell_type}{q_net}]  {result.reasons[g_idx]}")

    if result.unmatched_gds:
        print("unmatched GDS: " + ", ".join(map(str, result.unmatched_gds)))
    if result.unmatched_verilog:
        print(
            "unmatched Verilog: "
            + ", ".join(result.verilog.insts[idx].name for idx in result.unmatched_verilog)
        )
    if result.partition_mismatches:
        print("partition mismatches:")
        for mismatch in result.partition_mismatches:
            print(f"  {mismatch}")
    else:
        print("connectivity partition audit: PASS")

    if result.unmatched_gds or result.unmatched_verilog or result.partition_mismatches:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

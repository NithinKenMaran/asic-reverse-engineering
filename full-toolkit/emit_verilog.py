"""
Emit a structural Verilog netlist straight from a GDS, using gdsconn.py's
connectivity extraction and docs/gate-name-map.yaml's pin/category info -
no boolean derivation involved. Instantiates the real, raw
sky130_fd_sc_hd__<cell> names directly, meant to be simulated against the
actual vendor .functional.v models (see --sky130-root), not against any
logic derive_expr.py itself worked out. That makes this a genuinely
independent check of derive_expr.py's conclusions: a 4-state gate-level
simulator (iverilog) does the boolean evaluation and X-propagation, not our
own Python code.

  python3 emit_verilog.py gds/puzzle.gds --sky130-root /tmp/sky130_fd_sc_hd -o out/puzzle_netlist.v

Physical-only cells (decap/tapvpwrvgnd/diode/INTERNAL_*/VIA_*) are dropped -
no signal relevance, per gate-name-map.yaml's `category`.

Extraction-gap instances are also dropped rather than guessed at (see
CLAUDE.md's "known rough edges" - a number of instances on puzzle.gds have a
pin-fragment resolution ambiguity). Detection is a hard, unambiguous rule:
if two of an instance's own *different* declared pins - any direction, not
just input-vs-output - resolve to the *same* net, that pin resolution is
untrustworthy (an output feeding its own input is a hard contradiction in a
combinational cell; two different declared inputs silently reading the same
value is not impossible by construction but was never seen to be
intentional here) - drop that instance entirely rather than pick a side. If
a net still ends up with more than one real driver after that (not observed
so far, but not assumed impossible), every remaining driver on it is
dropped too and a warning is printed - never a silent guess as to which
driver is right.
Whatever a dropped instance was supposed to drive is simply left
unconnected (floating -> 'z' -> typically 'x' once read); iverilog's own
4-state simulation, not our judgement, decides whether that ends up
mattering for any particular simulation run.
"""
import argparse
import os
import re
import sys
from collections import defaultdict

import gdsconn
import render_diagram as rd
import derive_expr as de

PREFIX = "sky130_fd_sc_hd__"

BUS_RE = re.compile(r"^(.*)\[(\d+)\]$")


def find_self_colliding(netlist, gate_map):
    """Instances where two of the instance's own *different* declared pins
    (any direction - not just input-vs-output) resolve to the same net: the
    signature of the puzzle.gds pin-fragment collision gap (see module
    docstring). Originally only checked input-vs-output overlap (an output
    feeding its own input is a hard contradiction in a combinational cell),
    but debugging against an independently-recovered reference netlist
    (2026-09, see CLAUDE.md) found the same root cause also silently ties
    two *different* input pins together on wide gates like and4/and4b/
    and4bb - e.g. an and4bb genuinely needing 4 distinct signals instead
    computing on only 3, one used twice - with no multi-driver conflict to
    flag it, since nothing about "two inputs read the same value" is
    inherently invalid Verilog. Always drop these; never guess which pin is
    right - a real per-instance patch (see patch_from_recovered.py) is a
    stronger fix than any local heuristic here.

    Tried and reverted (2026-09-03): also blanket-dropping every instance
    with *any* netlist.ambiguous_pins entry (>1 candidate blob for some
    pin, regardless of collision) - motivated by chasing a `puzzle.gds`
    testbench mismatch that looked, from a single mux2's ambiguous select
    pin, like it might be another silent bad pick. Reverted immediately
    because it drops 2 instances on *warmup*, whose original sorted()[0]
    picks are proven correct by validate_full_function.py's exhaustive
    65,536/65,536 check - i.e. most ambiguous picks are actually fine, so
    "ambiguous" alone is too weak a signal to justify dropping. That
    particular mux2 turned out to be a red herring anyway - see CLAUDE.md,
    the real cause was a testbench race condition, not a netlist bug - but
    the general lesson stands: a real per-instance discrepancy needs
    simulation-level cross-reference (patch_from_recovered.py --debug-tb)
    to confirm, not a static structural rule alone."""
    bad = set()
    for idx, inst in enumerate(netlist.insts):
        info = rd.gate_info(inst.cell.name, gate_map)
        if not info:
            continue
        net_to_pins = defaultdict(set)
        for p in info.get("inputs", []) + info.get("outputs", []):
            net = netlist.pin_net.get((idx, p))
            if net is not None:
                net_to_pins[net].add(p)
        if any(len(pins) > 1 for pins in net_to_pins.values()):
            bad.add(idx)
    return bad


def drop_conflicted_instances(netlist, gate_map, keep):
    """Iteratively ensure no *kept* net has more than one real driver among
    *kept* instances. Converges in one pass in every case seen so far; the
    loop cap just guards against an unforeseen oscillation rather than
    hanging."""
    dropped_extra = set()
    for _ in range(10):
        drivers_by_net = defaultdict(list)
        for idx in keep:
            if idx in dropped_extra:
                continue
            inst = netlist.insts[idx]
            info = rd.gate_info(inst.cell.name, gate_map)
            for pin in info.get("outputs", []):
                net = netlist.pin_net.get((idx, pin))
                if net is not None:
                    drivers_by_net[net].append(idx)
        newly_bad = {idx for idxs in drivers_by_net.values() if len(idxs) > 1 for idx in idxs}
        newly_bad -= dropped_extra
        if not newly_bad:
            return dropped_extra
        print(f"[warn] {len(newly_bad)} instance(s) still multi-driving a net "
              f"after self-collision drop - dropping too: {sorted(newly_bad)}", file=sys.stderr)
        dropped_extra |= newly_bad
    return dropped_extra


def bus_group(names):
    """Group IO pin names like O[0]..O[7] into (base, {bit: name}); scalar
    names map to (name, None)."""
    buses = defaultdict(dict)
    scalars = []
    for name in names:
        m = BUS_RE.match(name)
        if m:
            buses[m.group(1)][int(m.group(2))] = name
        else:
            scalars.append(name)
    return buses, scalars


def verilog_ref(name):
    """A GDS IO pin name used directly as a Verilog port/net reference is
    already valid (bus bit-selects like O[3] are valid Verilog too)."""
    return name


def emit(gds_path, gate_map_path, sky130_root, out_path):
    gate_map = rd.load_gate_map(gate_map_path)
    netlist = gdsconn.extract(gds_path)
    design = de.Design(netlist, gate_map)  # reused only for input/output IO classification

    self_colliding = find_self_colliding(netlist, gate_map)

    candidate_keep = []
    for idx, inst in enumerate(netlist.insts):
        if netlist.inst_kind[idx] != "logic":
            continue
        info = rd.gate_info(inst.cell.name, gate_map)
        if not info or info.get("category") == "physical_only":
            continue
        if idx in self_colliding:
            continue
        candidate_keep.append(idx)

    dropped_extra = drop_conflicted_instances(netlist, gate_map, candidate_keep)
    keep = [idx for idx in candidate_keep if idx not in dropped_extra]
    total_dropped = self_colliding | dropped_extra

    print(f"[info] logic instances: {sum(1 for k in netlist.inst_kind if k == 'logic')} total, "
          f"{len(keep)} instantiated, {len(total_dropped)} dropped "
          f"({len(self_colliding)} self-colliding, {len(dropped_extra)} still-conflicted after that)",
          file=sys.stderr)

    # --- net naming ---
    io_name_of_net = {net: name for name, net in netlist.io_net.items() if name not in ("VPWR", "VGND")}
    net_name = {}
    for net, name in io_name_of_net.items():
        net_name[net] = verilog_ref(name)

    def name_for(net):
        if net in net_name:
            return net_name[net]
        n = f"n{net[0]}_{net[1]}"
        net_name[net] = n
        return n

    # --- ports (grouped into buses) ---
    input_names = sorted(set(design.io_input_name.values()))
    output_names = sorted(set(design.io_output_name.values()))
    in_buses, in_scalars = bus_group(input_names)
    out_buses, out_scalars = bus_group(output_names)

    lines = []
    module_name = netlist.top.name
    port_decls = []
    for name in in_scalars:
        port_decls.append(f"input  {name}")
    for base, bits in sorted(in_buses.items()):
        hi = max(bits)
        port_decls.append(f"input  [{hi}:0] {base}")
    for name in out_scalars:
        port_decls.append(f"output {name}")
    for base, bits in sorted(out_buses.items()):
        hi = max(bits)
        port_decls.append(f"output [{hi}:0] {base}")

    lines.append(f"module {module_name} (")
    lines.append(",\n".join(f"    {d}" for d in port_decls))
    lines.append(");")

    # --- gather instance pin connections first, so we know which internal
    # nets actually need a `wire` declaration ---
    inst_lines = []
    internal_nets_used = set()
    type_counts = defaultdict(int)
    for idx in sorted(keep):
        inst = netlist.insts[idx]
        stripped = rd.strip_cell_name(inst.cell.name)
        info = rd.gate_info(inst.cell.name, gate_map)
        type_counts[stripped] += 1
        conns = []
        for pin in info.get("inputs", []) + info.get("outputs", []):
            net = netlist.pin_net.get((idx, pin))
            if net is None:
                continue  # leave unconnected - floating in sim, not a guess
            if net not in io_name_of_net:
                internal_nets_used.add(net)
            conns.append(f".{pin}({name_for(net)})")
        lines_conns = ", ".join(conns)
        inst_lines.append(f"    {PREFIX}{stripped} g{idx} ({lines_conns});")

    if internal_nets_used:
        wire_decls = ", ".join(name_for(n) for n in sorted(internal_nets_used))
        lines.append(f"wire {wire_decls};")
    lines.extend(inst_lines)
    lines.append("endmodule")

    header = [
        f"// auto-generated by emit_verilog.py from {gds_path}",
        f"// {len(keep)} instances kept, {len(total_dropped)} dropped (see stderr log)",
        "`timescale 1ns/1ps",
    ]
    cell_dirs = []
    for stripped in sorted(type_counts):
        cell_dir = os.path.join(sky130_root, "cells", stripped)
        cell_dirs.append(cell_dir)
        path = os.path.join(cell_dir, f"{PREFIX}{stripped}.functional.v")
        header.append(f'`include "{path}"')

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        f.write("\n".join(header) + "\n\n" + "\n".join(lines) + "\n")

    print(f"[info] cell types used: {dict(sorted(type_counts.items()))}", file=sys.stderr)
    print(f"[info] wrote {out_path}", file=sys.stderr)
    # Some cell .functional.v files `include` a shared UDP primitive (dff/mux)
    # via a path relative to their own directory (e.g. "../../models/...").
    # iverilog only resolves that if told to search the including file's own
    # directory too, hence -I per cell dir actually used (harmless for types
    # that don't need it).
    iflags = " ".join(f"-I{d}" for d in cell_dirs)
    print(f"[info] iverilog include flags: {iflags}", file=sys.stderr)
    return module_name, input_names, output_names, iflags


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("gds")
    ap.add_argument("--gate-map", default="docs/gate-name-map.yaml")
    ap.add_argument("--sky130-root", required=True, help="checkout of google/skywater-pdk-libs-sky130_fd_sc_hd")
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args()
    emit(args.gds, args.gate_map, args.sky130_root, args.out)


if __name__ == "__main__":
    main()

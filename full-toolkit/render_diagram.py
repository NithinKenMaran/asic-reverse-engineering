"""
Render a gate-level-aligned hardware diagram, working backward from a
top-level output net, out to -L gate-hops.

  python3 render_diagram.py gds/04_final.gds -L 2 --from S

Levels: level 1 = the gate(s) whose output pin drives the requested net(s).
Level N = gates reached N gate-hops back from the output, via a breadth-first
walk over input pins. All gates found at the same level are placed in the
same Graphviz rank (`rank=same`), so they line up vertically; nets sit in
their own rank between the gate levels that produce/consume them.

Net "names" are the real IO pin name where a net reaches a top-level pin
(e.g. `S`, `clk`); otherwise there's no name for an internal net left in the
GDS (see CLAUDE.md), so we label it with what the GDS *does* give us: which
merged copper region it is, as `<layer>.b<blob index>`.

See gdsconn.py for how connectivity is extracted, and
docs/gate-name-map.yaml for the raw-cell-name -> readable-gate-name mapping
this depends on (including each gate's input/output pin names, needed here
to tell which pin is the driver when walking backward).
"""
import argparse
import re
import sys
import html
from collections import defaultdict

import yaml

import gdsconn

PREFIX = "sky130_fd_sc_hd__"


def load_gate_map(path):
    d = yaml.safe_load(open(path))
    return {g["raw"]: g for g in d["gates"]}


def strip_cell_name(raw_cell_name):
    s = raw_cell_name
    if s.startswith(PREFIX):
        s = s[len(PREFIX):]
    return re.sub(r"_\d+$", "", s)


def gate_info(raw_cell_name, gate_map):
    return gate_map.get(strip_cell_name(raw_cell_name))


def net_label(netlist, root):
    for (kind, idx, pin) in netlist.net_endpoints.get(root, []):
        if kind == "io":
            return pin
    layer_num, blob_idx = root
    return f"{gdsconn.LAYER_NAME.get(layer_num, layer_num)}.b{blob_idx}"


def bfs_levels(netlist, gate_map, start_nets, max_level):
    """Returns (depth_of_gate, depth_of_net): shortest gate-hop distance from
    the start net(s), walking backward against signal flow."""
    depth_of_net = {n: 0 for n in start_nets}
    depth_of_gate = {}

    frontier_nets = set(start_nets)
    gate_level = 1
    while frontier_nets and gate_level <= max_level:
        next_gates = set()
        for net in frontier_nets:
            for (kind, idx, pin) in netlist.net_endpoints.get(net, []):
                if kind != "inst" or idx in depth_of_gate:
                    continue
                info = gate_info(netlist.insts[idx].cell.name, gate_map)
                if info and pin in info.get("outputs", []):
                    depth_of_gate[idx] = gate_level
                    next_gates.add(idx)
        if not next_gates:
            break
        next_nets = set()
        for idx in next_gates:
            info = gate_info(netlist.insts[idx].cell.name, gate_map)
            for pin in info.get("inputs", []):
                net = netlist.pin_net.get((idx, pin))
                if net is None:
                    continue  # unresolved pin (e.g. RESET_B tie-off - see CLAUDE.md known gaps)
                if net not in depth_of_net:
                    depth_of_net[net] = gate_level
                next_nets.add(net)
        frontier_nets = next_nets
        gate_level += 1

    return depth_of_gate, depth_of_net


def gate_node_id(idx):
    return f"g{idx}"


def net_node_id(root):
    return f"n{root[0]}_{root[1]}"


def html_escape(s):
    return html.escape(str(s), quote=False)


def gate_html_label(readable_name, idx, inputs, outputs):
    rows = max(len(inputs), len(outputs), 1)
    lines = ['<TABLE BORDER="1" CELLBORDER="1" CELLSPACING="0" CELLPADDING="4">']
    for r in range(rows):
        lines.append("<TR>")
        if r < len(inputs):
            lines.append(f'<TD PORT="{html_escape(inputs[r])}">{html_escape(inputs[r])}</TD>')
        else:
            lines.append("<TD></TD>")
        if r == 0:
            lines.append(
                f'<TD ROWSPAN="{rows}"><B>{html_escape(readable_name)}</B><BR/>'
                f'<FONT POINT-SIZE="10">#{idx}</FONT></TD>'
            )
        if r < len(outputs):
            lines.append(f'<TD PORT="{html_escape(outputs[r])}">{html_escape(outputs[r])}</TD>')
        else:
            lines.append("<TD></TD>")
        lines.append("</TR>")
    lines.append("</TABLE>")
    return "\n".join(lines)


def net_html_label(name):
    return (
        '<TABLE BORDER="0" CELLBORDER="0" CELLSPACING="0" CELLPADDING="0">'
        f'<TR><TD><FONT POINT-SIZE="20">&#9679;</FONT></TD></TR>'
        f'<TR><TD>{html_escape(name)}</TD></TR>'
        "</TABLE>"
    )


def build_dot(netlist, gate_map, depth_of_gate, depth_of_net, title):
    out = []
    out.append("digraph hw {")
    out.append('  rankdir=LR;')
    out.append('  ranksep="0.8 equally";')
    out.append('  nodesep="0.4";')
    out.append(f'  label="{html_escape(title)}"; labelloc=t; fontsize=16;')
    out.append('  node [fontname="Helvetica"];')
    out.append('  edge [fontname="Helvetica", arrowsize=0.7];')

    # hop = distance from the output net counting both gates and nets as hops
    # (gate at depth g -> hop 2g-1; net at depth d -> hop 2d). same-hop nodes
    # get grouped into one `rank=same` so they align vertically.
    by_hop = defaultdict(list)  # hop -> [dot node id]

    for idx, g_depth in depth_of_gate.items():
        raw_name = netlist.insts[idx].cell.name
        info = gate_info(raw_name, gate_map)
        readable = info["name"] if info else raw_name
        inputs = info.get("inputs", []) if info else []
        outputs = info.get("outputs", []) if info else []
        nid = gate_node_id(idx)
        out.append(f'  {nid} [shape=plaintext label=<{gate_html_label(readable, idx, inputs, outputs)}>];')
        by_hop[2 * g_depth - 1].append(nid)

    for root, n_depth in depth_of_net.items():
        nid = net_node_id(root)
        name = net_label(netlist, root)
        out.append(f'  {nid} [shape=plaintext label=<{net_html_label(name)}>];')
        by_hop[2 * n_depth].append(nid)

    for hop, node_ids in sorted(by_hop.items()):
        ids = "; ".join(node_ids)
        out.append(f"  {{ rank=same; {ids}; }}")

    # edges: real signal-flow direction (net -> gate input port, gate output port -> net)
    seen_edges = set()
    for idx in depth_of_gate:
        raw_name = netlist.insts[idx].cell.name
        info = gate_info(raw_name, gate_map)
        if not info:
            continue
        gid = gate_node_id(idx)
        for pin in info.get("inputs", []):
            root = netlist.pin_net.get((idx, pin))
            if root is None or root not in depth_of_net:
                continue
            e = (net_node_id(root), f"{gid}:{pin}")
            if e in seen_edges:
                continue
            seen_edges.add(e)
            out.append(f'  {net_node_id(root)} -> {gid}:"{pin}";')
        for pin in info.get("outputs", []):
            root = netlist.pin_net.get((idx, pin))
            if root is None or root not in depth_of_net:
                continue
            e = (f"{gid}:{pin}", net_node_id(root))
            if e in seen_edges:
                continue
            seen_edges.add(e)
            out.append(f'  {gid}:"{pin}" -> {net_node_id(root)};')

    out.append("}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(
        description="Render a gate-level-aligned hardware diagram backward from an output net, out to -L gate-hops."
    )
    ap.add_argument("gds", nargs="?", default="gds/04_final.gds")
    ap.add_argument("-L", "--level", type=int, required=True, help="max gate-hops backward from the output net(s)")
    ap.add_argument("--from", dest="from_nets", default="S",
                     help="comma-separated top-level IO net name(s) to start from (default: S)")
    ap.add_argument("--gate-map", default="docs/gate-name-map.yaml")
    ap.add_argument("-o", "--out", default=None)
    args = ap.parse_args()

    gate_map = load_gate_map(args.gate_map)
    netlist = gdsconn.extract(args.gds)

    start_names = [s.strip() for s in args.from_nets.split(",") if s.strip()]
    start_nets = []
    for name in start_names:
        if name not in netlist.io_net:
            print(f"[error] IO net {name!r} not found. available IO nets: {sorted(netlist.io_net)}", file=sys.stderr)
            sys.exit(1)
        start_nets.append(netlist.io_net[name])

    depth_of_gate, depth_of_net = bfs_levels(netlist, gate_map, start_nets, args.level)

    print(f"[info] gates rendered: {len(depth_of_gate)}, nets rendered: {len(depth_of_net)}", file=sys.stderr)
    max_reached = max(depth_of_gate.values()) if depth_of_gate else 0
    print(f"[info] deepest gate level reached: {max_reached} (requested L={args.level})", file=sys.stderr)

    title = f"{args.gds} — from {','.join(start_names)}, L={args.level}"
    dot_src = build_dot(netlist, gate_map, depth_of_gate, depth_of_net, title)

    out_path = args.out or f"out/diagram_L{args.level}_from_{'_'.join(start_names)}.dot"
    import os
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        f.write(dot_src)
    print(f"[info] wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()

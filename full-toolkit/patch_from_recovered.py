"""
Patch the ~18 puzzle.gds instances emit_verilog.py has to drop (the known
pin-fragment collision gap - see CLAUDE.md) using the teammate's
independently-recovered ../jane-street-X/asic-puzzle-2026/recovered_puzzle.v
as an oracle for *just those specific gaps*, not as a wholesale replacement
for our own extraction.

Why this works: recovered_puzzle.v was built by an entirely different method
(DEF-based placement matching + real Liberty timing files, not GDS blob
connectivity), but every gate's `assign` line carries a trailing comment with
its exact physical placement in the *same* GDS database-unit coordinates our
own gdsconn.py extraction uses. Position is therefore a reliable, ~unique
join key between the two independently-derived netlists - one physical gate,
one (x, y). For any net our own extraction couldn't resolve, we look up the
gate at that position in their file, translate its expression's operand net
names back into *our* net names (recursively, by chasing each operand's own
producing position), and emit that as a plain `assign` for the specific net
we were missing - nothing else about our own netlist is touched or trusted
any less.

Cross-validated as it goes: every position lookup asserts the cell *type*
recovered_puzzle.v reports matches what our own GDS extraction found at that
same coordinate - a mismatch would mean the position join itself is wrong,
not just a data gap, and this raises loudly rather than patching over it.

  python3 patch_from_recovered.py gds/puzzle.gds \\
      --recovered ../../jane-street-X/asic-puzzle-2026/recovered_puzzle.v \\
      -o out/puzzle_patch.v
"""
import argparse
import re
import sys
from collections import Counter, defaultdict

import gdsconn
import render_diagram as rd
import derive_expr as de
import emit_verilog as ev

ASSIGN_RE = re.compile(r"^\s*assign\s+(\S+?)\s*=\s*(.+?);")
POS_COMMENT_RE = re.compile(r"//\s*sky130_fd_sc_hd__(\w+)\s*\((-?\d+),(-?\d+)\)")
TOKEN_RE = re.compile(r"\d+'b[01]+|[A-Za-z_]\w*(?:\[\d+\])?")
PRIMARY_INPUTS = {"I", "clk", "enable", "rst_n"}


SUFFIX_RE = re.compile(r"_\d+$")
ALWAYS_START_RE = re.compile(r"always\s*@\(posedge\s+(\S+?)(?:\s+or\s+negedge\s+rst_n)?\)\s*begin")
IF_RESET_RE = re.compile(r"if\s*\(!rst_n\)\s*(state_\d+)\s*<=\s*1'b([01]);")
ELSE_RE = re.compile(r"else\s*state_\d+\s*<=\s*(\S+);")
PLAIN_RE = re.compile(r"(state_\d+)\s*<=\s*(\S+);")


def parse_recovered(path):
    """net_name -> (x, y); net_name -> raw sky130 cell type (drive-strength
    suffix stripped, so it compares directly against our own
    rd.strip_cell_name output); net_name -> rhs expression text (only
    meaningful for combinational `assign` lines - for a register, the
    `assign netname = state_N; // TYPE (x,y)` line's own "expr" is just the
    literal token "state_N", not useful on its own; state_d_of[state_N]
    gives that register's real D-input operand, parsed from its `always`
    block below, which is what a register consumer actually needs."""
    lines = open(path).readlines()
    pos_of, celltype_of, expr_of = {}, {}, {}
    for line in lines:
        m = ASSIGN_RE.match(line)
        if not m:
            continue
        lhs, rhs = m.groups()
        expr_of[lhs] = rhs.strip()  # every assign's expr, even a bare tie-off with no position comment
        pm = POS_COMMENT_RE.search(line)
        if pm:
            ctype, x, y = pm.groups()
            pos_of[lhs] = (int(x), int(y))
            celltype_of[lhs] = SUFFIX_RE.sub("", ctype)

    state_d_of = {}  # state_N -> D-input operand token (register's real D source)
    i = 0
    while i < len(lines):
        m = ALWAYS_START_RE.search(lines[i])
        if not m:
            i += 1
            continue
        block = lines[i]
        j = i + 1
        while "end" not in lines[j] and j < len(lines):
            block += lines[j]
            j += 1
        mi = IF_RESET_RE.search(block)
        me = ELSE_RE.search(block)
        if mi and me:
            state_d_of[mi.group(1)] = me.group(1)
        else:
            mp = PLAIN_RE.search(block)
            if mp:
                state_d_of[mp.group(1)] = mp.group(2)
        i = j + 1
    return pos_of, celltype_of, expr_of, state_d_of


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("gds")
    ap.add_argument("--recovered", required=True)
    ap.add_argument("--gate-map", default="docs/gate-name-map.yaml")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--debug-tb", default=None,
                     help="also write a cycle-by-cycle register comparison testbench "
                          "(instantiates `puzzle` and `puzzle_ref` - drive it externally)")
    args = ap.parse_args()

    gate_map = rd.load_gate_map(args.gate_map)
    netlist = gdsconn.extract(args.gds)

    self_colliding = ev.find_self_colliding(netlist, gate_map)
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
    dropped_extra = ev.drop_conflicted_instances(netlist, gate_map, candidate_keep)
    dropped = self_colliding | dropped_extra
    kept = [idx for idx in candidate_keep if idx not in dropped]
    print(f"[info] {len(dropped)} dropped instances to patch: {sorted(dropped)}", file=sys.stderr)

    their_pos_of, their_celltype_of, their_expr_of, their_state_d_of = parse_recovered(args.recovered)
    their_at_pos = {pos: net for net, pos in their_pos_of.items()}
    print(f"[info] parsed {len(their_pos_of)} gate outputs, {len(their_state_d_of)} register D-inputs "
          f"from {args.recovered}", file=sys.stderr)
    REGISTER_TYPES = {raw for raw, g in gate_map.items() if g.get("category") == "register"}

    # recovered_puzzle.v's placement comments use each cell's *canonical*
    # (site-grid) bounding box corner, not klayout's raw SREF displacement -
    # for cells placed mirrored/rotated, those differ by a fixed correction:
    # empirically verified 722/722 exact matches across every gate type
    # (not just registers) in this design's placement, using only the 4
    # orientation codes it actually uses (r0, m0, m90, r180). ROW_PITCH is
    # the placement row height (2720, distinct from any one cell's own bbox
    # height - a site-grid property); MARGIN_X is the constant left/right
    # bbox overhang beyond the site-grid edge every cell in this library
    # shares (190 dbu on each side), which cancels the correction for pure
    # r0 placement and applies via the cell's own bbox width otherwise.
    ROW_PITCH = 2720
    MARGIN_X = 190

    def their_style_pos(inst):
        d = inst.cplx_trans.disp
        orient = str(inst.cplx_trans).split()[0]
        w = inst.cell.bbox().width() - 2 * MARGIN_X
        dx = -w if orient in ("m90", "r180") else 0
        dy = -ROW_PITCH if orient in ("m0", "r180") else 0
        return (d.x + dx, d.y + dy)

    my_pos_to_idx = {}
    for idx, inst in enumerate(netlist.insts):
        if netlist.inst_kind[idx] != "logic":
            continue
        my_pos_to_idx[their_style_pos(inst)] = idx

    io_name_of_net = {net: name for name, net in netlist.io_net.items() if name not in ("VPWR", "VGND")}

    def name_for(net):
        if net in io_name_of_net:
            return io_name_of_net[net]
        return f"n{net[0]}_{net[1]}"

    patch_cache = {}
    patch_lines = []

    def ensure_patched(my_idx):
        if my_idx in patch_cache:
            return patch_cache[my_idx]
        inst = netlist.insts[my_idx]
        stripped = rd.strip_cell_name(inst.cell.name)
        pos = their_style_pos(inst)
        their_net = their_at_pos.get(pos)
        if their_net is None:
            raise ValueError(f"g{my_idx} ({stripped}) @ {pos}: no matching gate in {args.recovered}")
        their_type = their_celltype_of[their_net]
        if their_type != stripped:
            raise ValueError(
                f"g{my_idx} @ {pos}: cell type mismatch - mine={stripped!r} theirs={their_type!r} "
                f"(position join is unreliable here, not just missing data)"
            )
        patched_name = f"patched_g{my_idx}"
        patch_cache[my_idx] = patched_name  # set before recursing (defensive; combinational, shouldn't cycle)
        expr = their_expr_of[their_net]
        translated = TOKEN_RE.sub(lambda m: translate(m.group(0)), expr)
        patch_lines.append(f"  assign {patched_name} = {translated};  // g{my_idx} {stripped} @ {pos}")
        return patched_name

    def translate(their_tok):
        if their_tok in PRIMARY_INPUTS:
            return their_tok
        if re.match(r"^\d+'b[01]+$", their_tok):
            return their_tok
        pos = their_pos_of.get(their_tok)
        if pos is None:
            # a net with no position comment is a bare tie-off constant in
            # their file too (e.g. "assign n15143 = 1'b0; // physically
            # undriven; tied low deterministically") - their own independent
            # extraction hit the same kind of gap and made the same "don't
            # guess, tie to a constant" call; reuse their literal directly.
            const = their_expr_of.get(their_tok)
            if const is not None and re.match(r"^\d+'b[01]+$", const):
                return const
            raise ValueError(f"no position for their net {their_tok!r} - can't translate")
        my_idx = my_pos_to_idx.get(pos)
        if my_idx is None:
            raise ValueError(f"no instance in our extraction at position {pos} (their net {their_tok!r})")
        if my_idx in dropped:
            return ensure_patched(my_idx)
        stripped = rd.strip_cell_name(netlist.insts[my_idx].cell.name)
        info = gate_map[stripped]
        outs = info.get("outputs", [])
        if len(outs) == 1:
            out_pin = outs[0]
        else:
            # conb: HI/LO, two independent constants from one instance -
            # both are always resolvable in our own extraction (conb is
            # never in the collision-prone/dropped set), so pick whichever
            # of the two has strictly more endpoints elsewhere in our own
            # net_endpoints - the broadcast/shared tie-off net feeding many
            # instances, which is what a widely-shared undriven net here is.
            candidates = [(p, netlist.pin_net.get((my_idx, p))) for p in outs]
            candidates = [(p, n) for p, n in candidates if n is not None]
            if not candidates:
                raise ValueError(f"our g{my_idx} ({stripped}) has no resolved output net at all")
            p, net = max(candidates, key=lambda pn: len(netlist.net_endpoints.get(pn[1], [])))
            return name_for(net)
        net = netlist.pin_net.get((my_idx, out_pin))
        if net is None:
            raise ValueError(f"our g{my_idx} ({stripped}) has no resolved {out_pin} net")
        return name_for(net)

    # driven-from-outside nets (kept instances' outputs + primary inputs)
    driven_nets = set()
    for idx in kept:
        info = rd.gate_info(netlist.insts[idx].cell.name, gate_map)
        for pin in info.get("outputs", []):
            net = netlist.pin_net.get((idx, pin))
            if net is not None:
                driven_nets.add(net)

    undriven_by_consumer = defaultdict(list)  # h_idx -> [(net, pin), ...]
    n_undriven_targets = 0
    for idx in kept:
        info = rd.gate_info(netlist.insts[idx].cell.name, gate_map)
        for pin in info.get("inputs", []):
            net = netlist.pin_net.get((idx, pin))
            if net is not None and net not in driven_nets and net not in io_name_of_net:
                undriven_by_consumer[idx].append((net, pin))
                n_undriven_targets += 1

    resolved, failed = [], []
    alias_lines = []
    seen_nets = set()
    for h_idx, targets in undriven_by_consumer.items():
        undriven_pins = {pin for _, pin in targets}
        info = rd.gate_info(netlist.insts[h_idx].cell.name, gate_map)
        try:
            # pins NOT currently undriven for this instance - a stable,
            # already-correct comparison base regardless of how many of its
            # *other* pins are simultaneously undriven too.
            my_other = Counter()
            for p in info.get("inputs", []):
                if p in undriven_pins:
                    continue
                n2 = netlist.pin_net.get((h_idx, p))
                if n2 is not None:
                    my_other[name_for(n2)] += 1

            h_pos = their_style_pos(netlist.insts[h_idx])
            their_h_net = their_at_pos.get(h_pos)
            if their_h_net is None:
                raise ValueError(f"consumer g{h_idx} @ {h_pos}: not found in {args.recovered}")
            their_h_type = their_celltype_of[their_h_net]
            my_h_type = rd.strip_cell_name(netlist.insts[h_idx].cell.name)
            if their_h_type != my_h_type:
                raise ValueError(f"consumer g{h_idx}: type mismatch mine={my_h_type} theirs={their_h_type}")

            if my_h_type in REGISTER_TYPES and "D" in undriven_pins:
                # a register's own D pin: their alias line's "expr" is just
                # the literal token "state_N" (not a boolean expression) -
                # the real D source lives in that state's `always` block,
                # already parsed into their_state_d_of. Single operand, no
                # set-matching against sibling pins needed at all.
                state_name = their_expr_of[their_h_net]
                d_tok = their_state_d_of.get(state_name)
                if d_tok is None:
                    raise ValueError(f"no always-block D-input found for {state_name} (their net {their_h_net})")
                sources = {"D": translate(d_tok)}
            else:
                operand_toks = TOKEN_RE.findall(their_expr_of[their_h_net])
                their_translated = Counter(translate(t) for t in operand_toks)
                leftover = their_translated - my_other
                if sum(leftover.values()) != len(undriven_pins):
                    raise ValueError(
                        f"ambiguous match: leftover={dict(leftover)} "
                        f"(expected exactly {len(undriven_pins)} for pins {sorted(undriven_pins)})"
                    )
                # Multiple simultaneously-undriven pins can't be told apart
                # by count alone; assigning them in a fixed (sorted) order
                # is only safe when those specific pins are mutually
                # symmetric in the gate's function (e.g. a21o's A1/A2) -
                # true for every case observed here, but not a proven
                # general fact, so it's called out explicitly in the patch
                # comment for auditability rather than asserted silently.
                sources = dict(zip(sorted(undriven_pins), sorted(leftover.elements())))

            for net, pin in targets:
                net_str = name_for(net)
                if net_str in seen_nets:
                    continue
                seen_nets.add(net_str)
                note = "" if len(undriven_pins) == 1 else f" (unordered among {sorted(undriven_pins)})"
                alias_lines.append(
                    f"  assign {net_str} = {sources[pin]};  // resolved via g{h_idx}.{pin} @ {h_pos}{note}"
                )
                resolved.append(net_str)
        except ValueError as e:
            for net, pin in targets:
                failed.append((name_for(net), str(e)))

    print(f"[info] resolved {len(resolved)}/{n_undriven_targets} undriven nets", file=sys.stderr)
    if failed:
        print(f"[warn] {len(failed)} could NOT be resolved (left undriven):", file=sys.stderr)
        for net_str, msg in failed:
            print(f"    {net_str}: {msg}", file=sys.stderr)

    # NOT a separate module: `n69_412` etc. are *internal* wires of the main
    # `puzzle` module emit_verilog.py already wrote, and Verilog module
    # bodies are separate namespaces - an `assign` for the same-named wire
    # in a different module would silently connect to nothing. These lines
    # are meant to be spliced into that module's body, before its
    # `endmodule` (see splice_into_module below / the -o output is a
    # fragment, not a standalone file).
    wire_decls = ", ".join(sorted(patch_cache.values()))
    out_lines = [
        f"  // auto-generated by patch_from_recovered.py - {len(dropped)} dropped instances,",
        f"  // {len(resolved)}/{n_undriven_targets} undriven nets resolved via {args.recovered}",
    ]
    if wire_decls:
        out_lines.append(f"  wire {wire_decls};")
    out_lines.extend(patch_lines)
    out_lines.extend(alias_lines)

    with open(args.out, "w") as f:
        f.write("\n".join(out_lines) + "\n")
    print(f"[info] wrote {args.out} ({len(patch_lines)} patch gate(s), {len(alias_lines)} net alias(es))", file=sys.stderr)

    if args.debug_tb:
        reg_pairs = []  # (my_idx, my_q_net_name, their_state_name)
        for idx, inst in enumerate(netlist.insts):
            info = rd.gate_info(inst.cell.name, gate_map)
            if not info or info.get("category") != "register":
                continue
            pos = their_style_pos(inst)
            their_net = their_at_pos.get(pos)
            if their_net is None:
                continue
            state_name = their_expr_of.get(their_net)
            q_net = netlist.pin_net.get((idx, "Q"))
            if state_name is None or q_net is None:
                continue
            reg_pairs.append((idx, name_for(q_net), state_name))
        n_registers = sum(
            1 for i in netlist.insts
            if (info := rd.gate_info(i.cell.name, gate_map)) and info.get("category") == "register"
        )
        print(f"[info] matched {len(reg_pairs)}/{n_registers} register pairs for debug comparison", file=sys.stderr)

        # broader pass: every kept instance's combinational output net too,
        # plus the patched gates themselves - pinpoints the exact first
        # divergent *combinational* signal, not just which register it
        # eventually reaches.
        all_pairs = list(reg_pairs)
        for idx in kept:
            info = rd.gate_info(netlist.insts[idx].cell.name, gate_map)
            outs = info.get("outputs", [])
            if info.get("category") == "register" or len(outs) != 1:
                continue  # registers already covered above; skip conb (2 outputs)
            pos = their_style_pos(netlist.insts[idx])
            their_net = their_at_pos.get(pos)
            if their_net is None:
                continue
            my_net = netlist.pin_net.get((idx, outs[0]))
            if my_net is None:
                continue
            all_pairs.append((idx, name_for(my_net), their_net))
        for my_idx, patched_name in patch_cache.items():
            pos = their_style_pos(netlist.insts[my_idx])
            their_net = their_at_pos.get(pos)
            if their_net is not None:
                all_pairs.append((my_idx, patched_name, their_net))
        print(f"[info] {len(all_pairs)} total signal pairs for debug comparison "
              f"({len(reg_pairs)} registers + {len(all_pairs)-len(reg_pairs)} combinational)", file=sys.stderr)

        checks = "\n".join(
            f'      if (dut_mine.{q} !== dut_theirs.{s}) '
            f'$display("MISMATCH cycle=%0d g{idx} mine.{q}=%b theirs.{s}=%b", cyc, dut_mine.{q}, dut_theirs.{s});'
            for idx, q, s in all_pairs
        )
        tb = f"""`timescale 1ns/1ps
module debug_compare_tb;
  reg clk = 0, rst_n = 0, enable = 0, I = 0;
  wire success_mine, success_theirs;
  wire [7:0] O_mine, O_theirs;
  integer cyc = 0;

  puzzle     dut_mine   (.I(I), .clk(clk), .enable(enable), .rst_n(rst_n), .success(success_mine), .O(O_mine));
  puzzle_ref dut_theirs (.I(I), .clk(clk), .enable(enable), .rst_n(rst_n), .success(success_theirs), .O(O_theirs));

  always #5 clk = ~clk;

  always @(posedge clk) begin
    cyc = cyc + 1;
{checks}
    if (success_mine !== success_theirs)
      $display("SUCCESS MISMATCH cycle=%0d mine=%b theirs=%b", cyc, success_mine, success_theirs);
  end
endmodule
"""
        with open(args.debug_tb, "w") as f:
            f.write(tb)
        print(f"[info] wrote {args.debug_tb} ({len(reg_pairs)} register checks/cycle) - "
              f"drive it externally, e.g. append the reset/121-bit/observe initial block", file=sys.stderr)


if __name__ == "__main__":
    main()

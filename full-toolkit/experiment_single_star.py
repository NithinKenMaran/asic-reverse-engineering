"""
Experiment 1: single-star injection sweep.

For each of the 121 serial positions, apply a board that is all-zero except
a single 1 at that position, run it through the real gate-level netlist,
and record every register that differs from the all-zero baseline. This is
the core empirical probe: which state elements react to a star at position
p reveals p's row, column, and region membership *from silicon behavior*,
with no assumption about grid layout or region boundaries.
"""
import json
import os
import time

from probe_tool import Prober, SRC_DIR

OUT = os.path.join(SRC_DIR, "..", "writeup", "evidence", "single_star_results.json")


def board_with_one(pos):
    return "0" * pos + "1" + "0" * (120 - pos)


def main():
    p = Prober()
    t0 = time.time()
    baseline = p.run("0" * 121)
    print(f"[info] baseline: success={baseline.success} O=0x{baseline.O:02x}")

    results = {}
    for pos in range(121):
        res = p.run(board_with_one(pos))
        diffs = {idx: v for idx, v in res.regs.items() if v != baseline.regs[idx]}
        results[pos] = {"success": res.success, "O": res.O, "diff_regs": diffs}
        if pos % 20 == 0:
            print(f"[info] pos={pos}/120 done, {len(diffs)} regs differ, elapsed={time.time()-t0:.1f}s")

    with open(OUT, "w") as f:
        json.dump({"baseline_regs": baseline.regs, "positions": results}, f)
    print(f"[info] wrote {OUT}, total elapsed={time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()

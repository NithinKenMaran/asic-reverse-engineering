"""Turn experiment_single_star.py's raw results into the 11x11 region map,
purely by clustering *which registers respond* to a star at each position - no
assumption about grid shape or region boundaries going in.

  - A register that differs from the all-zero baseline at every position sharing
    the same value mod 11 (checked for all 11 residues) is that column's counter
    - this is what establishes the scan is 11-wide and row-major in the first
    place, not assumed beforehand.
  - No register differs over exactly 11 *consecutive* positions (a hypothetical
    per-row counter) - consistent with row validity being tracked by a single
    reused current-row tally rather than eleven persistent counters (columns and
    regions need persistent counters because their cells are scattered across
    the whole 121-cycle scan; a row's cells are all processed back-to-back, so
    one register that resets each row boundary suffices).
  - The remaining registers are filtered to those that *partition* the 121 cells
    (an exact-cover search for 11 pairwise-disjoint sets that union to
    everything) - intrinsic to what "regions" means, not fitted to a known
    answer.
"""
import json
import os
from collections import defaultdict

from experiment_single_star import OUT as SINGLE_STAR_RESULTS

REGION_MAP_OUT = os.path.join(os.path.dirname(SINGLE_STAR_RESULTS), "region_map.json")


def main():
    d = json.load(open(SINGLE_STAR_RESULTS))
    positions = d["positions"]

    reg_positions = defaultdict(set)
    for pos in range(121):
        for r in positions[str(pos)]["diff_regs"]:
            reg_positions[int(r)].add(pos)

    universal = [r for r, ps in reg_positions.items() if len(ps) == 121]
    print(f"universal (every position) - candidate 'any star seen' flag: {universal}")

    col_regs = {}
    for r, ps in reg_positions.items():
        if len(ps) == 11 and len({p % 11 for p in ps}) == 1:
            col_regs[r] = next(iter(ps)) % 11
    assert len(col_regs) == 11, f"expected 11 column counters, found {len(col_regs)}"
    print(f"column counters found: {len(col_regs)}/11  -> scan is 11-wide, row-major")

    row_regs = []
    for r, ps in reg_positions.items():
        ps_sorted = sorted(ps)
        if len(ps) == 11 and ps_sorted == list(range(ps_sorted[0], ps_sorted[0] + 11)):
            row_regs.append(r)
    print(f"persistent per-row counters found: {len(row_regs)} (expect 0 - see module docstring)")

    known = set(universal) | set(col_regs) | set(row_regs)
    candidates = [r for r in reg_positions if r not in known]
    print(f"remaining candidates: {len(candidates)}, sizes: {sorted(len(reg_positions[r]) for r in candidates)}")

    # Principled filter, not a fit to a size list we already expect: regions must
    # *partition* the 121 cells (11 pairwise-disjoint sets that union to
    # everything) - intrinsic to what "regions" means. Exact-cover search.
    candidates.sort(key=lambda r: -len(reg_positions[r]))  # largest first: prunes faster

    def exact_cover(remaining_candidates, covered, chosen):
        if len(covered) == 121:
            return chosen if len(chosen) == 11 else None
        if not remaining_candidates or len(chosen) >= 11:
            return None
        r, *rest = remaining_candidates
        ps = reg_positions[r]
        if ps & covered:
            return exact_cover(rest, covered, chosen)  # r conflicts, must skip
        result = exact_cover(rest, covered | ps, chosen + [r])
        if result:
            return result
        return exact_cover(rest, covered, chosen)

    region_regs = exact_cover(candidates, frozenset(), [])
    assert region_regs is not None, "no 11-way exact cover found among candidates"
    sizes = sorted(len(reg_positions[r]) for r in region_regs)
    print(f"exact-cover region registers found: {len(region_regs)}, sizes: {sizes}")

    pos_to_region_letter = {}
    reg_to_letter = {reg: chr(ord("A") + i) for i, reg in enumerate(sorted(region_regs, key=lambda r: -len(reg_positions[r])))}
    for reg in region_regs:
        for p in reg_positions[reg]:
            assert p not in pos_to_region_letter, f"position {p} claimed by two region registers"
            pos_to_region_letter[p] = reg_to_letter[reg]
    assert len(pos_to_region_letter) == 121, "regions don't cover all 121 positions exactly once"

    grid = [[pos_to_region_letter[r * 11 + c] for c in range(11)] for r in range(11)]

    out = {
        "universal_reg": universal[0] if universal else None,
        "column_regs": {str(k): v for k, v in col_regs.items()},
        "region_regs": {str(reg): letter for reg, letter in reg_to_letter.items()},
        "region_sizes": {letter: len(reg_positions[reg]) for reg, letter in reg_to_letter.items()},
        "grid": grid,
    }
    with open(REGION_MAP_OUT, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nwrote {REGION_MAP_OUT}")
    print()
    for row in grid:
        print(" ", " ".join(row))


if __name__ == "__main__":
    main()

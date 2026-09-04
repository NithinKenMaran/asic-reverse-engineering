"""
Two-star Star Battle board representation and rule checker, built from the
region map empirically derived in experiment_single_star.py (not copied
from any outside source). Used two ways in this derivation:

1. To construct targeted single-rule-violating perturbations of the known
   winning board, whose predicted-vs-actual `success` bit is then checked
   against the real silicon (probe_tool.py) - i.e. this module encodes a
   *hypothesis* about the rules, and the experiments in
   experiment_perturbations.py are what actually tests that hypothesis
   against the hardware.
2. As the constraint model for the logical solver (solver.py), which is
   what derives the winning board in the first place without brute force
   or SAT.
"""
import json
import os

REGION_MAP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "writeup", "evidence", "region_map.json")

WINNING_BITS = (
    "0000000101010000100000000000010101010000000000001010000001000001000000"
    "100000101000010000000100000010000010010001010000000"
)
assert len(WINNING_BITS) == 121


def load_region_grid():
    d = json.load(open(REGION_MAP_PATH))
    return d["grid"]  # grid[row][col] -> region letter


def bits_to_grid(bits):
    assert len(bits) == 121
    return [[int(bits[r * 11 + c]) for c in range(11)] for r in range(11)]


def grid_to_bits(grid):
    return "".join(str(grid[r][c]) for r in range(11) for c in range(11))


def violations(grid, region_grid):
    """Return a dict describing every rule violation in `grid`. Empty dict
    means the board is fully valid under the two-star Star Battle rules."""
    v = {}
    total = sum(sum(row) for row in grid)
    if total != 22:
        v["total"] = total

    for r in range(11):
        c = sum(grid[r])
        if c != 2:
            v.setdefault("rows", {})[r] = c
    for c in range(11):
        s = sum(grid[r][c] for r in range(11))
        if s != 2:
            v.setdefault("cols", {})[c] = s

    region_counts = {}
    for r in range(11):
        for c in range(11):
            if grid[r][c]:
                reg = region_grid[r][c]
                region_counts[reg] = region_counts.get(reg, 0) + 1
    all_regions = {region_grid[r][c] for r in range(11) for c in range(11)}
    for reg in all_regions:
        cnt = region_counts.get(reg, 0)
        if cnt != 2:
            v.setdefault("regions", {})[reg] = cnt

    adjacent_pairs = []
    for r in range(11):
        for c in range(11):
            if not grid[r][c]:
                continue
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if dr == 0 and dc == 0:
                        continue
                    r2, c2 = r + dr, c + dc
                    if 0 <= r2 < 11 and 0 <= c2 < 11 and grid[r2][c2]:
                        if (r, c) < (r2, c2):
                            adjacent_pairs.append(((r, c), (r2, c2)))
    if adjacent_pairs:
        v["adjacent"] = adjacent_pairs
    return v


if __name__ == "__main__":
    region_grid = load_region_grid()
    grid = bits_to_grid(WINNING_BITS)
    v = violations(grid, region_grid)
    print("winning board violations (expect none):", v)
    print("total stars:", sum(sum(row) for row in grid))
    for row in grid:
        print(" ", "".join("*" if x else "." for x in row))

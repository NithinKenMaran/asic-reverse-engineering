"""Experiment 2: differential/perturbation testing against a valid board, run
through the recovered netlist (probe.py) and checked against the rule
hypothesis in star_battle.py.

Each test predicts (from the Python rule model) whether a perturbed board should
pass, then checks the *actual* netlist `success` bit agrees. This is what
validates the rule hypothesis - matching the star pattern is necessary but not
sufficient; the rules have to be shown to be *enforced* by the silicon. The
rectangle-swap test is the important one: it isolates the no-adjacency rule from
every counting rule, by construction (row/column/region counts and total all
held exactly constant while a touching pair is created).
"""
import itertools
import json
import os

from probe import Prober
from star_battle import WINNING_BITS, bits_to_grid, grid_to_bits, load_region_grid, violations

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evidence", "perturbation_results.json")


def find_first_star(grid):
    for r in range(11):
        for c in range(11):
            if grid[r][c]:
                return r, c


def find_isolated_addable_cell(grid):
    """An empty cell with no starred 8-neighbor, so adding a star there can't
    also trigger an adjacency violation - keeps this a count-only test."""
    for r in range(11):
        for c in range(11):
            if grid[r][c]:
                continue
            if any(
                0 <= r + dr < 11 and 0 <= c + dc < 11 and grid[r + dr][c + dc]
                for dr in (-1, 0, 1) for dc in (-1, 0, 1)
                if not (dr == 0 and dc == 0)
            ):
                continue
            return r, c


def find_rectangle_swap_adjacency_only(grid, region_grid):
    """Search all pairs of existing stars for a "rectangle swap" - remove two,
    add stars at the opposite two corners - which by construction preserves every
    row sum and column sum exactly. If it also preserves every region count and
    creates a fresh 8-adjacency, the result violates *only* the adjacency rule."""
    stars = [(r, c) for r in range(11) for c in range(11) if grid[r][c]]
    for (r1, c1), (r2, c2) in itertools.combinations(stars, 2):
        if r1 == r2 or c1 == c2:
            continue
        new_grid = [row[:] for row in grid]
        new_grid[r1][c1] = 0
        new_grid[r2][c2] = 0
        if new_grid[r1][c2] or new_grid[r2][c1]:
            continue  # already occupied
        new_grid[r1][c2] = 1
        new_grid[r2][c1] = 1
        v = violations(new_grid, region_grid)
        if set(v.keys()) == {"adjacent"}:
            return new_grid, (r1, c1), (r2, c2), v
    return None


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    p = Prober()
    region_grid = load_region_grid()
    base_grid = bits_to_grid(WINNING_BITS)
    base_v = violations(base_grid, region_grid)
    assert not base_v

    tests = {}

    res = p.run(WINNING_BITS)
    tests["valid_board"] = {"violations": {}, "predicted_pass": True, "success": res.success, "O": res.O}

    r, c = find_first_star(base_grid)
    g = [row[:] for row in base_grid]
    g[r][c] = 0
    v = violations(g, region_grid)
    res = p.run(grid_to_bits(g))
    tests["remove_one_star"] = {
        "cell": [r, c], "violations": {k: str(x) for k, x in v.items()},
        "predicted_pass": not v, "success": res.success, "O": res.O,
    }

    r, c = find_isolated_addable_cell(base_grid)
    g = [row[:] for row in base_grid]
    g[r][c] = 1
    v = violations(g, region_grid)
    res = p.run(grid_to_bits(g))
    tests["add_one_isolated_star"] = {
        "cell": [r, c], "violations": {k: str(x) for k, x in v.items()},
        "predicted_pass": not v, "success": res.success, "O": res.O,
    }

    found = find_rectangle_swap_adjacency_only(base_grid, region_grid)
    if found:
        g, (r1, c1), (r2, c2), v = found
        res = p.run(grid_to_bits(g))
        tests["adjacency_only_swap"] = {
            "moved": [[r1, c1], "->", [r1, c2]], "and": [[r2, c2], "->", [r2, c1]],
            "violations": {k: str(x) for k, x in v.items()},
            "predicted_pass": not v, "success": res.success, "O": res.O,
        }
    else:
        tests["adjacency_only_swap"] = None
        print("[warn] no adjacency-only rectangle swap found")

    print(json.dumps(tests, indent=2))
    with open(OUT, "w") as f:
        json.dump(tests, f, indent=2)
    print(f"[info] wrote {OUT}")

    all_ok = all(
        t is not None and (t["success"] == 1) == t["predicted_pass"]
        for t in tests.values()
    )
    print(f"\nALL PREDICTIONS MATCHED SILICON: {all_ok}")


if __name__ == "__main__":
    main()

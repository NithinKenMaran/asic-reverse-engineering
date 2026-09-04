import json

STAR, BLANK, UNKNOWN = 1, 0, -1


class Contradiction(Exception):
    pass


class Board:
    def __init__(self, region_grid):
        self.region_grid = region_grid
        self.cells = [[UNKNOWN] * 11 for _ in range(11)]
        self.regions = sorted({region_grid[r][c] for r in range(11) for c in range(11)})
        self.guesses = 0

    def clone(self):
        b = Board.__new__(Board)
        b.region_grid = self.region_grid
        b.cells = [row[:] for row in self.cells]
        b.regions = self.regions
        b.guesses = self.guesses
        return b

    def neighbors(self, r, c):
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                nr, nc = r + dr, c + dc
                if 0 <= nr < 11 and 0 <= nc < 11:
                    yield nr, nc

    def row_cells(self, r):
        return [(r, c) for c in range(11)]

    def col_cells(self, c):
        return [(r, c) for r in range(11)]

    def region_cells(self, reg):
        return [(r, c) for r in range(11) for c in range(11) if self.region_grid[r][c] == reg]

    def all_groups(self):
        for r in range(11):
            yield self.row_cells(r)
        for c in range(11):
            yield self.col_cells(c)
        for reg in self.regions:
            yield self.region_cells(reg)

    def set_star(self, r, c):
        cur = self.cells[r][c]
        if cur == STAR:
            return False
        if cur == BLANK:
            raise Contradiction(f"tried to star an already-blank cell {(r, c)}")
        self.cells[r][c] = STAR
        for nr, nc in self.neighbors(r, c):
            self._set_blank(nr, nc)
        return True

    def _set_blank(self, r, c):
        cur = self.cells[r][c]
        if cur == BLANK:
            return
        if cur == STAR:
            raise Contradiction(f"tried to blank an already-starred cell {(r, c)}")
        self.cells[r][c] = BLANK

    def set_blank(self, r, c):
        cur = self.cells[r][c]
        if cur == BLANK:
            return False
        if cur == STAR:
            raise Contradiction(f"tried to blank an already-starred cell {(r, c)}")
        self.cells[r][c] = BLANK
        return True

    def propagate(self):
        changed = True
        while changed:
            changed = False
            for group in self.all_groups():
                stars = [rc for rc in group if self.cells[rc[0]][rc[1]] == STAR]
                unknowns = [rc for rc in group if self.cells[rc[0]][rc[1]] == UNKNOWN]
                if len(stars) > 2 or len(stars) + len(unknowns) < 2:
                    raise Contradiction(f"group {group} can't reach exactly 2 stars")
                if len(stars) == 2:
                    for r, c in unknowns:
                        if self.set_blank(r, c):
                            changed = True
                elif len(stars) + len(unknowns) == 2:
                    for r, c in unknowns:
                        if self.set_star(r, c):
                            changed = True

            # technique 4: region confined to one row/column
            for reg in self.regions:
                cells = self.region_cells(reg)
                stars = [rc for rc in cells if self.cells[rc[0]][rc[1]] == STAR]
                unknowns = [rc for rc in cells if self.cells[rc[0]][rc[1]] == UNKNOWN]
                if len(stars) >= 2 or not unknowns:
                    continue
                rows = {r for r, c in unknowns}
                if len(rows) == 1:
                    r = next(iter(rows))
                    for rr, cc in self.row_cells(r):
                        if self.region_grid[rr][cc] != reg and self.cells[rr][cc] == UNKNOWN:
                            if self.set_blank(rr, cc):
                                changed = True
                cols = {c for r, c in unknowns}
                if len(cols) == 1:
                    c = next(iter(cols))
                    for rr, cc in self.col_cells(c):
                        if self.region_grid[rr][cc] != reg and self.cells[rr][cc] == UNKNOWN:
                            if self.set_blank(rr, cc):
                                changed = True

    def solved(self):
        return all(self.cells[r][c] != UNKNOWN for r in range(11) for c in range(11))

    def unknown_cells(self):
        return [(r, c) for r in range(11) for c in range(11) if self.cells[r][c] == UNKNOWN]

    def to_bits(self):
        return "".join(
            "1" if self.cells[r][c] == STAR else "0" for r in range(11) for c in range(11)
        )

    def render(self):
        return "\n".join(
            "".join("*" if self.cells[r][c] == STAR else ("." if self.cells[r][c] == BLANK else "?") for c in range(11))
            for r in range(11)
        )


def pick_branch_cell(board):
    """Prefer a cell in whichever group (row/col/region) is closest to
    fully determined - most information gained per guess, closest to how a
    human would pick the next cell to reason about."""
    best, best_score = None, None
    for group in board.all_groups():
        unknowns = [rc for rc in group if board.cells[rc[0]][rc[1]] == UNKNOWN]
        stars = sum(1 for rc in group if board.cells[rc[0]][rc[1]] == STAR)
        if not unknowns:
            continue
        score = len(unknowns) - (2 - stars)
        if best_score is None or score < best_score:
            best_score = score
            best = unknowns[0]
    return best


def solve(region_grid, find_all=False, limit=2):
    root = Board(region_grid)
    solutions = []

    def backtrack(board):
        try:
            board.propagate()
        except Contradiction:
            return
        if board.solved():
            solutions.append(board)
            return
        r, c = pick_branch_cell(board)
        for value in (STAR, BLANK):
            b2 = board.clone()
            b2.guesses += 1
            try:
                if value == STAR:
                    b2.set_star(r, c)
                else:
                    b2.set_blank(r, c)
            except Contradiction:
                continue
            backtrack(b2)
            if solutions and not find_all:
                return
            if len(solutions) >= limit:
                return

    backtrack(root)
    return solutions


if __name__ == "__main__":
    from star_battle import REGION_MAP_PATH
    region_grid = json.load(open(REGION_MAP_PATH))["grid"]
    solutions = solve(region_grid, find_all=True, limit=3)
    print(f"solutions found: {len(solutions)} (search capped at 3 to test uniqueness)")
    for s in solutions:
        print(f"guesses used: {s.guesses}")
        print(s.render())
        print("bits:", s.to_bits())
        print()

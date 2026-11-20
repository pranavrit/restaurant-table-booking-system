"""TableSelector: pure policy that picks the permitted table or combination (FR-016, FR-017, BR-02)."""
from __future__ import annotations

from ..domain import Table, label_key

MAX_COMBINED = 4


def _cliques(tables: list[Table]) -> list[tuple[Table, ...]]:
    """Every set of tables in which each table is marked combinable with all the others."""
    ordered = sorted(tables, key=lambda t: label_key(t.label))
    found: list[tuple[Table, ...]] = []

    def extend(current: list[Table], start: int) -> None:
        if current:
            found.append(tuple(current))
        if len(current) == MAX_COMBINED:
            return
        for i in range(start, len(ordered)):
            candidate = ordered[i]
            if all(candidate.id in member.combinable for member in current):
                extend(current + [candidate], i + 1)

    extend([], 0)
    return found


def set_fits(chosen: tuple[Table, ...] | list[Table], party: int, exception: bool = False) -> bool:
    """FR-016: total minimum seats not above the party, total maximum not below it."""
    chosen = list(chosen)
    if not chosen:
        return False
    if len(chosen) > 1 and not all(b.id in a.combinable for a in chosen for b in chosen if a is not b):
        return False
    if sum(t.min_seats for t in chosen) > party:
        return False
    return exception or sum(t.max_seats for t in chosen) >= party


class TableSelector:
    @staticmethod
    def exception_sets(party: int, tables: list[Table]) -> list[tuple[Table, ...]]:
        """For an exception booking above every table maximum: the largest permitted sets, best first."""
        options = [c for c in _cliques(tables) if set_fits(c, party, exception=True)]
        if not options:
            return []
        options.sort(key=lambda c: (-sum(t.max_seats for t in c), len(c), [label_key(t.label) for t in c]))
        best = sum(t.max_seats for t in options[0])
        return [c for c in options if sum(t.max_seats for t in c) == best]

    @staticmethod
    def select(party: int, tables: list[Table], exception: bool = False) -> tuple[Table, ...] | None:
        """Fewest total maximum seats, then the lowest label (FR-017).
        An exception booking may exceed the maximum; then the largest permitted set is used."""
        options = [c for c in _cliques(tables) if set_fits(c, party)]
        if options:
            return min(
                options,
                key=lambda c: (sum(t.max_seats for t in c), len(c), [label_key(t.label) for t in c]),
            )
        if exception:
            options = [c for c in _cliques(tables) if set_fits(c, party, exception=True)]
            if options:
                return min(
                    options,
                    key=lambda c: (-sum(t.max_seats for t in c), len(c), [label_key(t.label) for t in c]),
                )
        return None

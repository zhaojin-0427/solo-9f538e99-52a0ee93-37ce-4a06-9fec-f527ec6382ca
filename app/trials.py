"""Branch-withdrawal trials ("迁库试算").

A library code is the part of a catalogue number before its first ``/``.
Numbers without a slash are legacy numbers that are never withdrawn.  Identity
between numbers is established *only* along active credentials; every number
that appears on any credential (active or withdrawn) still belongs to the
catalogue and therefore to some original same-item group.

For a candidate library we remove all of its numbers (and every credential
touching them) and ask, for each original same-item group:

* did the group disappear entirely, or
* did the remaining numbers fall apart into two or more groups?

Only affected groups are persisted, keyed by the original group's
lexicographically smallest catalogue number.

Computing this for every library of a group naively costs O(K * E).  Instead,
each active credential is inserted into the leaves where it is still present
after the deletion, via a segment tree over the group's library codes.  A
rollback union-find walks that tree, so a credential is touched O(log K)
times per group and all libraries of the group together cost
O(E log(K + 1)) credential insertions.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

SQL_CHUNK = 400

_outcome_disappear = "disappear"
_outcome_split = "split"


def library_code(catalog_number: str) -> str | None:
    """Return the part before the first ``/``, or None for a legacy number."""
    slash = catalog_number.find("/")
    if slash <= 0:  # no slash, or an empty prefix: keep as a legacy number
        return None
    return catalog_number[:slash]


# ---------------------------------------------------------------------------
# Per-group computation
# ---------------------------------------------------------------------------


class _SegmentTree:
    """Range-add segment tree over the group's library indices."""

    __slots__ = ("size", "nodes")

    def __init__(self, size: int) -> None:
        self.size = size
        self.nodes: list[list[tuple[int, int]]] = [[] for _ in range(4 * size)]

    def add(self, ql: int, qr: int, item: tuple[int, int]) -> None:
        if ql > qr:
            return
        stack = [(1, 0, self.size - 1)]
        while stack:
            node, left, right = stack.pop()
            if ql <= left and right <= qr:
                self.nodes[node].append(item)
                continue
            mid = (left + right) // 2
            if ql <= mid:
                stack.append((node * 2, left, mid))
            if qr > mid:
                stack.append((node * 2 + 1, mid + 1, right))


class _RollbackDSU:
    """Union-find with union by size and no path compression, so it can roll
    back.  ``unions`` counts successful unions currently in effect."""

    __slots__ = ("parent", "size", "unions", "history")

    def __init__(self, n: int) -> None:
        self.parent = list(range(n))
        self.size = [1] * n
        self.unions = 0
        self.history: list[tuple[int, int] | None] = []

    def find(self, x: int) -> int:
        parent = self.parent
        while parent[x] != x:
            x = parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            self.history.append(None)
            return
        if self.size[ra] < self.size[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.size[ra] += self.size[rb]
        self.unions += 1
        self.history.append((rb, ra))

    def snapshot(self) -> int:
        return len(self.history)

    def rollback(self, snapshot: int) -> None:
        while len(self.history) > snapshot:
            record = self.history.pop()
            if record is None:
                continue
            child, parent = record
            self.parent[child] = child
            self.size[parent] -= self.size[child]
            self.unions -= 1


@dataclass(frozen=True)
class GroupImpact:
    library_code: str
    outcome: str
    original_size: int
    removed_count: int
    remaining_groups: int


def _present_ranges(size: int, labels: Sequence[int]) -> list[tuple[int, int]]:
    """Ranges of library indices for which an edge is still present.

    The edge is absent for the libraries of its endpoints, hence present on
    the complement of at most two points.
    """
    excluded = sorted({label for label in labels if label >= 0})
    ranges: list[tuple[int, int]] = []
    start = 0
    for point in excluded:
        if start <= point - 1:
            ranges.append((start, point - 1))
        start = point + 1
    if start <= size - 1:
        ranges.append((start, size - 1))
    return ranges


def group_impacts(
    vertices: Sequence[str],
    edges: Sequence[tuple[str, str]],
    library_of: Mapping[str, str | None],
    stats: dict | None = None,
) -> list[GroupImpact]:
    """Compute the affected libraries of one original same-item group.

    ``edges`` must contain exactly the active credentials inside the group.
    ``stats``, when supplied, receives ``edge_inserts`` (credential insertions
    into the segment tree) and ``libraries`` (K) for complexity checks.
    """
    verts = list(vertices)
    n = len(verts)
    if n == 0:
        return []

    index = {v: i for i, v in enumerate(verts)}
    codes: list[str] = []
    code_index: dict[str, int] = {}
    labels = [-1] * n
    for i, vertex in enumerate(verts):
        code = library_of.get(vertex)
        if code is not None:
            if code not in code_index:
                code_index[code] = len(codes)
                codes.append(code)
            labels[i] = code_index[code]

    k = len(codes)
    if k == 0:
        return []

    library_vertices = [0] * k
    for label in labels:
        if label >= 0:
            library_vertices[label] += 1

    tree = _SegmentTree(k)
    inserts = 0
    for a, b in edges:
        ia = index.get(a)
        ib = index.get(b)
        if ia is None or ib is None:
            continue
        for ql, qr in _present_ranges(k, (labels[ia], labels[ib])):
            tree.add(ql, qr, (ia, ib))
            inserts += 1

    if stats is not None:
        stats["edge_inserts"] = stats.get("edge_inserts", 0) + inserts
        stats["libraries"] = k

    dsu = _RollbackDSU(n)
    impacts: list[GroupImpact] = []

    def visit(node: int, left: int, right: int) -> None:
        snapshot = dsu.snapshot()
        for u, v in tree.nodes[node]:
            dsu.union(u, v)
        if left == right:
            remaining = n - library_vertices[left]
            if remaining == 0:
                impacts.append(
                    GroupImpact(
                        codes[left],
                        _outcome_disappear,
                        n,
                        library_vertices[left],
                        0,
                    )
                )
            else:
                components = remaining - dsu.unions
                if components > 1:
                    impacts.append(
                        GroupImpact(
                            codes[left],
                            _outcome_split,
                            n,
                            library_vertices[left],
                            components,
                        )
                    )
        else:
            mid = (left + right) // 2
            visit(node * 2, left, mid)
            visit(node * 2 + 1, mid + 1, right)
        dsu.rollback(snapshot)

    visit(1, 0, k - 1)
    impacts.sort(key=lambda impact: impact.library_code)
    return impacts


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Graph:
    numbers: frozenset[str]
    edges: tuple[tuple[str, str], ...]
    adjacency: dict[str, tuple[str, ...]]


def load_graph(conn: sqlite3.Connection) -> _Graph:
    numbers: set[str] = set()
    for row in conn.execute("SELECT catalog_a, catalog_b FROM credentials"):
        numbers.add(row[0])
        numbers.add(row[1])

    adjacency: dict[str, list[str]] = {}
    edges: list[tuple[str, str]] = []
    for row in conn.execute(
        "SELECT catalog_a, catalog_b FROM credentials WHERE active = 1"
    ):
        a, b = row[0], row[1]
        edges.append((a, b))
        adjacency.setdefault(a, []).append(b)
        adjacency.setdefault(b, []).append(a)
    return _Graph(
        frozenset(numbers),
        tuple(edges),
        {key: tuple(value) for key, value in adjacency.items()},
    )


def _components(graph: _Graph, seeds: Iterable[str]) -> list[list[str]]:
    seen: set[str] = set()
    components: list[list[str]] = []
    for seed in seeds:
        if seed not in graph.numbers or seed in seen:
            continue
        seen.add(seed)
        stack = [seed]
        component: list[str] = []
        while stack:
            vertex = stack.pop()
            component.append(vertex)
            for neighbour in graph.adjacency.get(vertex, ()):
                if neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
        components.append(component)
    return components


def _component_edges(component: Sequence[str], graph: _Graph) -> list[tuple[str, str]]:
    edges: list[tuple[str, str]] = []
    for vertex in component:
        for neighbour in graph.adjacency.get(vertex, ()):
            if vertex < neighbour:  # report each undirected edge once
                edges.append((vertex, neighbour))
    return edges


def _library_map(numbers: Iterable[str]) -> dict[str, str | None]:
    return {number: library_code(number) for number in numbers}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _chunks(values: Sequence[str]) -> Iterable[Sequence[str]]:
    for start in range(0, len(values), SQL_CHUNK):
        yield values[start : start + SQL_CHUNK]


def _delete_group_rows(conn: sqlite3.Connection, keys: Iterable[str]) -> None:
    keys = sorted(set(keys))
    for chunk in _chunks(keys):
        placeholders = ",".join("?" * len(chunk))
        conn.execute(
            f"DELETE FROM trial_results WHERE group_key IN ({placeholders})", chunk
        )
        conn.execute(
            f"DELETE FROM trial_group_members WHERE group_key IN ({placeholders})",
            chunk,
        )


def _group_keys_for_numbers(
    conn: sqlite3.Connection, numbers: Iterable[str]
) -> set[str]:
    keys: set[str] = set()
    ordered = sorted(set(numbers))
    for chunk in _chunks(ordered):
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT DISTINCT group_key FROM trial_group_members "
            f"WHERE catalog_no IN ({placeholders})",
            chunk,
        )
        keys.update(row[0] for row in rows)
    return keys


def _members_for_keys(
    conn: sqlite3.Connection, keys: Iterable[str]
) -> set[str]:
    members: set[str] = set()
    ordered = sorted(set(keys))
    for chunk in _chunks(ordered):
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT catalog_no FROM trial_group_members "
            f"WHERE group_key IN ({placeholders})",
            chunk,
        )
        members.update(row[0] for row in rows)
    return members


def _write_component(
    conn: sqlite3.Connection,
    component: Sequence[str],
    graph: _Graph,
    library_of: Mapping[str, str | None],
) -> None:
    key = min(component)
    conn.executemany(
        "INSERT INTO trial_group_members(catalog_no, group_key) VALUES (?, ?)",
        [(vertex, key) for vertex in component],
    )
    impacts = group_impacts(component, _component_edges(component, graph), library_of)
    if impacts:
        conn.executemany(
            """
            INSERT INTO trial_results(
                library_code, group_key, outcome, original_size,
                removed_count, remaining_groups)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    impact.library_code,
                    key,
                    impact.outcome,
                    impact.original_size,
                    impact.removed_count,
                    impact.remaining_groups,
                )
                for impact in impacts
            ],
        )


def _refresh_libraries(
    conn: sqlite3.Connection, graph: _Graph, codes: Iterable[str]
) -> None:
    codes = {code for code in codes if code}
    if not codes:
        return
    counts = Counter(
        code for code in (library_code(number) for number in graph.numbers) if code
    )
    for code in sorted(codes):
        if counts.get(code):
            conn.execute(
                """
                INSERT INTO trial_libraries(library_code, catalog_count)
                VALUES (?, ?)
                ON CONFLICT(library_code) DO UPDATE
                    SET catalog_count = excluded.catalog_count
                """,
                (code, counts[code]),
            )
        else:
            conn.execute(
                "DELETE FROM trial_libraries WHERE library_code = ?", (code,)
            )


def rebuild_all(conn: sqlite3.Connection) -> None:
    """Rebuild every group.  Used on first enablement and bulk replacement."""
    graph = load_graph(conn)
    conn.execute("DELETE FROM trial_results")
    conn.execute("DELETE FROM trial_group_members")
    conn.execute("DELETE FROM trial_libraries")

    library_of = _library_map(graph.numbers)
    seen: set[str] = set()
    for start in sorted(graph.numbers):
        if start in seen:
            continue
        seen.add(start)
        stack = [start]
        component: list[str] = []
        while stack:
            vertex = stack.pop()
            component.append(vertex)
            for neighbour in graph.adjacency.get(vertex, ()):
                if neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
        _write_component(conn, component, graph, library_of)

    counts = Counter(
        code for code in (library_code(number) for number in graph.numbers) if code
    )
    conn.executemany(
        "INSERT INTO trial_libraries(library_code, catalog_count) VALUES (?, ?)",
        sorted(counts.items()),
    )
    conn.execute(
        """
        INSERT INTO meta(key, value) VALUES ('trial_initialized', '1')
        ON CONFLICT(key) DO UPDATE SET value = '1'
        """
    )


def ensure_initialized(conn: sqlite3.Connection) -> bool:
    """Build all groups once.  Returns True when a full rebuild happened."""
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'trial_initialized'"
    ).fetchone()
    if row is None or row[0] != "1":
        rebuild_all(conn)
        return True
    return False


def update_after_change(
    conn: sqlite3.Connection, changed_numbers: Iterable[str]
) -> None:
    """Recheck only the old and new groups touched by a catalogue change.

    ``changed_numbers`` must include both the old and the new endpoints of
    every credential that was inserted, updated, withdrawn or restored.
    """
    changed = {number for number in changed_numbers if number}
    if not changed:
        return

    old_keys = _group_keys_for_numbers(conn, changed)
    old_vertices = _members_for_keys(conn, old_keys)
    _delete_group_rows(conn, old_keys)

    graph = load_graph(conn)
    library_of = _library_map(graph.numbers)

    # Seed from every member of the old groups, so groups that merely split
    # away from the changed vertex are also recomputed, and from the changed
    # numbers themselves (which may be new to the catalogue).
    seeds = sorted(
        vertex for vertex in (old_vertices | changed) if vertex in graph.numbers
    )
    for component in _components(graph, seeds):
        # An untouched old group may have merged into this new component;
        # discard its stale rows before writing the merged group.
        stale_keys = _group_keys_for_numbers(conn, component)
        if stale_keys:
            _delete_group_rows(conn, stale_keys)
        _write_component(conn, component, graph, library_of)

    _refresh_libraries(
        conn, graph, (library_code(number) for number in changed)
    )

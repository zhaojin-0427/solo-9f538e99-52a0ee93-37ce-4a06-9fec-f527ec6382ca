from __future__ import annotations

import io
import math
import random
import sqlite3

import pytest

from app import db as db_module
from app import services, trials
from app.trials import group_impacts, library_code
from tests.conftest import load_rows, post


# ---------------------------------------------------------------------------
# Reference implementation (deliberately naive)
# ---------------------------------------------------------------------------


def _code(number):
    return library_code(number)


def _components(vertices, edges):
    adjacency = {}
    for a, b in edges:
        adjacency.setdefault(a, []).append(b)
        adjacency.setdefault(b, []).append(a)
    seen = set()
    result = []
    for start in sorted(vertices):
        if start in seen:
            continue
        seen.add(start)
        stack = [start]
        component = []
        while stack:
            vertex = stack.pop()
            component.append(vertex)
            for neighbour in adjacency.get(vertex, ()):
                if neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
        result.append(component)
    return result


def brute_impacts(numbers, active_edges, libraries):
    vertices = set(numbers)
    edges = [(a, b) for a, b in active_edges if a in vertices and b in vertices]
    groups = _components(vertices, edges)
    results = {}
    for library in sorted(libraries):
        rows = {}
        for group in groups:
            remaining = {v for v in group if _code(v) != library}
            removed = len(group) - len(remaining)
            if not remaining:
                rows[min(group)] = ("disappear", len(group), removed, 0)
                continue
            kept_edges = [
                (a, b) for a, b in edges if a in remaining and b in remaining
            ]
            pieces = _components(remaining, kept_edges)
            if len(pieces) > 1:
                rows[min(group)] = ("split", len(group), removed, len(pieces))
        if rows:
            results[library] = rows
    return results


def _brute_for_group(vertices, edges, library_of):
    by_library = {}
    for vertex in vertices:
        code = library_of.get(vertex)
        if code is not None:
            by_library.setdefault(code, set()).add(vertex)
    expected = brute_impacts(vertices, edges, by_library)
    return expected


# ---------------------------------------------------------------------------
# Unit tests: library code parsing
# ---------------------------------------------------------------------------


def test_library_code_parsing():
    assert library_code("CAT-A/123") == "CAT-A"
    assert library_code("CAT-A/123/456") == "CAT-A"
    assert library_code("/123") is None            # empty prefix -> no code
    assert library_code("123") is None             # legacy number
    assert library_code("") is None


# ---------------------------------------------------------------------------
# Unit tests: group_impacts vs brute force
# ---------------------------------------------------------------------------


def _run_group_impacts(vertices, edges, library_of):
    grouped = {}
    for component in _components(set(vertices), edges):
        component_edges = [
            (a, b) for a, b in edges if a in component and b in component
        ]
        for impact in group_impacts(component, component_edges, library_of):
            grouped.setdefault(impact.library_code, {})[min(component)] = (
                impact.outcome,
                impact.original_size,
                impact.removed_count,
                impact.remaining_groups,
            )
    return grouped


def test_group_impacts_random_against_brute_force():
    rng = random.Random(20261124)
    codes = ["A", "B", "C", "D"]
    checked = 0
    for _ in range(400):
        libraries = rng.sample(codes, rng.randint(1, len(codes)))
        numbers = []
        for library in libraries:
            for suffix in range(1, rng.randint(2, 4)):
                numbers.append(f"{library}/{suffix}")
        if rng.random() < 0.5:
            numbers.append(f"LEGACY{rng.randint(1, 3)}")
        numbers = sorted(set(numbers))
        if len(numbers) < 2:
            continue
        edges = []
        for _ in range(rng.randint(1, 9)):
            a, b = rng.sample(numbers, 2)
            edges.append((a, b))
        library_of = {number: _code(number) for number in numbers}
        expected = _brute_for_group(numbers, edges, library_of)
        actual = _run_group_impacts(numbers, edges, library_of)
        assert actual == expected, (numbers, edges, expected, actual)
        checked += 1
    assert checked > 350


def test_same_library_multiple_numbers_split_group():
    # Chain: A/1 -- B/1 -- A/2 -- C/1.  Removing A leaves B/1 and C/1 (and the
    # legacy number) as separate groups.
    vertices = ["A/1", "A/2", "B/1", "C/1", "OLD"]
    edges = [
        ("A/1", "B/1"),
        ("B/1", "A/2"),
        ("A/2", "C/1"),
        ("C/1", "OLD"),
    ]
    library_of = {number: _code(number) for number in vertices}
    impacts = {i.library_code: i for i in group_impacts(vertices, edges, library_of)}
    assert set(impacts) == {"A", "B", "C"}
    assert set(impacts) == {"A", "B", "C"}
    assert impacts["A"].outcome == "split"
    assert impacts["A"].original_size == 5
    assert impacts["A"].removed_count == 2
    assert impacts["A"].remaining_groups == 2
    # B and C are also cut points; each leaves 2 groups.
    assert impacts["B"].remaining_groups == 2
    assert impacts["C"].remaining_groups == 2


def test_group_disappears_when_all_numbers_belong_to_library():
    vertices = ["A/1", "A/2", "A/3"]
    edges = [("A/1", "A/2"), ("A/2", "A/3")]
    library_of = {number: _code(number) for number in vertices}
    [impact] = group_impacts(vertices, edges, library_of)
    assert impact.library_code == "A"
    assert impact.outcome == "disappear"
    assert impact.remaining_groups == 0
    assert impact.original_size == 3 and impact.removed_count == 3


def test_legacy_numbers_survive_and_can_keep_group_together():
    vertices = ["A/1", "B/1", "LEGACY"]
    edges = [("A/1", "LEGACY"), ("LEGACY", "B/1")]
    library_of = {number: _code(number) for number in vertices}
    impacts = {i.library_code: i for i in group_impacts(vertices, edges, library_of)}
    # Removing A leaves LEGACY-B/1 connected: no impact.
    assert "A" not in impacts
    assert "B" not in impacts


def test_edge_inserts_are_logarithmic_in_library_count():
    rng = random.Random(7)
    k = 512
    vertices = [f"L{i:03d}/1" for i in range(k)]
    edges = []
    for _ in range(600):
        a, b = rng.sample(vertices, 2)
        edges.append((a, b))
    library_of = {number: _code(number) for number in vertices}
    stats = {}
    group_impacts(vertices, edges, library_of, stats=stats)
    # An edge is absent at the (at most two) libraries of its endpoints, so it
    # is present on at most three ranges; a range-add visits O(log K) nodes.
    bound = len(edges) * 3 * 2 * (math.ceil(math.log2(k + 1)) + 1)
    assert stats["edge_inserts"] <= bound, (stats, bound)
    assert stats["libraries"] == k


def test_mixed_scale_stays_within_logarithmic_bound():
    rng = random.Random(31)
    libraries = [f"LIB{i:03d}" for i in range(100)]
    vertices = [f"{library}/{suffix}" for library in libraries for suffix in range(15)]
    vertices.append("LEGACY")
    edges = []
    for _ in range(2000):
        a, b = rng.sample(vertices, 2)
        edges.append((a, b))
    library_of = {number: _code(number) for number in vertices}
    stats = {}
    group_impacts(vertices, edges, library_of, stats=stats)
    k = stats["libraries"]
    bound = len(edges) * 3 * 2 * (math.ceil(math.log2(k + 1)) + 1)
    assert stats["edge_inserts"] <= bound, (stats, bound)


# ---------------------------------------------------------------------------
# Persistence and web
# ---------------------------------------------------------------------------

TRIAL_ROWS = [
    ("T-1", "A/1", "B/1", 10, 0),
    ("T-2", "B/1", "A/2", 10, 0),
    ("T-3", "A/2", "C/1", 10, 0),
    ("T-4", "C/1", "OLD", 10, 0),
    ("T-5", "D/1", "D/2", 10, 0),
]


def _trial_snapshot(app):
    with app.app_context():
        conn = db_module.get_db()
        results = [
            tuple(row)
            for row in conn.execute(
                "SELECT library_code, group_key, outcome, original_size, "
                "removed_count, remaining_groups FROM trial_results "
                "ORDER BY library_code, group_key"
            )
        ]
        members = [
            tuple(row)
            for row in conn.execute(
                "SELECT catalog_no, group_key FROM trial_group_members ORDER BY catalog_no"
            )
        ]
        libraries = [
            tuple(row)
            for row in conn.execute(
                "SELECT library_code, catalog_count FROM trial_libraries ORDER BY library_code"
            )
        ]
    return results, members, libraries


def _expected_db(app):
    with app.app_context():
        conn = db_module.get_db()
        numbers = set()
        edges = []
        for row in conn.execute("SELECT catalog_a, catalog_b FROM credentials"):
            numbers.update((row[0], row[1]))
        for row in conn.execute(
            "SELECT catalog_a, catalog_b FROM credentials WHERE active = 1"
        ):
            edges.append((row[0], row[1]))

    # Only library codes that still have a number in the catalogue count.
    codes = {_code(number) for number in numbers if _code(number)}
    groups = _components(numbers, edges)
    expected = {}
    for code in codes:
        rows = {}
        for group in groups:
            remaining = {v for v in group if _code(v) != code}
            removed = len(group) - len(remaining)
            if not remaining:
                rows[min(group)] = ("disappear", len(group), removed, 0)
                continue
            kept = [
                (a, b) for a, b in edges if a in remaining and b in remaining
            ]
            pieces = _components(remaining, kept)
            if len(pieces) > 1:
                rows[min(group)] = ("split", len(group), removed, len(pieces))
        if rows:
            expected[code] = rows
    return expected, numbers


def _filtered_impacts(impacts):
    return {
        library: dict(rows)
        for library, rows in impacts.items()
        if library
    }


def test_initial_import_builds_trials(client, app):
    load_rows(app, TRIAL_ROWS)
    expected, numbers = _expected_db(app)
    result_rows, members, libraries = _trial_snapshot(app)
    actual = {}
    for library, key, outcome, size, removed, remaining in result_rows:
        actual.setdefault(library, {})[key] = (outcome, size, removed, remaining)
    assert actual == _filtered_impacts(expected)
    assert {number for number, _ in members} == numbers
    assert dict(libraries) == {
        "A": 2, "B": 1, "C": 1, "D": 2
    }


def test_trial_page_reads_persisted_results(client, app):
    load_rows(app, TRIAL_ROWS)
    before = _trial_snapshot(app)
    page = client.get("/trials?library=A")
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    # Original group key is the lexicographically smallest number.
    assert "A/1" in body
    assert "拆开" in body
    # Group left after removing A: B/1 and C/1-OLD, i.e. 2 groups.
    expected, _ = _expected_db(app)
    assert expected["A"]["A/1"][3] == 2
    assert _trial_snapshot(app) == before

    page = client.get("/trials?library=NOPE")
    assert "没有馆代码" in page.get_data(as_text=True)
    page = client.get("/trials?library=D")
    assert "消失" in page.get_data(as_text=True)


def test_merge_import_changing_both_endpoints_updates_old_and_new_groups(client, app):
    load_rows(
        app,
        [
            ("M-1", "A/1", "B/1", 10, 0),
            ("M-2", "A/2", "B/2", 10, 0),
            ("M-3", "C/1", "C/2", 10, 0),
        ],
    )
    # Move M-1 from A/1-B/1 to C/1-D/1: the old group splits/vanishes and the
    # new endpoint creates a new group.  Both sides must be rechecked.
    csv_text = (
        "voucher_no,catalog_a,catalog_b,minutes,locked\n"
        "M-1,C/1,D/1,10,0\n"
    )
    resp = client.post(
        "/import",
        data={
            "csrf_token": _csrf(client),
            "mode": "merge",
            "csv_file": (io.BytesIO(csv_text.encode()), "merge.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "更新 1 份" in resp.get_data(as_text=True)

    expected, numbers = _expected_db(app)
    result_rows, members, _ = _trial_snapshot(app)
    actual = {}
    for library, key, outcome, size, removed, remaining in result_rows:
        actual.setdefault(library, {})[key] = (outcome, size, removed, remaining)
    assert actual == _filtered_impacts(expected)
    assert {number for number, _ in members} == numbers
    # The new group key follows the original group membership.
    member_numbers = {number for number, _ in members}
    assert "A/1" not in member_numbers and "B/1" not in member_numbers
    assert "C/1" in member_numbers
    assert "D/1" in member_numbers


def test_confirm_and_restore_refresh_only_that_graph(client, app, worker):
    load_rows(
        app,
        [
            ("W-1", "A/1", "B/1", 5, 0),
            ("W-2", "B/1", "C/1", 7, 0),
            ("Z-1", "Z/1", "Z/2", 9, 0),
        ],
    )
    from tests.conftest import solve_and_drain

    job = solve_and_drain(app, worker, ("A/1", "C/1"))
    assert job["status"] == "done"
    plan_id = job["plan_id"]
    resp = post(client, f"/plans/{plan_id}/confirm", token_path=f"/plans/{plan_id}")
    assert "确认成功" in resp.get_data(as_text=True)

    expected, _ = _expected_db(app)
    result_rows, _, _ = _trial_snapshot(app)
    actual = {}
    for library, key, outcome, size, removed, remaining in result_rows:
        actual.setdefault(library, {})[key] = (outcome, size, removed, remaining)
    assert actual == _filtered_impacts(expected)

    with app.app_context():
        conn = db_module.get_db()
        cid = conn.execute(
            "SELECT id FROM credentials WHERE voucher_no = 'W-1'"
        ).fetchone()[0]
    resp = post(client, f"/credentials/{cid}/restore", token_path="/credentials")
    assert "已恢复" in resp.get_data(as_text=True)

    expected, _ = _expected_db(app)
    result_rows, _, _ = _trial_snapshot(app)
    actual = {}
    for library, key, outcome, size, removed, remaining in result_rows:
        actual.setdefault(library, {})[key] = (outcome, size, removed, remaining)
    assert actual == _filtered_impacts(expected)


def test_replace_rebuilds_all_and_drops_absent_libraries(client, app):
    load_rows(app, TRIAL_ROWS)
    csv_text = (
        "voucher_no,catalog_a,catalog_b,minutes,locked\n"
        "N-1,X/1,Y/1,5,0\n"
    )
    resp = client.post(
        "/import",
        data={
            "csrf_token": _csrf(client),
            "mode": "replace",
            "csv_file": (io.BytesIO(csv_text.encode()), "replace.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "已替换供数" in resp.get_data(as_text=True)
    result_rows, members, libraries = _trial_snapshot(app)
    assert {library for library, _ in libraries} == {"X", "Y"}
    assert {number for number, _ in members} == {"X/1", "Y/1"}
    expected, _ = _expected_db(app)
    actual = {}
    for library, key, outcome, size, removed, remaining in result_rows:
        actual.setdefault(library, {})[key] = (outcome, size, removed, remaining)
    assert actual == _filtered_impacts(expected)


def test_trial_write_failure_rolls_back_feed_and_trials(app, monkeypatch):
    load_rows(app, TRIAL_ROWS)
    before_feed = _feed_snapshot(app)
    before_trials = _trial_snapshot(app)

    def boom(conn, changed):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(trials, "update_after_change", boom)
    rows = [
        {"voucher_no": "T-9", "catalog_a": "A/9", "catalog_b": "B/9",
         "minutes": 5, "locked": 0}
    ]
    with app.app_context():
        conn = db_module.get_db()
        with pytest.raises(sqlite3.Error):
            with db_module.transaction(conn):
                services.import_credentials(conn, rows, "merge")
    assert _feed_snapshot(app) == before_feed
    assert _trial_snapshot(app) == before_trials
    with app.app_context():
        conn = db_module.get_db()
        assert conn.execute(
            "SELECT COUNT(*) FROM credentials WHERE voucher_no = 'T-9'"
        ).fetchone()[0] == 0


def _feed_snapshot(app):
    with app.app_context():
        conn = db_module.get_db()
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT voucher_no, catalog_a, catalog_b, minutes, locked, active "
                "FROM credentials ORDER BY id"
            )
        ]


def _csrf(client):
    from tests.conftest import csrf

    return csrf(client)


def test_merge_that_removes_last_number_of_a_library_cleans_it(client, app):
    load_rows(app, [("U-1", "A/1", "B/1", 5, 0)])
    csv_text = (
        "voucher_no,catalog_a,catalog_b,minutes,locked\n"
        "U-1,C/1,B/1,5,0\n"
    )
    resp = client.post(
        "/import",
        data={
            "csrf_token": _csrf(client),
            "mode": "merge",
            "csv_file": (io.BytesIO(csv_text.encode()), "move.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "更新 1 份" in resp.get_data(as_text=True)
    result_rows, _, libraries = _trial_snapshot(app)
    assert {library for library, _ in libraries} == {"B", "C"}
    assert all(library != "A" for library, _, _, _, _, _ in result_rows)
    page = client.get("/trials?library=A")
    assert "没有馆代码" in page.get_data(as_text=True)


def test_incremental_updates_match_full_rebuild(client, app, worker):
    """After a sequence of edits, incremental state equals a full rebuild."""
    load_rows(
        app,
        [
            ("Q-1", "A/1", "B/1", 5, 0),
            ("Q-2", "B/1", "C/1", 6, 0),
            ("Q-3", "C/1", "D/1", 7, 0),
            ("Q-4", "A/2", "E/1", 8, 0),
        ],
    )
    csv_text = (
        "voucher_no,catalog_a,catalog_b,minutes,locked\n"
        "Q-2,A/1,C/2,6,0\n"
        "Q-5,E/1,F/1,4,0\n"
    )
    client.post(
        "/import",
        data={
            "csrf_token": _csrf(client),
            "mode": "merge",
            "csv_file": (io.BytesIO(csv_text.encode()), "inc.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    incremental = _trial_snapshot(app)
    with app.app_context():
        conn = db_module.get_db()
        with db_module.transaction(conn):
            trials.rebuild_all(conn)
    rebuilt = _trial_snapshot(app)
    assert incremental == rebuilt

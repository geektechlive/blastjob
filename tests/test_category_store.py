"""Category searches storage (#91): validation, compare-and-set, term versions and hits."""

from __future__ import annotations

import sqlite3

import pytest

from blastjob.core import category_store as cs
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path)
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _job(conn: sqlite3.Connection, n: int = 1) -> int:
    return dbc.upsert_job(
        conn,
        source="adzuna",
        external_id=str(n),
        company="Acme",
        title="CIO",
        url=f"https://example.com/{n}",
    )


def _add(conn, label="Example robotics firms", terms="robotics startup", dream=False) -> int:
    return cs.add_category(conn, label=label, terms=terms, is_dream=dream)


# ----------------------------------------------------------------------------- migration


def test_the_tables_exist_with_defaults(conn):
    cid = _add(conn)
    row = cs.get_category(conn, cid)
    assert row["slug"] == "example-robotics-firms"
    assert (row["enabled"], row["is_dream"], row["version"], row["terms_version"]) == (1, 0, 0, 0)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(category_hits)")}
    assert {"job_id", "category_id", "terms_version", "first_seen"} <= cols


# ----------------------------------------------------------------------------- validation


@pytest.mark.parametrize(
    ("label", "terms"),
    [
        ("", "robotics"),  # no label
        ("x" * 61, "robotics"),  # label too long
        ("!!!", "robotics"),  # no slug characters at all
        ("Robots", ""),  # no terms
        ("Robots", "\n \n"),  # only blanks
        ("Robots", "a\nb\nc\nd"),  # more than 3 terms
        ("Robots", "x" * 81),  # term too long
        ("Robots", "bad\x07term"),  # not printable
        ("Robots", "bad\rterm"),  # CR inside a term
    ],
)
def test_invalid_input_is_refused_with_a_message(conn, label, terms):
    with pytest.raises(cs.CategoryError) as e:
        cs.add_category(conn, label=label, terms=terms, is_dream=False)
    assert str(e.value)
    assert conn.execute("SELECT COUNT(*) FROM category_searches").fetchone()[0] == 0


def test_crlf_line_endings_are_accepted_and_blank_lines_ignored(conn):
    cid = _add(conn, terms="alpha term\r\n\r\nbeta term\r\n")
    assert cs.terms_of(cs.get_category(conn, cid)) == ["alpha term", "beta term"]


def test_the_eleventh_category_is_refused(conn):
    for i in range(cs.MAX_CATEGORIES):
        _add(conn, label=f"Group {i}")
    with pytest.raises(cs.CategoryError):
        _add(conn, label="One too many")


def test_a_duplicate_slug_is_refused(conn):
    _add(conn, label="Example Robotics")
    with pytest.raises(cs.CategoryError):
        _add(conn, label="example-robotics!")


def test_slug_is_lowercase_hyphenated_and_capped():
    assert cs.slugify("Example  Robotics, Inc.") == "example-robotics-inc"
    assert len(cs.slugify("a" * 60)) <= cs.MAX_SLUG
    assert not cs.slugify("a" * 39 + " b").endswith("-")


# ----------------------------------------------------------------------------- versions


def test_a_stale_version_is_refused_and_leaves_the_stored_row(conn):
    cid = _add(conn)
    cs.update_category(conn, cid, 0, label="Renamed", terms="robotics startup", is_dream=False)
    with pytest.raises(cs.StaleCategory):
        cs.update_category(conn, cid, 0, label="Late", terms="other", is_dream=True)
    row = cs.get_category(conn, cid)
    assert (row["label"], row["version"]) == ("Renamed", 1)
    with pytest.raises(cs.StaleCategory):
        cs.set_enabled(conn, cid, 0, False)
    with pytest.raises(cs.StaleCategory):
        cs.delete_category(conn, cid, 0)
    assert cs.get_category(conn, cid) is not None


def test_a_missing_category_is_not_found(conn):
    with pytest.raises(cs.CategoryNotFound):
        cs.update_category(conn, 99, 0, label="x", terms="y", is_dream=False)
    with pytest.raises(cs.CategoryNotFound):
        cs.delete_category(conn, 99, 0)


def test_terms_version_moves_with_terms_and_dream_but_not_label_or_enabled(conn):
    cid = _add(conn)
    cs.update_category(conn, cid, 0, label="New label", terms="robotics startup", is_dream=False)
    assert cs.get_category(conn, cid)["terms_version"] == 0
    cs.set_enabled(conn, cid, 1, False)
    assert cs.get_category(conn, cid)["terms_version"] == 0
    cs.update_category(conn, cid, 2, label="New label", terms="drone startup", is_dream=False)
    assert cs.get_category(conn, cid)["terms_version"] == 1
    cs.update_category(conn, cid, 3, label="New label", terms="drone startup", is_dream=True)
    assert cs.get_category(conn, cid)["terms_version"] == 2
    assert cs.get_category(conn, cid)["version"] == 4


# ----------------------------------------------------------------------------- hits


def test_only_hits_for_the_current_terms_of_an_enabled_category_count(conn):
    cid = _add(conn, dream=True)
    jid = _job(conn)
    cs.attach_hit(conn, jid, cid, 0)
    conn.commit()
    assert [h.label for h in cs.current_hits(conn, [jid])[jid]] == ["Example robotics firms"]
    assert cs.current_dream_job_ids(conn) == frozenset({jid})

    # A label edit keeps the hit.
    cs.update_category(conn, cid, 0, label="Renamed", terms="robotics startup", is_dream=True)
    assert [h.label for h in cs.current_hits(conn, [jid])[jid]] == ["Renamed"]

    # Disabling hides it; re-enabling with the same terms shows it again.
    cs.set_enabled(conn, cid, 1, False)
    assert cs.current_hits(conn, [jid]) == {}
    assert cs.current_dream_job_ids(conn) == frozenset()
    cs.set_enabled(conn, cid, 2, True)
    assert jid in cs.current_hits(conn, [jid])

    # A terms edit hides the old hit; finding the job again under the new terms restores it.
    cs.update_category(conn, cid, 3, label="Renamed", terms="drone startup", is_dream=True)
    assert cs.current_hits(conn, [jid]) == {}
    cs.attach_hit(conn, jid, cid, 1)
    conn.commit()
    assert jid in cs.current_hits(conn, [jid])


def test_one_job_found_by_two_categories_has_two_hits(conn):
    a, b = _add(conn, label="Group A"), _add(conn, label="Group B", dream=True)
    jid = _job(conn)
    cs.attach_hit(conn, jid, a, 0)
    cs.attach_hit(conn, jid, b, 0)
    conn.commit()
    assert {h.label for h in cs.current_hits(conn, [jid])[jid]} == {"Group A", "Group B"}
    cs.attach_hit(conn, jid, a, 0)  # again: still one row each
    assert conn.execute("SELECT COUNT(*) FROM category_hits").fetchone()[0] == 2


def test_deleting_a_category_keeps_its_jobs_and_drops_its_hits(conn):
    cid = _add(conn, dream=True)
    jid = _job(conn)
    cs.attach_hit(conn, jid, cid, 0)
    conn.commit()
    cs.delete_category(conn, cid, 0)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM category_hits").fetchone()[0] == 0
    assert cs.current_dream_job_ids(conn) == frozenset()

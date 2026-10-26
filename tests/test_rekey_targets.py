"""`rekey_companies` also re-keys the target-watch tables (#93).

See docs/how-it-works/same-role.md. Offline: a tmp data dir, invented companies.
"""

from __future__ import annotations

import datetime as dt

import pytest

from blastjob.core import company as co
from blastjob.core import run_lock
from blastjob.core import target_store as ts
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig

NOW = dt.datetime(2026, 10, 24, 12, 0, 0)
VARIANT = "Acme Widgets Intl"
CANON = "Acme Widgets"
SPAN = "Acme Widgets named Dana Roe as its new chief executive officer."


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(co, "_COMPANY_ALIASES", {})  # restored after the test
    c = BlastJobConfig()
    c.paths.data_dir = str(tmp_path)
    c.discovery.company_aliases = {VARIANT: CANON}
    return c


@pytest.fixture
def conn(cfg):
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _event(span: str = SPAN, **kw):
    base = {
        "event_type": "new_ceo",
        "source_url": "https://news.example.com/a",
        "page_title": "Acme names a CEO",
        "source_span": span,
        "published_on": "2026-10-20",
        "source_basis": "news_report",
        "identity_basis": "located_mention",
    }
    return ts.NewEvent(**{**base, **kw})


def _target_under_old_key(conn, name: str = VARIANT, events=(_event(),)) -> str:
    """A target stored under the key the name had before the alias existed."""
    old = co.company_key(name)  # no alias installed yet: the plain key
    ts.add_targets(conn, [ts.TargetInput(name=name)], now=NOW)
    ts.record_scan(conn, old, ok=True, status="ok", events=list(events), now=NOW)
    return old


def _fp_rows(conn):
    return conn.execute("SELECT company_key, fingerprint FROM company_events").fetchall()


def test_target_and_events_move_with_recomputed_fingerprints_and_notified_at_kept(conn, cfg):
    old = _target_under_old_key(conn)
    ts.mark_notified(conn, [1], now=NOW)

    result = co.rekey_companies(conn, cfg)

    new = co.company_key(CANON)
    assert new != old
    assert result["target_companies"] == co.RekeyResult(updated=1)
    assert result["company_events"] == co.RekeyResult(updated=1)
    assert ts.get_target(conn, new) is not None and ts.get_target(conn, old) is None
    (row,) = conn.execute("SELECT * FROM company_events").fetchall()
    assert row["company_key"] == new
    assert row["fingerprint"] == ts.fingerprint(new, "new_ceo", SPAN)
    assert row["notified_at"] == NOW.isoformat(timespec="seconds")


def test_a_rescan_after_the_move_inserts_no_duplicate_event(conn, cfg):
    _target_under_old_key(conn)
    co.rekey_companies(conn, cfg)
    new = co.company_key(CANON)

    inserted = ts.record_scan(conn, new, ok=True, status="ok", events=[_event()], now=NOW)

    assert inserted == 0
    assert conn.execute("SELECT COUNT(*) FROM company_events").fetchone()[0] == 1


def test_an_unchanged_target_is_a_no_op(conn, cfg):
    ts.add_targets(conn, [ts.TargetInput(name="Globex")], now=NOW)
    ts.record_scan(conn, "globex", ok=True, status="ok", events=[_event()], now=NOW)
    before = [tuple(r) for r in _fp_rows(conn)]

    result = co.rekey_companies(conn, cfg)

    assert result["target_companies"] == co.RekeyResult()
    assert result["company_events"] == co.RekeyResult()
    assert [tuple(r) for r in _fp_rows(conn)] == before


def test_a_target_whose_new_key_is_already_a_target_stays_with_its_events(conn, cfg):
    old = _target_under_old_key(conn)
    ts.add_targets(conn, [ts.TargetInput(name=CANON)], now=NOW)

    result = co.rekey_companies(conn, cfg)

    assert result["target_companies"] == co.RekeyResult(conflicts=1)
    assert result["company_events"] == co.RekeyResult()
    assert ts.get_target(conn, old) is not None
    (row,) = conn.execute("SELECT * FROM company_events").fetchall()
    assert row["company_key"] == old
    assert row["fingerprint"] == ts.fingerprint(old, "new_ceo", SPAN)


def test_a_duplicate_event_is_merged_keeping_the_earliest_stamps(conn, cfg):
    old = _target_under_old_key(conn)
    new = co.company_key(CANON)
    # The same sentence already sits under the new key (an orphan's earlier scan), notified
    # later than the moved row and not dismissed.
    conn.execute(
        "INSERT INTO company_events (company_key, event_type, source_url, page_title, "
        "source_span, published_on, source_basis, identity_basis, fingerprint, found_at, "
        "notified_at, dismissed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            new, "new_ceo", "https://news.example.com/a", "", SPAN, "2026-10-20", "news_report",
            "located_mention", ts.fingerprint(new, "new_ceo", SPAN), "x", "2026-10-25T00:00:00", "",
        ),
    )  # fmt: skip
    conn.execute(
        "UPDATE company_events SET notified_at='2026-10-24T12:00:00', "
        "dismissed_at='2026-10-26T00:00:00' WHERE company_key=?",
        (old,),
    )
    conn.commit()

    result = co.rekey_companies(conn, cfg)

    assert result["target_companies"] == co.RekeyResult(updated=1)
    assert result["company_events"] == co.RekeyResult(merged=1)
    (row,) = conn.execute("SELECT * FROM company_events").fetchall()
    assert row["company_key"] == new
    assert row["notified_at"] == "2026-10-24T12:00:00"
    assert row["dismissed_at"] == ""  # the twin keeps its own (never dismissed)


def test_an_orphan_event_is_rekeyed_from_its_old_key(conn, cfg):
    old = _target_under_old_key(conn)
    ts.remove_target(conn, old)

    result = co.rekey_companies(conn, cfg)

    new = co.company_key(CANON)
    assert result["target_companies"] == co.RekeyResult()
    assert result["company_events"] == co.RekeyResult(updated=1)
    (row,) = conn.execute("SELECT * FROM company_events").fetchall()
    assert row["company_key"] == new
    assert row["fingerprint"] == ts.fingerprint(new, "new_ceo", SPAN)


def test_an_orphan_event_whose_key_maps_to_itself_is_left_alone(conn, cfg):
    ts.add_targets(conn, [ts.TargetInput(name="Globex")], now=NOW)
    ts.record_scan(conn, "globex", ok=True, status="ok", events=[_event()], now=NOW)
    ts.remove_target(conn, "globex")
    before = [tuple(r) for r in _fp_rows(conn)]

    result = co.rekey_companies(conn, cfg)

    assert result["company_events"] == co.RekeyResult()
    assert [tuple(r) for r in _fp_rows(conn)] == before


def test_a_target_with_a_live_lease_is_skipped(conn, cfg):
    old = _target_under_old_key(conn)
    conn.execute(
        "UPDATE target_companies SET scan_claimed_at=? WHERE company_key=?",
        (dt.datetime.now().isoformat(timespec="seconds"), old),
    )
    conn.commit()

    result = co.rekey_companies(conn, cfg)

    assert result["target_companies"] == co.RekeyResult(skipped_leased=1)
    assert ts.get_target(conn, old) is not None
    assert conn.execute("SELECT company_key FROM company_events").fetchone()[0] == old


def test_a_stale_lease_does_not_block_the_move(conn, cfg):
    old = _target_under_old_key(conn)
    stale = dt.datetime.now() - dt.timedelta(minutes=ts.LEASE_MINUTES + 5)
    conn.execute(
        "UPDATE target_companies SET scan_claimed_at=? WHERE company_key=?",
        (stale.isoformat(timespec="seconds"), old),
    )
    conn.commit()

    result = co.rekey_companies(conn, cfg)

    assert result["target_companies"].updated == 1


def test_a_long_span_dedupes_on_what_is_stored(conn, cfg):
    long_span = "Acme Widgets named Dana Roe CEO. " * 40  # well over the stored length
    ts.add_targets(conn, [ts.TargetInput(name="Globex")], now=NOW)

    first = ts.record_scan(
        conn, "globex", ok=True, status="ok", events=[_event(long_span)], now=NOW
    )
    second = ts.record_scan(
        conn, "globex", ok=True, status="ok", events=[_event(long_span)], now=NOW
    )

    assert (first, second) == (1, 0)
    (row,) = conn.execute("SELECT * FROM company_events").fetchall()
    assert row["fingerprint"] == ts.fingerprint("globex", "new_ceo", row["source_span"])


def test_spans_sharing_their_stored_prefix_are_one_event(conn):
    prefix = "x" * ts.SPAN_STORE_CHARS
    ts.add_targets(conn, [ts.TargetInput(name="Globex")], now=NOW)

    inserted = ts.record_scan(
        conn,
        "globex",
        ok=True,
        status="ok",
        events=[_event(prefix + " tail one"), _event(prefix + " tail two")],
        now=NOW,
    )

    assert inserted == 1


def test_an_error_in_the_target_pass_rolls_back_the_whole_pass(conn, cfg, monkeypatch):
    conn.execute(
        "INSERT INTO company_follows (company, company_key, followed_at) VALUES (?,?,?)",
        (VARIANT, "acme widgets intl", "x"),
    )
    conn.commit()

    def boom(*_a, **_k):
        raise RuntimeError("injected")

    monkeypatch.setattr(ts, "rekey_targets", boom)

    with pytest.raises(RuntimeError, match="injected"):
        co.rekey_companies(conn, cfg)

    key = conn.execute("SELECT company_key FROM company_follows").fetchone()[0]
    assert key == "acme widgets intl"


def test_aliases_come_from_the_config_passed_in(conn, cfg):
    conn.execute(
        "INSERT INTO company_follows (company, company_key, followed_at) VALUES (?,?,?)",
        (VARIANT, "acme widgets intl", "x"),
    )
    conn.commit()
    assert co.company_key(VARIANT) == "acme widgets intl"  # no alias installed yet

    co.rekey_companies(conn, cfg)

    assert conn.execute("SELECT company_key FROM company_follows").fetchone()[0] == "acme widgets"


def test_rekey_refuses_while_another_run_holds_the_lock(conn, cfg):
    with run_lock.hold(cfg.paths.data_dir, "daily run"):
        # A different context would contend; flock refuses a second open file description.
        import contextvars

        ctx = contextvars.Context()
        with pytest.raises(run_lock.RunLockHeld):
            ctx.run(co.rekey_companies, conn, cfg)


def test_an_orphan_event_that_maps_onto_a_live_target_stays_put(conn, cfg):
    old = _target_under_old_key(conn)
    ts.remove_target(conn, old)  # the owner hid these events by removing the target
    ts.add_targets(conn, [ts.TargetInput(name=CANON)], now=NOW)  # the canonical name is live

    result = co.rekey_companies(conn, cfg)

    assert result["company_events"] == co.RekeyResult(conflicts=1)
    (row,) = conn.execute("SELECT * FROM company_events").fetchall()
    assert row["company_key"] == old


def test_a_merge_does_not_copy_a_dismissal_onto_an_undismissed_twin(conn, cfg):
    old = _target_under_old_key(conn)
    new = co.company_key(CANON)
    conn.execute(
        "INSERT INTO company_events (company_key, event_type, source_url, page_title, "
        "source_span, published_on, source_basis, identity_basis, fingerprint, found_at, "
        "notified_at, dismissed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            new, "new_ceo", "https://news.example.com/a", "", SPAN, "2026-10-20", "news_report",
            "located_mention", ts.fingerprint(new, "new_ceo", SPAN), "x", "", "",
        ),
    )  # fmt: skip
    conn.execute(
        "UPDATE company_events SET dismissed_at='2026-10-26T00:00:00', "
        "notified_at='2026-10-24T12:00:00' WHERE company_key=?",
        (old,),
    )
    conn.commit()

    co.rekey_companies(conn, cfg)

    (row,) = conn.execute("SELECT * FROM company_events").fetchall()
    assert row["dismissed_at"] == ""
    assert row["notified_at"] == "2026-10-24T12:00:00"


def test_rekey_refuses_to_join_a_callers_open_transaction(conn, cfg):
    conn.execute(
        "INSERT INTO company_follows (company, company_key, followed_at) VALUES ('a','a','x')"
    )
    assert conn.in_transaction

    with pytest.raises(ValueError, match="transaction"):
        co.rekey_companies(conn, cfg)

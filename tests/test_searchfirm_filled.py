"""A filled retained search is not stored, and a stored one closes on its page (#115, ADR 0073)."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from blastjob.core import closure
from blastjob.db import client as dbc
from blastjob.discovery import searchfirms as sf
from blastjob.discovery.pipeline import run_discovery
from blastjob.discovery.searchfirm_reconcile import RECHECK_CAP, reconcile_search_firm_rows
from blastjob.models.config import BlastJobConfig, SearchFirm

NOW = datetime(2026, 11, 10, 8, 0, 0)
FIRM = SearchFirm(name="Acme Executive Search", slug="acme", domain="acmesearch.example")
FILLED = "Back to Jobs\nChief Technology Officer\nThis position has been filled.\nThe role."
OPEN = "Back to Jobs\nChief Technology Officer\nAcme has retained us for this search."


def _cfg(tmp_path: Path, *, auto: bool = False) -> BlastJobConfig:
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path)
    cfg.discovery.closure_auto = auto
    return cfg


def _row(conn: sqlite3.Connection, jid: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()


def _firm_job(conn: sqlite3.Connection, n: int, **cols: object) -> int:
    jid = dbc.upsert_job(
        conn,
        source="searchfirm:acme",
        external_id=f"acmesearch.example/opportunities/{n}",
        company="Acme Executive Search (retained search)",
        title="Fabrikam - Chief Technology Officer",
        url=f"https://www.acmesearch.example/opportunities/{n}",
        raw_jd="snippet text",
    )
    if cols:
        sets = ", ".join(f"{k}=?" for k in cols)
        conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*cols.values(), jid))
        conn.commit()
    return jid


class Reader:
    """Injected page reader: url -> text (or an exception to raise)."""

    def __init__(self, pages: dict[str, str | Exception] | None = None, default: str = OPEN):
        self.pages = pages or {}
        self.default = default
        self.calls: list[str] = []

    async def __call__(self, url: str) -> str:
        self.calls.append(url)
        page = self.pages.get(url, self.default)
        if isinstance(page, Exception):
            raise page
        return page


# -- the marker ------------------------------------------------------------------


def test_marker_matches_only_the_filled_sentence() -> None:
    assert sf.filled_sentence("x\nThis position has been filled.\ny") == (
        "This position has been filled"
    )
    assert sf.filled_sentence("THIS SEARCH HAS BEEN FILLED") is not None
    assert sf.filled_sentence("this role has been filled") is not None
    assert sf.filled_sentence("The role was filled last year") is None
    assert sf.filled_sentence("We have filled many positions") is None


# -- new postings ----------------------------------------------------------------


_POSTING = {
    "title": "Chief Technology Officer",
    "url": "https://www.acmesearch.example/opportunities/7/chief-technology-officer",
    "description": "A retained search.",
}


def _adapter(page: str, **kw: object) -> sf.SearchFirmAdapter:
    async def fake_search(query: str, api_key: str) -> list[dict]:
        return [_POSTING]

    async def fake_reader(url: str) -> str:
        return page

    return sf.SearchFirmAdapter(
        [FIRM],
        queries=["chief technology officer"],
        regions=["Ohio"],
        search=fake_search,
        reader=fake_reader,
        **kw,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_a_filled_new_page_is_not_returned_and_is_counted() -> None:
    a = _adapter(FILLED)
    assert await a.fetch() == []
    (report,) = a.firm_reports
    assert report.filled == 1 and report.kept == 1


@pytest.mark.asyncio
async def test_an_open_new_page_is_returned_with_no_filled_count() -> None:
    a = _adapter(OPEN)
    assert len(await a.fetch()) == 1
    assert a.firm_reports[0].filled == 0


@pytest.mark.asyncio
async def test_a_new_page_left_unread_by_the_cap_is_stored_as_today() -> None:
    a = _adapter(FILLED, max_reads=0)
    (posting,) = await a.fetch()
    assert posting.description == "A retained search."


# -- close_from_page_evidence ----------------------------------------------------


def test_close_from_page_evidence_closes_stamps_and_records_the_page(tmp_path: Path) -> None:
    conn = dbc.init_db(_cfg(tmp_path))
    jid = _firm_job(conn, 1)

    assert closure.close_from_page_evidence(conn, jid, now=NOW, auto=False, page_text=FILLED)

    row = _row(conn, jid)
    assert row["posting_check"] == "closed"
    assert row["posting_checked_at"] == NOW.isoformat(timespec="seconds")
    assert row["posting_closed_at"] == NOW.isoformat(timespec="seconds")
    assert row["raw_jd"] == FILLED


def test_close_from_page_evidence_leaves_a_manually_pinned_row_alone(tmp_path: Path) -> None:
    conn = dbc.init_db(_cfg(tmp_path))
    jid = _firm_job(conn, 1, posting_check="verified_open", posting_check_manual=1)

    assert not closure.close_from_page_evidence(conn, jid, now=NOW, auto=True, page_text=FILLED)

    row = _row(conn, jid)
    assert (row["posting_check"], row["raw_jd"], row["posting_closed_at"]) == (
        "verified_open",
        "snippet text",
        "",
    )


def test_close_from_page_evidence_moves_a_kept_row_only_when_auto(tmp_path: Path) -> None:
    conn = dbc.init_db(_cfg(tmp_path))
    shadow = _firm_job(conn, 1, review_state="kept")
    live = _firm_job(conn, 2, review_state="kept")

    closure.close_from_page_evidence(conn, shadow, now=NOW, auto=False, page_text=FILLED)
    closure.close_from_page_evidence(conn, live, now=NOW, auto=True, page_text=FILLED)

    assert (_row(conn, shadow)["application_state"] or "") == ""
    assert _row(conn, live)["application_state"] == "posting_closed"


# -- reconcile re-read ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stored_row_whose_reread_carries_the_marker_is_closed(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    conn = dbc.init_db(cfg)
    jid = _firm_job(conn, 1)

    result = await reconcile_search_firm_rows(cfg, conn, read=Reader(default=FILLED), now=NOW)

    row = _row(conn, jid)
    assert result.closed == [jid]
    assert row["posting_check"] == "closed" and row["raw_jd"] == FILLED
    assert row["firm_page_checked_at"] == NOW.isoformat(timespec="seconds")


@pytest.mark.asyncio
async def test_a_page_without_the_marker_only_stamps_the_read(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    conn = dbc.init_db(cfg)
    jid = _firm_job(conn, 1)

    result = await reconcile_search_firm_rows(cfg, conn, read=Reader(default=OPEN), now=NOW)

    row = _row(conn, jid)
    assert result.closed == []
    assert (row["posting_check"], row["raw_jd"], row["posting_closed_at"]) == (
        "",
        "snippet text",
        "",
    )
    assert row["firm_page_checked_at"] == NOW.isoformat(timespec="seconds")


@pytest.mark.asyncio
async def test_a_failed_or_empty_read_changes_and_stamps_nothing(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    conn = dbc.init_db(cfg)
    boom = _firm_job(conn, 1)
    empty = _firm_job(conn, 2)
    reader = Reader(
        {
            "https://www.acmesearch.example/opportunities/1": RuntimeError("503"),
            "https://www.acmesearch.example/opportunities/2": "",
        }
    )

    result = await reconcile_search_firm_rows(cfg, conn, read=reader, now=NOW)

    assert result.closed == []
    for jid in (boom, empty):
        row = _row(conn, jid)
        assert (row["posting_check"], row["firm_page_checked_at"]) == ("", "")


@pytest.mark.asyncio
async def test_a_manually_pinned_row_is_never_read(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    conn = dbc.init_db(cfg)
    jid = _firm_job(conn, 1, posting_check="verified_open", posting_check_manual=1)
    reader = Reader(default=FILLED)

    await reconcile_search_firm_rows(cfg, conn, read=reader, now=NOW)

    assert reader.calls == []
    assert _row(conn, jid)["posting_check"] == "verified_open"


@pytest.mark.asyncio
async def test_closed_archived_and_other_source_rows_are_not_read(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    conn = dbc.init_db(cfg)
    _firm_job(conn, 1, posting_check="closed")
    _firm_job(conn, 2, review_state="archived")
    dbc.upsert_job(conn, source="manual", external_id="m1", company="X", title="Y", raw_jd="z")
    reader = Reader(default=FILLED)

    await reconcile_search_firm_rows(cfg, conn, read=reader, now=NOW)

    assert reader.calls == []


@pytest.mark.asyncio
async def test_rereads_rotate_so_the_sixth_row_is_read_on_the_second_run(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    conn = dbc.init_db(cfg)
    ids = [_firm_job(conn, n) for n in range(1, RECHECK_CAP + 2)]
    first = Reader()
    second = Reader()

    await reconcile_search_firm_rows(cfg, conn, read=first, now=NOW)
    later = datetime(2026, 11, 17, 8, 0, 0)
    await reconcile_search_firm_rows(cfg, conn, read=second, now=later)

    assert len(first.calls) == RECHECK_CAP
    assert second.calls[0].endswith(f"/{len(ids)}")
    assert len(second.calls) == RECHECK_CAP


@pytest.mark.asyncio
async def test_reconcile_without_a_reader_still_backfills_places(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.discovery.search_firm_places = ["Fairhaven"]
    conn = dbc.init_db(cfg)
    jid = _firm_job(conn, 1, raw_jd="Contoso, Fairhaven")

    result = await reconcile_search_firm_rows(cfg, conn, read=None, now=NOW)

    assert result.placed == [jid]
    assert _row(conn, jid)["geo_location_raw"] == "Fairhaven"


# -- closure must leave a page-closed search-firm row alone (R10) ------------------


@pytest.mark.asyncio
async def test_a_closure_pass_does_not_reopen_a_page_closed_search_firm_row(
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path, auto=True)
    conn = dbc.init_db(cfg)
    jid = _firm_job(conn, 1, review_state="kept")
    closure.close_from_page_evidence(conn, jid, now=NOW, auto=True, page_text=FILLED)
    assert _row(conn, jid)["application_state"] == "posting_closed"

    async def none_list(*_a: object) -> list:
        return []

    deps = closure.closure_links.LinkDeps(
        probe_company=lambda *_a: [],
        probe_workday=lambda *_a: [],
        probe_dayforce=lambda *_a: [],
        fetch_board=none_list,
    )
    await closure.run_closure(cfg, conn, deps=deps, now=NOW)

    row = _row(conn, jid)
    assert (row["posting_check"], row["application_state"]) == ("closed", "posting_closed")


# -- one orchestration for both entry points (R6) ---------------------------------


def _spy_reconcile(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from blastjob.discovery import searchfirm_reconcile as rec

    calls: list[str] = []

    async def spy(cfg, conn, *, read, now=None):  # type: ignore[no-untyped-def]
        calls.append("reconcile")
        return rec.ReconcileResult()

    monkeypatch.setattr(rec, "reconcile_search_firm_rows", spy)
    return calls


@pytest.mark.asyncio
async def test_the_weekly_discovery_path_reconciles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    conn = dbc.init_db(cfg)
    calls = _spy_reconcile(monkeypatch)

    await run_discovery(cfg, conn, adapters=[_adapter(OPEN)])

    assert calls == ["reconcile"]


@pytest.mark.asyncio
async def test_a_run_with_no_search_firm_adapter_does_not_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    conn = dbc.init_db(cfg)
    calls = _spy_reconcile(monkeypatch)

    await run_discovery(cfg, conn, adapters=[])

    assert calls == []


@pytest.mark.asyncio
async def test_the_on_demand_sweep_reconciles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blastjob import daily

    cfg = _cfg(tmp_path)
    cfg.discovery.search_firms = [FIRM]
    cfg.discovery.search_firm_queries = ["chief technology officer"]
    cfg.discovery.search_firm_regions = ["Ohio"]
    conn = dbc.init_db(cfg)
    calls = _spy_reconcile(monkeypatch)
    monkeypatch.setenv("JINA_API_KEY", "test-key")

    async def no_hits(query: str, api_key: str, timeout_s: int = 30) -> list[dict]:
        return []

    monkeypatch.setattr(sf, "_default_search", no_hits)

    await daily.sweep_search_firms(cfg, conn, score=False)

    assert calls == ["reconcile"]


def test_migration_adds_the_page_check_column(tmp_path: Path) -> None:
    conn = dbc.init_db(_cfg(tmp_path))
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert "firm_page_checked_at" in cols


@pytest.mark.asyncio
async def test_a_reconcile_crash_is_reported_and_does_not_sink_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blastjob.discovery import searchfirm_reconcile as rec

    cfg = _cfg(tmp_path)
    conn = dbc.init_db(cfg)

    async def boom(cfg, conn, *, read, now=None):  # type: ignore[no-untyped-def]
        raise RuntimeError("disk")

    monkeypatch.setattr(rec, "reconcile_search_firm_rows", boom)

    result = await run_discovery(cfg, conn, adapters=[_adapter(OPEN)])

    assert result.inserted_or_updated == 1
    assert "RuntimeError" in result.errors["searchfirm reconcile"]

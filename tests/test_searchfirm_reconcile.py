"""Stored search-firm rows get reconciled against their stored page text (#113)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from blastjob.db import client as dbc
from blastjob.discovery.searchfirm_reconcile import backfill_search_firm_places
from blastjob.models.config import BlastJobConfig


def _cfg(tmp_path: Path) -> BlastJobConfig:
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path)
    cfg.discovery.search_firm_places = ["Ohio", "Fairhaven"]
    cfg.discovery.home_state = "oh"
    return cfg


def _insert(
    conn: sqlite3.Connection, ext_id: str, *, source: str, raw_jd: str, location: str = ""
) -> int:
    return dbc.upsert_job(
        conn,
        source=source,
        external_id=ext_id,
        company="Acme Search (retained search)",
        title="Chief Technology Officer",
        url=f"https://acme.example/{ext_id}",
        raw_jd=raw_jd,
        geo_location_raw=location,
    )


def test_row_with_no_place_gets_it_from_stored_page_text(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    conn = dbc.init_db(cfg)
    jid = _insert(conn, "a1", source="searchfirm:acme", raw_jd="Contoso Fairhaven, OH. The role.")

    changed = backfill_search_firm_places(cfg, conn)

    row = conn.execute(
        "SELECT geo_location_raw, geo_region FROM jobs WHERE id=?", (jid,)
    ).fetchone()
    assert changed == [jid]
    assert (row["geo_location_raw"], row["geo_region"]) == ("Fairhaven", "OH")


def test_row_that_already_has_a_place_is_left_alone(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    conn = dbc.init_db(cfg)
    jid = _insert(
        conn, "a2", source="searchfirm:acme", raw_jd="Fairhaven text", location="Somewhere"
    )

    assert backfill_search_firm_places(cfg, conn) == []
    row = conn.execute("SELECT geo_location_raw FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["geo_location_raw"] == "Somewhere"


def test_rows_from_other_sources_are_untouched(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    conn = dbc.init_db(cfg)
    jid = _insert(conn, "a3", source="manual", raw_jd="Fairhaven text")

    assert backfill_search_firm_places(cfg, conn) == []
    row = conn.execute("SELECT geo_location_raw FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["geo_location_raw"] == ""


def test_page_text_naming_no_configured_place_changes_nothing(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    conn = dbc.init_db(cfg)
    _insert(conn, "a4", source="searchfirm:acme", raw_jd="A role somewhere in the Midwest.")

    assert backfill_search_firm_places(cfg, conn) == []

"""Backfill: re-read Workday detail for geo-rejected rows so a req whose
additional locations include the home state stops being dropped (issue #3)."""

from __future__ import annotations

import asyncio
import sqlite3

import httpx

from blastjob.db import client as dbc
from blastjob.discovery import workday_locations as wl
from blastjob.models.config import BlastJobConfig, TargetBoard
from tests.geo_fixture import apply_region

URL = "https://acme.wd1.myworkdayjobs.com/Careers/job/Springfield-VA/IT-Director_R9"


def _cfg(tmp_path) -> BlastJobConfig:
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path)
    apply_region(cfg)
    cfg.discovery.target_boards = [
        TargetBoard(ats="workday", company="Acme", token="acme/wd1/Careers")
    ]
    return cfg


def _job(conn: sqlite3.Connection, ext: str, *, url: str = URL, **cols) -> int:
    row = {
        "source": "workday:acme",
        "external_id": ext,
        "company": "Acme",
        "title": "IT Director",
        "url": url,
        "raw_jd": "Lead the claims technology organization. " * 20,
        "geo_location_raw": "Springfield, VA",
        "geo_city": "Springfield",
        "geo_region": "VA",
        "geo_status": "verified",
        "geo_remote": 0,
        "review_state": "new",
        "first_seen": "2026-10-15T00:00:00",
        "last_seen": "2026-10-15T00:00:00",
    }
    row.update(cols)
    names = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    cur = conn.execute(f"INSERT INTO jobs ({names}) VALUES ({marks})", tuple(row.values()))
    conn.commit()
    return int(cur.lastrowid or 0)


def _transport(extra: list[str], calls: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        info = {"title": "IT Director", "location": "Springfield, VA"}
        if extra:
            info["additionalLocations"] = extra
        return httpx.Response(200, json={"jobPostingInfo": info})

    return httpx.MockTransport(handler)


def _conn(cfg: BlastJobConfig) -> sqlite3.Connection:
    return dbc.init_db(cfg)


def test_report_finds_a_home_state_location_and_writes_nothing(tmp_path):
    cfg = _cfg(tmp_path)
    conn = _conn(cfg)
    jid = _job(conn, "R9")
    calls: list[str] = []
    fixes = asyncio.run(
        wl.plan(conn, cfg, transport=_transport(["Red Bank NJ", "Fairhaven OH"], calls))
    )
    assert [(f.job_id, f.new_location) for f in fixes] == [
        (jid, "Springfield, VA; Red Bank NJ; Fairhaven OH")
    ]
    assert fixes[0].now_passes is True
    assert calls == ["/wday/cxs/acme/Careers/job/Springfield-VA/IT-Director_R9"]
    stored = conn.execute("SELECT geo_location_raw FROM jobs WHERE id=?", (jid,)).fetchone()[0]
    assert stored == "Springfield, VA"


def test_apply_stores_the_full_location_list(tmp_path):
    cfg = _cfg(tmp_path)
    conn = _conn(cfg)
    jid = _job(conn, "R9")
    fixes = asyncio.run(wl.plan(conn, cfg, transport=_transport(["Fairhaven OH"], [])))
    assert wl.apply(conn, fixes) == [jid]
    stored = conn.execute("SELECT geo_location_raw FROM jobs WHERE id=?", (jid,)).fetchone()[0]
    assert stored == "Springfield, VA; Fairhaven OH"


def test_no_additional_locations_means_no_fix(tmp_path):
    cfg = _cfg(tmp_path)
    conn = _conn(cfg)
    _job(conn, "R9")
    assert asyncio.run(wl.plan(conn, cfg, transport=_transport([], []))) == []


def test_only_geo_rejected_undecided_workday_rows_are_probed(tmp_path):
    cfg = _cfg(tmp_path)
    conn = _conn(cfg)
    _job(conn, "placed", geo_status="approximate", geo_distance_mi=3.0)
    _job(conn, "remote", geo_remote=1)
    _job(conn, "kept", review_state="kept")
    _job(conn, "other-src", source="adzuna")
    calls: list[str] = []
    assert asyncio.run(wl.plan(conn, cfg, transport=_transport(["Fairhaven OH"], calls))) == []
    assert calls == []


def test_a_failed_probe_is_skipped_not_fatal(tmp_path):
    cfg = _cfg(tmp_path)
    conn = _conn(cfg)
    _job(conn, "R9")

    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    fixes = asyncio.run(wl.plan(conn, cfg, transport=httpx.MockTransport(boom)))
    assert fixes == []


def test_format_report_names_each_change(tmp_path):
    fix = wl.LocationFix(7, "Acme", "IT Director", "Springfield, VA", "Springfield, VA; X", True)
    text = wl.format_report([fix], applied=None)
    assert "#7" in text and "Springfield, VA; X" in text and "report only" in text

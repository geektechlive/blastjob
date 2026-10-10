"""scripts/regression_e2e.py on a synthetic database: a settings-only config
passes every check against its own baseline; an unconfigured one fails."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from blastjob import settings_store as ss
from blastjob.db import client as dbc

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import golden_matching  # noqa: E402
import regression_e2e as e2e  # noqa: E402

SETTINGS = {
    "candidate": {
        "target_titles": ["Director of Operations"],
        "seniority": "director",
        "field": "operations",
        "former_employers": ["Contoso Analytics"],
    },
    "discovery": {
        "home_state": "OH",
        "home_lat": 39.92,
        "home_lon": -83.0,
        "towns": {"harrowgate": [39.92, -83.0], "fairhaven": [39.95, -83.05]},
        "title_keep_terms": ["director"],
        "title_drop_terms": ["engineer"],
        "target_boards": [{"company": "Fabrikam", "ats": "workday", "token": "fab/wd5/Ext"}],
        "adzuna_queries": ["director of operations"],
        "company_aliases": {"Contoso Analytics Group": "Contoso Analytics"},
    },
}


def _db(tmp_path, settings: dict | None) -> Path:
    path = tmp_path / "src" / "blastjob.db"
    conn = dbc.init_db_at(path)
    if settings:
        ss.import_mapping(conn, settings)
    jd = "Lead a team of 12 and own the operations budget. " * 20
    for n, (company, title, city) in enumerate(
        [
            ("Fabrikam", "Director of Operations", "Fairhaven"),
            ("Contoso Analytics Group", "Operations Director", "Harrowgate"),
            ("Northwind", "Software Engineer", "Fairhaven"),
        ]
    ):
        dbc.upsert_job(
            conn,
            source="adzuna",
            external_id=str(n),
            company=company,
            title=title,
            raw_jd=jd,
            geo_city=city,
            geo_region="OH",
            geo_location_raw=f"{city}, OH",
        )
    conn.commit()
    conn.close()
    return path


def _baseline(db: Path) -> dict:
    return json.loads(json.dumps(golden_matching.snapshot(str(db), full=True), default=str))


def test_a_settings_only_config_passes_every_check(tmp_path):
    db = _db(tmp_path, SETTINGS)

    report = e2e.run(db, _baseline(db), sample=5)

    assert report.ok, report.text()
    names = {name for name, _ok, _d in report.checks}
    assert {"matching not skipped", "scoring prompt carries the profile"} <= names
    assert "RESUME_SYSTEM carries the profile" in names


def test_alumni_and_aliases_come_through_from_settings(tmp_path):
    db = _db(tmp_path, SETTINGS)
    base = _baseline(db)
    assert base["aliases"] == {"contoso analytics group": "contoso analytics"}
    assert base["alumni"]["matches"] == ["Contoso Analytics Group"]


def test_an_unconfigured_profile_fails_and_never_scores(tmp_path):
    db = _db(tmp_path, SETTINGS)
    baseline = _baseline(db)
    bare = _db(tmp_path / "bare", None)

    report = e2e.run(bare, baseline, sample=5)

    assert not report.ok
    failed = {name for name, ok, _d in report.checks if not ok}
    assert "candidate profile configured" in failed
    assert "adapters equal the baseline" in failed


def test_the_input_database_is_never_written(tmp_path):
    db = _db(tmp_path, SETTINGS)
    before = db.read_bytes()
    e2e.run(db, _baseline(db), sample=5)
    assert db.read_bytes() == before


@pytest.mark.parametrize("where", ["unset", "default"])
def test_refuses_without_a_scratch_data_dir(tmp_path, monkeypatch, where):
    if where == "unset":
        monkeypatch.delenv("BLASTJOB_DATA_DIR")
    else:
        monkeypatch.setenv("BLASTJOB_DATA_DIR", "~/.blastjob")
    assert e2e.refuse_unsafe(tmp_path / "copy.db")


def test_refuses_a_database_inside_the_default_data_dir():
    assert "pass a COPY" in (e2e.refuse_unsafe(Path("~/.blastjob/blastjob.db").expanduser()) or "")

"""Editing the curated company fields in the cockpit (#109): the revision counter,
`update_curated`, the `POST /companies/{key}/curated` route and the form."""

from __future__ import annotations

import json
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from blastjob.core import company as co
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web.app import create_app
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN

KEY = "acme widgets"
STALE_MESSAGE = "The profile changed since you opened it. Reload and try again."


@pytest.fixture
def env(tmp_path):
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path)
    conn = dbc.init_db(cfg)
    co.upsert_profile(
        conn,
        co.CompanyProfile(
            company="Acme Widgets",
            hq="Fairhaven, OH",
            ceo="Jordan Hale",
            culture_rating=3.2,
            culture_notes="Original notes",
            risks="Original risks",
            questions="Original question",
            workup_path="/tmp/acme-workup.md",
            extra={"k": "v"},
            sources=[{"label": "Acme", "url": "https://example.com", "accessed": "2026-09-05"}],
        ),
    )
    conn.close()
    client = TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))
    return client, cfg


def _tok() -> dict[str, str]:
    return {CSRF_HEADER: LAUNCH_TOKEN}


def _row(cfg):
    conn = dbc.init_db(cfg)
    try:
        return dict(
            conn.execute("SELECT * FROM company_profiles WHERE company_key=?", (KEY,)).fetchone()
        )
    finally:
        conn.close()


def _form(cfg, **over):
    base = {
        "culture_rating": "4.5",
        "culture_notes": "New notes",
        "risks": "New risks",
        "questions": "Q one\nQ two",
        "revision": str(_row(cfg)["revision"]),
    }
    return {**base, **over}


# ----- storage -------------------------------------------------------------


def test_migration_adds_columns_to_an_existing_database(tmp_path):
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path)
    conn = dbc.init_db(cfg)
    co.upsert_profile(conn, co.CompanyProfile(company="Acme Widgets"))
    conn.execute("ALTER TABLE company_profiles DROP COLUMN revision")
    conn.execute("ALTER TABLE company_profiles DROP COLUMN review_evidence")
    conn.execute("DELETE FROM schema_migrations WHERE version >= 74")
    conn.commit()

    dbc.migrate(conn)

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(company_profiles)")}
    assert {"revision", "review_evidence"} <= cols
    row = conn.execute("SELECT revision, review_evidence FROM company_profiles").fetchone()
    assert row["revision"] == 0
    conn.close()


def test_review_evidence_is_storable_and_readable(env):
    _, cfg = env
    items = [{"site": "example.com", "rating_kind": "overall", "value": 3.9}]
    conn = dbc.init_db(cfg)
    co.upsert_profile(conn, co.CompanyProfile(company="Acme Widgets", review_evidence=items))
    assert co.get_profile(conn, "Acme Widgets")["review_evidence"] == items
    refreshed = co.CompanyProfile(
        company="Acme Widgets", review_evidence=[{"site": "example.com", "value": 4.0}]
    )
    row = conn.execute("SELECT updated_at FROM company_profiles").fetchone()
    co.refresh_research_facts(conn, refreshed, expected_updated_at=row["updated_at"])
    stored = co.get_profile(conn, "Acme Widgets")["review_evidence"]
    assert stored == [{"site": "example.com", "value": 4.0}]
    conn.close()


def test_get_profile_decodes_blank_review_evidence_to_empty_list(env):
    _, cfg = env
    conn = dbc.init_db(cfg)
    conn.execute("UPDATE company_profiles SET review_evidence=''")
    assert co.get_profile(conn, "Acme Widgets")["review_evidence"] == []
    conn.close()


def test_every_profile_write_bumps_revision(env):
    _, cfg = env
    conn = dbc.init_db(cfg)
    r0 = _row(cfg)["revision"]

    co.upsert_profile(conn, co.CompanyProfile(company="Acme Widgets", hq="Elsewhere"))
    r1 = _row(cfg)["revision"]
    assert r1 == r0 + 1

    row = _row(cfg)
    co.refresh_research_facts(
        conn,
        co.CompanyProfile(company="Acme Widgets", hq="Newtown"),
        expected_updated_at=row["updated_at"],
    )
    r2 = _row(cfg)["revision"]
    assert r2 == r1 + 1

    assert co.update_curated(conn, KEY, {"risks": "x"}, r2) == r2 + 1
    assert _row(cfg)["revision"] == r2 + 1
    conn.close()


def test_update_curated_writes_only_curated_columns(env):
    _, cfg = env
    before = _row(cfg)
    conn = dbc.init_db(cfg)
    new_rev = co.update_curated(
        conn,
        KEY,
        {
            "culture_rating": 0.0,
            "culture_notes": "n",
            "risks": "r",
            "questions": "q",
            "hq": "HACKED",
            "workup_path": "/etc/passwd",
            "extra": "{}",
        },
        before["revision"],
    )
    conn.close()
    after = _row(cfg)
    assert new_rev == before["revision"] + 1
    assert (after["culture_rating"], after["culture_notes"]) == (0.0, "n")
    assert (after["risks"], after["questions"]) == ("r", "q")
    for col in (*co.RESEARCH_OWNED, "workup_path", "extra", "summary", "strategy"):
        assert after[col] == before[col], col


def test_update_curated_refuses_stale_revision_and_unknown_key(env):
    _, cfg = env
    before = _row(cfg)
    conn = dbc.init_db(cfg)
    assert co.update_curated(conn, KEY, {"risks": "x"}, before["revision"] + 5) is None
    assert co.update_curated(conn, "nobody", {"risks": "x"}, 0) is None
    conn.close()
    assert _row(cfg) == before


# ----- route ---------------------------------------------------------------


def test_valid_save_updates_fields_and_rerenders_notes(env):
    client, cfg = env
    r = client.post(f"/companies/{KEY}/curated", data=_form(cfg), headers=_tok())
    assert r.status_code == 200
    assert "New notes" in r.text and "4.5/5" in r.text
    row = _row(cfg)
    assert row["culture_rating"] == 4.5 and row["risks"] == "New risks"
    assert row["questions"] == "Q one\nQ two"


def test_rating_zero_is_saved_and_displayed(env):
    client, cfg = env
    r = client.post(
        f"/companies/{KEY}/curated", data=_form(cfg, culture_rating="0"), headers=_tok()
    )
    assert r.status_code == 200 and "0.0/5" in r.text
    assert _row(cfg)["culture_rating"] == 0.0
    page = client.get(f"/companies/{KEY}")
    assert "0.0/5" in page.text


def test_blank_rating_becomes_null(env):
    client, cfg = env
    r = client.post(f"/companies/{KEY}/curated", data=_form(cfg, culture_rating=""), headers=_tok())
    assert r.status_code == 200
    assert _row(cfg)["culture_rating"] is None


@pytest.mark.parametrize("bad", ["7", "-1", "abc", "nan", "inf"])
def test_invalid_rating_is_400_and_form_is_reshown(env, bad):
    client, cfg = env
    before = _row(cfg)
    r = client.post(
        f"/companies/{KEY}/curated",
        data=_form(cfg, culture_rating=bad, culture_notes="kept text"),
        headers=_tok(),
    )
    assert r.status_code == 400
    assert "kept text" in r.text and "<form" in r.text
    assert _row(cfg) == before


def test_overlong_text_is_400(env):
    client, cfg = env
    before = _row(cfg)
    for field, size in (("culture_notes", 8001), ("risks", 8001), ("questions", 4001)):
        r = client.post(
            f"/companies/{KEY}/curated", data=_form(cfg, **{field: "x" * size}), headers=_tok()
        )
        assert r.status_code == 400, field
    assert _row(cfg) == before


def test_nul_characters_are_stripped(env):
    client, cfg = env
    client.post(
        f"/companies/{KEY}/curated", data=_form(cfg, culture_notes="a\x00b"), headers=_tok()
    )
    assert _row(cfg)["culture_notes"] == "ab"


def test_stale_revision_is_refused_and_nothing_written(env):
    client, cfg = env
    form = _form(cfg)
    conn = dbc.init_db(cfg)
    co.update_curated(conn, KEY, {"risks": "someone else"}, _row(cfg)["revision"])
    conn.close()
    before = _row(cfg)
    r = client.post(f"/companies/{KEY}/curated", data=form, headers=_tok())
    assert r.status_code == 409
    assert STALE_MESSAGE in r.text
    assert _row(cfg) == before


def test_missing_csrf_token_is_refused(env):
    client, cfg = env
    before = _row(cfg)
    r = client.post(f"/companies/{KEY}/curated", data=_form(cfg))
    assert r.status_code == 403
    assert _row(cfg) == before


def test_out_of_range_revision_is_rejected_without_a_500(env):
    client, cfg = env
    before = _row(cfg)
    r = client.post(
        f"/companies/{KEY}/curated", data=_form(cfg, revision=str(2**63)), headers=_tok()
    )
    assert r.status_code == 422
    assert _row(cfg) == before


def test_unknown_key_is_404(env):
    client, cfg = env
    r = client.post("/companies/nobody/curated", data=_form(cfg), headers=_tok())
    assert r.status_code == 404


def test_research_owned_columns_untouched_by_route(env):
    client, cfg = env
    before = _row(cfg)
    client.post(f"/companies/{KEY}/curated", data=_form(cfg), headers=_tok())
    after = _row(cfg)
    for col in (*co.RESEARCH_OWNED, "workup_path", "extra", "summary", "strategy"):
        assert after[col] == before[col], col


def test_html_payload_renders_escaped(env):
    client, cfg = env
    payload = "<script>alert(1)</script>"
    r = client.post(
        f"/companies/{KEY}/curated",
        data=_form(cfg, culture_notes=payload, risks=payload, questions=payload),
        headers=_tok(),
    )
    assert r.status_code == 200
    assert payload not in r.text and "&lt;script&gt;" in r.text
    page = client.get(f"/companies/{KEY}")
    assert payload not in page.text


def test_company_page_carries_the_form_with_revision(env):
    client, cfg = env
    page = client.get(f"/companies/{KEY}")
    assert f'hx-post="/companies/{quote(KEY)}/curated"' in page.text
    assert 'name="revision"' in page.text and "Edit notes" in page.text
    assert json.dumps(_row(cfg)["revision"]) in page.text

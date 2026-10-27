"""The Category searches section of /targets (#91): add, edit, toggle, delete."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from blastjob.core import category_store as cs
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web.app import create_app
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN


@pytest.fixture
def env(tmp_path):
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path)
    conn = dbc.init_db(cfg)
    client = TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))
    yield client, conn
    conn.close()


def _tok() -> dict[str, str]:
    return {CSRF_HEADER: LAUNCH_TOKEN}


def _form(label="Example robotics firms", terms="robotics startup", dream=False, **extra):
    data = {"label": label, "terms": terms, **extra}
    if dream:
        data["is_dream"] = "on"
    return data


def _add(client, **kw):
    return client.post(
        "/targets/categories", data=_form(**kw), headers=_tok(), follow_redirects=False
    )


def test_the_section_renders_on_the_targets_page_with_an_add_form(env):
    client, _ = env
    r = client.get("/targets")
    assert r.status_code == 200
    assert "Category searches" in r.text
    assert 'action="/targets/categories"' in r.text and 'name="is_dream"' in r.text


def test_adding_a_category_stores_it_and_lists_it(env):
    client, conn = env
    r = _add(client, dream=True)
    assert r.status_code == 303 and r.headers["location"] == "/targets"
    (row,) = cs.list_categories(conn)
    assert (row["label"], row["is_dream"], row["enabled"]) == ("Example robotics firms", 1, 1)
    page = client.get("/targets").text
    assert "Example robotics firms" in page and "robotics startup" in page


def test_an_invalid_add_is_a_400_with_a_clear_message_and_keeps_the_draft(env):
    client, conn = env
    r = client.post(
        "/targets/categories",
        data=_form(terms="a\nb\nc\nd"),
        headers=_tok(),
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "3 search terms at most" in r.text
    assert cs.list_categories(conn) == []


def test_the_eleventh_category_is_a_400(env):
    client, _ = env
    for i in range(cs.MAX_CATEGORIES):
        assert _add(client, label=f"Group {i}").status_code == 303
    r = _add(client, label="One too many")
    assert r.status_code == 400 and "at most" in r.text


def test_a_duplicate_label_is_a_400(env):
    client, _ = env
    _add(client, label="Example Robotics")
    assert _add(client, label="example robotics").status_code == 400


def test_a_post_without_the_csrf_token_is_refused(env):
    client, conn = env
    r = client.post("/targets/categories", data=_form(), follow_redirects=False)
    assert r.status_code == 403
    assert cs.list_categories(conn) == []
    cid = cs.add_category(conn, label="Keep", terms="x", is_dream=False)
    for path in (
        f"/targets/categories/{cid}",
        f"/targets/categories/{cid}/enabled",
        f"/targets/categories/{cid}/delete",
    ):
        assert client.post(path, data={"version": 0}, follow_redirects=False).status_code == 403
    assert cs.get_category(conn, cid)["version"] == 0


def test_an_edit_saves_and_bumps_the_version(env):
    client, conn = env
    cid = cs.add_category(conn, label="Group", terms="alpha", is_dream=False)
    r = client.post(
        f"/targets/categories/{cid}",
        data={**_form(label="Group renamed", terms="beta", dream=True), "version": 0},
        headers=_tok(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    row = cs.get_category(conn, cid)
    assert (row["label"], row["terms"], row["is_dream"], row["version"]) == (
        "Group renamed",
        "beta",
        1,
        1,
    )


def test_a_stale_edit_is_a_409_and_the_newer_text_stays(env):
    client, conn = env
    cid = cs.add_category(conn, label="Group", terms="alpha", is_dream=False)
    cs.update_category(conn, cid, 0, label="Newer", terms="alpha", is_dream=False)
    r = client.post(
        f"/targets/categories/{cid}",
        data={**_form(label="Late", terms="beta"), "version": 0},
        headers=_tok(),
    )
    assert r.status_code == 409 and "changed since you opened it" in r.text
    assert cs.get_category(conn, cid)["label"] == "Newer"


def test_an_invalid_edit_is_a_400_and_a_missing_one_a_404(env):
    client, conn = env
    cid = cs.add_category(conn, label="Group", terms="alpha", is_dream=False)
    bad = client.post(
        f"/targets/categories/{cid}", data={**_form(terms=""), "version": 0}, headers=_tok()
    )
    assert bad.status_code == 400
    gone = client.post("/targets/categories/999", data={**_form(), "version": 0}, headers=_tok())
    assert gone.status_code == 404


def test_disable_and_enable(env):
    client, conn = env
    cid = cs.add_category(conn, label="Group", terms="alpha", is_dream=False)
    r = client.post(
        f"/targets/categories/{cid}/enabled",
        data={"version": 0, "enabled": "0"},
        headers=_tok(),
        follow_redirects=False,
    )
    assert r.status_code == 303 and cs.get_category(conn, cid)["enabled"] == 0
    stale = client.post(
        f"/targets/categories/{cid}/enabled", data={"version": 0, "enabled": "1"}, headers=_tok()
    )
    assert stale.status_code == 409 and cs.get_category(conn, cid)["enabled"] == 0


def test_delete_keeps_the_job_and_drops_the_hit(env):
    client, conn = env
    cid = cs.add_category(conn, label="Group", terms="alpha", is_dream=True)
    jid = dbc.upsert_job(conn, source="adzuna", external_id="1", title="CIO", company="Acme")
    cs.attach_hit(conn, jid, cid, 0)
    conn.commit()
    stale = client.post(f"/targets/categories/{cid}/delete", data={"version": 5}, headers=_tok())
    assert stale.status_code == 409 and cs.get_category(conn, cid) is not None
    r = client.post(
        f"/targets/categories/{cid}/delete",
        data={"version": 0},
        headers=_tok(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert cs.get_category(conn, cid) is None
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM category_hits").fetchone()[0] == 0


def test_a_missing_delete_is_a_404_and_huge_ids_are_not_a_500(env):
    client, _ = env
    assert (
        client.post(
            "/targets/categories/999/delete", data={"version": 0}, headers=_tok()
        ).status_code
        == 404
    )
    huge = 2**70
    r = client.post(f"/targets/categories/{huge}/delete", data={"version": 0}, headers=_tok())
    assert r.status_code == 422


def test_labels_and_terms_are_escaped_on_the_page_and_on_job_badges(env):
    client, conn = env
    label = '<script>alert("x")</script>'
    cid = cs.add_category(conn, label=label, terms="<b>bold</b>", is_dream=True)
    page = client.get("/targets").text
    assert "<script>alert" not in page and "<b>bold</b>" not in page
    assert "&lt;script&gt;" in page

    jid = dbc.upsert_job(
        conn,
        source="adzuna",
        external_id="1",
        title="CIO",
        company="Acme",
        url="https://example.com/1",
        geo_location_raw="Phoenix, AZ",
        geo_city="Phoenix",
        geo_region="AZ",
    )
    conn.execute("UPDATE jobs SET geo_status='verified' WHERE id=?", (jid,))
    cs.attach_hit(conn, jid, cid, 0)
    conn.commit()
    detail = client.get(f"/jobs/{jid}").text
    assert "<script>alert" not in detail
    assert "Dream: &lt;script&gt;" in detail

"""The story bank (#101): storage, validation, version checks, the figure check, the routes."""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest
from fastapi.testclient import TestClient

from blastjob.core import stories as st
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web.app import create_app
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN

H = {CSRF_HEADER: LAUNCH_TOKEN}
WORK_HISTORY = "# Work history\n\nAt Acme I cut costs by 40% and led a team of 1200.\n"
PAYLOAD = "<script>alert(1)</script>"
STAR = ("situation", "task", "action", "result")


def _fields(**over: str) -> dict[str, str]:
    base = {
        "title": "Vendor consolidation",
        "employer": "Acme",
        "year": "2021",
        "situation": "Too many vendors.",
        "task": "Cut the list.",
        "action": "Ran a review.",
        "result": "Cut costs by 40%.",
        "tags": "cost savings, vendors",
    }
    return {**base, **over}


def _row(**over: str) -> dict[str, str]:
    return {"title": "t", "situation": "", "task": "", "action": "", "result": "", **over}


@pytest.fixture
def cfg(tmp_path) -> BlastJobConfig:
    c = BlastJobConfig()
    c.paths.data_dir = str(tmp_path)
    (tmp_path / "work_history.md").write_text(WORK_HISTORY, encoding="utf-8")
    return c


@pytest.fixture
def conn(cfg) -> sqlite3.Connection:
    c = dbc.init_db(cfg)
    yield c
    c.close()


@pytest.fixture
def client(cfg, conn):
    with TestClient(create_app(load_config=lambda: cfg.model_copy(deep=True))) as c:
        yield c


def _count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0])


# ------------------------------------------------------------------ validation


def test_an_empty_story_is_refused(conn):
    with pytest.raises(st.StoryError):
        st.add_story(conn, **_fields(situation="", task="", action="", result=""))
    assert _count(conn) == 0


def test_a_blank_title_is_refused(conn):
    with pytest.raises(st.StoryError):
        st.add_story(conn, **_fields(title="   "))


@pytest.mark.parametrize(
    ("field", "limit"),
    [
        ("title", 80),
        ("employer", 80),
        ("situation", 1500),
        ("task", 1500),
        ("action", 1500),
        ("result", 1500),
        ("tags", 200),
    ],
)
def test_overlong_fields_are_refused_and_the_limit_is_accepted(conn, field, limit):
    with pytest.raises(st.StoryError):
        st.add_story(conn, **_fields(**{field: "x" * (limit + 1)}))
    st.add_story(conn, **_fields(**{field: "x" * limit}))
    assert _count(conn) == 1


@pytest.mark.parametrize("year", ["19", "1969", "abcd", "20 21", "2021.5", "-2021", "99999"])
def test_a_bad_year_is_refused(conn, year):
    with pytest.raises(st.StoryError):
        st.add_story(conn, **_fields(year=year))


def test_a_future_year_is_refused_and_this_year_and_empty_are_accepted(conn):
    this_year = dt.date.today().year
    with pytest.raises(st.StoryError):
        st.add_story(conn, **_fields(year=str(this_year + 1)))
    st.add_story(conn, **_fields(year=str(this_year)))
    st.add_story(conn, **_fields(year=""))
    st.add_story(conn, **_fields(year="1970"))
    assert _count(conn) == 3


# ------------------------------------------------------------------ versions


def test_a_new_story_starts_at_version_one(conn):
    sid = st.add_story(conn, **_fields())
    assert st.get_story(conn, sid)["version"] == 1


def test_an_edit_bumps_the_version(conn):
    sid = st.add_story(conn, **_fields())
    st.update_story(conn, sid, 1, **_fields(result="Cut costs by 40% again."))
    row = st.get_story(conn, sid)
    assert row["version"] == 2 and row["result"] == "Cut costs by 40% again."


def test_a_stale_edit_is_rejected_and_the_newer_text_is_kept(conn):
    sid = st.add_story(conn, **_fields())
    st.update_story(conn, sid, 1, **_fields(result="Newer text."))
    with pytest.raises(st.StaleStory):
        st.update_story(conn, sid, 1, **_fields(result="Older text."))
    assert st.get_story(conn, sid)["result"] == "Newer text."


def test_two_quick_edits_from_the_same_version_save_only_the_first(conn):
    sid = st.add_story(conn, **_fields())
    st.update_story(conn, sid, 1, **_fields(title="First"))
    with pytest.raises(st.StaleStory):
        st.update_story(conn, sid, 1, **_fields(title="Second"))
    row = st.get_story(conn, sid)
    assert row["title"] == "First" and row["version"] == 2


def test_a_stale_delete_is_rejected_and_the_story_stays(conn):
    sid = st.add_story(conn, **_fields())
    st.update_story(conn, sid, 1, **_fields(title="Edited"))
    with pytest.raises(st.StaleStory):
        st.delete_story(conn, sid, 1)
    assert st.get_story(conn, sid) is not None


def test_a_current_delete_removes_the_story(conn):
    sid = st.add_story(conn, **_fields())
    st.delete_story(conn, sid, 1)
    assert st.get_story(conn, sid) is None


def test_editing_or_deleting_a_missing_story_says_not_found(conn):
    with pytest.raises(st.StoryNotFound):
        st.update_story(conn, 99, 1, **_fields())
    with pytest.raises(st.StoryNotFound):
        st.delete_story(conn, 99, 1)


def test_a_rejected_edit_with_bad_fields_changes_nothing(conn):
    sid = st.add_story(conn, **_fields())
    with pytest.raises(st.StoryError):
        st.update_story(conn, sid, 1, **_fields(year="nope"))
    row = st.get_story(conn, sid)
    assert row["version"] == 1 and row["year"] == 2021


# ------------------------------------------------------------------ order


def test_the_list_is_newest_updated_first_with_id_breaking_ties(conn):
    a = st.add_story(conn, **_fields(title="A"))
    b = st.add_story(conn, **_fields(title="B"))
    c = st.add_story(conn, **_fields(title="C"))
    same = "2026-01-01T00:00:00.000000"
    conn.execute("UPDATE stories SET updated_at=? WHERE id IN (?, ?)", (same, a, b))
    conn.execute("UPDATE stories SET updated_at='2026-02-01T00:00:00.000000' WHERE id=?", (c,))
    conn.commit()
    assert [r["title"] for r in st.list_stories(conn)] == ["C", "B", "A"]
    st.update_story(conn, a, 1, **_fields(title="A"))
    assert [r["title"] for r in st.list_stories(conn)][0] == "A"


# ------------------------------------------------------------------ figure check


def test_a_figure_not_in_the_work_history_is_listed():
    assert st.unverified_figures(_row(result="Saved 95%."), WORK_HISTORY) == ["95%"]


def test_a_figure_in_the_work_history_is_not_listed():
    row = _row(situation="Team of 1200.", result="40%.")
    assert st.unverified_figures(row, WORK_HISTORY) == []


def test_a_truncated_figure_is_not_cleared_by_a_longer_one():
    assert st.unverified_figures(_row(result="Led 1,200 people."), WORK_HISTORY) == []
    assert st.unverified_figures(_row(result="Saved 4%."), WORK_HISTORY) == ["4%"]


def test_a_repeated_figure_is_listed_once():
    row = _row(situation="95%", task="95%", result="95%")
    assert st.unverified_figures(row, WORK_HISTORY) == ["95%"]


# ------------------------------------------------------------------ migration


def test_the_migration_applies_to_an_existing_database(cfg):
    c = dbc.init_db(cfg)
    c.execute("DROP TABLE stories")
    c.execute(
        "DELETE FROM schema_migrations WHERE version=(SELECT MAX(version) FROM schema_migrations)"
    )
    c.commit()
    dbc.migrate(c)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(stories)")}
    assert {"title", "employer", "year", "situation", "tags", "version", "updated_at"} <= cols
    c.close()


# ------------------------------------------------------------------ routes


def test_post_without_a_token_is_403(client, conn):
    assert client.post("/stories", data=_fields()).status_code == 403
    assert _count(conn) == 0


def test_post_with_a_foreign_origin_is_403(client, conn):
    r = client.post("/stories", data=_fields(), headers={**H, "Origin": "https://evil.example.org"})
    assert r.status_code == 403 and _count(conn) == 0


def test_edit_and_delete_without_a_token_are_403(client, conn):
    sid = st.add_story(conn, **_fields())
    assert client.post(f"/stories/{sid}", data={**_fields(), "version": 1}).status_code == 403
    assert client.post(f"/stories/{sid}/delete", data={"version": 1}).status_code == 403
    assert st.get_story(conn, sid)["version"] == 1


def test_the_page_renders_and_the_nav_links_to_it(client):
    r = client.get("/stories")
    assert r.status_code == 200 and "<form" in r.text
    assert 'href="/stories"' in client.get("/targets").text


def test_add_edit_delete_through_the_routes(client, conn):
    r = client.post("/stories", data=_fields(), headers=H)
    assert r.status_code == 200 and "Vendor consolidation" in r.text
    sid = int(conn.execute("SELECT id FROM stories").fetchone()[0])
    r = client.post(f"/stories/{sid}", data={**_fields(title="Renamed"), "version": 1}, headers=H)
    assert r.status_code == 200 and "Renamed" in r.text
    r = client.post(f"/stories/{sid}/delete", data={"version": 2}, headers=H)
    assert r.status_code == 200 and _count(conn) == 0


def test_a_stale_edit_through_the_route_is_409_and_keeps_the_newer_text(client, conn):
    sid = st.add_story(conn, **_fields())
    st.update_story(conn, sid, 1, **_fields(title="Newer title"))
    r = client.post(f"/stories/{sid}", data={**_fields(title="Older"), "version": 1}, headers=H)
    assert r.status_code == 409
    assert st.get_story(conn, sid)["title"] == "Newer title"
    assert "Newer title" in r.text


def test_a_stale_delete_through_the_route_is_409(client, conn):
    sid = st.add_story(conn, **_fields())
    st.update_story(conn, sid, 1, **_fields())
    r = client.post(f"/stories/{sid}/delete", data={"version": 1}, headers=H)
    assert r.status_code == 409 and _count(conn) == 1


def test_invalid_input_through_the_route_is_400_and_saves_nothing(client, conn):
    r = client.post("/stories", data=_fields(year="1800"), headers=H)
    assert r.status_code == 400 and _count(conn) == 0
    empty = _fields(situation="", task="", action="", result="")
    assert client.post("/stories", data=empty, headers=H).status_code == 400
    assert _count(conn) == 0


def test_a_missing_story_is_404(client):
    bad = {**_fields(), "version": 1}
    assert client.post("/stories/99", data=bad, headers=H).status_code == 404
    assert client.post("/stories/99/delete", data={"version": 1}, headers=H).status_code == 404


def test_html_is_escaped_in_every_field(client, conn):
    data = _fields(
        title=PAYLOAD,
        employer=PAYLOAD,
        situation=PAYLOAD,
        task=PAYLOAD,
        action=PAYLOAD,
        result=PAYLOAD,
        tags=PAYLOAD,
    )
    client.post("/stories", data=data, headers=H)
    page = client.get("/stories").text
    assert PAYLOAD not in page
    assert "&lt;script&gt;" in page


def test_a_rejected_form_is_escaped_too(client, conn):
    r = client.post("/stories", data=_fields(title=PAYLOAD, year="1800"), headers=H)
    assert r.status_code == 400
    assert PAYLOAD not in r.text


def test_the_page_lists_figures_missing_from_the_work_history(client, conn):
    st.add_story(conn, **_fields(result="Saved 95% and cut costs by 40%."))
    page = client.get("/stories").text
    assert "Not in your work history: 95%" in page
    assert "Not in your work history: 95%, 40%" not in page


def test_the_page_lists_stories_in_order(client, conn):
    a = st.add_story(conn, **_fields(title="Alpha story"))
    st.add_story(conn, **_fields(title="Beta story"))
    st.update_story(conn, a, 1, **_fields(title="Alpha story"))
    page = client.get("/stories").text
    assert page.index("Alpha story") < page.index("Beta story")


# ------------------------------------------------------------------ review fixes

HUGE = 2**70


def test_a_huge_version_or_id_is_a_4xx_not_a_500(client, conn):
    sid = st.add_story(conn, **_fields())
    r = client.post(f"/stories/{sid}", data={**_fields(), "version": HUGE}, headers=H)
    assert 400 <= r.status_code < 500
    r = client.post(f"/stories/{sid}/delete", data={"version": HUGE}, headers=H)
    assert 400 <= r.status_code < 500
    r = client.post(f"/stories/{HUGE}", data={**_fields(), "version": 1}, headers=H)
    assert 400 <= r.status_code < 500
    r = client.post(f"/stories/{HUGE}/delete", data={"version": 1}, headers=H)
    assert 400 <= r.status_code < 500
    assert st.get_story(conn, sid)["version"] == 1


def test_an_unreadable_work_history_warns_instead_of_a_clean_result(client, conn, tmp_path):
    (tmp_path / "work_history.md").write_bytes(b"\xff\xfe\x00 not utf-8 \x80")
    st.add_story(conn, **_fields(result="Saved 95%."))
    r = client.get("/stories")
    assert r.status_code == 200
    assert "check figures" in r.text
    assert "Not in your work history" not in r.text


def test_crlf_does_not_count_toward_the_length_cap(conn):
    text = "\r\n".join(["x" * 99] * 15)  # 1,499 characters with LF, 1,513 with CRLF
    assert len(text) > 1500
    sid = st.add_story(conn, **_fields(result=text))
    stored = st.get_story(conn, sid)["result"]
    assert "\r" not in stored and len(stored) == 1499
    sid2 = st.add_story(conn, **_fields(result="a\rb\r\nc"))
    assert st.get_story(conn, sid2)["result"] == "a\nb\nc"

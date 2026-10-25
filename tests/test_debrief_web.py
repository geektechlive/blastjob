"""The debrief routes: paste, choose a speaker, compute, list, delete (#84).

Offline: the transcript is an invented one, the work history is a file in a temp data dir.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web import routes_debrief
from blastjob.web.app import create_app
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN
from tests.debrief_fixtures import BASIC, OTHER, OWNER, SENTINEL, header, vtt

TOK = {CSRF_HEADER: LAUNCH_TOKEN}
WORK_HISTORY = "# Work history\n\nAt Acme I cut costs by 40% and led a team of 1200.\n"
OWNER_NUMBERS = [
    (0, OTHER, "Welcome"),
    (10, OWNER, "At Acme we cut costs by 95% and saved $182 per seat."),
    (50, OTHER, f"{SENTINEL} Thanks"),
    (90, OWNER, "What does the first year look like?"),
    (100, OTHER, "Bye"),
]


class Env:
    def __init__(self, client: TestClient, conn: sqlite3.Connection, job_id: int, other_id: int):
        self.client, self.conn, self.job, self.other_job = client, conn, job_id, other_id

    @property
    def store(self):
        return self.client.app.state.debrief_store  # type: ignore[attr-defined]

    def upload(self, text: str, job: int | None = None, label: str = "", **kw):
        return self.client.post(
            f"/jobs/{job or self.job}/debrief/upload",
            data={"transcript": text, "label": label},
            headers=TOK,
            follow_redirects=False,
            **kw,
        )

    def upload_id(self, text: str, job: int | None = None) -> str:
        r = self.upload(text, job)
        assert r.status_code == 303, r.text
        return r.headers["location"].rstrip("/").rsplit("/", 1)[1]

    def compute(self, upload_id: str, speaker: str, job: int | None = None, headers=TOK):
        return self.client.post(
            f"/jobs/{job or self.job}/debrief/{upload_id}/compute",
            data={"speaker": speaker},
            headers=headers,
            follow_redirects=False,
        )

    def rows(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM interview_debriefs ORDER BY id").fetchall()


def _job(conn: sqlite3.Connection, ext: str) -> int:
    cur = conn.execute(
        "INSERT INTO jobs (source, external_id, title, company, first_seen, last_seen) "
        "VALUES ('manual', ?, 'VP Technology', 'Acme', '2026-10-01', '2026-10-01')",
        (ext,),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


@pytest.fixture
def env(tmp_path):
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path)
    (tmp_path / "work_history.md").write_text(WORK_HISTORY, encoding="utf-8")
    conn = dbc.init_db(cfg)
    job, other = _job(conn, "a"), _job(conn, "b")
    with TestClient(create_app(load_config=lambda: cfg.model_copy(deep=True))) as client:
        yield Env(client, conn, job, other)
    conn.close()


# ----------------------------------------------------------------------------- the flow


def test_the_paste_page_renders_for_a_job_and_404s_for_none(env):
    page = env.client.get(f"/jobs/{env.job}/debrief")
    assert page.status_code == 200 and "<textarea" in page.text
    assert env.client.get("/jobs/9999/debrief").status_code == 404


def test_upload_keeps_the_turns_in_memory_and_lists_the_speakers(env):
    uid = env.upload_id(header(OWNER_NUMBERS))
    assert len(env.store) == 1
    assert env.rows() == []
    page = env.client.get(f"/jobs/{env.job}/debrief/{uid}")
    assert page.status_code == 200
    assert OWNER in page.text and OTHER in page.text
    assert "Which speaker are you" in page.text


def test_compute_stores_the_debrief_and_drops_the_transcript(env):
    uid = env.upload_id(header(OWNER_NUMBERS))
    r = env.compute(uid, OWNER)
    assert r.status_code == 303 and r.headers["location"] == f"/jobs/{env.job}"
    assert len(env.store) == 0
    (row,) = env.rows()
    assert row["job_id"] == env.job and row["owner_label"] == OWNER
    page = env.client.get(f"/jobs/{env.job}").text
    assert "You said 95%: not in your work history" in page
    assert "You said $182: not in your work history" in page
    assert "40%" not in page.split("Interview debrief", 1)[1]
    assert "Question-like lines near the close" in page


def test_no_interviewer_text_or_filename_reaches_the_database(env):
    uid = env.upload_id(header(OWNER_NUMBERS), env.job)
    env.compute(uid, OWNER)
    dump = "\n".join(env.conn.iterdump())
    assert SENTINEL not in dump
    columns = {r["name"] for r in env.conn.execute("PRAGMA table_info(interview_debriefs)")}
    assert not columns & {"source_name", "filename", "transcript", "text"}


def test_a_webvtt_paste_uses_cue_times(env):
    text = vtt(
        [(0, 10, OTHER, "Hello."), (12, 40, OWNER, "I led a team of 12."), (50, 60, OTHER, "Ok")]
    )
    uid = env.upload_id(text)
    env.compute(uid, OWNER)
    assert env.rows()[0]["time_basis"] == "cue"


def test_the_label_is_cut_to_sixty_characters_and_escaped_on_the_page(env):
    r = env.upload(header(BASIC), label="<b>x</b>" + "y" * 100)
    uid = r.headers["location"].rsplit("/", 1)[1]
    env.compute(uid, OWNER)
    assert len(env.rows()[0]["label"]) == 60
    page = env.client.get(f"/jobs/{env.job}").text
    assert "<b>x</b>" not in page and "&lt;b&gt;x&lt;/b&gt;" in page


def test_an_unreadable_transcript_re_renders_the_form_with_the_reason(env):
    r = env.upload("nothing useful here")
    assert r.status_code == 400
    assert "WebVTT" in r.text and "<textarea" in r.text
    assert len(env.store) == 0


def test_a_missing_work_history_says_the_numbers_check_is_unavailable(env, tmp_path):
    (tmp_path / "work_history.md").unlink()
    uid = env.upload_id(header(OWNER_NUMBERS))
    env.compute(uid, OWNER)
    page = env.client.get(f"/jobs/{env.job}").text
    assert "numbers check unavailable" in page.lower()
    assert "not in your work history" not in page


# ----------------------------------------------------------------------------- refusals


def test_an_unknown_upload_id_means_upload_again(env):
    r = env.compute("deadbeef", OWNER)
    assert r.status_code == 410 and "upload again" in r.text.lower()


def test_an_expired_upload_means_upload_again(env):
    uid = env.upload_id(header(BASIC))
    env.store.clear()
    r = env.compute(uid, OWNER)
    assert r.status_code == 410
    assert env.client.get(f"/jobs/{env.job}/debrief/{uid}").status_code == 410


def test_an_upload_id_from_another_job_is_refused(env):
    uid = env.upload_id(header(BASIC), env.job)
    assert env.compute(uid, OWNER, job=env.other_job).status_code == 410
    assert env.client.get(f"/jobs/{env.other_job}/debrief/{uid}").status_code == 410
    assert env.rows() == []
    assert len(env.store) == 1


def test_a_speaker_not_in_the_transcript_is_a_400_and_keeps_the_upload(env):
    uid = env.upload_id(header(BASIC))
    r = env.compute(uid, "Somebody Else")
    assert r.status_code == 400
    assert len(env.store) == 1 and env.rows() == []


def test_a_failed_insert_leaves_nothing_in_the_store(env, monkeypatch):
    def boom(*a, **k):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(routes_debrief.debrief, "insert_debrief", boom)
    uid = env.upload_id(header(BASIC))
    r = env.compute(uid, OWNER)
    assert r.status_code == 500 and "upload it again" in r.text.lower()
    assert len(env.store) == 0 and env.rows() == []


def test_concurrent_computes_store_one_row(env):
    uid = env.upload_id(header(BASIC))
    codes: list[int] = []
    barrier = threading.Barrier(4)

    def go() -> None:
        barrier.wait()
        codes.append(env.compute(uid, OWNER).status_code)

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes) == [303, 410, 410, 410]
    assert len(env.rows()) == 1


# ----------------------------------------------------------------------------- the body path


def test_an_oversized_body_is_rejected_before_it_is_parsed(env, monkeypatch):
    def fail(*a, **k):
        raise AssertionError("parsed an oversized body")

    monkeypatch.setattr(routes_debrief, "parse_transcript", fail)
    r = env.upload("x" * (3 * 1024 * 1024))
    assert r.status_code == 413
    assert len(env.store) == 0


def test_a_multipart_post_is_refused_so_nothing_is_spooled_to_disk(env, monkeypatch):
    from starlette import formparsers

    def fail(*a, **k):
        raise AssertionError("multipart parsing started")

    monkeypatch.setattr(formparsers.MultiPartParser, "parse", fail)
    r = env.client.post(
        f"/jobs/{env.job}/debrief/upload",
        files={"transcript": ("t.txt", header(BASIC).encode())},
        headers=TOK,
    )
    assert r.status_code == 415
    assert len(env.store) == 0


def test_other_routes_keep_their_normal_body_handling(env):
    # the cap is path-aware: a small post elsewhere is untouched
    assert env.client.get("/").status_code == 200


# ----------------------------------------------------------------------------- CSRF, delete


def test_every_post_needs_the_token(env):
    uid = env.upload_id(header(BASIC))
    r1 = env.client.post(f"/jobs/{env.job}/debrief/upload", data={"transcript": header(BASIC)})
    r2 = env.client.post(f"/jobs/{env.job}/debrief/{uid}/compute", data={"speaker": OWNER})
    env.compute(uid, OWNER)
    rid = env.rows()[0]["id"]
    r3 = env.client.post(f"/jobs/{env.job}/debrief/{rid}/delete")
    assert (r1.status_code, r2.status_code, r3.status_code) == (403, 403, 403)
    assert len(env.rows()) == 1


def test_a_foreign_origin_is_refused(env):
    r = env.client.post(
        f"/jobs/{env.job}/debrief/upload",
        data={"transcript": header(BASIC)},
        headers={**TOK, "origin": "http://evil.example.com"},
    )
    assert r.status_code == 403


def test_delete_removes_the_debrief_and_is_scoped_to_its_job(env):
    env.compute(env.upload_id(header(BASIC)), OWNER)
    rid = env.rows()[0]["id"]
    wrong = env.client.post(
        f"/jobs/{env.other_job}/debrief/{rid}/delete", headers=TOK, follow_redirects=False
    )
    assert wrong.status_code == 404 and len(env.rows()) == 1
    ok = env.client.post(
        f"/jobs/{env.job}/debrief/{rid}/delete", headers=TOK, follow_redirects=False
    )
    assert ok.status_code == 303 and env.rows() == []


def test_the_store_is_cleared_when_the_app_shuts_down(env):
    env.upload_id(header(BASIC))
    store = env.store
    env.client.__exit__(None, None, None)
    assert len(store) == 0


def test_debriefs_list_newest_first_on_the_job_page(env):
    env.compute(env.upload_id(header(BASIC)), OWNER)
    env.conn.execute("UPDATE interview_debriefs SET label='roundalpha'")
    env.conn.commit()
    r = env.upload(header(BASIC), label="roundbeta")
    env.compute(r.headers["location"].rsplit("/", 1)[1], OWNER)
    page = env.client.get(f"/jobs/{env.job}").text
    assert page.index("roundbeta") < page.index("roundalpha")

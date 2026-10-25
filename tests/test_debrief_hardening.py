"""Hostile input and edge paths for the debrief: ReDoS, caps, path and header tricks (#84)."""

from __future__ import annotations

import asyncio
import dataclasses
import time

import pytest
from fastapi.testclient import TestClient

from blastjob.core.debrief import compute_debrief, insert_debrief
from blastjob.core.transcript import (
    MAX_NAME_CHARS,
    MAX_SPEAKERS,
    MAX_TURNS,
    TranscriptError,
    parse_transcript,
)
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web import routes_debrief
from blastjob.web.app import create_app
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN
from tests.debrief_fixtures import BASIC, OTHER, OWNER, header, vtt

BUDGET_S = 1.0
TOK = {CSRF_HEADER: LAUNCH_TOKEN}


def _fast(text: str) -> None:
    started = time.monotonic()
    try:
        parse_transcript(text)
    except TranscriptError:
        pass
    assert time.monotonic() - started < BUDGET_S


def test_a_hostile_voice_tag_does_not_backtrack():
    cue = "00:00:00.000 --> 00:00:05.000\n<v" + ".a" * 4000 + " "
    _fast("WEBVTT\n\n" + cue + "\n")


def test_a_hostile_header_line_is_cheap():
    _fast("00:00:01 " + " " * 200_000 + "x\nhello\n00:00:09 B\nhi\n")


def test_hostile_unclosed_tags_are_cheap():
    cue = "00:00:00.000 --> 00:00:05.000\nA: " + "<x" * 200_000
    _fast("WEBVTT\n\n" + cue + "\n")


def test_a_very_long_inline_line_is_cheap():
    _fast("[00:00:01] " + "N" * 300_000 + "\n[00:00:09] B: hi\n")


def test_a_long_turn_keeps_all_its_words():
    body = " ".join(["word"] * 3000)
    t = parse_transcript(f"00:00:01 Speaker 1\n{body}\n00:00:09 Speaker 2\nhi\n")
    assert len(t.turns[0].text.split()) == 3000


def test_a_long_inline_turn_keeps_all_its_words():
    body = " ".join(["word"] * 3000)
    t = parse_transcript(f"[00:00:01] Ann: {body}\n[00:00:09] Bo: hi\n")
    assert len(t.turns[0].text.split()) == 3000


def test_speaker_names_are_capped_for_voice_tags_and_inline_lines():
    long_name = "N" * 100
    t = parse_transcript(vtt([(0, 4, long_name, "Hello."), (5, 9, OWNER, "Hi.")]))
    assert all(len(x.speaker) <= MAX_NAME_CHARS for x in t.turns)
    with pytest.raises(TranscriptError):
        parse_transcript(f"[00:00:01] {long_name}: hi\n[00:00:09] Bo: hello\n")


def test_too_many_turns_are_refused():
    turns = [(i, OTHER if i % 2 else OWNER, "x") for i in range(MAX_TURNS + 1)]
    with pytest.raises(TranscriptError, match="turns"):
        parse_transcript(header(turns))


def test_too_many_speakers_are_refused():
    turns = [(i, f"Speaker {i}", "x") for i in range(MAX_SPEAKERS + 1)]
    with pytest.raises(TranscriptError, match="speakers"):
        parse_transcript(header(turns))


def test_a_long_owner_label_is_cut_before_it_is_stored(tmp_path):
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path)
    conn = dbc.init_db(cfg)
    d = dataclasses.replace(
        compute_debrief(parse_transcript(header(BASIC)), OWNER, ""), owner_label="N" * 500
    )
    insert_debrief(conn, 1, "x1", "", d)
    stored = conn.execute("SELECT owner_label FROM interview_debriefs").fetchone()[0]
    assert len(stored) == MAX_NAME_CHARS
    conn.close()


@pytest.fixture
def client(tmp_path):
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path)
    conn = dbc.init_db(cfg)
    conn.execute(
        "INSERT INTO jobs (source, external_id, title, company, first_seen, last_seen) "
        "VALUES ('manual', 'a', 'VP', 'Acme', '2026-10-01', '2026-10-01')"
    )
    conn.commit()
    conn.close()
    with TestClient(create_app(load_config=lambda: cfg.model_copy(deep=True))) as c:
        yield c


def test_a_trailing_newline_in_the_path_does_not_escape_the_cap(client):
    r = client.post(
        "/jobs/1/debrief/upload%0A",
        files={"transcript": ("t.txt", header(BASIC).encode())},
        headers=TOK,
    )
    assert r.status_code == 415
    big = client.post(
        "/jobs/1/debrief/upload%0A",
        data={"transcript": "x" * (3 * 1024 * 1024)},
        headers=TOK,
    )
    assert big.status_code == 413


def test_any_multipart_content_type_header_is_refused(client):
    r = client.post(
        "/jobs/1/debrief/upload",
        content=b"--b--",
        headers=[
            (CSRF_HEADER, LAUNCH_TOKEN),
            ("content-type", "application/x-www-form-urlencoded"),
            ("content-type", "multipart/form-data; boundary=b"),
        ],
    )
    assert r.status_code == 415


def test_parsing_runs_off_the_event_loop(client, monkeypatch):
    seen: list[bool] = []
    real = routes_debrief.parse_transcript

    def spy(text):
        try:
            asyncio.get_running_loop()
            seen.append(False)  # on the event loop's own thread
        except RuntimeError:
            seen.append(True)
        return real(text)

    monkeypatch.setattr(routes_debrief, "parse_transcript", spy)
    client.post(
        "/jobs/1/debrief/upload",
        data={"transcript": header(BASIC)},
        headers=TOK,
        follow_redirects=False,
    )
    assert seen == [True]

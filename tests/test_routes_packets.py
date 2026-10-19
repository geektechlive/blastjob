"""Tests for routes_packets.py, especially SSE build events and Last-Event-ID parsing."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from blastjob.web import routes_packets
from blastjob.web.deps import Deps
from blastjob.web.routes_packets import parse_last_event_id
from blastjob.web.runtime import RUNTIME, Event, Run


def test_parse_last_event_id():
    # None or empty
    assert parse_last_event_id(None) == 0
    assert parse_last_event_id("") == 0

    # Non-negative integers
    assert parse_last_event_id("0") == 0
    assert parse_last_event_id("1") == 1
    assert parse_last_event_id("42") == 42
    assert parse_last_event_id("  10  ") == 10

    # Malformed or negative fall back to default
    assert parse_last_event_id("abc") == 0
    assert parse_last_event_id("-1") == 0
    assert parse_last_event_id("-99") == 0
    assert parse_last_event_id("1.5") == 0
    assert parse_last_event_id("none") == 0

    # Custom default
    assert parse_last_event_id(None, default=7) == 7
    assert parse_last_event_id("abc", default=7) == 7
    assert parse_last_event_id("-1", default=7) == 7
    assert parse_last_event_id("15", default=7) == 15


def test_build_events_malformed_last_event_id_does_not_500():
    app = FastAPI()
    deps = Deps(
        conn=lambda: None,
        load_config=lambda: None,
        render=lambda *a, **k: None,
        nav_counts=lambda: {},
        radius=lambda: 0.0,
    )
    routes_packets.register(app, deps)
    client = TestClient(app, raise_server_exceptions=False)

    for malformed in ("abc", "-1", "xyz", "none", "1.5"):
        r = client.get("/builds/unknown-run-id/events", headers={"last-event-id": malformed})
        assert r.status_code == 200
        assert "text/event-stream" in r.headers.get("content-type", "")
        # For unknown run_id, RUNTIME.stream yields synthetic error event
        assert "event: error" in r.text
        assert "id: 1" in r.text


def test_build_events_last_event_id_replay():
    run = Run(id="run-replay-test", name="tailor", status="done")
    run.events = [
        Event(1, "output", "chunk 1"),
        Event(2, "output", "chunk 2"),
        Event(3, "output", "chunk 3"),
    ]
    RUNTIME._runs[run.id] = run
    try:
        app = FastAPI()
        deps = Deps(
            conn=lambda: None,
            load_config=lambda: None,
            render=lambda *a, **k: None,
            nav_counts=lambda: {},
            radius=lambda: 0.0,
        )
        routes_packets.register(app, deps)
        client = TestClient(app)

        # Valid last-event-id resumes after that id
        r = client.get(f"/builds/{run.id}/events", headers={"last-event-id": "2"})
        assert r.status_code == 200
        assert "chunk 1" not in r.text
        assert "chunk 2" not in r.text
        assert "chunk 3" in r.text

        # Malformed last-event-id falls back to 0 (replays from start)
        for malformed in ("abc", "-1"):
            r = client.get(f"/builds/{run.id}/events", headers={"last-event-id": malformed})
            assert r.status_code == 200
            assert "chunk 1" in r.text
            assert "chunk 2" in r.text
            assert "chunk 3" in r.text
    finally:
        RUNTIME._runs.pop(run.id, None)

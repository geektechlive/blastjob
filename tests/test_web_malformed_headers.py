"""A malformed Last-Event-ID or Content-Length must not 500 (#55)."""

from __future__ import annotations

import tempfile

import pytest
from fastapi.testclient import TestClient

from blastjob.models.config import BlastJobConfig
from blastjob.web.app import create_app
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN
from blastjob.web.upload_guard import parse_content_length


@pytest.fixture
def client():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    return TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))


@pytest.mark.parametrize("value", ["abc", "", "1.5", "-"])
def test_events_with_malformed_last_event_id_falls_back_to_zero(client, value):
    r = client.get("/builds/no-such-run/events", headers={"Last-Event-ID": value})
    assert r.status_code == 200
    assert "event: error" in r.text


def test_events_honours_a_valid_last_event_id(client):
    r = client.get("/builds/no-such-run/events", headers={"Last-Event-ID": "7"})
    assert r.status_code == 200
    assert "id: 8" in r.text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), ("", None), ("abc", None), ("-5", None), ("12", 12), (b"34", 34), (" 9 ", 9)],
)
def test_parse_content_length(raw, expected):
    assert parse_content_length(raw) == expected


@pytest.mark.parametrize("path", ["/work-history/ingest", "/network/import"])
def test_upload_routes_survive_malformed_content_length(client, path):
    r = client.post(
        path,
        files={"files": ("resume.txt", b"hello", "text/plain")},
        headers={CSRF_HEADER: LAUNCH_TOKEN, "Content-Length": "abc"},
    )
    assert r.status_code < 500

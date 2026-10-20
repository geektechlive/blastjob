"""Content-Security-Policy: a per-request script nonce and no inline handlers (#28)."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web import app as web_app
from blastjob.web.app import create_app
from blastjob.web.runtime import RUNTIME, Run

TEMPLATES = Path(web_app.__file__).parent / "templates"
STATIC = Path(web_app.__file__).parent / "static"

NONCE_IN_POLICY = re.compile(r"script-src 'self' 'nonce-([A-Za-z0-9_-]{16,})'")
INLINE_SCRIPT = re.compile(r"<script\b(?![^>]*\bsrc=)([^>]*)>", re.I)
# on*= attributes, plus every htmx spelling of hx-on (hx-on:, hx-on::, hx-on-, data-hx-on).
INLINE_HANDLER = re.compile(r"""(?:\s|^)(?:data-)?(?:on[a-z]+\s*=|hx-on\b)""", re.I)


@pytest.fixture
def client():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    conn = dbc.init_db(cfg)
    job = dbc.upsert_job(conn, source="t", external_id="1", company="Acme", title="VP Ops")
    conn.execute("UPDATE jobs SET review_state='surfaced', alignment_score=90 WHERE id=?", (job,))
    conn.commit()
    conn.close()
    app = create_app(load_config=lambda: cfg, save_config=lambda c: None)
    return TestClient(app), app


def _nonce(response) -> str:
    match = NONCE_IN_POLICY.search(response.headers["Content-Security-Policy"])
    assert match, response.headers["Content-Security-Policy"]
    return match.group(1)


def test_policy_has_every_directive(client):
    c, _ = client
    policy = c.get("/").headers["Content-Security-Policy"]
    for directive in (
        "default-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ):
        assert directive in policy
    assert "unsafe-eval" not in policy
    assert "unsafe-inline" not in policy.split("script-src")[1].split(";")[0]


def test_nonce_is_fresh_per_request(client):
    c, _ = client
    assert _nonce(c.get("/")) != _nonce(c.get("/"))


def test_refused_request_still_carries_a_policy(client):
    c, _ = client
    r = c.get("/", headers={"host": "evil.example.com"})
    assert r.status_code == 421
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]


def _plain_get_paths(app) -> list[str]:
    skip = ("/static", "/docs", "/redoc", "/openapi.json", "/builds")
    paths = {
        r.path
        for r in app.routes
        if "GET" in (getattr(r, "methods", None) or ())
        and "{" not in r.path
        and not r.path.startswith(skip)
    }
    return sorted(paths)


def test_every_inline_script_in_every_reachable_page_carries_the_nonce(client):
    c, app = client
    seen_html = 0
    for path in _plain_get_paths(app):
        r = c.get(path)
        if "text/html" not in r.headers.get("content-type", ""):
            continue
        seen_html += 1
        nonce = _nonce(r)
        for attrs in INLINE_SCRIPT.findall(r.text):
            assert f'nonce="{nonce}"' in attrs, f"{path}: inline script without nonce"
    assert seen_html >= 10


def test_page_with_a_running_panel_has_no_unnonced_script(client):
    c, _ = client
    run = Run(id="ingest-csp-1", name="ingest")
    RUNTIME._runs[run.id] = run
    try:
        r = c.get("/work-history/ingest")
    finally:
        del RUNTIME._runs[run.id]
    assert 'data-run-id="ingest-csp-1"' in r.text
    nonce = _nonce(r)
    assert all(f'nonce="{nonce}"' in a for a in INLINE_SCRIPT.findall(r.text))


def test_no_template_has_an_inline_event_handler_or_hx_on():
    for path in sorted(TEMPLATES.glob("*.html")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            assert not INLINE_HANDLER.search(line), f"{path.name}:{n}: {line.strip()[:80]}"
            assert "javascript:" not in line, f"{path.name}:{n}: javascript: URL"


def test_every_inline_script_tag_in_templates_uses_the_nonce():
    for path in sorted(TEMPLATES.glob("*.html")):
        for attrs in INLINE_SCRIPT.findall(path.read_text(encoding="utf-8")):
            assert 'nonce="{{ csp_nonce }}"' in attrs, path.name


def test_swapped_panels_carry_no_script_of_their_own():
    """A script inside an htmx-swapped partial is checked against the PAGE's nonce,
    not the partial's, so panels are wired from the static file instead."""
    for name in ("_build_panel.html", "_followup_panel.html"):
        assert "<script" not in (TEMPLATES / name).read_text(encoding="utf-8")
    assert (STATIC / "cockpit.js").exists()

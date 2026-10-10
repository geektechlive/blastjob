"""Tests for web/packets.py -- surfacing the tailored-bundle location the user
already has (build_runs.artifact_dir) instead of leaving it write-only.

Covers the Codex-caught bug: a failed re-tailor must not hide an earlier
successful packet, and must not silently pretend the failed run's directory
is the answer.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web import packets


@pytest.fixture
def cfg():
    c = BlastJobConfig()
    c.paths.data_dir = tempfile.mkdtemp()
    c.paths.output_dir = tempfile.mkdtemp()
    return c


@pytest.fixture
def conn(cfg):
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _make_bundle(root: Path, name: str, *, resume=True, cover=True, extra=()) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    if resume:
        (d / "Alex_Rivera_Resume_-_Acme_VP_Ops.md").write_text("resume", encoding="utf-8")
        (d / "Alex_Rivera_Resume_-_Acme_VP_Ops.docx").write_bytes(b"docx")
    if cover:
        (d / "Alex_Rivera_Cover_Letter_-_Acme_VP_Ops.md").write_text("cover", encoding="utf-8")
    for fname, content in extra:
        (d / fname).write_text(content, encoding="utf-8")
    return d


# --- packet_for_job -----------------------------------------------------


def test_no_build_ever_returns_none(conn):
    jid = dbc.upsert_job(conn, source="s", external_id="e1", title="t")
    assert packets.packet_for_job(conn, jid) is None


def test_current_layout_packet_found(conn, cfg):
    """New layout: {root}/{company-slug}-{date}/"""
    jid = dbc.upsert_job(conn, source="s", external_id="e2", title="t")
    out_root = Path(cfg.paths.output_dir)
    bundle = _make_bundle(out_root, "northwind-2026-09-12")
    dbc.claim_for_build(conn, jid, "run-A")
    dbc.finish_build(conn, "run-A", artifact_dir=str(bundle), ok=True)

    result = packets.packet_for_job(conn, jid, load_config=lambda: cfg)
    assert result is not None
    assert result["dir"] == str(bundle)
    assert result["exists"] is True
    assert result["status"] == "done"
    assert {f["name"] for f in result["files"]} == {
        "Alex_Rivera_Resume_-_Acme_VP_Ops.md",
        "Alex_Rivera_Resume_-_Acme_VP_Ops.docx",
        "Alex_Rivera_Cover_Letter_-_Acme_VP_Ops.md",
    }


def test_legacy_layout_packet_found(conn, cfg):
    """Legacy layout: {root}/{date}/{company-slug}/{role-slug}/ -- must be read
    verbatim from artifact_dir, never recomputed from today's slugs."""
    jid = dbc.upsert_job(conn, source="s", external_id="e3", title="t")
    out_root = Path(cfg.paths.output_dir)
    bundle = out_root / "2026-08-25" / "fairview-robotics" / "chief-information-officer"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "Alex_Rivera_Resume_-_Fairview_Robotics_CIO.md").write_text(
        "resume", encoding="utf-8"
    )
    dbc.claim_for_build(conn, jid, "run-A")
    dbc.finish_build(conn, "run-A", artifact_dir=str(bundle), ok=True)

    result = packets.packet_for_job(conn, jid, load_config=lambda: cfg)
    assert result["dir"] == str(bundle)
    assert result["exists"] is True


def test_missing_directory_reports_not_exists(conn, cfg):
    jid = dbc.upsert_job(conn, source="s", external_id="e4", title="t")
    moved_away = str(Path(cfg.paths.output_dir) / "gone-2026-01-01")
    dbc.claim_for_build(conn, jid, "run-A")
    dbc.finish_build(conn, "run-A", artifact_dir=moved_away, ok=True)

    result = packets.packet_for_job(conn, jid, load_config=lambda: cfg)
    assert result["exists"] is False
    assert result["files"] == []


def test_failed_latest_run_after_successful_earlier_run_returns_previous(conn, cfg):
    """The bug an independent review caught: filtering to status='done' BEFORE
    ordering hides a failed re-tailor attempt. The latest attempt's status must
    be surfaced, and the earlier successful packet returned, clearly labelled
    as a previous packet (not the current one)."""
    jid = dbc.upsert_job(conn, source="s", external_id="e5", title="t")
    out_root = Path(cfg.paths.output_dir)
    good_bundle = _make_bundle(out_root, "acme-2026-09-01")

    dbc.claim_for_build(conn, jid, "run-10")
    dbc.finish_build(conn, "run-10", artifact_dir=str(good_bundle), ok=True)
    # job build_state is now 'tailored', so a second claim needs build_state
    # reset the way a real re-tailor failure would leave it -- simulate directly.
    conn.execute("UPDATE jobs SET build_state='failed' WHERE id=?", (jid,))
    conn.commit()
    dbc.claim_for_build(conn, jid, "run-11")
    dbc.finish_build(conn, "run-11", artifact_dir="", ok=False)

    result = packets.packet_for_job(conn, jid, load_config=lambda: cfg)
    assert result is not None
    assert result["dir"] == str(good_bundle)
    assert result["latest_status"] == "failed"
    assert result["previous"] is True
    assert result["exists"] is True


def test_failed_latest_with_no_prior_success_has_no_dir(conn, cfg):
    jid = dbc.upsert_job(conn, source="s", external_id="e6", title="t")
    dbc.claim_for_build(conn, jid, "run-1")
    dbc.finish_build(conn, "run-1", artifact_dir="", ok=False)

    result = packets.packet_for_job(conn, jid, load_config=lambda: cfg)
    assert result is not None
    assert result["status"] == "failed"
    assert result["exists"] is False
    assert result["dir"] == ""


def test_latest_done_run_is_not_marked_previous(conn, cfg):
    jid = dbc.upsert_job(conn, source="s", external_id="e7", title="t")
    out_root = Path(cfg.paths.output_dir)
    bundle = _make_bundle(out_root, "acme-2026-09-10")
    dbc.claim_for_build(conn, jid, "run-1")
    dbc.finish_build(conn, "run-1", artifact_dir=str(bundle), ok=True)

    result = packets.packet_for_job(conn, jid, load_config=lambda: cfg)
    assert result["previous"] is False
    assert result["status"] == "done"
    assert result["latest_status"] == "done"


# --- staleness ------------------------------------------------------------


def test_fresh_packet_is_not_stale(cfg):
    data_dir = Path(cfg.paths.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "work_history.md").write_text("v1", encoding="utf-8")
    built_at = __import__("datetime").datetime.now().isoformat()
    stale, reason = packets.staleness(built_at, cfg)
    assert stale is False
    assert reason == ""


def test_stale_packet_flags_updated_work_history(cfg):
    import datetime as dt
    import time

    data_dir = Path(cfg.paths.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    built_at = (dt.datetime.now() - dt.timedelta(days=1)).isoformat()
    (data_dir / "work_history.md").write_text("corrected", encoding="utf-8")
    time.sleep(0.01)

    stale, reason = packets.staleness(built_at, cfg)
    assert stale is True
    assert "work history" in reason
    assert "packet built" in reason


def test_staleness_ignores_missing_tracked_files(cfg):
    built_at = __import__("datetime").datetime.now().isoformat()
    # data_dir has no work_history.md / templates at all
    stale, reason = packets.staleness(built_at, cfg)
    assert stale is False


# --- packet_files -----------------------------------------------------


def test_packet_files_classifies_resume_and_cover(tmp_path):
    bundle = _make_bundle(tmp_path, "b1", extra=[("metadata.json", "{}")])
    files = packets.packet_files(bundle)
    by_name = {f["name"]: f for f in files}
    assert by_name["Alex_Rivera_Resume_-_Acme_VP_Ops.md"]["is_resume"] is True
    assert by_name["Alex_Rivera_Resume_-_Acme_VP_Ops.md"]["is_cover"] is False
    assert by_name["Alex_Rivera_Cover_Letter_-_Acme_VP_Ops.md"]["is_cover"] is True
    assert by_name["Alex_Rivera_Cover_Letter_-_Acme_VP_Ops.md"]["is_resume"] is False
    assert by_name["metadata.json"]["is_resume"] is False
    assert by_name["metadata.json"]["is_cover"] is False
    assert all(f["size"] > 0 for f in files)


def test_packet_files_on_missing_dir_returns_empty(tmp_path):
    assert packets.packet_files(tmp_path / "nope") == []


# --- quarantine listing -------------------------------------------------


def test_quarantined_documents_are_listed_not_hidden(tmp_path):
    """A quarantined packet must not read as an empty file list -- the user needs
    to see what's held, not a folder that looks like the build failed."""
    from blastjob.core import quarantine as qz

    bundle = _make_bundle(tmp_path, "b2")
    stem = "Alex_Rivera_Resume_-_Acme_VP_Ops"
    qz.quarantine(bundle, stems=[stem], reasons=["banned characters"])

    files = packets.packet_files(bundle)
    by_name = {f["name"]: f for f in files}
    assert f"{stem}.md" in by_name
    assert by_name[f"{stem}.md"]["quarantined"] is True
    # The cover letter was never touched -- still listed, not quarantined.
    assert by_name["Alex_Rivera_Cover_Letter_-_Acme_VP_Ops.md"]["quarantined"] is False


def test_non_quarantined_files_are_flagged_false(tmp_path):
    bundle = _make_bundle(tmp_path, "b3")
    files = packets.packet_files(bundle)
    assert all(f["quarantined"] is False for f in files)


def test_packet_files_is_not_a_recursive_walk(tmp_path):
    """Only the one known `quarantine/` subdirectory is descended -- not an
    arbitrary nested folder."""
    bundle = _make_bundle(tmp_path, "b4")
    nested = bundle / "some_other_subdir"
    nested.mkdir()
    (nested / "stray.txt").write_text("x", encoding="utf-8")
    names = {f["name"] for f in packets.packet_files(bundle)}
    assert "stray.txt" not in names


def test_packet_for_job_reports_quarantined_flag(conn, cfg):
    from blastjob.core import quarantine as qz

    jid = dbc.upsert_job(conn, source="s", external_id="e-q", title="t")
    out_root = Path(cfg.paths.output_dir)
    bundle = _make_bundle(out_root, "acme-2026-09-16")
    dbc.claim_for_build(conn, jid, "run-A")
    dbc.finish_build(conn, "run-A", artifact_dir=str(bundle), ok=True)
    qz.quarantine(bundle, stems=["Alex_Rivera_Resume_-_Acme_VP_Ops"], reasons=["x"])

    result = packets.packet_for_job(conn, jid, load_config=lambda: cfg)
    assert result["quarantined"] is True


def test_packet_for_job_not_quarantined_by_default(conn, cfg):
    jid = dbc.upsert_job(conn, source="s", external_id="e-nq", title="t")
    out_root = Path(cfg.paths.output_dir)
    bundle = _make_bundle(out_root, "acme-2026-09-17")
    dbc.claim_for_build(conn, jid, "run-A")
    dbc.finish_build(conn, "run-A", artifact_dir=str(bundle), ok=True)

    result = packets.packet_for_job(conn, jid, load_config=lambda: cfg)
    assert result["quarantined"] is False


def test_packet_file_path_refuses_a_quarantined_name(cfg):
    from blastjob.core import quarantine as qz

    out_root = Path(cfg.paths.output_dir)
    bundle = _make_bundle(out_root, "acme-2026-09-18")
    stem = "Alex_Rivera_Resume_-_Acme_VP_Ops"
    qz.quarantine(bundle, stems=[stem], reasons=["x"])

    assert packets.packet_file_path(cfg, str(bundle), f"{stem}.md") is None
    # The cover letter, never quarantined, is still servable.
    assert (
        packets.packet_file_path(cfg, str(bundle), "Alex_Rivera_Cover_Letter_-_Acme_VP_Ops.md")
        is not None
    )


def test_role_documents_never_picks_a_quarantined_file(tmp_path):
    from blastjob.core import quarantine as qz

    bundle = _make_bundle(tmp_path, "b5")
    stem = "Alex_Rivera_Resume_-_Acme_VP_Ops"
    qz.quarantine(bundle, stems=[stem], reasons=["x"])

    files = packets.packet_files(bundle)
    docs = packets.role_documents(files, "Acme VP Ops")
    assert "resume" not in docs
    # Cover letter (never quarantined) is still picked -- but it's .md in this
    # fixture, not .docx, so role_documents (which only matches .docx) finds
    # nothing for it either. Add a cover .docx to prove the positive case.
    (bundle / "Alex_Rivera_Cover_Letter_-_Acme_VP_Ops.docx").write_bytes(b"docx")
    files2 = packets.packet_files(bundle)
    docs2 = packets.role_documents(files2, "Acme VP Ops")
    assert docs2.get("cover") == "Alex_Rivera_Cover_Letter_-_Acme_VP_Ops.docx"
    assert "resume" not in docs2


# --- is_local_request ------------------------------------------------------


def test_is_local_request_true_for_loopback_v4():
    req = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    assert packets.is_local_request(req) is True


def test_is_local_request_true_for_loopback_v6():
    req = SimpleNamespace(client=SimpleNamespace(host="::1"))
    assert packets.is_local_request(req) is True


def test_is_local_request_false_for_lan_peer():
    """The cockpit is LAN-bound for iPad access; only the peer address (not the
    Origin header, which RFC1918 also satisfies) can tell an iPad from the Mac."""
    req = SimpleNamespace(client=SimpleNamespace(host="192.168.1.42"))
    assert packets.is_local_request(req) is False


def test_is_local_request_false_when_client_is_none():
    req = SimpleNamespace(client=None)
    assert packets.is_local_request(req) is False


# --- safe_output_path -------------------------------------------------


def test_safe_output_path_accepts_path_under_root(cfg):
    out_root = Path(cfg.paths.output_dir)
    candidate = out_root / "acme-2026-09-12"
    result = packets.safe_output_path(cfg, str(candidate))
    assert result == candidate.resolve()


def test_safe_output_path_rejects_traversal(cfg):
    traversal = str(Path(cfg.paths.output_dir) / ".." / ".." / "etc" / "passwd")
    assert packets.safe_output_path(cfg, traversal) is None


def test_safe_output_path_rejects_unrelated_absolute_path(cfg):
    assert packets.safe_output_path(cfg, "/etc/passwd") is None


def test_safe_output_path_rejects_empty(cfg):
    assert packets.safe_output_path(cfg, "") is None

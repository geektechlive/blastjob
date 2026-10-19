"""Filing packets by stage inside the output folder (issue #20).

Filing is one directory rename per top-level entry; the database is never
rewritten (stored paths stay in root form; readers use `paths.locate`).
Invented companies and people only.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from blastjob.core import filing_lock
from blastjob.core import packet_filing as pf
from blastjob.core.paths import ARCHIVE, CLOSED_BEFORE_APPLYING, SUBMITTED
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig


@pytest.fixture
def out(tmp_path: Path) -> Path:
    root = tmp_path / "out"
    root.mkdir()
    return root


@pytest.fixture
def conn():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _job(
    conn,
    company: str = "Acme Corp",
    title: str = "VP IT",
    *,
    review: str = "kept",
    state: str = "",
    applied_at: str = "",
    posting_check: str = "",
    ext: str | None = None,
) -> int:
    jid = dbc.upsert_job(
        conn, source="s1", external_id=ext or f"{company}-{title}", company=company, title=title
    )
    conn.execute(
        "UPDATE jobs SET review_state=?, application_state=?, applied_at=?, posting_check=? "
        "WHERE id=?",
        (review, state, applied_at, posting_check, jid),
    )
    conn.commit()
    return jid


def _entry(out: Path, name: str, *files: str) -> Path:
    folder = out / name
    folder.mkdir(parents=True)
    for f in files or ("vp-it.metadata.json",):
        (folder / f).parent.mkdir(parents=True, exist_ok=True)
        (folder / f).write_text("x")
    return folder


def _build(conn, job_id: int, folder: Path, *, status: str = "done", lease_min: int = -1) -> None:
    now = datetime.now()
    conn.execute(
        "INSERT INTO build_runs (job_id, run_id, status, lease_expires_at, artifact_dir, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (
            job_id,
            f"run-{job_id}-{folder.name}-{status}-{now.timestamp()}",
            status,
            (now + timedelta(minutes=lease_min)).isoformat(),
            str(folder),
            now.isoformat(),
            now.isoformat(),
        ),
    )
    conn.commit()


def _send(conn, job_id: int, folder: Path) -> None:
    conn.execute(
        "INSERT INTO packet_sends (job_id, artifact_dir, resume_path, resume_sha256, sent_at) "
        "VALUES (?,?,?,?,?)",
        (job_id, str(folder), str(folder / "r.docx"), "abc", datetime.now().isoformat()),
    )
    conn.commit()


def _tree_hash(out: Path) -> str:
    listing = sorted(str(p.relative_to(out)) for p in out.rglob("*"))
    return hashlib.sha256("\n".join(listing).encode()).hexdigest()


def _db_dump(conn) -> str:
    return "\n".join(conn.iterdump())


# --- the bucket of one job ------------------------------------------------------


@pytest.mark.parametrize(
    ("review", "state", "applied_at", "check", "bucket", "contradiction"),
    [
        ("kept", "", "", "", pf.ROOT, False),
        ("kept", "drafted", "", "", pf.ROOT, False),
        ("kept", "applied", "2026-09-01", "", SUBMITTED, False),
        ("kept", "screen", "2026-09-01", "", SUBMITTED, False),
        ("kept", "rejected", "2026-09-01", "closed", SUBMITTED, False),
        ("kept", "posting_closed", "", "", CLOSED_BEFORE_APPLYING, False),
        # A closed posting alone (shadow mode, likely_closed) never files: only
        # the state the closure lifecycle moved the job to does.
        ("kept", "", "", "closed", pf.ROOT, False),
        ("kept", "drafted", "", "closed", pf.ROOT, False),
        ("kept", "filled", "", "", pf.ROOT, True),
        ("kept", "screen", "", "", pf.ROOT, True),
        ("kept", "applied", "", "", pf.ROOT, True),
        # A contradiction outranks the posting check, so it stays visible.
        ("kept", "rejected", "", "closed", pf.ROOT, True),
        ("archived", "", "", "", ARCHIVE, False),
        ("archived", "", "2026-09-01", "", ARCHIVE, False),
        ("surfaced", "", "", "", pf.ROOT, False),
        ("queued", "", "", "", pf.ROOT, False),
        ("new", "", "", "", pf.ROOT, False),
    ],
)
def test_bucket_for_job(review, state, applied_at, check, bucket, contradiction) -> None:
    job = {
        "review_state": review,
        "application_state": state,
        "applied_at": applied_at,
        "posting_check": check,
    }
    got = pf.bucket_for_job(job, has_send=False, has_applied_event=False)
    assert (got.bucket, got.contradiction) == (bucket, contradiction)


@pytest.mark.parametrize("evidence", ["send", "event"])
def test_send_evidence_beats_review_state(evidence) -> None:
    job = {"review_state": "archived", "application_state": "", "applied_at": ""}
    got = pf.bucket_for_job(job, has_send=evidence == "send", has_applied_event=evidence == "event")
    assert got.bucket == SUBMITTED and not got.contradiction


# --- planning and moving entries -------------------------------------------------


def test_applied_job_entry_moves_whole_and_the_db_is_untouched(conn, out) -> None:
    jid = _job(conn, state="applied", applied_at="2026-09-01")
    folder = _entry(out, "acme-corp-2026-09-01", "vp-it.metadata.json", "quarantine/x.docx")
    _build(conn, jid, folder)
    before = _db_dump(conn)

    res = pf.file_packets(conn, out, apply=True)

    assert [(m.entry, m.src, m.dst) for m in res.moves] == [
        ("acme-corp-2026-09-01", pf.ROOT, SUBMITTED)
    ]
    assert not folder.exists()
    assert (out / SUBMITTED / "acme-corp-2026-09-01" / "quarantine" / "x.docx").is_file()
    assert _db_dump(conn) == before


def test_entry_bucket_is_the_most_visible_of_its_jobs(conn, out) -> None:
    keep = _job(conn, title="VP IT")
    gone = _job(conn, title="CIO", review="archived")
    folder = _entry(out, "acme-corp-2026-09-01")
    _build(conn, keep, folder)
    _build(conn, gone, folder)
    assert pf.file_packets(conn, out, apply=True).moves == ()
    assert folder.is_dir()


def test_unlinked_entries_go_to_archive_but_system_folders_never_move(conn, out) -> None:
    _entry(out, "comparisons", "a.md")
    _entry(out, "unfiled-followups", "vp.followup-99.md")
    (out / "loose.md").write_text("x")
    (out / ARCHIVE).mkdir()

    res = pf.file_packets(conn, out, apply=True)

    assert [m.entry for m in res.moves] == ["comparisons"]
    assert (out / ARCHIVE / "comparisons").is_dir()
    assert (out / "unfiled-followups").is_dir()
    assert (out / "loose.md").is_file()


def test_name_rules_link_an_entry_with_no_build(conn, out) -> None:
    # A workup + prep + follow-up folder for a live application, no build_runs
    # row: the entry name and the follow-up's job id link it, so it is filed
    # with the application instead of being archived.
    jid = _job(conn, "Fabrikam Credit Union", state="screen", applied_at="2026-09-05")
    _entry(
        out,
        "fabrikam-credit-union-2026-09-05",
        "company_workup.md",
        "vp-it.interview_prep.md",
        f"vp-it.followup-{jid}.md",
    )
    res = pf.file_packets(conn, out, apply=True)
    assert [(m.entry, m.dst) for m in res.moves] == [
        ("fabrikam-credit-union-2026-09-05", SUBMITTED)
    ]


def test_followup_name_links_its_job_by_id(conn, out) -> None:
    jid = _job(conn, "Northwind Traders", state="applied", applied_at="2026-09-05")
    _entry(out, "notes-folder", f"vp-it.followup-{jid}.md")
    res = pf.file_packets(conn, out, apply=False)
    assert [(m.entry, m.dst) for m in res.moves] == [("notes-folder", SUBMITTED)]


def test_company_name_rule_matches_the_whole_slug_and_a_rebuild_suffix(conn, out) -> None:
    _job(conn, "Net", state="applied", applied_at="2026-09-05")
    _entry(out, "net-at-work-2026-09-05")
    _entry(out, "net-2026-09-05-2")
    res = pf.file_packets(conn, out, apply=False)
    assert sorted((m.entry, m.dst) for m in res.moves) == [
        ("net-2026-09-05-2", SUBMITTED),
        ("net-at-work-2026-09-05", ARCHIVE),
    ]


def test_name_rule_ignores_jobs_that_are_not_kept(conn, out) -> None:
    _job(conn, "Contoso", review="surfaced")
    _entry(out, "contoso-2026-09-05")
    res = pf.file_packets(conn, out, apply=False)
    assert [(m.entry, m.dst) for m in res.moves] == [("contoso-2026-09-05", ARCHIVE)]


def test_contradiction_stays_at_root_and_is_reported(conn, out) -> None:
    jid = _job(conn, state="filled")
    folder = _entry(out, "acme-corp-2026-09-01")
    _build(conn, jid, folder)
    res = pf.file_packets(conn, out, apply=True)
    assert res.moves == ()
    assert [c.job_id for c in res.contradictions] == [jid]
    assert "claims an application" in pf.format_report(res)


def test_target_exists_is_a_conflict_and_nothing_moves(conn, out) -> None:
    jid = _job(conn, state="applied", applied_at="2026-09-01")
    folder = _entry(out, "acme-corp-2026-09-01")
    _entry(out, f"{SUBMITTED}/acme-corp-2026-09-01", "other.md")
    _build(conn, jid, folder)
    res = pf.file_packets(conn, out, apply=True)
    assert res.moves == ()
    assert [c.entry for c in res.conflicts] == ["acme-corp-2026-09-01"]
    assert folder.is_dir()


def test_symlinked_entries_and_entries_holding_symlinks_are_skipped(conn, out, tmp_path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (out / "linked-2026-09-01").symlink_to(elsewhere)
    holder = _entry(out, "holder-2026-09-01")
    (holder / "escape").symlink_to(elsewhere)
    res = pf.file_packets(conn, out, apply=True)
    assert res.moves == ()
    assert sorted(s.entry for s in res.skipped) == ["holder-2026-09-01", "linked-2026-09-01"]
    assert holder.is_dir() and (out / "linked-2026-09-01").is_symlink()


def test_a_live_build_skips_only_the_entries_it_can_write(conn, out, caplog) -> None:
    from datetime import date

    today = date.today().isoformat()
    jid = _job(conn, state="applied", applied_at="2026-09-01")
    folder = _entry(out, "acme-corp-2026-09-01")
    _build(conn, jid, folder)
    # Contoso is being rebuilt: its older packet and today's folders are skipped.
    other = _job(conn, "Contoso", state="applied", applied_at="2026-09-01")
    old = _entry(out, "contoso-2026-09-01")
    _build(conn, other, old)
    _build(conn, other, out / "never-recorded", status="running", lease_min=10)
    todays = _entry(out, f"northwind-{today}")

    with caplog.at_level("WARNING"):
        res = pf.file_packets(conn, out, apply=True)

    assert [m.entry for m in res.moves] == ["acme-corp-2026-09-01"]
    assert sorted(s.entry for s in res.skipped) == ["contoso-2026-09-01", f"northwind-{today}"]
    assert old.is_dir() and todays.is_dir()
    assert "contoso-2026-09-01" in caplog.text


def test_a_busy_lock_is_logged(conn, out, caplog) -> None:
    jid = _job(conn, state="applied", applied_at="2026-09-01")
    _build(conn, jid, _entry(out, "acme-corp-2026-09-01"))
    with filing_lock.shared(out), caplog.at_level("WARNING"):
        res = pf.file_packets(conn, out, apply=True)
    assert res.busy
    assert "filing lock" in caplog.text


def test_name_rule_never_pulls_a_filed_application_back_to_root(conn, out) -> None:
    # Role A at Acme was applied to and filed; role B at Acme is shortlisted
    # later. B's company name must not drag A's packet back to the top level.
    a = _job(conn, title="VP IT", state="applied", applied_at="2026-09-13")
    b = _job(conn, title="CIO")
    filed = _entry(out, f"{SUBMITTED}/acme-corp-2026-09-13")
    _build(conn, a, out / "acme-corp-2026-09-13")
    _build(conn, b, _entry(out, "acme-corp-2026-10-01"))
    res = pf.file_packets(conn, out, apply=True)
    assert res.moves == ()
    assert filed.is_dir()


def test_name_rule_never_files_an_archived_role_as_submitted(conn, out) -> None:
    a = _job(conn, title="VP IT", state="applied", applied_at="2026-09-13")
    y = _job(conn, title="CIO", review="archived")
    _build(conn, a, _entry(out, "acme-corp-2026-09-13"))
    _build(conn, y, _entry(out, "acme-corp-2026-09-01"))
    res = pf.file_packets(conn, out, apply=False)
    assert sorted((m.entry, m.dst) for m in res.moves) == [
        ("acme-corp-2026-09-01", ARCHIVE),
        ("acme-corp-2026-09-13", SUBMITTED),
    ]


def test_a_job_scoped_pass_never_walks_unrelated_entries(conn, out, monkeypatch) -> None:
    a = _job(conn, state="applied", applied_at="2026-09-01")
    _build(conn, a, _entry(out, "acme-corp-2026-09-01"))
    _entry(out, "contoso-2026-09-01", "notes.md")
    _entry(out, "comparisons", "a.md")
    walked: list[str] = []
    real = pf._files_in

    def spy(folder):
        walked.append(folder.name)
        return real(folder)

    monkeypatch.setattr(pf, "_files_in", spy)
    pf.file_packets(conn, out, apply=False, job_id=a)
    assert "contoso-2026-09-01" not in walked and "comparisons" not in walked


def test_filing_modules_never_import_the_web_layer() -> None:
    import ast
    import inspect

    from blastjob.core import packet_renames, paths

    for mod in (pf, packet_renames, filing_lock, paths):
        tree = ast.parse(inspect.getsource(mod))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("blastjob.web"), mod.__name__


def test_an_expired_running_lease_does_not_block(conn, out) -> None:
    jid = _job(conn, state="applied", applied_at="2026-09-01")
    _build(conn, jid, _entry(out, "acme-corp-2026-09-01"))
    _build(conn, jid, out / "acme-corp-2026-09-01", status="running", lease_min=-10)
    assert [m.entry for m in pf.file_packets(conn, out, apply=True).moves] == [
        "acme-corp-2026-09-01"
    ]


def test_a_writer_holding_the_shared_lock_makes_filing_report_busy(conn, out) -> None:
    jid = _job(conn, state="applied", applied_at="2026-09-01")
    folder = _entry(out, "acme-corp-2026-09-01")
    _build(conn, jid, folder)
    with filing_lock.shared(out):
        res = pf.file_packets(conn, out, apply=True)
    assert res.busy and folder.is_dir()


def test_filing_holds_the_lock_exclusively(conn, out) -> None:
    lock = out / filing_lock.LOCK_NAME
    seen: list[bool] = []

    def probe() -> None:
        fd = os.open(lock, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            seen.append(False)
        except BlockingIOError:
            seen.append(True)
        finally:
            os.close(fd)

    with filing_lock.exclusive(out) as ok:
        assert ok
        probe()
    assert seen == [True]


def test_report_mode_writes_nothing(conn, out) -> None:
    jid = _job(conn, state="applied", applied_at="2026-09-01")
    _build(conn, jid, _entry(out, "acme-corp-2026-09-01"))
    _entry(out, "comparisons", "a.md")
    tree, db = _tree_hash(out), _db_dump(conn)

    res = pf.file_packets(conn, out, apply=False)

    assert len(res.moves) == 2 and not res.applied
    assert _tree_hash(out) == tree and _db_dump(conn) == db
    assert not (out / filing_lock.LOCK_NAME).exists()
    assert "Would move" in pf.format_report(res)


def test_reopen_into_the_shortlist_moves_the_entry_back_to_root(conn, out) -> None:
    jid = _job(conn, state="posting_closed")
    folder = _entry(out, "acme-corp-2026-09-01")
    _build(conn, jid, folder)
    pf.file_packets(conn, out, apply=True)
    assert (out / CLOSED_BEFORE_APPLYING / folder.name).is_dir()

    conn.execute("UPDATE jobs SET application_state='' WHERE id=?", (jid,))
    conn.commit()
    res = pf.file_packets(conn, out, apply=True)

    assert [(m.src, m.dst) for m in res.moves] == [(CLOSED_BEFORE_APPLYING, pf.ROOT)]
    assert folder.is_dir()


def test_scoped_pass_touches_only_the_jobs_entries(conn, out) -> None:
    a = _job(conn, state="applied", applied_at="2026-09-01")
    b = _job(conn, "Contoso", state="applied", applied_at="2026-09-01")
    _build(conn, a, _entry(out, "acme-corp-2026-09-01"))
    _build(conn, b, _entry(out, "contoso-2026-09-01"))
    _entry(out, "comparisons", "a.md")
    res = pf.file_packets(conn, out, apply=True, job_id=a)
    assert [m.entry for m in res.moves] == ["acme-corp-2026-09-01"]
    assert (out / "contoso-2026-09-01").is_dir() and (out / "comparisons").is_dir()


def test_legacy_nested_date_entry_moves_whole(conn, out) -> None:
    jid = _job(conn, "Sample Equipment", title="CIO", review="archived")
    legacy = _entry(out, "2026-04-21", "sample-equipment/cio/metadata.json")
    _build(conn, jid, legacy / "sample-equipment" / "cio")
    res = pf.file_packets(conn, out, apply=True)
    assert [(m.entry, m.dst) for m in res.moves] == [("2026-04-21", ARCHIVE)]
    assert (out / ARCHIVE / "2026-04-21" / "sample-equipment" / "cio" / "metadata.json").is_file()


def test_submitted_without_an_as_sent_record_is_flagged(conn, out) -> None:
    a = _job(conn, state="applied", applied_at="2026-09-01")
    b = _job(conn, "Contoso", state="applied", applied_at="2026-09-01")
    fa = _entry(out, "acme-corp-2026-09-01")
    fb = _entry(out, "contoso-2026-09-01")
    _build(conn, a, fa)
    _build(conn, b, fb)
    _send(conn, b, fb)
    res = pf.file_packets(conn, out, apply=False)
    assert res.unsent_submitted == ("acme-corp-2026-09-01",)
    assert "no as-sent record" in pf.format_report(res)


def test_an_already_filed_entry_is_left_alone(conn, out) -> None:
    jid = _job(conn, state="applied", applied_at="2026-09-01")
    folder = _entry(out, f"{SUBMITTED}/acme-corp-2026-09-01")
    _build(conn, jid, out / "acme-corp-2026-09-01")
    assert pf.file_packets(conn, out, apply=True).moves == ()
    assert folder.is_dir()


def test_missing_output_dir_is_a_no_op(conn, tmp_path) -> None:
    gone = tmp_path / "nope"
    res = pf.file_packets(conn, gone, apply=True)
    assert res.moves == () and not gone.exists()


def test_hook_never_raises(conn, out, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(pf, "file_packets", boom)
    assert pf.file_after_change(conn, out, 1) is None

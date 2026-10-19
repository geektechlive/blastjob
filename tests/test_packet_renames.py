"""The `--file-packets` rename step: a kept job's newest packet documents get
the current capped `document_token` (issue #19/#20). Invented data only."""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from blastjob.core import artifacts
from blastjob.core import packet_renames as pr
from blastjob.core.paths import document_token, name_token, person_slug, slugify
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web import packets

COMPANY = "Fabrikam Industrial Holdings"
ROLE = "Director of Manufacturing Systems and Information Technology Strategy"
PERSON = person_slug("Alex J. Rivera")
LEGACY = f"{name_token(COMPANY)}_{name_token(ROLE)}"
CURRENT = document_token(COMPANY, ROLE)


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


def _packet(conn, out: Path, token: str = LEGACY, *, review: str = "kept") -> tuple[int, Path]:
    jid = dbc.upsert_job(conn, source="s1", external_id="e1", company=COMPANY, title=ROLE)
    conn.execute("UPDATE jobs SET review_state=? WHERE id=?", (review, jid))
    folder = out / "fabrikam-industrial-holdings-2026-09-10"
    folder.mkdir()
    slug = slugify(ROLE)
    (folder / f"{slug}.metadata.json").write_text(json.dumps({"company": COMPANY, "role": ROLE}))
    for name in (
        f"{PERSON}_Resume_-_{token}.docx",
        f"{PERSON}_Resume_-_{token}.md",
        f"{PERSON}_Resume_-_{token}_ATS.ats.txt",
        f"{PERSON}_Cover_Letter_-_{token}.docx",
    ):
        (folder / name).write_text(name)
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO build_runs (job_id, run_id, status, lease_expires_at, artifact_dir, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (jid, "r1", "done", now, str(folder), now, now),
    )
    conn.commit()
    files = artifacts.role_files(folder, company=COMPANY, role=ROLE, job_id=jid)
    artifacts.register_paths(conn, files, job_id=jid, output_root=out)
    return jid, folder


def _tree_hash(out: Path) -> str:
    listing = sorted(str(p.relative_to(out)) for p in out.rglob("*"))
    return hashlib.sha256("\n".join(listing).encode()).hexdigest()


def test_the_example_role_really_has_a_shorter_token() -> None:
    assert CURRENT != LEGACY and len(CURRENT) <= 40


def test_legacy_documents_are_renamed_and_their_index_rows_follow(conn, out) -> None:
    jid, folder = _packet(conn, out)

    plan = pr.plan_renames(conn, out)
    res = pr.apply_renames(conn, out, plan)

    assert len(res.renamed) == 4 and not res.failed
    names = sorted(p.name for p in folder.iterdir())
    assert f"{PERSON}_Resume_-_{CURRENT}.docx" in names
    assert f"{PERSON}_Resume_-_{CURRENT}_ATS.ats.txt" in names
    assert not any(LEGACY in n for n in names)
    stored = {Path(r["path"]).name for r in artifacts.artifacts_for(conn, jid)}
    assert f"{PERSON}_Cover_Letter_-_{CURRENT}.docx" in stored
    assert not any(LEGACY in n for n in stored)
    # Every matcher still finds the renamed files.
    found = artifacts.role_files(folder, company=COMPANY, role=ROLE, job_id=jid)
    assert sum(1 for p in found if CURRENT in p.name) == 4
    docs = packets.role_documents(packets.packet_files(folder), ROLE, company=COMPANY)
    assert docs == {
        "resume": f"{PERSON}_Resume_-_{CURRENT}.docx",
        "cover": f"{PERSON}_Cover_Letter_-_{CURRENT}.docx",
    }


def test_a_pre_mfg_hashed_token_is_still_matched_and_renamed(conn, out) -> None:
    from blastjob.core import paths

    prior = paths._document_token(COMPANY, ROLE, paths._PRIOR_TIGHT_TABLES[0])
    assert prior != CURRENT
    jid, folder = _packet(conn, out, token=prior)
    assert len(artifacts.role_files(folder, company=COMPANY, role=ROLE, job_id=jid)) == 5
    assert len(pr.plan_renames(conn, out).renames) == 4


def test_a_sent_family_is_never_renamed(conn, out) -> None:
    jid, folder = _packet(conn, out)
    sent = folder / f"{PERSON}_Resume_-_{LEGACY}.docx"
    conn.execute(
        "INSERT INTO packet_sends (job_id, artifact_dir, resume_path, resume_sha256, sent_at) "
        "VALUES (?,?,?,?,?)",
        (jid, str(folder), str(sent), "abc", datetime.now().isoformat()),
    )
    conn.commit()

    plan = pr.plan_renames(conn, out)

    # The whole resume family stays (renaming its .md alone would split it);
    # the cover letter was not sent and is renamed.
    assert [r.dst.name for r in plan.renames] == [f"{PERSON}_Cover_Letter_-_{CURRENT}.docx"]
    assert any("as-sent" in s.why for s in plan.skipped)


def test_quarantined_documents_are_never_renamed(conn, out) -> None:
    jid, folder = _packet(conn, out)
    (folder / "quarantine").mkdir()
    held = folder / f"{PERSON}_Cover_Letter_-_{LEGACY}.docx"
    held.rename(folder / "quarantine" / held.name)
    plan = pr.plan_renames(conn, out)
    assert all("Cover_Letter" not in r.src.name for r in plan.renames)
    assert any("quarantine" in s.why for s in plan.skipped)


def test_an_existing_target_skips_the_family(conn, out) -> None:
    jid, folder = _packet(conn, out)
    (folder / f"{PERSON}_Resume_-_{CURRENT}.docx").write_text("already")
    plan = pr.plan_renames(conn, out)
    assert all("Resume" not in r.src.name for r in plan.renames)
    assert any("exists" in s.why for s in plan.skipped)


def test_only_kept_jobs_are_renamed(conn, out) -> None:
    _packet(conn, out, review="archived")
    assert pr.plan_renames(conn, out).renames == ()


@pytest.mark.parametrize(
    ("state", "applied_at"),
    [("applied", "2026-09-10"), ("interview", "2026-09-10"), ("posting_closed", "")],
)
def test_only_packets_still_to_send_are_renamed(conn, out, state, applied_at) -> None:
    # An application already went out under the old name (often with no as-sent
    # record, which predates migration 39), and a closed posting will never be sent.
    jid, _ = _packet(conn, out)
    conn.execute(
        "UPDATE jobs SET application_state=?, applied_at=? WHERE id=?", (state, applied_at, jid)
    )
    conn.commit()
    assert pr.plan_renames(conn, out).renames == ()


def test_renames_find_a_filed_packet(conn, out) -> None:
    jid, folder = _packet(conn, out)
    (out / "Submitted").mkdir()
    folder.rename(out / "Submitted" / folder.name)
    plan = pr.plan_renames(conn, out)
    assert len(plan.renames) == 4
    assert all(r.src.parent == out / "Submitted" / folder.name for r in plan.renames)


def test_report_mode_writes_nothing(conn, out) -> None:
    _packet(conn, out)
    tree, db = _tree_hash(out), "\n".join(conn.iterdump())
    text = pr.format_renames(pr.plan_renames(conn, out), applied=False)
    assert "Would rename 4" in text
    assert _tree_hash(out) == tree and "\n".join(conn.iterdump()) == db


def test_a_db_failure_renames_the_files_back(conn, out, monkeypatch) -> None:
    jid, folder = _packet(conn, out)
    before = sorted(p.name for p in folder.iterdir())
    plan = pr.plan_renames(conn, out)
    # A stale index row already holding a target path trips artifacts.path UNIQUE.
    target = plan.renames[0].dst
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO artifacts (job_id, kind, path, created_at, updated_at) VALUES (?,?,?,?,?)",
        (jid, "resume", str(target), now, now),
    )
    conn.commit()

    res = pr.apply_renames(conn, out, plan)

    assert res.renamed == () and res.failed
    assert sorted(p.name for p in folder.iterdir()) == before


def test_a_released_family_is_never_renamed(conn, out) -> None:
    # RELEASED.txt is the audit trail of a quarantine override and names the
    # files it released; a rename would leave it naming a file that is gone.
    jid, folder = _packet(conn, out)
    name = f"{PERSON}_Resume_-_{LEGACY}.docx"
    (folder / "RELEASED.txt").write_text(f"2026-09-10T10:00:00 -- released: ok [{name}]\n")
    plan = pr.plan_renames(conn, out)
    assert all("Resume" not in r.src.name for r in plan.renames)
    assert any("RELEASED.txt" in s.why for s in plan.skipped)


def test_a_rename_with_no_index_row_registers_the_new_path(conn, out) -> None:
    jid, folder = _packet(conn, out)
    conn.execute("DELETE FROM artifacts")
    conn.commit()
    res = pr.apply_renames(conn, out, pr.plan_renames(conn, out))
    assert len(res.renamed) == 4
    stored = {Path(r["path"]).name for r in artifacts.artifacts_for(conn, jid)}
    assert stored == {r.dst.name for r in res.renamed}
    assert all(Path(r["path"]).is_file() for r in artifacts.artifacts_for(conn, jid))


def test_a_rerun_repairs_an_index_row_left_on_the_old_name(conn, out) -> None:
    # A crash after the rename but before the commit: the file has the current
    # token, the row still names the old file.
    jid, folder = _packet(conn, out)
    old = folder / f"{PERSON}_Resume_-_{LEGACY}.docx"
    new = folder / f"{PERSON}_Resume_-_{CURRENT}.docx"
    old.rename(new)

    plan = pr.plan_renames(conn, out)
    assert [(r.src.name, r.dst.name) for r in plan.repairs] == [(old.name, new.name)]
    pr.apply_renames(conn, out, plan)

    rows = [Path(r["path"]) for r in artifacts.artifacts_for(conn, jid)]
    assert all(p.is_file() for p in rows)
    assert new.resolve() in rows

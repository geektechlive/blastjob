"""The artifact index (migration 43, `core/artifacts.py`) and the hooks that
register what a build, a refine, a prep pack and a follow-up draft wrote.

Filenames in these tests are produced by the REAL naming helpers and
exporters, so a rename in `core/build.py`'s conventions fails here instead of
silently classifying everything as "other".
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from pathlib import Path

import pytest

from blastjob.core import artifacts
from blastjob.core.paths import application_dir, name_token, person_slug, resolve_role_slug
from blastjob.db import client as dbc
from blastjob.exporters import ats, markdown
from blastjob.models.config import BlastJobConfig

COMPANY = "Acme Corp"
ROLE = "VP, IT Platforms"
OTHER_ROLE = "VP IT Platforms and Data"


@pytest.fixture
def cfg() -> BlastJobConfig:
    c = BlastJobConfig()
    c.paths.data_dir = tempfile.mkdtemp()
    c.paths.output_dir = tempfile.mkdtemp()
    return c


@pytest.fixture
def conn(cfg):
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _job(conn, ext: str = "e1", title: str = ROLE) -> int:
    jid = dbc.upsert_job(conn, source="s1", external_id=ext, company=COMPANY, title=title)
    conn.execute("UPDATE jobs SET review_state='kept' WHERE id=?", (jid,))
    conn.commit()
    return jid


def _stems(role: str) -> tuple[str, str]:
    person = person_slug("Alex J. Rivera")
    co, ro = name_token(COMPANY), name_token(role)
    return f"{person}_Resume_-_{co}_{ro}", f"{person}_Cover_Letter_-_{co}_{ro}"


def _fake_build(out_dir: Path, role: str, company: str = COMPANY) -> list[Path]:
    """Write a packet the way `core/build.py` names it, via the real helpers."""
    resume_stem, cover_stem = _stems(role)
    slug = resolve_role_slug(out_dir, role)
    written = [
        markdown.write("# Resume", out_dir, stem=resume_stem),
        ats.write("# Resume", out_dir, stem=f"{resume_stem}_ATS"),
        markdown.write("Dear Hiring Manager,", out_dir, stem=cover_stem),
    ]
    (out_dir / f"{resume_stem}.docx").write_bytes(b"docx")
    (out_dir / f"{cover_stem}.docx").write_bytes(b"docx")
    written += [out_dir / f"{resume_stem}.docx", out_dir / f"{cover_stem}.docx"]
    for suffix in (
        "job_description.md",
        "company_research.md",
        "fit_score.json",
        "fit_score.md",
        "cover_letter_score.json",
        "cover_letter_score.md",
        "coverage_gaps.md",
        "packet_report.json",
    ):
        p = out_dir / f"{slug}.{suffix}"
        p.write_text("x", encoding="utf-8")
        written.append(p)
    meta = out_dir / f"{slug}.metadata.json"
    meta.write_text(json.dumps({"company": company, "role": role}), encoding="utf-8")
    written.append(meta)
    return written


# --- kind_for ------------------------------------------------------------------


def test_kind_for_classifies_real_build_filenames(tmp_path):
    resume_stem, cover_stem = _stems(ROLE)
    slug = resolve_role_slug(tmp_path, ROLE)
    ats_name = ats.write("# R", tmp_path, stem=f"{resume_stem}_ATS").name
    expected = {
        f"{resume_stem}.md": "resume",
        f"{resume_stem}.docx": "resume",
        f"{resume_stem}.pdf": "resume",
        f"{resume_stem}.v2.md": "resume",
        ats_name: "ats_text",
        f"{cover_stem}.md": "cover_letter",
        f"{cover_stem}.docx": "cover_letter",
        f"{slug}.job_description.md": "job_description",
        f"{slug}.company_research.md": "company_research",
        f"{slug}.fit_score.json": "fit_score",
        f"{slug}.fit_score.md": "fit_score",
        f"{slug}.cover_letter_score.json": "cover_letter_score",
        f"{slug}.cover_letter_score.md": "cover_letter_score",
        f"{slug}.coverage_gaps.md": "coverage_gaps",
        f"{slug}.metadata.json": "metadata",
        f"{slug}.packet_report.json": "packet_report",
        f"{slug}.interview_prep.md": "interview_prep",
        "vp-it-platforms.followup-12.md": "followup",
        # legacy per-role folders
        "resume.md": "resume",
        "resume.docx": "resume",
        "cover_letter.md": "cover_letter",
        "job_description.md": "job_description",
        "metadata.json": "metadata",
        "fit_score.json": "fit_score",
        # hand-filed documents
        "Wild interview transcript.md": "transcript",
        "wild-debrief.md": "debrief",
        "Wild cheat sheet.pdf": "cheatsheet",
        "notes.txt": "other",
    }
    got = {name: artifacts.kind_for(name) for name in expected}
    assert got == expected


# --- register ------------------------------------------------------------------


def test_register_is_idempotent_and_refreshes_sha_and_bytes(conn, tmp_path):
    jid = _job(conn)
    f = tmp_path / "notes.md"
    f.write_text("one", encoding="utf-8")
    first = artifacts.register(conn, f, "other", job_id=jid)
    f.write_text("two, longer", encoding="utf-8")
    again = artifacts.register(conn, f, "other", job_id=jid)
    assert first == again
    rows = artifacts.artifacts_for(conn, jid)
    assert len(rows) == 1
    assert rows[0]["sha256"] == hashlib.sha256(b"two, longer").hexdigest()
    assert rows[0]["bytes"] == len(b"two, longer")
    assert rows[0]["path"] == str(f.resolve())


def test_register_never_reassigns_job_or_build_run(conn, tmp_path):
    a, b = _job(conn, "a"), _job(conn, "b")
    f = tmp_path / "x.md"
    f.write_text("x", encoding="utf-8")
    artifacts.register(conn, f, "other", job_id=a)
    artifacts.register(conn, f, "other", job_id=b, build_run_id=None)
    assert [r["path"] for r in artifacts.artifacts_for(conn, a)] == [str(f.resolve())]
    assert artifacts.artifacts_for(conn, b) == []


def test_register_fills_a_null_job_id(conn, tmp_path):
    jid = _job(conn)
    f = tmp_path / "x.md"
    f.write_text("x", encoding="utf-8")
    artifacts.register(conn, f, "other")
    artifacts.register(conn, f, "other", job_id=jid)
    assert len(artifacts.artifacts_for(conn, jid)) == 1


def test_register_rejects_a_missing_file_and_a_bad_kind(conn, tmp_path):
    with pytest.raises(FileNotFoundError):
        artifacts.register(conn, tmp_path / "gone.md", "other")
    f = tmp_path / "x.md"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        artifacts.register(conn, f, "spreadsheet")


# --- which files belong to a role ------------------------------------------------


def test_role_files_in_a_shared_folder_skip_the_other_roles_files(tmp_path):
    mine = _fake_build(tmp_path, ROLE)
    theirs = _fake_build(tmp_path, OTHER_ROLE)
    (tmp_path / "RELEASED.txt").write_text("x", encoding="utf-8")
    got = artifacts.role_files(tmp_path, company=COMPANY, role=ROLE, job_id=3)
    assert sorted(got) == sorted(p.resolve() for p in mine)
    assert not set(got) & {p.resolve() for p in theirs}


def test_role_files_includes_quarantined_documents_and_own_followups(tmp_path):
    mine = _fake_build(tmp_path, ROLE)
    qdir = tmp_path / "quarantine"
    qdir.mkdir()
    resume = next(p for p in mine if p.name.endswith("_Resume_-_Acme_Corp_VP_IT_Platforms.docx"))
    held = qdir / resume.name
    resume.rename(held)
    (qdir / "WHY.txt").write_text("why", encoding="utf-8")
    followup = tmp_path / "vp-it-platforms.followup-3.md"
    followup.write_text("x", encoding="utf-8")
    (tmp_path / "vp-it-platforms.followup-99.md").write_text("x", encoding="utf-8")
    got = set(artifacts.role_files(tmp_path, company=COMPANY, role=ROLE, job_id=3))
    assert held.resolve() in got
    assert followup.resolve() in got
    assert (tmp_path / "vp-it-platforms.followup-99.md").resolve() not in got
    assert (qdir / "WHY.txt").resolve() not in got


def test_role_files_in_a_legacy_per_role_folder_takes_everything(tmp_path):
    for name in ("resume.md", "cover_letter.md", "metadata.json", "job_description.md"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    got = artifacts.role_files(tmp_path, company=COMPANY, role=ROLE, job_id=1)
    assert len(got) == 4


# --- hooks -----------------------------------------------------------------------


def test_build_hook_registers_this_roles_files_with_the_run(conn, cfg, monkeypatch):
    from blastjob.web import build as web_build
    from blastjob.web.runtime import Run

    jid = _job(conn)
    conn.execute("UPDATE jobs SET raw_jd=? WHERE id=?", ("x" * 2000, jid))
    conn.commit()
    out = application_dir(Path(cfg.paths.output_dir), COMPANY)
    theirs = _fake_build(out, OTHER_ROLE)

    async def fake_build(**_k):
        return out, None

    monkeypatch.setattr("blastjob.core.build.run_build", lambda **k: fake_build(**k))

    async def go():
        _fake_build(out, ROLE)
        await web_build.make_tailor_work(jid, lambda: cfg)(Run(id="run-x", name="tailor"))

    asyncio.run(go())
    rows = artifacts.artifacts_for(conn, jid)
    run_pk = conn.execute("SELECT id FROM build_runs WHERE run_id='run-x'").fetchone()["id"]
    assert len(rows) == 14
    assert {r["build_run_id"] for r in rows} == {run_pk}
    assert not {r["path"] for r in rows} & {str(p.resolve()) for p in theirs}


def test_build_hook_writes_and_indexes_an_apply_link_page(conn, cfg, monkeypatch):
    from blastjob.web import build as web_build
    from blastjob.web.runtime import Run

    jid = _job(conn)
    conn.execute(
        "UPDATE jobs SET raw_jd=?, url=? WHERE id=?",
        ("x" * 2000, "https://boards.example.com/acme/jobs/1", jid),
    )
    conn.commit()
    out = application_dir(Path(cfg.paths.output_dir), COMPANY)

    async def fake_build(**_k):
        return out, None

    monkeypatch.setattr("blastjob.core.build.run_build", lambda **k: fake_build(**k))

    async def go():
        _fake_build(out, ROLE)
        await web_build.make_tailor_work(jid, lambda: cfg)(Run(id="run-a", name="tailor"))

    asyncio.run(go())
    kinds = {r["kind"]: r["path"] for r in artifacts.artifacts_for(conn, jid)}
    assert "apply_link" in kinds
    page = Path(kinds["apply_link"]).read_text(encoding="utf-8")
    assert 'href="https://boards.example.com/acme/jobs/1"' in page


def test_apply_link_page_has_its_own_kind():
    assert artifacts.kind_for("vp-it.apply_link.html") == "apply_link"


def test_build_hook_failure_never_fails_the_build(conn, cfg, monkeypatch):
    from blastjob.web import build as web_build
    from blastjob.web.runtime import Run

    jid = _job(conn)
    conn.execute("UPDATE jobs SET raw_jd=? WHERE id=?", ("x" * 2000, jid))
    conn.commit()
    out = application_dir(Path(cfg.paths.output_dir), COMPANY)

    async def fake_build(**_k):
        return out, None

    def boom(*_a, **_k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr("blastjob.core.build.run_build", lambda **k: fake_build(**k))
    monkeypatch.setattr(artifacts, "role_files", boom)
    result = asyncio.run(web_build.make_tailor_work(jid, lambda: cfg)(Run(id="r2", name="t")))
    assert result["ok"] is True
    status = conn.execute("SELECT status FROM build_runs WHERE run_id='r2'").fetchone()
    assert status["status"] == "done"


def _done_run(conn, jid: int, folder: Path, run_id: str = "run-1") -> int:
    dbc.claim_for_build(conn, jid, run_id)
    dbc.finish_build(conn, run_id, artifact_dir=str(folder), ok=True)
    return conn.execute("SELECT id FROM build_runs WHERE run_id=?", (run_id,)).fetchone()["id"]


class _Stream:
    def __init__(self, chunks):
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    @property
    def text_stream(self):
        async def it():
            for c in self._chunks:
                yield c

        return it()


def test_prep_writes_into_the_recorded_packet_folder_and_registers(conn, cfg, monkeypatch):
    from blastjob.core import prep

    jid = _job(conn)
    folder = Path(cfg.paths.output_dir) / "acme-corp-2026-09-01"
    folder.mkdir()
    _fake_build(folder, ROLE)
    run_pk = _done_run(conn, jid, folder)

    async def stream(*_a, **_k):
        return _Stream(["## Role at a glance\n"])

    monkeypatch.setattr(prep, "make_stream", stream)
    path = asyncio.run(prep.generate_prep(cfg, conn, jid))
    assert path.parent.resolve() == folder.resolve()
    assert path.name == "vp-it-platforms.interview_prep.md"
    rows = [r for r in artifacts.artifacts_for(conn, jid) if r["kind"] == "interview_prep"]
    assert len(rows) == 1 and rows[0]["build_run_id"] == run_pk


def test_prep_falls_back_to_todays_folder_when_the_packet_folder_is_gone(conn, cfg, monkeypatch):
    from blastjob.core import prep

    jid = _job(conn)
    _done_run(conn, jid, Path(cfg.paths.output_dir) / "deleted-folder")

    async def stream(*_a, **_k):
        return _Stream(["body\n"])

    monkeypatch.setattr(prep, "make_stream", stream)
    path = asyncio.run(prep.generate_prep(cfg, conn, jid))
    assert path.parent == application_dir(Path(cfg.paths.output_dir), COMPANY)
    assert [r["kind"] for r in artifacts.artifacts_for(conn, jid)] == ["interview_prep"]


def test_followup_hook_registers_the_draft(conn, cfg, monkeypatch):
    from blastjob.core import followup as core_followup
    from blastjob.web import followup as web_followup
    from blastjob.web.runtime import Run

    jid = _job(conn)
    folder = Path(cfg.paths.output_dir) / "acme-corp-2026-09-01"
    folder.mkdir()
    run_pk = _done_run(conn, jid, folder)

    async def fake_generate(_cfg, *, job, artifact_dir, **_k):
        path = core_followup.write_followup(artifact_dir, job["title"], "hi", job_id=job["id"])
        return core_followup.FollowupDraft(path=path, body="hi", subject="s", to="", mailto="")

    monkeypatch.setattr(core_followup, "generate_followup", fake_generate)
    work = web_followup.make_followup_work(jid, lambda: cfg, artifact_dir=str(folder))
    asyncio.run(work(Run(id="f1", name="followup")))
    rows = artifacts.artifacts_for(conn, jid)
    assert [(r["kind"], r["build_run_id"]) for r in rows] == [("followup", run_pk)]


def test_refine_hook_registers_new_and_renamed_files_for_this_job_only(conn, cfg, monkeypatch):
    from blastjob.web import refine as web_refine
    from blastjob.web.runtime import Run

    jid = _job(conn)
    folder = Path(cfg.paths.output_dir) / "acme-corp-2026-09-01"
    folder.mkdir()
    _fake_build(folder, ROLE)
    _fake_build(folder, OTHER_ROLE)
    run_pk = _done_run(conn, jid, folder)
    resume_stem, _ = _stems(ROLE)

    async def fake_refine(run_dir, *_a, **_k):
        current = run_dir / f"{resume_stem}.md"
        current.rename(run_dir / f"{resume_stem}.v1.md")
        current.write_text("# Revised", encoding="utf-8")
        return current, None

    monkeypatch.setattr("blastjob.core.refine.run_refine", fake_refine)
    work = web_refine.make_refine_work(str(folder), "tighten", lambda: cfg, job_id=jid)
    asyncio.run(work(Run(id="r1", name="refine")))
    rows = artifacts.artifacts_for(conn, jid)
    names = {Path(r["path"]).name for r in rows}
    assert f"{resume_stem}.v1.md" in names and f"{resume_stem}.md" in names
    assert all("Data" not in n for n in names)
    assert {r["build_run_id"] for r in rows} == {run_pk}

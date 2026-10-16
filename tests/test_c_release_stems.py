"""Release frees only the job's own role.

The packet folder is `{company}-{date}`, shared by every role tailored for that
company that day. Releasing one job's held packet used to move EVERY held file
back while recording the override for one role only, so the other role's files
came back public with a report still saying QUARANTINED.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from blastjob.core import quarantine
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web.app import create_app
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN

TOKEN = {CSRF_HEADER: LAUNCH_TOKEN}


def _cfg() -> BlastJobConfig:
    c = BlastJobConfig()
    c.paths.data_dir = tempfile.mkdtemp()
    c.paths.output_dir = tempfile.mkdtemp()
    return c


def _held_role(conn, out: Path, ext_id: str, title: str, token: str, slug: str) -> tuple[int, str]:
    jid = dbc.upsert_job(conn, source="s1", external_id=ext_id, company="Acme", title=title)
    stem = f"Alex_Rivera_Resume_-_Acme_{token}"
    (out / f"{stem}.md").write_text("# Resume", encoding="utf-8")
    (out / f"{stem}.docx").write_text("docx", encoding="utf-8")
    (out / f"{slug}.packet_report.json").write_text('{"state": "quarantined"}', encoding="utf-8")
    conn.execute(
        "INSERT INTO build_runs (job_id, run_id, status, artifact_dir, lease_expires_at, "
        "created_at, updated_at) VALUES (?,?,?,?,datetime('now'),datetime('now'),datetime('now'))",
        (jid, f"tailor-{ext_id}", "done", str(out)),
    )
    conn.commit()
    quarantine.quarantine(out, stems=[stem], reasons=["banned characters"])
    return jid, stem


def test_release_frees_only_the_jobs_own_role_in_a_shared_folder():
    cfg = _cfg()
    conn = dbc.init_db(cfg)
    out = Path(cfg.paths.output_dir) / "acme-2026-10-16"
    out.mkdir(parents=True)
    a, a_stem = _held_role(conn, out, "e1", "VP IT", "VP_IT", "vp-it")
    _b, b_stem = _held_role(conn, out, "e2", "CIO", "CIO", "cio")
    conn.close()

    r = TestClient(create_app(load_config=lambda: cfg, save_config=lambda _c: None)).post(
        f"/jobs/{a}/packet/release", data={"reason": "checked by hand"}, headers=TOKEN
    )

    assert r.status_code == 200
    assert (out / f"{a_stem}.md").is_file(), "the released role's resume is back"
    assert not (out / f"{b_stem}.md").exists(), "the other role must stay held"
    assert (out / "quarantine" / f"{b_stem}.md").is_file()

"""Invented fixtures for the outreach-ask tests (invented names, example.com only)."""

from __future__ import annotations

import sqlite3
import tempfile

from blastjob.core.company import company_key
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig


def make_cfg() -> BlastJobConfig:
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    return cfg


def add_job(
    conn: sqlite3.Connection, ext: str, company: str = "Acme", score: int | None = 80
) -> int:
    jid = dbc.upsert_job(
        conn, source="t", external_id=ext, company=company, title="VP Ops", raw_jd="Lead Ops."
    )
    conn.execute("UPDATE jobs SET review_state='kept', alignment_score=? WHERE id=?", (score, jid))
    conn.commit()
    return jid


def add_network(
    conn: sqlite3.Connection,
    name: str,
    company: str = "Acme",
    position: str = "COO",
    url: str = "",
) -> int:
    cur = conn.execute(
        "INSERT INTO network_contacts (full_name, company, company_key, position, url, seniority)"
        " VALUES (?, ?, ?, ?, ?, 'exec')",
        (name, company, company_key(company), position, url),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


def add_contact(conn: sqlite3.Connection, job_id: int, name: str = "Pat Roe") -> int:
    cur = conn.execute(
        "INSERT INTO contacts (job_id, name, title, role, url, why, created_at) "
        "VALUES (?, ?, 'CFO', 'executive', ?, 'Senior leader.', '2026-10-01T00:00:00')",
        (job_id, name, f"https://example.com/in/{name.replace(' ', '').lower()}"),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)

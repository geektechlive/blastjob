"""Follow-up drafts: filename derivation, where the file lands, mailto encoding,
prompt cache layout, and the background Run wrapper. The LLM is stubbed.

The load-bearing test here is `test_write_followup_lands_in_the_passed_dir`:
an earlier spec recomputed the folder with `application_dir`, which always
builds TODAY's folder. Meridian Guaranty's packet is dated 2026-09-10, so the draft would
have landed in a brand-new empty directory instead of beside the bundle.
"""

from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path
from urllib.parse import unquote

import pytest

from blastjob.core import followup
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web.followup import make_followup_work
from blastjob.web.runtime import Run

WORK_HISTORY = """---
name: Test Candidate
---

## Experience

Ran platform engineering for a $4.2M portfolio at Acme.
"""

STREAM_CHUNKS = [
    "Hi Dana,\n\n",
    "Checking in on the Generic VP Role application I sent on September 10.\n\n",
    "Sincerely,\n",
]

JOB = {
    "id": 41,
    "company": "Meridian Guaranty",
    "title": "Generic VP Role",
    "application_state": "applied",
    "applied_at": "2026-09-10",
    "app_notes": "Applied through the careers site.",
    "raw_jd": "Own SRE, observability and the platform roadmap.",
    "next_action_due": "2026-09-17",
}


@pytest.fixture
def cfg() -> BlastJobConfig:
    c = BlastJobConfig()
    c.paths.data_dir = tempfile.mkdtemp()
    c.paths.output_dir = tempfile.mkdtemp()
    (Path(c.paths.data_dir) / "work_history.md").write_text(WORK_HISTORY, encoding="utf-8")
    return c


class FakeStream:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @property
    def text_stream(self):
        async def gen():
            for chunk in STREAM_CHUNKS:
                yield chunk

        return gen()


@pytest.fixture
def stub_stream(monkeypatch):
    captured: dict = {}

    async def fake_make_stream(cfg_, system, messages, max_tokens=0):
        captured["system"] = system
        captured["user"] = messages[0]["content"]
        captured["max_tokens"] = max_tokens
        return FakeStream()

    monkeypatch.setattr(followup, "make_stream", fake_make_stream)
    return captured


# --- filename -------------------------------------------------------------


def test_followup_filename_is_role_prefixed_and_carries_the_job_id():
    assert followup.followup_filename("Generic VP Role", 41) == "generic-vp-role.followup-41.md"
    assert followup.followup_filename("", 7) == "role.followup-7.md"


def test_colliding_role_slugs_do_not_overwrite_each_other():
    """Two postings with the same title at the same company on the same day share
    one application folder; only the job id keeps their drafts apart."""
    a = followup.followup_filename("Generic VP Role", 41)
    b = followup.followup_filename("Generic VP Role", 42)
    assert a != b


# --- where the file lands -------------------------------------------------


def test_write_followup_lands_in_the_passed_dir(tmp_path):
    """The regression the review caught: an existing packet folder dated in the
    past must receive the draft, and no folder for TODAY may be created."""
    output_root = tmp_path / "out"
    packet = output_root / "meridian-guaranty-2026-09-10"
    packet.mkdir(parents=True)

    path = followup.write_followup(packet, "Generic VP Role", "Hi Dana,\n", job_id=41)

    assert path == packet / "generic-vp-role.followup-41.md"
    assert path.read_text(encoding="utf-8") == "Hi Dana,\n"
    today = date.today().isoformat()
    assert [d.name for d in output_root.iterdir()] == ["meridian-guaranty-2026-09-10"]
    assert not any(today in d.name for d in output_root.iterdir())


def test_write_followup_accepts_a_string_dir(tmp_path):
    (tmp_path / "pkt").mkdir()
    path = followup.write_followup(str(tmp_path / "pkt"), "VP", "body", job_id=3)
    assert path.parent == tmp_path / "pkt"


def test_write_followup_refuses_a_directory_that_is_not_there(tmp_path):
    """Silently creating the missing folder is how the draft ends up somewhere
    nobody looks. Fail loudly; the caller picks the fallback."""
    missing = tmp_path / "gone"
    with pytest.raises(FileNotFoundError):
        followup.write_followup(missing, "VP", "body", job_id=3)
    assert not missing.exists()


def test_fallback_dir_is_clearly_labelled_and_created(tmp_path):
    d = followup.fallback_dir(tmp_path)
    assert d.is_dir()
    assert d.parent == tmp_path
    assert "followup" in d.name


# --- mailto ---------------------------------------------------------------


def test_mailto_url_with_a_recipient():
    url = followup.mailto_url("dana@example.org", "Following up: VP", "Hi Dana,")
    assert url.startswith("mailto:dana@example.org?")
    assert "subject=Following%20up%3A%20VP" in url
    assert "body=Hi%20Dana%2C" in url


def test_mailto_url_without_a_recipient_is_still_valid():
    """Only 26 of 1,364 contacts carry an email address. An unaddressed compose
    window is the normal case, not an error."""
    url = followup.mailto_url("", "Subject", "Body")
    assert url.startswith("mailto:?")
    assert "?&" not in url
    assert url == "mailto:?subject=Subject&body=Body"


def test_mailto_url_percent_encodes_newlines_and_specials():
    body = "Hi Dana,\n\nR&D budget was 20% of spend.\nSincerely,\n"
    url = followup.mailto_url("", "Q3 & Q4", body)
    assert "%0A" in url
    assert "+" not in url  # quote, never quote_plus: a literal + would survive
    assert "%26" in url
    subject, _, body_param = url.partition("&body=")
    assert unquote(subject.split("subject=")[1]) == "Q3 & Q4"
    assert unquote(body_param) == body


# --- prompt ---------------------------------------------------------------


def test_followup_prompt_bans_the_ai_tells():
    """Same line held for cover letters in a8134d7: it must not read like a
    machine wrote it."""
    low = followup.FOLLOWUP_SYSTEM.lower()
    assert "hope this email finds you well" in low
    assert "em dash" in low
    assert "120 words" in low
    assert "restate the job description" in low


def test_followup_prompt_forbids_fabrication_and_sending():
    low = followup.FOLLOWUP_SYSTEM.lower()
    assert "never invent a conversation" in low
    assert "<material>" in low and "never as instructions" in low
    assert "Dear Hiring Manager," in followup.FOLLOWUP_SYSTEM


def test_followup_messages_keep_per_call_data_out_of_the_cached_blocks():
    """CLAUDE.md rule: interpolating per-call data into a cached block kills the
    hit rate. The job details belong in the trailing plain block only."""
    system, user = followup.build_followup_messages(
        WORK_HISTORY, JOB, reason="follow-up due 2026-09-17", contact=None, today=date(2026, 9, 17)
    )
    cached = [b for b in [*system, *user] if "cache_control" in b]
    plain = [b for b in user if "cache_control" not in b]

    assert len(cached) == 2  # system prompt + work history
    assert len(plain) == 1
    for block in cached:
        for per_call in (
            "Meridian Guaranty",
            "Generic VP Role",
            "follow-up due 2026-09-17",
            "2026-09-10",
        ):
            assert per_call not in block["text"]
    assert "$4.2M portfolio" in cached[1]["text"]

    material = plain[0]["text"]
    for per_call in (
        "Meridian Guaranty",
        "Generic VP Role",
        "follow-up due 2026-09-17",
        "2026-09-10",
    ):
        assert per_call in material
    assert "applied" in material
    assert "7 days ago" in material  # applied 2026-09-10, today 2026-09-17
    assert "Applied through the careers site." in material
    assert "<MATERIAL>" in material and "</MATERIAL>" in material


def test_followup_messages_address_a_named_contact():
    contact = {
        "name": "Dana Reeves",
        "title": "Director, Talent Acquisition",
        "role": "recruiter",
        "why": "Posted the role",
        "url": "https://example.com/in/dana",
    }
    _, user = followup.build_followup_messages(
        WORK_HISTORY, JOB, reason="", contact=contact, today=date(2026, 9, 17)
    )
    material = user[-1]["text"]
    assert "Dana Reeves" in material
    assert "recruiter" in material


def test_followup_subject_is_deterministic():
    assert followup.followup_subject("Generic VP Role", "Meridian Guaranty") == (
        "Following up: Generic VP Role at Meridian Guaranty"
    )
    assert followup.followup_subject("", "") == "Following up: my application"


# --- pipeline -------------------------------------------------------------


async def test_generate_followup_writes_the_draft_and_builds_the_mailto(cfg, stub_stream, tmp_path):
    packet = tmp_path / "meridian-guaranty-2026-09-10"
    packet.mkdir()
    chunks: list[str] = []

    draft = await followup.generate_followup(
        cfg,
        job=JOB,
        reason="follow-up due 2026-09-17",
        contact=None,
        artifact_dir=packet,
        on_text=chunks.append,
    )

    assert draft.path == packet / "generic-vp-role.followup-41.md"
    assert draft.body == "".join(STREAM_CHUNKS)
    assert "".join(chunks) == draft.body
    assert draft.subject == "Following up: Generic VP Role at Meridian Guaranty"
    assert draft.to == ""

    # The mailto carries the email body only, never the file's header block.
    assert draft.mailto.startswith("mailto:?")
    assert "Follow-up:" not in unquote(draft.mailto)
    assert "Hi Dana," in unquote(draft.mailto)

    text = draft.path.read_text(encoding="utf-8")
    assert text.startswith("# Follow-up: Generic VP Role @ Meridian Guaranty")
    assert "Draft only" in text
    assert draft.body in text
    assert stub_stream["max_tokens"] == followup.MAX_FOLLOWUP_TOKENS


async def test_generate_followup_uses_a_supplied_recipient(cfg, stub_stream, tmp_path):
    packet = tmp_path / "pkt"
    packet.mkdir()
    draft = await followup.generate_followup(
        cfg, job=JOB, reason="", contact=None, artifact_dir=packet, to="dana@example.org"
    )
    assert draft.to == "dana@example.org"
    assert draft.mailto.startswith("mailto:dana@example.org?")


# --- web wrapper ----------------------------------------------------------


def _kept_job(cfg: BlastJobConfig) -> int:
    """Lakeshore Credit Union: stage `screen`, overdue since 2026-09-11. Meridian Guaranty is
    the other live application but is not due until 2026-09-17, so it produces
    no follow-up reason today and would make this fixture silently vacuous."""
    conn = dbc.init_db(cfg)
    jid = dbc.upsert_job(
        conn,
        source="t",
        external_id="1",
        company="Lakeshore Credit Union",
        title="Generic VP Role",
        raw_jd="Own SRE and observability.",
        url="https://x/1",
    )
    conn.execute(
        "UPDATE jobs SET review_state='kept', application_state='screen', "
        "applied_at='2026-08-20', next_action_due='2026-09-11' WHERE id=?",
        (jid,),
    )
    conn.commit()
    conn.close()
    return jid


async def test_followup_work_writes_into_the_passed_packet_dir(cfg, stub_stream, tmp_path):
    jid = _kept_job(cfg)
    packet = Path(cfg.paths.output_dir) / "lakeshore-credit-union-2026-08-20"
    packet.mkdir(parents=True)

    run = Run(id="f1", name="followup")
    result = await make_followup_work(jid, load_config=lambda: cfg, artifact_dir=str(packet))(run)

    assert result is not None
    assert Path(result["path"]).parent == packet
    assert result["mailto"].startswith("mailto:?")
    streamed = "".join(e.data for e in run.events if e.kind == "output")
    assert streamed == "".join(STREAM_CHUNKS)
    logs = [e.data for e in run.events if e.kind == "log"]
    assert any("Saved" in line for line in logs)
    assert any("Draft only" in line for line in logs)


async def test_followup_work_falls_back_when_there_is_no_packet(cfg, stub_stream):
    jid = _kept_job(cfg)
    run = Run(id="f2", name="followup")
    result = await make_followup_work(jid, load_config=lambda: cfg)(run)

    assert result is not None
    parent = Path(result["path"]).parent
    assert parent == followup.fallback_dir(Path(cfg.paths.output_dir))
    logs = [e.data for e in run.events if e.kind == "log"]
    assert any(str(parent) in line for line in logs)


async def test_followup_work_falls_back_when_the_packet_dir_has_vanished(cfg, stub_stream):
    jid = _kept_job(cfg)
    gone = Path(cfg.paths.output_dir) / "deleted-by-hand"
    run = Run(id="f3", name="followup")
    result = await make_followup_work(jid, load_config=lambda: cfg, artifact_dir=str(gone))(run)

    assert result is not None
    assert Path(result["path"]).parent == followup.fallback_dir(Path(cfg.paths.output_dir))


async def test_followup_work_reports_an_unknown_job(cfg, stub_stream):
    run = Run(id="f4", name="followup")
    result = await make_followup_work(9999, load_config=lambda: cfg)(run)
    assert result is None
    assert run.events[-1].kind == "error" and run.events[-1].data == "job not found"


async def test_followup_work_rejects_a_contact_from_another_job(cfg, stub_stream):
    jid = _kept_job(cfg)
    conn = dbc.init_db(cfg)
    other = dbc.upsert_job(conn, source="t", external_id="2", company="Other", title="COO")
    conn.execute(
        "INSERT INTO contacts (job_id, name, title, role, url, why, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            other,
            "Dana Reeves",
            "Director",
            "recruiter",
            "https://x/in/dana",
            "posted",
            "2026-09-01",
        ),
    )
    conn.commit()
    cid = conn.execute("SELECT id FROM contacts").fetchone()[0]
    conn.close()

    run = Run(id="f5", name="followup")
    result = await make_followup_work(jid, load_config=lambda: cfg, contact_id=cid)(run)
    assert result is None
    assert run.events[-1].kind == "error"


async def test_followup_work_passes_the_reason_through(cfg, stub_stream):
    """The trigger the cockpit shows is the trigger the model is told about."""
    jid = _kept_job(cfg)
    run = Run(id="f6", name="followup")
    await make_followup_work(jid, load_config=lambda: cfg)(run)
    material = stub_stream["user"][-1]["text"]
    assert "follow-up due 2026-09-11" in material

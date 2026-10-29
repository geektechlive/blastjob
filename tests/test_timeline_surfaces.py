"""Where the timeline findings (#105) surface: the verify path, the packet page,
the build log, the work-history page and the prompts.

Fixtures are invented and every date is in the past and not `Present`, so no
result depends on the day the suite runs.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import jinja2
import pytest
from fastapi.testclient import TestClient

from blastjob import config as cfg_mod
from blastjob.core import build as core_build
from blastjob.core import packet_gate, packet_verify
from blastjob.core.packet_report import Finding, PacketReport, Severity
from blastjob.core.resume_timeline_checks import TIMELINE_CODES
from blastjob.llm import prompts
from blastjob.models.config import BlastJobConfig
from blastjob.web import packet_view as pv
from blastjob.web import workhistory as wh
from blastjob.web.app import create_app
from blastjob.web.security import LAUNCH_TOKEN

HISTORY = """## Experience

### Director of Operations | Contoso Health | Oakbrook, OH
*Jan 2019 - Dec 2019*
- Built the platform within five years.
"""

CLEAN_HISTORY = HISTORY.replace("within five years", "within one year")

RESUME_MD = """## Experience

### Director of Operations
**Contoso Health** | Oakbrook, OH | Jan 2019 - Dec 2019
- Built the platform within five years.
"""

RESUME_ATS = """SUMMARY:
Operations leader.

EXPERIENCE:
Director of Operations
Contoso Health | Oakbrook, OH | Jan 2019 - Dec 2019
- Built the platform within five years.
"""


def _verify(tmp_path: Path, resume_md: str, history: str = HISTORY):
    return core_build._verify_packet(
        resume_md=resume_md,
        cover_letter_md="",
        work_history_md=history,
        job_description="VP of Operations",
        out_dir=tmp_path,
        resume_stem="Resume",
        fit_score=None,
    )


# ----- the verify path -------------------------------------------------------


def test_the_timeline_check_runs_as_its_own_named_check(tmp_path):
    report = _verify(tmp_path, RESUME_MD)
    assert "resume_timeline" in report.checks_run
    assert any(f.code == "duration_exceeds_role" for f in report.findings)


def test_an_ats_text_resume_is_checked_end_to_end(tmp_path):
    report = _verify(tmp_path, RESUME_ATS)
    assert any(f.code == "duration_exceeds_role" for f in report.findings)


def test_timeline_findings_never_change_sendable(tmp_path):
    flagged = _verify(tmp_path, RESUME_MD)
    timeline = [f for f in flagged.findings if f.code in TIMELINE_CODES]
    assert timeline
    assert all(f.severity is not Severity.FATAL for f in timeline)
    only_timeline = PacketReport(findings=tuple(timeline))
    assert packet_gate.gate(only_timeline).sendable is True


def test_a_crashing_timeline_check_does_not_hide_the_structure_checks(tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("regex blew up")

    monkeypatch.setattr(packet_verify.resume_timeline_checks, "check_resume", boom)
    report = _verify(tmp_path, RESUME_MD)
    assert "resume_structure" in report.checks_run
    assert "resume_timeline" in report.checks_failed


def test_timeline_notice_lists_only_timeline_findings():
    report = PacketReport(
        findings=(
            Finding(Severity.MAJOR, "duration_exceeds_role", "Ran about 12 months."),
            Finding(Severity.MAJOR, "unsupported_claim", "Something else."),
        )
    )
    notice = packet_verify.timeline_notice(report)
    assert "Check before sending" in notice
    assert "Ran about 12 months." in notice
    assert "Something else." not in notice
    assert packet_verify.timeline_notice(PacketReport()) == ""


# ----- the packet page -------------------------------------------------------

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "src" / "blastjob" / "web" / "templates"


def _page(tmp_path: Path, findings: list[dict]) -> str:
    report = {"state": "ready", "sendable": True, "findings": findings}
    (tmp_path / "role.packet_report.json").write_text(json.dumps(report), encoding="utf-8")
    view = pv.load_packet_report(str(tmp_path), role_slug="role")
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=jinja2.select_autoescape(),
    )
    return env.get_template("_packet_report.html").render(packet_report=view, job_id=1)


def test_the_banner_lists_timeline_findings_of_any_severity(tmp_path):
    html = _page(
        tmp_path,
        [
            {"severity": "major", "code": "duration_exceeds_role", "message": "Ran 12 months <b>"},
            {"severity": "minor", "code": "timeline_not_checked", "message": "Not checked: Lead"},
            {"severity": "major", "code": "unsupported_claim", "message": "Unrelated"},
        ],
    )
    banner = html.split('id="timeline-banner"')[1].split("</div>")[0]
    assert "Check before sending" in banner
    assert "Ran 12 months &lt;b&gt;" in banner  # autoescaped, never raw
    assert "Not checked: Lead" in banner
    assert "Unrelated" not in banner


def test_no_banner_without_timeline_findings(tmp_path):
    html = _page(tmp_path, [{"severity": "major", "code": "unsupported_claim", "message": "x"}])
    assert "timeline-banner" not in html


# ----- the work-history page -------------------------------------------------


@pytest.fixture
def client_cfg():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    app = create_app(load_config=lambda: cfg, save_config=lambda c: None)
    return TestClient(app), cfg


def _path(cfg: BlastJobConfig) -> Path:
    return cfg_mod.data_dir(cfg) / "work_history.md"


def _seed(cfg: BlastJobConfig, content: str) -> str:
    path = _path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return wh.content_hash(content)


def _post(client, content: str, version: str):
    return client.post(
        "/work-history",
        data={"_token": LAUNCH_TOKEN, "content": content, "base_version": version},
    )


def test_get_shows_date_consistency_notes(client_cfg):
    client, cfg = client_cfg
    _seed(cfg, HISTORY)
    html = client.get("/work-history").text
    assert "Date consistency notes" in html
    assert "within five years" in html


def test_a_clean_history_says_so(client_cfg):
    client, cfg = client_cfg
    _seed(cfg, CLEAN_HISTORY)
    assert "No date problems found" in client.get("/work-history").text


def test_a_successful_save_shows_notes_for_the_saved_content(client_cfg):
    client, cfg = client_cfg
    version = _seed(cfg, CLEAN_HISTORY)
    html = _post(client, HISTORY, version).text
    assert "Saved." in html
    assert "Date consistency notes" in html and "within five years" in html


def test_a_conflict_shows_notes_for_what_was_typed_not_what_is_on_disk(client_cfg):
    client, cfg = client_cfg
    _seed(cfg, CLEAN_HISTORY)
    html = _post(client, HISTORY, wh.content_hash("an older copy")).text
    assert "changed on disk" in html
    assert "within five years" in html


def test_an_error_render_shows_notes_for_what_was_typed(client_cfg, monkeypatch):
    client, cfg = client_cfg
    version = _seed(cfg, CLEAN_HISTORY)

    def boom(self, *a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    html = _post(client, HISTORY, version).text
    assert "Save failed" in html
    assert "within five years" in html


def test_an_empty_body_error_render_has_no_notes_section(client_cfg):
    client, cfg = client_cfg
    version = _seed(cfg, CLEAN_HISTORY)
    html = _post(client, "   ", version).text
    assert "cannot be empty" in html
    assert "Date consistency notes" not in html


def test_a_crashing_date_check_still_renders_the_editor(client_cfg, monkeypatch):
    client, cfg = client_cfg
    _seed(cfg, HISTORY)

    def boom(*a, **kw):
        raise RuntimeError("regex blew up")

    monkeypatch.setattr(wh.resume_timeline_checks, "check_history", boom)
    response = client.get("/work-history")
    assert response.status_code == 200
    assert "did not run" in response.text


# ----- prompts ---------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt", [prompts.RESUME_SYSTEM, prompts.RESUME_ATS_SYSTEM, prompts.REFINE_SYSTEM]
)
def test_every_resume_writing_prompt_carries_the_timeline_rules(prompt):
    assert "FIRST bullet under the later title" in prompt
    assert "Across both roles" in prompt
    assert "Never move a dated result" in prompt
    assert "Never stack two titles under one heading" in prompt
    assert "—" not in prompt.split("Never stack two titles")[0].split("Across both")[-1]


def test_refine_says_structure_beats_feedback():
    assert "PRECEDENCE" in prompts.REFINE_SYSTEM
    assert "keep separate headings" in prompts.REFINE_SYSTEM
    assert "say so" not in prompts.REFINE_SYSTEM  # output is the resume body only


def test_the_ats_prompt_defines_the_role_line_shape():
    assert "Employer | City, ST | Mon YYYY - Mon YYYY" in prompts.RESUME_ATS_SYSTEM

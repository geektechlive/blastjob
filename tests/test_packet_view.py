"""`web/packet_view.py`: reading `{role_slug}.packet_report.json` back for the
cockpit, and `_packet_report.html`: rendering it.

Mirrors the shape of `test_cover_letter_score.py`'s fixtures: the
FATAL-finding sample below is adapted from a real quarantined report, and the
clean-pass sample is modelled on a real COO-search report
(`~/Documents/blastjob/fairview-robotics-2026-09-16/chief-operations-officer.packet_report.json`)
with its findings trimmed to majors/minors only, so "ready" fixtures look like
a real ready packet.

Template rendering uses the same `jinja2.select_autoescape()` FastAPI's
`Jinja2Templates` wires up in `web/app.py` -- so the escaping assertions here
exercise the real autoescape path, not a stand-in.
"""

from __future__ import annotations

import json
from pathlib import Path

import jinja2
import pytest

from blastjob.web import packet_view as pv

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "src" / "blastjob" / "web" / "templates"


@pytest.fixture
def env() -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=jinja2.select_autoescape(),
    )


def _render(env: jinja2.Environment, packet_report, *, job_id=1, packet=None) -> str:
    return env.get_template("_packet_report.html").render(
        packet_report=packet_report, job_id=job_id, packet=packet
    )


# ---------------------------------------------------------------------------
# load_packet_report
# ---------------------------------------------------------------------------


def _write_report(tmp_path: Path, name: str, data: dict) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


READY_REPORT = {
    "state": "ready",
    "sendable": True,
    "reasons": [],
    "groundedness": 97,
    "jd_phrasing": 68,
    "cover_letter_groundedness": 78,
    "findings": [
        {
            "severity": "minor",
            "code": "page_count_estimate",
            "message": "Estimated length is ~2.1 pages (1152 words); industry guidance "
            "caps a senior resume at two.",
            "artifact": "resume",
            "evidence": "1152 words",
            "source": "industry guidance (Jan 2025); vendor resume-length study (n=482, 2018)",
        },
        {
            "severity": "major",
            "code": "unsupported_claim",
            "message": "At Contoso Health Analytics they built IT operations into a "
            "multi-team organization.",
            "artifact": "resume",
            "evidence": "Grew the support team from a single analyst and one engineer",
            "source": "work_history.md",
        },
    ],
}

QUARANTINED_REPORT = {
    "state": "quarantined",
    "sendable": False,
    "reasons": ["banned_characters: Contains illegal characters"],
    "groundedness": 41,
    "jd_phrasing": 55,
    "cover_letter_groundedness": None,
    "findings": [
        {
            "severity": "fatal",
            "code": "banned_characters",
            "message": "Contains illegal characters",
            "artifact": "resume",
            "evidence": "�",
            "source": "Workday rejection 2026-09-10: Contains illegal characters",
        },
        {
            "severity": "major",
            "code": "cover_letter_employer_claim_unsupported",
            "message": "Claim about the employer has no verbatim support in the company research.",
            "artifact": "cover_letter",
            "evidence": "",
            "source": "company research sidecar",
        },
        {
            "severity": "minor",
            "code": "tricolon_overuse",
            "message": "7 three-item parallel lists found.",
            "artifact": "resume",
            "evidence": "logistics, data platforms and customer operations",
            "source": "packet-quality-program plan (fixed document cap)",
        },
    ],
}


def test_missing_sidecar_returns_none(tmp_path):
    assert pv.load_packet_report(str(tmp_path)) is None


def test_no_packet_dir_returns_none():
    assert pv.load_packet_report("") is None
    assert pv.load_packet_report(None) is None  # type: ignore[arg-type]


def test_nonexistent_directory_returns_none(tmp_path):
    assert pv.load_packet_report(str(tmp_path / "does-not-exist")) is None


def test_malformed_json_returns_none(tmp_path):
    (tmp_path / "role.packet_report.json").write_text("{not valid json", encoding="utf-8")
    assert pv.load_packet_report(str(tmp_path)) is None


def test_json_that_is_not_an_object_returns_none(tmp_path):
    path = tmp_path / "role.packet_report.json"
    path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert pv.load_packet_report(str(tmp_path)) is None


def test_ready_report_loads_and_groups_findings(tmp_path):
    _write_report(tmp_path, "role.packet_report.json", READY_REPORT)
    view = pv.load_packet_report(str(tmp_path), role_slug="role")
    assert view is not None
    assert view.state == "ready"
    assert view.sendable is True
    assert view.fatal_count == 0
    assert view.major_count == 1
    assert view.minor_count == 1
    assert view.groundedness == 97
    assert view.jd_phrasing == 68
    assert view.cover_letter_groundedness == 78


def test_quarantined_report_surfaces_fatal_findings(tmp_path):
    _write_report(tmp_path, "role.packet_report.json", QUARANTINED_REPORT)
    view = pv.load_packet_report(str(tmp_path), role_slug="role")
    assert view is not None
    assert view.state == "quarantined"
    assert view.sendable is False
    assert view.fatal_count == 1
    assert view.fatal[0].code == "banned_characters"
    assert "Workday rejection 2026-09-10" in view.fatal[0].source
    assert view.major_count == 1
    assert view.minor_count == 1
    # cover_letter_groundedness is explicitly null (no letter scored), not 0.
    assert view.cover_letter_groundedness is None


def test_role_slug_disambiguates_shared_folder(tmp_path):
    _write_report(tmp_path, "other-role.packet_report.json", QUARANTINED_REPORT)
    _write_report(tmp_path, "role.packet_report.json", READY_REPORT)
    view = pv.load_packet_report(str(tmp_path), role_slug="role")
    assert view is not None
    assert view.state == "ready"


def test_falls_back_to_any_report_when_role_slug_unresolved(tmp_path):
    _write_report(tmp_path, "role.packet_report.json", READY_REPORT)
    view = pv.load_packet_report(str(tmp_path), role_slug="")
    assert view is not None
    assert view.state == "ready"


def test_unknown_severity_is_treated_as_advisory_not_dropped(tmp_path):
    data = {
        "state": "ready",
        "sendable": True,
        "reasons": [],
        "groundedness": None,
        "jd_phrasing": None,
        "cover_letter_groundedness": None,
        "findings": [
            {
                "severity": "weird",
                "code": "mystery",
                "message": "unrecognized severity",
                "artifact": "resume",
                "evidence": "",
                "source": "",
            }
        ],
    }
    _write_report(tmp_path, "role.packet_report.json", data)
    view = pv.load_packet_report(str(tmp_path), role_slug="role")
    assert view is not None
    assert view.fatal_count == 0
    assert view.major_count == 0
    assert view.minor_count == 1


def test_malformed_findings_entry_is_skipped_not_fatal(tmp_path):
    data = {
        "state": "ready",
        "sendable": True,
        "reasons": [],
        "groundedness": None,
        "jd_phrasing": None,
        "cover_letter_groundedness": None,
        "findings": ["not a dict", None, 42],
    }
    _write_report(tmp_path, "role.packet_report.json", data)
    view = pv.load_packet_report(str(tmp_path), role_slug="role")
    assert view is not None
    assert view.fatal_count == view.major_count == view.minor_count == 0


def test_bool_score_is_not_mistaken_for_an_int(tmp_path):
    data = {
        "state": "ready",
        "sendable": True,
        "reasons": [],
        "groundedness": True,
        "jd_phrasing": None,
        "cover_letter_groundedness": None,
        "findings": [],
    }
    _write_report(tmp_path, "role.packet_report.json", data)
    view = pv.load_packet_report(str(tmp_path), role_slug="role")
    assert view is not None
    assert view.groundedness is None


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


def test_template_renders_nothing_for_none(env):
    html = _render(env, None)
    assert html.strip() == ""


def test_template_renders_fatal_findings_first(env, tmp_path):
    _write_report(tmp_path, "role.packet_report.json", QUARANTINED_REPORT)
    view = pv.load_packet_report(str(tmp_path), role_slug="role")
    html = _render(env, view)
    assert "quarantined" in html
    assert "Contains illegal characters" in html
    assert "Workday rejection 2026-09-10" in html
    fatal_pos = html.index("Fatal")
    major_pos = html.index("Major")
    minor_pos = html.index("Minor")
    assert fatal_pos < major_pos < minor_pos


def test_template_renders_ready_report_without_alarm(env, tmp_path):
    _write_report(tmp_path, "role.packet_report.json", READY_REPORT)
    view = pv.load_packet_report(str(tmp_path), role_slug="role")
    html = _render(env, view)
    assert "ready" in html
    assert "not sendable" not in html
    assert "Fatal" not in html


def test_template_states_uncalibrated_for_both_scores(env, tmp_path):
    _write_report(tmp_path, "role.packet_report.json", READY_REPORT)
    view = pv.load_packet_report(str(tmp_path), role_slug="role")
    html = _render(env, view)
    assert html.count("Uncalibrated") >= 2


def test_template_never_calls_jd_phrasing_a_fit_score(env, tmp_path):
    _write_report(tmp_path, "role.packet_report.json", READY_REPORT)
    view = pv.load_packet_report(str(tmp_path), role_slug="role")
    html = _render(env, view)
    assert "Not a fit score" in html


def test_template_labels_minor_findings_advisory(env, tmp_path):
    _write_report(tmp_path, "role.packet_report.json", READY_REPORT)
    view = pv.load_packet_report(str(tmp_path), role_slug="role")
    html = _render(env, view)
    assert "Advisory only" in html
    assert "Textkernel" in html
    assert "<details>" in html  # minor findings collapsed


def test_template_escapes_malicious_finding_text(env, tmp_path):
    data = {
        "state": "ready",
        "sendable": True,
        "reasons": ["<script>alert(1)</script>"],
        "groundedness": None,
        "jd_phrasing": None,
        "cover_letter_groundedness": None,
        "findings": [
            {
                "severity": "fatal",
                "code": "xss_probe",
                "message": "<script>alert('xss')</script>",
                "artifact": "resume",
                "evidence": "<img src=x onerror=alert(1)>",
                "source": "<b>bold source</b>",
            }
        ],
    }
    _write_report(tmp_path, "role.packet_report.json", data)
    view = pv.load_packet_report(str(tmp_path), role_slug="role")
    html = _render(env, view)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<img src=x onerror=alert(1)>" not in html
    assert "&lt;b&gt;bold source&lt;/b&gt;" in html


# ---------------------------------------------------------------------------
# New fields: checks_run/checks_failed/checks_skipped, quarantined_artifacts,
# coverage_gaps, artifact_dir, per-finding line
# ---------------------------------------------------------------------------

REPORT_WITH_CRASHED_CHECKS = {
    "state": "quarantined",
    "sendable": False,
    "reasons": ["banned_characters: Contains illegal characters"],
    "groundedness": None,
    "jd_phrasing": None,
    "cover_letter_groundedness": None,
    "artifact_dir": "/home/alexrivera/Documents/blastjob/acme-2026-09-16",
    "checks_run": ["structure", "arithmetic"],
    "checks_failed": ["prose_check"],
    "checks_skipped": ["cover_letter_groundedness"],
    "quarantined_artifacts": ["resume"],
    "coverage_gaps": [
        {
            "requirement": "5+ years leading platform migrations",
            "history_quote": "Led the migration of 40+ applications off end-of-life "
            "infrastructure onto a modern platform over 3 years.",
            "priority": "must",
        }
    ],
    "findings": [
        {
            "severity": "fatal",
            "code": "banned_characters",
            "message": "Contains illegal characters",
            "artifact": "resume",
            "line": 12,
            "evidence": "�",
            "source": "Workday rejection 2026-09-10",
        }
    ],
}


def test_view_exposes_checks_failed(tmp_path):
    _write_report(tmp_path, "role.packet_report.json", REPORT_WITH_CRASHED_CHECKS)
    view = pv.load_packet_report(str(tmp_path), role_slug="role")
    assert view is not None
    assert view.checks_run == ("structure", "arithmetic")
    assert view.checks_failed == ("prose_check",)
    assert view.checks_skipped == ("cover_letter_groundedness",)
    assert view.artifact_dir == "/home/alexrivera/Documents/blastjob/acme-2026-09-16"


def test_missing_new_fields_default_gracefully(tmp_path):
    """A sidecar written before these fields existed (or before core/build.py
    lands its serializer) must not crash the view."""
    _write_report(tmp_path, "role.packet_report.json", READY_REPORT)
    view = pv.load_packet_report(str(tmp_path), role_slug="role")
    assert view is not None
    assert view.checks_run == ()
    assert view.checks_failed == ()
    assert view.checks_skipped == ()
    assert view.quarantined_artifacts == ()
    assert view.coverage_gaps == ()
    assert view.artifact_dir == ""


def test_finding_line_number_is_parsed(tmp_path):
    _write_report(tmp_path, "role.packet_report.json", REPORT_WITH_CRASHED_CHECKS)
    view = pv.load_packet_report(str(tmp_path), role_slug="role")
    assert view is not None
    assert view.fatal[0].line == 12


def test_coverage_gap_quote_is_parsed_verbatim(tmp_path):
    _write_report(tmp_path, "role.packet_report.json", REPORT_WITH_CRASHED_CHECKS)
    view = pv.load_packet_report(str(tmp_path), role_slug="role")
    assert view is not None
    assert len(view.coverage_gaps) == 1
    gap = view.coverage_gaps[0]
    assert gap.requirement == "5+ years leading platform migrations"
    assert "Led the migration of 40+ applications" in gap.history_quote
    assert gap.priority == "must"


# ---------------------------------------------------------------------------
# Template: verification-incomplete, quarantined artifacts, coverage gaps,
# line numbers, verification_failed state, release form
# ---------------------------------------------------------------------------


def test_template_shows_verification_incomplete_when_checks_crashed(env, tmp_path):
    _write_report(tmp_path, "role.packet_report.json", REPORT_WITH_CRASHED_CHECKS)
    view = pv.load_packet_report(str(tmp_path), role_slug="role")
    html = _render(env, view)
    assert "Verification incomplete" in html
    assert "prose_check" in html


def test_template_no_findings_line_states_check_count_when_clean(env, tmp_path):
    data = {
        "state": "ready",
        "sendable": True,
        "reasons": [],
        "groundedness": None,
        "jd_phrasing": None,
        "cover_letter_groundedness": None,
        "checks_run": ["structure", "arithmetic", "prose_check"],
        "checks_failed": [],
        "checks_skipped": [],
        "findings": [],
    }
    _write_report(tmp_path, "role.packet_report.json", data)
    view = pv.load_packet_report(str(tmp_path), role_slug="role")
    html = _render(env, view)
    assert "No findings recorded" in html
    assert "3 checks ran" in html


def test_template_names_which_artifact_is_quarantined(env, tmp_path):
    _write_report(tmp_path, "role.packet_report.json", REPORT_WITH_CRASHED_CHECKS)
    view = pv.load_packet_report(str(tmp_path), role_slug="role")
    html = _render(env, view)
    assert "resume" in html.lower()


def test_template_renders_finding_line_number(env, tmp_path):
    _write_report(tmp_path, "role.packet_report.json", REPORT_WITH_CRASHED_CHECKS)
    view = pv.load_packet_report(str(tmp_path), role_slug="role")
    html = _render(env, view)
    assert "12" in html


def test_template_renders_coverage_gaps_with_history_quote(env, tmp_path):
    _write_report(tmp_path, "role.packet_report.json", REPORT_WITH_CRASHED_CHECKS)
    view = pv.load_packet_report(str(tmp_path), role_slug="role")
    html = _render(env, view)
    assert "5+ years leading platform migrations" in html
    assert "Led the migration of 40+ applications" in html
    assert "own words" in html.lower()


def test_template_verification_failed_state_renders_amber_not_ready(env, tmp_path):
    data = {
        "state": "verification_failed",
        "sendable": False,
        "reasons": [],
        "groundedness": None,
        "jd_phrasing": None,
        "cover_letter_groundedness": None,
        "findings": [],
    }
    _write_report(tmp_path, "role.packet_report.json", data)
    view = pv.load_packet_report(str(tmp_path), role_slug="role")
    html = _render(env, view)
    assert "not verified" in html.lower()
    assert '<span class="badge pr-badge-ready">' not in html
    assert '<span class="badge pr-badge-other">not verified</span>' in html


def test_release_form_renders_when_folder_is_quarantined(env, tmp_path):
    _write_report(tmp_path, "role.packet_report.json", QUARANTINED_REPORT)
    view = pv.load_packet_report(str(tmp_path), role_slug="role")
    html = _render(env, view, job_id=42, packet={"quarantined": True})
    assert 'hx-post="/jobs/42/packet/release"' in html
    assert "<textarea" in html
    assert 'name="reason"' in html
    assert "required" in html
    assert "hx-confirm" in html


def test_release_form_absent_when_sidecar_says_quarantined_but_folder_is_not(env, tmp_path):
    """Gate on the folder (`packet.quarantined`), not the sidecar state --
    the folder is the fact, the sidecar is a label."""
    _write_report(tmp_path, "role.packet_report.json", QUARANTINED_REPORT)
    view = pv.load_packet_report(str(tmp_path), role_slug="role")
    html = _render(env, view, job_id=42, packet={"quarantined": False})
    assert "packet/release" not in html


def test_release_form_absent_when_packet_is_none(env, tmp_path):
    _write_report(tmp_path, "role.packet_report.json", QUARANTINED_REPORT)
    view = pv.load_packet_report(str(tmp_path), role_slug="role")
    html = _render(env, view, job_id=42, packet=None)
    assert "packet/release" not in html


def test_release_form_absent_on_a_ready_packet(env, tmp_path):
    _write_report(tmp_path, "role.packet_report.json", READY_REPORT)
    view = pv.load_packet_report(str(tmp_path), role_slug="role")
    html = _render(env, view, job_id=42, packet={"quarantined": False})
    assert "packet/release" not in html


# ---------------------------------------------------------------------------
# record_override
# ---------------------------------------------------------------------------


def test_record_override_round_trips(tmp_path):
    path = _write_report(tmp_path, "role.packet_report.json", dict(QUARANTINED_REPORT))
    ok = pv.record_override(str(tmp_path), reason="the character is in the company's own name")
    assert ok is True
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["state"] == "acknowledged_override"
    assert data["sendable"] is True
    assert data["override"]["reason"] == "the character is in the company's own name"
    assert "at" in data["override"]
    # The reasons the packet was held for stay on the record.
    assert data["reasons"] == QUARANTINED_REPORT["reasons"]


def test_record_override_refuses_when_not_quarantined(tmp_path):
    path = _write_report(tmp_path, "role.packet_report.json", dict(READY_REPORT))
    ok = pv.record_override(str(tmp_path), reason="whatever")
    assert ok is False
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["state"] == "ready"


def test_record_override_returns_false_when_sidecar_missing(tmp_path):
    assert pv.record_override(str(tmp_path), reason="x") is False


def test_record_override_uses_role_slug_in_shared_folder(tmp_path):
    """A company-day folder can hold several roles' sidecars -- the write
    must land on the same file the reader would read."""
    _write_report(tmp_path, "other-role.packet_report.json", dict(READY_REPORT))
    path = _write_report(tmp_path, "role.packet_report.json", dict(QUARANTINED_REPORT))
    ok = pv.record_override(str(tmp_path), reason="x", role_slug="role")
    assert ok is True
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["state"] == "acknowledged_override"
    other = json.loads((tmp_path / "other-role.packet_report.json").read_text(encoding="utf-8"))
    assert other["state"] == "ready"


def test_record_override_is_atomic(tmp_path, monkeypatch):
    """A crash mid-write must not truncate the sidecar -- write to a tmp file
    in the same directory, then os.replace."""
    path = _write_report(tmp_path, "role.packet_report.json", dict(QUARANTINED_REPORT))
    original = path.read_text(encoding="utf-8")

    real_replace = pv.os.replace

    def boom(*args, **kwargs):
        raise OSError("simulated crash")

    monkeypatch.setattr(pv.os, "replace", boom)
    ok = pv.record_override(str(tmp_path), reason="x")
    assert ok is False
    # The original sidecar must be untouched -- no tmp-write-without-replace
    # left it truncated or half-written.
    assert path.read_text(encoding="utf-8") == original
    monkeypatch.setattr(pv.os, "replace", real_replace)

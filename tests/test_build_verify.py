"""`core/build._verify_packet` as it is actually called.

Every defect pinned here survived a 1,519-test suite because the unit tests
called the checkers directly, with arguments the build never passes. These tests
call the real entry point with the real call shape.

Found 2026-09-16, all four in code shipped that same day:
- a cover-letter finding quarantined the resume (check_arithmetic hardcoded
  Artifact.RESUME, the gate ignored artifacts, and every stem was moved);
- the cover letter was passed as its own prose baseline, disabling four rules;
- a crashing checker was swallowed silently, so `state: ready, findings: []`
  meant either "clean" or "nothing ran" and nobody could tell which;
- a raise after the checks orphaned the packet: web/build.py recorded
  artifact_dir="" and the files became unreachable from the job page.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blastjob.core import build as core_build
from blastjob.core import packet_verify
from blastjob.core.packet_report import Artifact, Severity

WORK_HISTORY = """---
name: Alex J. Rivera
---

## Experience

### Director of Business Operations | Contoso Health Analytics | Oakbrook, OH (Nov 2020 - Dec 2023)
- Cut $800K from the annual run rate through vendor renegotiation and tool consolidation.
"""

RESUME = """# Alex J. Rivera

## Experience

### Director of Business Operations
**Contoso Health Analytics** | Oakbrook, OH | Nov 2020 - Dec 2023
- Cut $800K from the annual run rate through vendor renegotiation and tool consolidation.
"""

# The letter cites the EMPLOYER's own published figures. None of them can ever
# appear in the candidate's work history -- that is the whole point of a cover letter.
COVER_LETTER = """Dear Hiring Team,

Fairview Robotics is a $1.3B group targeting $100M in run-rate savings by FY27. That is
the kind of consolidation work I have spent my career on.

Sincerely,
Alex J. Rivera
"""


def _verify(tmp_path: Path, **kw):
    defaults = dict(
        resume_md=RESUME,
        cover_letter_md=COVER_LETTER,
        work_history_md=WORK_HISTORY,
        job_description="SVP of Business Operations",
        out_dir=tmp_path,
        resume_stem="Resume",
        fit_score=None,
    )
    defaults.update(kw)
    return core_build._verify_packet(**defaults)


# ----- the cover letter must not take the resume down with it ---------------


def test_an_employer_figure_in_the_letter_is_not_fatal_when_a_research_span_backs_it(tmp_path):
    report = _verify(
        tmp_path,
        company_facts=[
            _fact("Fairview Robotics is a $1.3B collective of outdoor brands"),
            _fact("Fairview Robotics targets $100M in run-rate savings by FY27"),
        ],
    )
    assert [f for f in report.findings if f.severity is Severity.FATAL] == []


def test_an_employer_figure_with_no_backing_span_is_fatal_on_the_letter_only(tmp_path):
    report = _verify(tmp_path, company_facts=[])
    fatals = [f for f in report.findings if f.severity is Severity.FATAL]
    assert fatals, "an unbacked figure must still be caught"
    assert all(f.artifact is Artifact.COVER_LETTER for f in fatals)


def test_quarantine_stems_follow_the_failing_artifact(tmp_path):
    """A resume fatal takes the resume and the ATS file derived from it; a
    cover-letter fatal takes the letter alone."""
    from blastjob.core.packet_report import GateVerdict, PacketState

    def stems(artifacts):
        verdict = GateVerdict(state=PacketState.QUARANTINED, quarantined_artifacts=artifacts)
        return core_build._quarantine_stems(verdict, resume_stem="R", cover_stem="C")

    assert stems(frozenset({Artifact.COVER_LETTER})) == ["C"]
    assert sorted(stems(frozenset({Artifact.RESUME}))) == ["R", "R_ATS"]
    assert sorted(stems(frozenset(Artifact))) == ["C", "R", "R_ATS"]


# ----- no document is ever its own prose baseline ---------------------------


def test_no_document_is_its_own_prose_baseline_in_the_real_call_shape(tmp_path, monkeypatch):
    """The structural test. Four prose rules were dead for weeks because
    build.py handed the cover letter to itself as `baseline`, and every unit
    test passed a distinct sample so the suite could not see it."""
    seen: list[dict] = []

    def _spy(text, **kwargs):
        seen.append({"text": text, **kwargs})
        return []

    monkeypatch.setattr(packet_verify.prose_check, "check_prose", _spy)
    _verify(tmp_path)

    assert seen, "check_prose must actually be called"
    for call in seen:
        for key, value in call.items():
            if key == "text" or not isinstance(value, str) or not value:
                continue
            assert value != call["text"], f"{key} was handed the document itself"


# ----- a broken checker must be visible -------------------------------------


def test_a_crashing_checker_is_named_in_the_report(tmp_path, monkeypatch):
    def _boom(*_a, **_kw):
        raise RuntimeError("checker exploded")

    monkeypatch.setattr(packet_verify.prose_check, "check_prose", _boom)
    report = _verify(tmp_path)

    assert report.checks_failed, "a crashed check must be recorded"
    assert any("prose" in name for name in report.checks_failed)
    assert report.checks_run, "the checks that did run must be recorded too"


def test_every_checker_crashing_never_looks_like_a_clean_packet(tmp_path, monkeypatch):
    def _boom(*_a, **_kw):
        raise RuntimeError("checker exploded")

    for mod, attr in (
        (packet_verify.resume_format, "check_structure"),
        (packet_verify.packet_gate, "check_arithmetic"),
        (packet_verify.prose_check, "check_prose"),
    ):
        monkeypatch.setattr(mod, attr, _boom)
    report = _verify(tmp_path)

    assert len(report.checks_failed) == len(report.checks_run) + len(report.checks_failed)
    assert report.checks_run == ()
    # The panel must not be able to render this as "no findings, all clear".
    assert report.checks_failed


def _fact(span: str):
    """A CompanyFact-shaped stand-in: only `source_span` is trusted."""
    from blastjob.core.research_facts import CompanyFact

    return CompanyFact(statement=span, entity="Fairview Robotics", source_span=span)


# ----- a verification bug must never orphan the packet ----------------------


def test_a_gate_crash_still_leaves_the_packet_reachable(tmp_path, monkeypatch):
    """`web/build.py` records `artifact_dir=str(out_dir or "")`. If run_build
    raises after writing the documents, that records "" and the packet exists on
    disk but is unreachable from the job page forever."""
    monkeypatch.setattr(
        packet_verify.packet_gate,
        "gate",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("x")),
    )
    report = _verify(tmp_path)
    verdict = core_build._gate_and_report(report, tmp_path, "role", app_config=None)
    # None is the contract for "verification itself failed" -- the caller must
    # not quarantine on it, because one broken checker would otherwise hold
    # every packet and teach the user to click Release without reading.
    assert verdict is None
    written = json.loads((tmp_path / "role.packet_report.json").read_text())
    assert written["state"] == "verification_failed"
    assert written["sendable"] is False
    assert "RuntimeError" in written["reasons"][0]


@pytest.mark.parametrize("field", ["checks_run", "checks_failed", "checks_skipped"])
def test_report_json_carries_the_check_bookkeeping(tmp_path, field):
    report = _verify(tmp_path)
    verdict = core_build._gate_and_report(report, tmp_path, "role", app_config=None)
    written = json.loads((tmp_path / "role.packet_report.json").read_text())
    assert field in written, f"{field} must reach the sidecar"
    assert verdict is not None


# ----- one defect, one finding ----------------------------------------------


def test_a_date_contradiction_is_not_reported_twice(tmp_path):
    """A wrong date range used to produce up to three artifacts: a FATAL from
    resume_format, a MAJOR `unsupported_claim` re-emitted from the fit score
    with an EMPTY evidence field, and a depressed groundedness number. The gate
    then listed the MAJOR under "claims with no support in your work history",
    which is a mislabel -- it is a date contradiction, and resume_format already
    reports it with the better history aggregation.
    """
    from blastjob.models.fit_score import ClaimCheck, FitScore

    fit = FitScore(
        overall_score=70,
        groundedness_score=70,
        jd_alignment_score=70,
        claims=[
            ClaimCheck(
                claim_text="Contoso: resume says 2015-2023, history says 2017-2023",
                grounded=False,
                source_section="dates",
            ),
            ClaimCheck(claim_text="A genuinely unsupported prose claim", grounded=False),
        ],
    )
    report = _verify(tmp_path, fit_score=fit)
    unsupported = [f for f in report.findings if f.code == "unsupported_claim"]
    assert len(unsupported) == 1, "the date contradiction must not become a second finding"
    assert unsupported[0].message == "A genuinely unsupported prose claim"

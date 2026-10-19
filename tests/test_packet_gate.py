"""Tests for `core/packet_gate.py`: the arithmetic check, the sendability
gate, and the packet-state transition machine.

TDD: written before the implementation existed. Run with
`uv run --extra dev pytest -q tests/test_packet_gate.py`.
"""

from __future__ import annotations

from blastjob.core.packet_gate import check_arithmetic, gate
from blastjob.core.packet_report import (
    Artifact,
    Finding,
    GateVerdict,
    PacketReport,
    PacketState,
    Severity,
)
from blastjob.core.scoring import GROUNDEDNESS_FLOOR_DEFAULT

# ---------------------------------------------------------------------------
# check_arithmetic
# ---------------------------------------------------------------------------


def test_arithmetic_flags_the_550k_defect():
    """The motivating real-world defect: $550K = $240K + an unrelated $310K
    training-travel saving. Exact sum, both addends genuinely present in the
    history -- and the claim is still false, because $550K itself was never
    stated. Must be FATAL."""
    # Arrange
    work_history_md = (
        "Negotiated $240K in licensing savings. Cut in-housed services costs "
        "by $310K via a training-travel reduction."
    )
    text = "Delivered $550K+ across licensing, in-housed services, and support contracts."

    # Act
    findings = check_arithmetic(text, work_history_md)

    # Assert
    assert len(findings) == 1
    assert findings[0].severity is Severity.FATAL
    assert findings[0].code == "unverified_total"
    assert findings[0].evidence == "$550K"


def test_arithmetic_passes_when_total_is_stated_verbatim():
    # Arrange
    work_history_md = "Delivered $550K in total value across three initiatives."
    text = "Delivered $550K in measurable savings."

    # Act
    findings = check_arithmetic(text, work_history_md)

    # Assert
    assert findings == []


def test_arithmetic_does_not_sum_addends_even_when_it_could():
    """A total that IS the exact sum of two figures present in the history is
    still flagged, because summing is never performed (V7)."""
    # Arrange
    work_history_md = "Saved $100K on vendor A. Saved $50K on vendor B."
    text = "Saved $150K across vendor consolidation."

    # Act
    findings = check_arithmetic(text, work_history_md)

    # Assert
    assert len(findings) == 1
    assert findings[0].evidence == "$150K"


def test_arithmetic_money_whole_token_not_substring():
    """A truncated figure must not read as verified because it is a
    substring of a trusted, larger figure (mirrors core/prep.py's own
    '$1,200 inside a trusted $1,200,000' example)."""
    # Arrange
    work_history_md = "Managed a $1,200,000 annual budget."
    text = "Managed a $1,200 budget line."

    # Act
    findings = check_arithmetic(text, work_history_md)

    # Assert
    assert len(findings) == 1
    assert findings[0].evidence == "$1,200"


def test_arithmetic_5m_not_satisfied_by_500k():
    # Arrange
    work_history_md = "Owned a $500K infrastructure budget."
    text = "Owned a $5M infrastructure budget."

    # Act
    findings = check_arithmetic(text, work_history_md)

    # Assert
    assert len(findings) == 1
    assert findings[0].evidence == "$5M"


def test_arithmetic_percent_whole_token_not_substring():
    """'3%' must not read as verified merely because '13%' is trusted
    (mirrors prep.py's own '3% inside 13%' example)."""
    # Arrange
    work_history_md = "Reduced staff turnover by 13% year over year."
    text = "Cut turnover by 3% in the first quarter."

    # Act
    findings = check_arithmetic(text, work_history_md)

    # Assert
    assert len(findings) == 1
    assert findings[0].evidence == "3%"


def test_arithmetic_percent_matches_verbatim():
    # Arrange
    work_history_md = "Increased gross margin by 22.5% over two years."
    text = "Grew margin 22.5% through pricing discipline."

    # Act
    findings = check_arithmetic(text, work_history_md)

    # Assert
    assert findings == []


def test_arithmetic_scale_word_and_letter_are_the_same_figure():
    """'$5 million' and '$5M' are the same value under normalization, and
    that equivalence is correct (not a substring bug)."""
    # Arrange
    work_history_md = "Delivered $5 million in cost savings over three years."
    text = "Delivered $5M in savings."

    # Act
    findings = check_arithmetic(text, work_history_md)

    # Assert
    assert findings == []


def test_arithmetic_dedupes_repeated_unverified_figure():
    # Arrange
    work_history_md = "No dollar figures here at all."
    text = "Saved $77K in year one. Saved $77K again in year two."

    # Act
    findings = check_arithmetic(text, work_history_md)

    # Assert
    assert len(findings) == 1
    assert findings[0].evidence == "$77K"


def test_arithmetic_no_figures_no_findings():
    # Arrange
    work_history_md = "Led the platform team through a major migration."
    text = "Led cross-functional teams through complex technical migrations."

    # Act
    findings = check_arithmetic(text, work_history_md)

    # Assert
    assert findings == []


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------


def _finding(
    severity: Severity,
    code: str = "some_code",
    message: str = "a defect",
    artifact: Artifact = Artifact.RESUME,
) -> Finding:
    return Finding(severity=severity, code=code, message=message, artifact=artifact)


def test_gate_quarantines_on_a_fatal_finding():
    # Arrange
    report = PacketReport(
        groundedness=95,
        findings=(_finding(Severity.FATAL, code="banned_characters"),),
    )

    # Act
    verdict = gate(report)

    # Assert
    assert verdict.state is PacketState.QUARANTINED
    assert not verdict.sendable
    assert any("banned_characters" in r for r in verdict.reasons)


def test_gate_quarantine_reasons_name_every_distinct_fatal_code():
    # Arrange
    report = PacketReport(
        findings=(
            _finding(Severity.FATAL, code="banned_characters", message="illegal char"),
            _finding(Severity.FATAL, code="unverified_total", message="'$9M' unverified"),
            _finding(Severity.MINOR, code="passive_voice", message="advisory only"),
        )
    )

    # Act
    verdict = gate(report)

    # Assert
    assert verdict.state is PacketState.QUARANTINED
    joined = " | ".join(verdict.reasons)
    assert "banned_characters" in joined
    assert "unverified_total" in joined
    assert "passive_voice" not in joined  # MINOR never drives quarantine


def test_gate_cover_letter_fatal_also_quarantines():
    """A cover-letter FATAL is covered by the same first rule -- the gate
    does not care which artifact a fatal finding is about."""
    # Arrange
    report = PacketReport(
        groundedness=99,
        findings=(
            _finding(Severity.FATAL, code="contradicts_research", artifact=Artifact.COVER_LETTER),
        ),
    )

    # Act
    verdict = gate(report)

    # Assert
    assert verdict.state is PacketState.QUARANTINED


def test_gate_low_groundedness_with_no_fatal_never_blocks():
    """The uncalibrated score alone must never quarantine (V6)."""
    # Arrange
    report = PacketReport(groundedness=GROUNDEDNESS_FLOOR_DEFAULT - 40)

    # Act
    verdict = gate(report)

    # Assert
    assert verdict.state is PacketState.READY
    assert verdict.sendable
    assert len(verdict.reasons) == 1
    assert str(GROUNDEDNESS_FLOOR_DEFAULT - 40) in verdict.reasons[0]
    assert verdict.reasons[0] != str(GROUNDEDNESS_FLOOR_DEFAULT - 40)  # never a bare number


def test_gate_low_groundedness_reason_lists_unsupported_claims():
    # Arrange
    report = PacketReport(
        groundedness=GROUNDEDNESS_FLOOR_DEFAULT - 10,
        findings=(
            _finding(
                Severity.MAJOR,
                code="unsupported_claim",
                message="Ran a $12M cloud migration from kickoff to cutover",
            ),
            _finding(
                Severity.MAJOR,
                code="unsupported_claim",
                message="Built the security program from scratch",
            ),
        ),
    )

    # Act
    verdict = gate(report)

    # Assert
    assert verdict.state is PacketState.READY
    reason = verdict.reasons[0]
    assert "Ran a $12M cloud migration from kickoff to cutover" in reason
    assert "Built the security program from scratch" in reason
    assert "2" in reason  # names the count, not just the score


def test_gate_groundedness_none_is_ready():
    # Arrange
    report = PacketReport(groundedness=None)

    # Act
    verdict = gate(report)

    # Assert
    assert verdict.state is PacketState.READY
    assert verdict.sendable


def test_gate_high_groundedness_ready_with_no_reasons():
    # Arrange
    report = PacketReport(groundedness=97)

    # Act
    verdict = gate(report)

    # Assert
    assert verdict.state is PacketState.READY
    assert verdict.reasons == ()


def test_gate_cover_letter_groundedness_none_does_not_block():
    """No cover letter at all must not prevent a READY verdict."""
    # Arrange
    report = PacketReport(groundedness=90, cover_letter_groundedness=None)

    # Act
    verdict = gate(report)

    # Assert
    assert verdict.state is PacketState.READY
    assert verdict.sendable


# ---------------------------------------------------------------------------
# transition
# ---------------------------------------------------------------------------


def _verdict(state: PacketState) -> GateVerdict:
    return GateVerdict(state=state)


# ----- artifact scoping and the trusted figure set --------------------------
#
# Written 2026-09-16 after review found that a cover-letter finding quarantined
# the resume: check_arithmetic hardcoded Artifact.RESUME, build.py ran it on the
# letter, and gate() quarantined on any fatal with no artifact scoping. A letter
# that correctly cited an employer's revenue took the resume down with it.


def test_550k_and_550000_are_one_figure():
    """A model reformatting a true figure must not quarantine the packet."""
    assert check_arithmetic("saved $550,000", "saved $550K in year one") == []


def test_550k_and_549750_stay_two_figures():
    """Notation tolerance must not become magnitude tolerance."""
    findings = check_arithmetic("saved $549,750", "saved $550K in year one")
    assert [f.code for f in findings] == ["unverified_total"]


def test_a_percentage_never_matches_a_dollar_figure():
    findings = check_arithmetic("cut costs 50%", "saved $50 on paper")
    assert [f.code for f in findings] == ["unverified_total"]


def test_scale_words_and_letters_are_the_same_figure():
    assert check_arithmetic("a $3.5B business", "a $3.5 billion business") == []


def test_cover_letter_findings_are_tagged_cover_letter():
    findings = check_arithmetic(
        "revenue of $1.3B", "no figures here", artifact=Artifact.COVER_LETTER
    )
    assert findings and all(f.artifact is Artifact.COVER_LETTER for f in findings)


def test_an_employer_figure_is_trusted_from_a_verified_research_span():
    """A letter must be able to cite the employer's own published numbers. The
    trusted span is verbatim research text that research_facts already verified
    against the sidecar -- the same material the letter prompt was given."""
    findings = check_arithmetic(
        "a $1.3B group targeting $100M in savings",
        "The candidate's history mentions no such figures",
        artifact=Artifact.COVER_LETTER,
        trusted_spans=[
            "Fairview Robotics is a $1.3B collective",
            "targeting $100M run-rate savings",
        ],
    )
    assert findings == []


def test_an_employer_figure_absent_from_every_span_is_still_fatal():
    findings = check_arithmetic(
        "a $9.9B group",
        "no figures here",
        artifact=Artifact.COVER_LETTER,
        trusted_spans=["Fairview Robotics is a $1.3B collective"],
    )
    assert [f.severity for f in findings] == [Severity.FATAL]


def _fatal(artifact: Artifact) -> Finding:
    return Finding(severity=Severity.FATAL, code="unverified_total", message="x", artifact=artifact)


def test_a_cover_letter_fatal_quarantines_only_the_cover_letter():
    verdict = gate(PacketReport(findings=(_fatal(Artifact.COVER_LETTER),)))
    assert verdict.state is PacketState.QUARANTINED
    assert verdict.quarantined_artifacts == frozenset({Artifact.COVER_LETTER})


def test_a_resume_fatal_quarantines_the_resume():
    verdict = gate(PacketReport(findings=(_fatal(Artifact.RESUME),)))
    assert verdict.quarantined_artifacts == frozenset({Artifact.RESUME})


def test_fatals_on_both_artifacts_quarantine_both():
    verdict = gate(PacketReport(findings=(_fatal(Artifact.RESUME), _fatal(Artifact.COVER_LETTER))))
    assert verdict.quarantined_artifacts == frozenset({Artifact.RESUME, Artifact.COVER_LETTER})


def test_a_fatal_with_an_unmapped_artifact_quarantines_everything(monkeypatch):
    """Unscoped means we do not know which document is unsafe, and an unknown
    document is unsendable."""
    monkeypatch.setattr("blastjob.core.packet_report.DERIVED_ARTIFACTS", {})
    verdict = gate(PacketReport(findings=(_fatal(Artifact.RESUME),)))
    assert verdict.quarantined_artifacts == frozenset(Artifact)


def test_a_ready_packet_quarantines_nothing():
    assert gate(PacketReport(groundedness=97)).quarantined_artifacts == frozenset()


_PERCENT_FORMS = [
    "30%",
    "30 %",
    "30 percent",
    "30 per cent",
    "30 pct",
    "30 Percent",
    "thirty percent",
    "Thirty percent",
    "thirty-five percent",
]


def test_arithmetic_invented_percent_flagged_in_every_spelling():
    for form in _PERCENT_FORMS:
        findings = check_arithmetic(f"Grew revenue {form} in a year.", "Led the team.")
        assert len(findings) == 1, form
        assert findings[0].evidence == form


def test_arithmetic_percent_forms_verify_each_other():
    for trusted_form in ("30%", "30 percent", "thirty percent", "30 per cent", "30 pct"):
        for text_form in ("30%", "30 %", "30 percent", "thirty percent", "30 pct"):
            findings = check_arithmetic(
                f"Grew revenue {text_form}.", f"Grew sales {trusted_form} at Acme."
            )
            assert findings == [], (trusted_form, text_form)


def test_arithmetic_three_percent_not_verified_by_thirty_or_thirteen():
    assert len(check_arithmetic("Cut cost 3 percent.", "Cut cost 30%.")) == 1
    assert len(check_arithmetic("Cut cost three percent.", "Cut cost 13 percent.")) == 1
    assert len(check_arithmetic("Cut cost 30 percent.", "Cut cost thirteen percent.")) == 1


def test_arithmetic_spelled_hundred_and_compound_words():
    assert check_arithmetic("Hit a hundred percent.", "Hit 100%.") == []
    assert check_arithmetic("Up forty two percent.", "Up 42%.") == []
    assert len(check_arithmetic("Up forty-two percent.", "Up 24%.")) == 1

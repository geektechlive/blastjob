"""The groundedness gate: deterministic claim verification + the cockpit summary.

Every tailor build writes a `fit_score.json` full of per-claim verdicts and, until
now, nothing read it back. These tests pin the two halves of the fix:

1. `core.scoring` must not take the model's word for a claim. An `evidence_quote`
   that is not actually in `work_history.md` marks the claim unsupported no matter
   what the model said.
2. `web.fitscore` must load a packet's score without ever raising, so a missing or
   truncated file degrades to "not scored" instead of 500ing the cockpit.

This is model-assisted review, not a guarantee: it catches quotes that were never
in the source, not claims assembled out of several true ones.
"""

from __future__ import annotations

import json
from pathlib import Path

from blastjob.core.scoring import (
    GROUNDEDNESS_FLOOR_DEFAULT,
    needs_review,
    parse_score,
    verify_claims,
)
from blastjob.models.fit_score import ClaimCheck
from blastjob.web.fitscore import load_fit_score

WORK_HISTORY = (
    "## Experience — Northwind Logistics\n"
    "Runs the network, endpoint, and line-of-business application\n"
    "portfolio for a 110-person business unit, including an ERP\n"
    "rollout. Screened two acquisition targets for the finance group.\n"
)


def _score_json(**overrides) -> str:
    data = {
        "overall_score": 68,
        "jd_alignment_score": 82,
        "groundedness_score": 62,
        "unsupported_count": 0,
        "summary": "Strong JD-targeted resume.",
        "claims": [],
    }
    data.update(overrides)
    return json.dumps(data)


# ---------------------------------------------------------------------------
# 1. Deterministic verification — the model cannot vouch for itself
# ---------------------------------------------------------------------------


def test_fabricated_evidence_quote_is_unsupported_even_when_model_says_grounded():
    """The whole point. A quote the work history never contained is a fabrication,
    and `grounded: true` in the JSON does not get to overrule Python."""
    claims = [
        ClaimCheck(
            claim_text="Owns cybersecurity governance (NIST, SOC 2, HITRUST).",
            grounded=True,
            evidence_quote="Led the NIST, SOC 2 and HITRUST compliance programs.",
            source_section="Experience — Northwind Logistics",
        )
    ]
    checked = verify_claims(claims, WORK_HISTORY)
    assert checked[0].grounded is False


def test_verify_claims_does_not_mutate_the_claims_it_was_given():
    original = ClaimCheck(claim_text="x", grounded=True, evidence_quote="not in history")
    verify_claims([original], WORK_HISTORY)
    assert original.grounded is True  # caller's object untouched


def test_a_real_quote_wrapped_across_lines_still_counts_as_grounded():
    """Whitespace-only difference is not a fabrication. The work history hard-wraps;
    the model quotes it as one line. Normalizing whitespace removes a false positive
    (it does not catch anything new)."""
    claims = [
        ClaimCheck(
            claim_text="Owns ERP strategy.",
            grounded=True,
            evidence_quote=(
                "Runs the network, endpoint, and line-of-business application "
                "portfolio for a 110-person business unit, including an ERP rollout."
            ),
            source_section="Experience — Northwind Logistics",
        )
    ]
    checked = verify_claims(claims, WORK_HISTORY)
    assert checked[0].grounded is True


def test_grounded_claim_with_no_evidence_quote_is_unsupported():
    """An empty string is a substring of everything, so the naive check passes it.
    A claim with no quote behind it has not been evidenced."""
    claims = [ClaimCheck(claim_text="Drove AI to 99% adoption.", grounded=True, evidence_quote="")]
    checked = verify_claims(claims, WORK_HISTORY)
    assert checked[0].grounded is False


def test_parse_score_runs_the_same_verification():
    text = _score_json(
        claims=[
            {
                "claim_text": "Ran a global team.",
                "grounded": True,
                "evidence_quote": "Managed a global team across four continents.",
                "source_section": "Summary",
            }
        ]
    )
    score = parse_score(text, WORK_HISTORY)
    assert score.claims[0].grounded is False
    assert score.unsupported_count == 1
    assert score.groundedness_score == 0


# ---------------------------------------------------------------------------
# 2. The floor
# ---------------------------------------------------------------------------


def test_floor_default_is_85():
    assert GROUNDEDNESS_FLOOR_DEFAULT == 85


def test_floor_boundary():
    assert needs_review(84, floor=85) is True
    assert needs_review(85, floor=85) is False
    assert needs_review(86, floor=85) is False


def test_floor_is_configurable():
    assert needs_review(70, floor=60) is False


# ---------------------------------------------------------------------------
# 3. The cockpit loader
# ---------------------------------------------------------------------------

# Modelled on meridian-2026-09-10 on disk (13 of 35 unsupported, groundedness 62),
# trimmed to four claims with the text anonymized. Note there is no
# `undated_employers` key: packets written before that field exist must still load.
MERIDIAN_SHAPED = {
    "overall_score": 68,
    "jd_alignment_score": 82,
    "groundedness_score": 62,
    "unsupported_count": 2,
    "summary": "Strong JD-targeted resume whose metrics are mostly traceable.",
    "claims": [
        {
            "claim_text": "Operations leader with two decades running enterprise IT.",
            "grounded": True,
            "evidence_quote": "Seasoned operations leader with over 20 years of experience.",
            "source_section": "Summary",
        },
        {
            "claim_text": "Runs operations for a nationwide software business.",
            "grounded": False,
            "evidence_quote": "Owns the IT budget and vendor portfolio outright.",
            "source_section": "Experience — Acme Co",
        },
        {
            "claim_text": "Owns cybersecurity governance (NIST, SOC 2, HITRUST).",
            "grounded": False,
            "evidence_quote": "",
            "source_section": "Skills",
        },
        {
            "claim_text": "Executed technical diligence on M&A.",
            "grounded": True,
            "evidence_quote": "Ran technical screening on two acquisition targets.",
            "source_section": "Experience — Acme Co",
        },
    ],
}


def _packet(tmp_path: Path, name: str, payload: object) -> Path:
    d = tmp_path / "acme-2026-09-10"
    d.mkdir(exist_ok=True)
    body = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
    (d / name).write_text(body, encoding="utf-8")
    return d


def test_loads_a_real_shaped_packet(tmp_path):
    d = _packet(tmp_path, "vp-ops.fit_score.json", MERIDIAN_SHAPED)
    s = load_fit_score(str(d))

    assert s.scored is True
    assert s.groundedness_score == 62
    assert s.jd_alignment_score == 82
    assert s.overall_score == 68
    assert s.total_claims == 4
    assert s.unsupported_count == 2
    assert [c.claim_text for c in s.unsupported] == [
        "Runs operations for a nationwide software business.",
        "Owns cybersecurity governance (NIST, SOC 2, HITRUST).",
    ]
    assert s.unsupported[0].evidence_quote == "Owns the IT budget and vendor portfolio outright."
    assert s.unsupported[0].source_section == "Experience — Acme Co"
    assert s.source_path.endswith("vp-ops.fit_score.json")


def test_the_floor_badges_a_62_packet_as_needs_review(tmp_path):
    d = _packet(tmp_path, "vp-ops.fit_score.json", MERIDIAN_SHAPED)
    assert load_fit_score(str(d)).needs_review is True
    assert load_fit_score(str(d), floor=50).needs_review is False


def test_unsupported_count_is_recounted_not_trusted(tmp_path):
    """The stored count came from whatever code version wrote the file."""
    payload = dict(MERIDIAN_SHAPED, unsupported_count=99)
    d = _packet(tmp_path, "vp-ops.fit_score.json", payload)
    assert load_fit_score(str(d)).unsupported_count == 2


def test_refine_layout_bare_filename_is_found(tmp_path):
    d = _packet(tmp_path, "fit_score.json", MERIDIAN_SHAPED)
    assert load_fit_score(str(d)).scored is True


def test_missing_file_degrades_to_not_scored(tmp_path):
    d = tmp_path / "empty-packet"
    d.mkdir()
    s = load_fit_score(str(d))
    assert s.scored is False
    assert s.status == "not scored"
    assert s.reason
    assert s.unsupported == ()


def test_missing_directory_degrades_to_not_scored(tmp_path):
    s = load_fit_score(str(tmp_path / "nope"))
    assert s.scored is False


def test_empty_packet_dir_argument_degrades_to_not_scored():
    assert load_fit_score("").scored is False
    assert load_fit_score(None).scored is False


def test_corrupt_json_degrades_to_not_scored(tmp_path):
    d = _packet(tmp_path, "vp-ops.fit_score.json", '{"overall_score": 68, "claims": [')
    s = load_fit_score(str(d))
    assert s.scored is False
    assert s.status == "not scored"


def test_wrong_shaped_json_degrades_to_not_scored(tmp_path):
    d = _packet(tmp_path, "vp-ops.fit_score.json", '{"claims": "not a list"}')
    assert load_fit_score(str(d)).scored is False


def test_role_slug_hint_picks_the_right_file_among_several(tmp_path):
    d = _packet(tmp_path, "vp-ops.fit_score.json", MERIDIAN_SHAPED)
    other = dict(MERIDIAN_SHAPED, groundedness_score=91)
    (d / "director-infra.fit_score.json").write_text(json.dumps(other), encoding="utf-8")

    assert load_fit_score(str(d), role_slug="director-infra").groundedness_score == 91
    # No hint: still deterministic, and it says which file it read.
    both = load_fit_score(str(d))
    assert both.scored is True
    assert both.source_path.endswith(".fit_score.json")


def test_summary_calls_itself_model_assisted_not_a_guarantee(tmp_path):
    d = _packet(tmp_path, "vp-ops.fit_score.json", MERIDIAN_SHAPED)
    assert "model-assisted" in load_fit_score(str(d)).note.lower()


# ---------------------------------------------------------------------------
# 4. The panel template
# ---------------------------------------------------------------------------


def _render(**ctx) -> str:
    """Render through the cockpit's OWN Jinja environment.

    Rendering through a locally built environment would only prove that the test
    turned autoescaping on. The escaping that matters is the app's.
    """
    from blastjob.web.app import templates

    return templates.env.get_template("_groundedness.html").render(**ctx)


def test_panel_renders_nothing_without_the_context_var():
    assert _render().strip() == ""
    assert _render(groundedness=None).strip() == ""


def test_panel_shows_score_floor_and_each_unsupported_claim(tmp_path):
    d = _packet(tmp_path, "vp-ops.fit_score.json", MERIDIAN_SHAPED)
    html = _render(groundedness=load_fit_score(str(d)))

    assert "62/100" in html
    assert "needs review" in html
    assert "uncalibrated" in html.lower()
    assert "model-assisted" in html.lower()
    assert "2 of 4" in html
    assert "Owns cybersecurity governance (NIST, SOC 2, HITRUST)." in html
    assert "Owns the IT budget and vendor portfolio outright." in html
    assert "Experience — Acme Co" in html
    # Grounded claims are not listed as problems.
    assert "Executed technical diligence on M&amp;A." not in html


def test_panel_escapes_model_text_rather_than_trusting_it(tmp_path):
    payload = dict(
        MERIDIAN_SHAPED,
        claims=[
            {
                "claim_text": "<script>alert('xss')</script>",
                "grounded": False,
                "evidence_quote": "<img src=x onerror=alert(1)>",
                "source_section": "Summary",
            }
        ],
    )
    d = _packet(tmp_path, "vp-ops.fit_score.json", payload)
    html = _render(groundedness=load_fit_score(str(d)))

    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "onerror=alert(1)>" not in html


def test_panel_says_unverified_when_the_packet_was_never_scored(tmp_path):
    d = tmp_path / "empty-packet"
    d.mkdir()
    html = _render(groundedness=load_fit_score(str(d)))
    assert "not scored" in html
    assert "unverified" in html


def test_panel_above_the_floor_is_not_badged_needs_review(tmp_path):
    d = _packet(tmp_path, "vp-ops.fit_score.json", dict(MERIDIAN_SHAPED, groundedness_score=91))
    html = _render(groundedness=load_fit_score(str(d)))
    assert "needs review" not in html
    assert "at or above floor" in html

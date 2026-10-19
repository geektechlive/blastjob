"""The gate that decides whether a generated packet is sendable, plus the
arithmetic check that guards against invented totals.

See CLAUDE.md's "Truth guarantees" and
docs/ARCHITECTURE.md ("Revisions after adversarial
review (v2)", V5-V7) for the reasoning encoded here:

- **V7. No computed totals at all.** `check_arithmetic` deliberately does NOT
  check "is this figure the sum of other figures that exist" -- that check
  would have PASSED the defect that motivated this whole program: a real
  packet claimed "$470K+ across licensing, support contracts, and
  monitoring", which is exactly $170K + $300K, where the $300K was a
  training-travel saving belonging to none of those three categories.
  Arithmetic equality says nothing about whether two figures share a metric,
  a period or an owner. The rule is absolute instead: a money or percentage
  figure may appear in generated text only if that exact figure appears, as
  a whole token, in `work_history.md`. No summation is performed on either
  side.
- **V5. The gate takes the whole `PacketReport`.** A resume can be perfectly
  grounded while its cover letter invents a fact, so the gate reads one
  aggregated report rather than a bare score, and owns an explicit state
  machine (`transition`) rather than a boolean.
- **V6. Groundedness alone never blocks; mechanical fatals do.** A FATAL
  finding (this module's `unverified_total`, or any check upstream) always
  quarantines. A low groundedness score with NO fatal finding never blocks --
  `GROUNDEDNESS_FLOOR_DEFAULT` in `core/scoring.py` is explicitly
  uncalibrated, and it fires on honest stretch applications. A gate that
  blocks on it teaches the user to click through warnings until the gate stops
  meaning anything, so a low score only ever produces a loud, specific
  READY-with-reasons.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

from blastjob.core.packet_report import (
    Artifact,
    Finding,
    GateVerdict,
    PacketReport,
    PacketState,
    Severity,
    artifacts_to_quarantine,
)
from blastjob.core.percent_figures import PERCENT, percent_key
from blastjob.core.scoring import GROUNDEDNESS_FLOOR_DEFAULT
from blastjob.models.config import BlastJobConfig

# ---------------------------------------------------------------------------
# check_arithmetic
# ---------------------------------------------------------------------------

# The same figure shapes and whole-token discipline as core/prep.py's
# `mark_unverified_numbers` (money, percentage): a truncated figure or a
# misplaced decimal are the likeliest real-world errors, and a substring test
# clears both ("$1,200" would wrongly read as verified inside a trusted
# "$1,200,000"; "3%" would wrongly read as verified inside a trusted "13%").
# Kept as a local copy rather than importing prep's private regex -- prep.py
# is owned by another workstream and its combined pattern also matches bare
# magnitudes/headcounts ("12 years", "500 employees") that are not arithmetic
# claims this check should police.
_SCALE = r"(?:[KMBTkmbt]|(?i:thousand|million|billion|trillion))\b"
_MONEY = rf"\$\d[\d,]*(?:\.\d+)?(?:\s?{_SCALE})?"
_PERCENT = PERCENT
# The percent branch gets a lookbehind guard (money already starts with an
# unambiguous '$') so a match cannot start mid-number, and the shared
# lookahead guard stops it from ending mid-number.
_FIGURE = re.compile(rf"(?:{_MONEY}|(?<![\w.,$]){_PERCENT})(?!\w|[.,]\d)")

_SCALE_WORDS = {"thousand": "k", "million": "m", "billion": "b", "trillion": "t"}
_SCALE_FACTOR = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000, "t": 1_000_000_000_000}


def _normalize(token: str) -> str:
    """One comparable key per figure: notation-insensitive, magnitude-exact.

    '$470K', '$470,000' and '$470 thousand' are one figure; '$470K' and
    '$469,750' are two; '50%' never equals '$50'. Scale letters and scale words
    expand into digits, commas and spaces drop, money and percent live in
    separate namespaces, and the comparison is exact Decimal equality -- no
    rounding, no tolerance, and (per V7) no summation on either side.

    Reconciling notation cannot make this check more permissive: it only equates
    figures of identical magnitude, so the '$470K' vs '$470,000' FATAL it
    removes was always a false alarm, never a caught fabrication. Before this,
    a model that reformatted a true figure quarantined the packet.
    """
    percent = percent_key(token)
    if percent is not None:
        return percent
    text = token.strip().lower().replace(",", "")
    for word, letter in _SCALE_WORDS.items():
        text = text.replace(word, letter)
    text = text.replace(" ", "")
    unit = "$"
    digits = text.lstrip("$")
    factor = 1
    if digits and digits[-1] in _SCALE_FACTOR:
        factor = _SCALE_FACTOR[digits[-1]]
        digits = digits[:-1]
    try:
        value = Decimal(digits) * factor
    except InvalidOperation:
        return f"{unit}{text}"
    return f"{unit}{value.normalize():f}"


def check_arithmetic(
    text: str,
    work_history_md: str,
    *,
    artifact: Artifact = Artifact.RESUME,
    trusted_spans: Sequence[str] = (),
) -> list[Finding]:
    """Flag every money/percentage figure in `text` that does not appear, as
    the same whole figure, in a TRUSTED text.

    Deliberately NOT "is this the sum of figures that exist" -- see the
    module docstring. Whole-token comparison only, never substring: a figure
    is flagged unless the exact normalized figure is present. Each distinct
    unverified figure produces one FATAL `unverified_total` finding, even if
    it recurs in `text`.

    RULE (trusted figures). For the resume the trusted text is
    `work_history.md`, full stop. For the cover letter it is `work_history.md`
    PLUS `trusted_spans` -- the `source_span` of each distilled `CompanyFact`,
    which is verbatim research text `research_facts` already verified against
    the sanitized sidecar, and the only part of a fact that is not the model's
    own compression. A fact's `statement` is NEVER trusted: the distiller
    verifies the span's presence, never the statement's content against it, so
    a figure could be minted there.

    This check cannot verify an employer's real revenue and never claims to. It
    verifies one thing: that a figure was not invented during generation. Every
    figure must trace to the user's history or to a research span the distiller
    accepted -- the same material the letter prompt was written from
    (`research_facts.facts_block`), so nothing new is trusted here.

    Consequence, deliberate: a confidential posting has no research, so no
    spans, so an employer figure in its letter is FATAL. Correct -- the letter
    prompt got no facts block either.
    """
    trusted = "\n".join([work_history_md, *trusted_spans])
    known = frozenset(_normalize(m.group(0)) for m in _FIGURE.finditer(trusted))
    findings: list[Finding] = []
    seen: set[str] = set()

    for match in _FIGURE.finditer(text):
        token = match.group(0)
        key = _normalize(token)
        if key in known or key in seen:
            continue
        seen.add(key)
        findings.append(
            Finding(
                severity=Severity.FATAL,
                code="unverified_total",
                message=(
                    f"{token!r} does not appear in your work history -- a figure "
                    "can only ship if it is stated there verbatim, not computed."
                ),
                artifact=artifact,
                evidence=token,
                source=(
                    "work_history.md or a verified company-research span"
                    if trusted_spans
                    else "work_history.md"
                ),
            )
        )
    return findings


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------


def gate(report: PacketReport, cfg: BlastJobConfig | None = None) -> GateVerdict:
    """Decide whether `report` describes a sendable packet.

    Decision order (see module docstring for the "why"):
      1. Any FATAL finding, in either artifact -> QUARANTINED, naming each
         fatal code.
      2. No FATAL finding, but resume groundedness is below the (uncalibrated)
         floor -> READY, with a reason listing the unsupported claims. A low
         score alone never blocks.
      3. Otherwise -> READY.

    `cfg` is accepted (unused today) to keep this call site stable if a
    per-user floor override is ever added to `BlastJobConfig` -- there is
    none yet, so the floor is always `GROUNDEDNESS_FLOOR_DEFAULT`.
    """
    fatals = report.fatal()
    # A scorer that never ran is told to the user, never assumed clean. It adds a
    # visible reason only: it changes neither the state nor `sendable`, because a
    # missing model call is not evidence of a defect (and groundedness is
    # uncalibrated), but "not checked" must not read as "checked and fine".
    not_checked = _skipped_reasons(report)
    if fatals:
        reasons = tuple(dict.fromkeys(f"{f.code}: {f.message}" for f in fatals))
        return GateVerdict(
            state=PacketState.QUARANTINED,
            reasons=reasons + not_checked,
            quarantined_artifacts=artifacts_to_quarantine(fatals),
        )

    if report.groundedness is not None and report.groundedness < GROUNDEDNESS_FLOOR_DEFAULT:
        return GateVerdict(
            state=PacketState.READY, reasons=(_groundedness_reason(report), *not_checked)
        )

    return GateVerdict(state=PacketState.READY, reasons=not_checked)


_SKIPPED_LABELS = {
    "cover_letter_generation": "The cover letter was requested but could not be generated, so "
    "this packet has no letter.",
    "resume_groundedness": "Resume groundedness was not checked (the scoring call did not run), "
    "so claims in the resume were not verified against your work history.",
    "cover_letter_claims": "The cover letter's claims were not checked (the scoring call did not "
    "run), so its statements about you and the employer are unverified.",
}


def _skipped_reasons(report: PacketReport) -> tuple[str, ...]:
    return tuple(
        _SKIPPED_LABELS.get(name, f"{name} was not checked.") for name in report.checks_skipped
    )


def _groundedness_reason(report: PacketReport) -> str:
    """Human-readable, specific reason for a below-floor resume groundedness
    score -- never a bare number. Lists the unsupported claims when the
    upstream check recorded them as `unsupported_claim` findings on the
    resume (see this module's INTEGRATION PATCH note for how `core/build.py`
    is expected to populate those); otherwise falls back to a reason that
    still names the score, the floor, and what to do about it.
    """
    unsupported = [
        f
        for f in report.findings
        if f.artifact is Artifact.RESUME and f.code == "unsupported_claim"
    ]
    header = (
        f"Groundedness score {report.groundedness} is below the review floor of "
        f"{GROUNDEDNESS_FLOOR_DEFAULT} (uncalibrated -- a prompt to look, not a "
        "block)."
    )
    if not unsupported:
        return f"{header} Review the resume's claims against your work history before sending."

    count = len(unsupported)
    shown = "; ".join(f.message for f in unsupported[:5])
    more = f" (+{count - 5} more)" if count > 5 else ""
    plural = "claim" if count == 1 else "claims"
    return f"{header} {count} {plural} with no support in your work history: {shown}{more}"

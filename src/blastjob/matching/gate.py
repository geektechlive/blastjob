"""Gate + rank: turn a raw alignment score + comp + geo into a review disposition.

Threshold policy: the surface bar is `cfg.alignment_threshold` (shipped default
**78**, see models/config.py) and BAND (5) sizes a visible borderline band below
it. With the default that is:
- alignment >= 78           -> SURFACED (the main list)
- 73 <= alignment < 78      -> QUEUED  (borderline band — visible, catches score wobble)
- alignment < 73            -> not surfaced (stays 'new'; still in DB with its score)
The numbers move with the configured threshold; only the arithmetic is fixed.

Comp policy (provenance-aware — tuned to this market):
- POSTED max total below the floor -> hard reject (not surfaced), even at high
  alignment. This is the ONLY comp reject, because it is the only figure the
  employer actually published.
- INFERRED comp (an LLM read a number out of the JD prose) is NOT verified. A
  sub-floor inferred figure goes to the review queue, never the bin — a model
  misreading "$150k" out of a benefits paragraph must not bury a real role.
- Only USD (or unlabelled "$") comp is compared to the floor; another currency
  surfaces, and so does a minimum-only figure ("From $150k": max unknown).
- comp UNKNOWN or ESTIMATED -> SURFACED (shown with a "comp unverified" badge),
  never hidden. Exec roles almost never post pay, so treating unverified comp as
  a reason to quarantine empties the main list. It's surfaced and flagged, not
  hard-ranked against verified numbers.

Geo policy (three-way — see matching/geo.py, which measures straight-line MILES,
not drive time):
- remote, or a configured home town inside `commute_radius_miles` -> passes.
- an affirmative other-place signal (a US state that isn't the configured home
  state, or a foreign country) with no remote option -> hard reject, the one
  geo reject.
- UNKNOWN (location string unparseable, geo not configured, or a home-state
  town not in the configured town list) -> QUEUED, never dropped.
- inside the extended radius but beyond the normal one -> QUEUED as conditional:
  acceptable only if the hybrid days are light, which a human must confirm.
"""

from __future__ import annotations

from dataclasses import dataclass

from blastjob.models.config import DiscoveryConfig
from blastjob.models.job import (
    VERIFIED_COMP_PROVENANCE,
    CompProvenance,
    GeoStatus,
    ReviewState,
)

BAND = 5  # visible borderline band below the surface threshold


@dataclass
class GateOutcome:
    review_state: ReviewState
    reason: str


def below_floor(comp_max: int | None, comp_currency: str | None, floor: int) -> bool:
    """The one comp-vs-floor rule (gate, compare, funnel). The floor is in
    dollars: a figure in another currency, or an unknown max (0, e.g. "From
    $150,000"), is not comparable to it, so it never counts as below the floor."""
    comparable = (comp_currency or "USD").upper() == "USD"
    return comparable and bool(comp_max) and int(comp_max or 0) < floor


def decide(
    *,
    alignment: int,
    comp_provenance: str,
    comp_max: int,
    geo_status: GeoStatus,
    geo_passes: bool,
    cfg: DiscoveryConfig,
    geo_conditional: bool = False,
    comp_currency: str = "USD",
) -> GateOutcome:
    # 1) Hard rejects that override alignment.
    if not geo_passes and geo_status != GeoStatus.UNKNOWN:
        return GateOutcome(ReviewState.NEW, "outside commute range and not remote")

    # Only a figure the employer actually published may bury a role. An LLM's
    # reading of the JD (INFERRED) is our own guess and gets a human look instead.
    verified = comp_provenance == VERIFIED_COMP_PROVENANCE
    sub_floor = below_floor(comp_max, comp_currency, cfg.comp_floor_usd)
    if verified and sub_floor:
        return GateOutcome(
            ReviewState.NEW, f"posted comp ${comp_max:,} below floor ${cfg.comp_floor_usd:,}"
        )

    # 2) Alignment band (threshold is configurable in Settings).
    surface_min = cfg.alignment_threshold
    borderline_min = surface_min - BAND
    if alignment < borderline_min:
        return GateOutcome(ReviewState.NEW, f"alignment {alignment} below band")

    # 3) Reasons a good-alignment role still needs a human look -> review queue.
    #    NOTE: merely *missing* comp (unknown/estimated) is NOT a queue reason —
    #    exec roles rarely post pay, so those surface (badged) rather than being
    #    quarantined. The one comp queue reason is a sub-floor INFERRED figure:
    #    that is a number we produced, so it needs eyes before it costs a role.
    reasons = []
    if borderline_min <= alignment < surface_min:
        reasons.append(f"borderline alignment {alignment}")
    if comp_provenance == CompProvenance.INFERRED.value and sub_floor:
        reasons.append(
            f"inferred comp ${comp_max:,} below floor ${cfg.comp_floor_usd:,} "
            "— read from the JD, not posted; verify"
        )
    if geo_status == GeoStatus.UNKNOWN:
        reasons.append("location needs confirmation")
    if geo_conditional:
        reasons.append(
            f"beyond the {cfg.commute_radius_miles:g}-mile radius "
            "— OK only if hybrid <=2 days/wk; verify"
        )

    if reasons:
        return GateOutcome(ReviewState.QUEUED, "; ".join(reasons))
    return GateOutcome(ReviewState.SURFACED, f"alignment {alignment}")

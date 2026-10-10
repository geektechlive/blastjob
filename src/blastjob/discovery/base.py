"""Normalized shape every discovery adapter emits.

Adapters know their source's wire format; everything downstream (normalize,
matching, storage) speaks only RawPosting. Keeping this thin means adding a new
board is one small adapter, not a change to the pipeline.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field


class RawPosting(BaseModel):
    """One job as an adapter saw it, lightly normalized. Identity is
    (source, external_id) — never re-derived from company/title/location."""

    source: str  # "greenhouse" | "ashby" | "lever" | "adzuna" | ...
    external_id: str  # the source's own id / slug — stable per posting

    company: str = ""
    title: str = ""
    url: str = ""  # apply/detail URL
    description: str = ""  # plain-text JD (HTML stripped)

    # Location, as structured as the source gives us. Used by the keyless geo
    # filter: an affirmative other-state, non-remote region is auto-"far"
    # with no lookup.
    location_raw: str = ""
    city: str = ""
    region: str = ""  # state / province
    country: str = ""
    remote: bool | None = None
    workplace_type: str = ""  # "", "Remote", "Hybrid", "OnSite"

    # Compensation when the source publishes it (Adzuna, Ashby). Absent -> the
    # matcher infers/queues. `comp_is_estimated` marks predicted figures.
    comp_min: int = 0
    comp_max: int = 0
    comp_currency: str = "USD"
    comp_is_estimated: bool = False
    comp_summary: str = ""  # human string when present, e.g. "$211K – $290K"

    posted_at: str = ""
    extra: dict = Field(default_factory=dict)


class Adapter(Protocol):
    """A discovery source. `fetch` is async and must be side-effect free
    (no DB writes) — the pipeline owns persistence.

    Sync mode. Three shapes, not two — a source's absences are only as strong
    as what it promised to return:

    - FULL snapshot (`snapshot = True`, `stale_after_days = 0`, the default):
      the fetch listed everything currently open on the board, so a posting
      missing from a successful fetch is gone and is stale-marked at once.
      Greenhouse/Lever/Ashby list whole boards this way.
    - WINDOWED snapshot (`snapshot = True`, `stale_after_days = N`): the fetch
      only saw a query- or page-limited WINDOW of the board (Workday caps at
      100 per relevance-ranked searchText; Adzuna reads page 1 only), so one
      run's absence proves nothing. The pipeline ages these out on the TTL
      instead, and confirms the closure against the source's detail endpoint
      when the adapter offers `confirm_open`.
    - INCREMENTAL (`snapshot = False`): alert emails, search-firm sweeps. The
      source returns only what is new; its absences mean nothing, ever, and the
      pipeline must never stale-mark on its behalf.

    Optional closure probe, for windowed sources with a per-posting detail
    endpoint: `confirm_open([(external_id, url), ...]) -> {ids still open}`.
    Anything it cannot disprove must come back as open — a network blip is not
    evidence a role closed."""

    source: str

    async def fetch(self) -> list[RawPosting]: ...


def is_snapshot(adapter: object) -> bool:
    return bool(getattr(adapter, "snapshot", True))


def stale_after_days(adapter: object) -> int:
    """0 = a full-board snapshot (absence is proof); N > 0 = a windowed source
    whose postings age out only after N days unseen."""
    try:
        return max(0, int(getattr(adapter, "stale_after_days", 0) or 0))
    except (TypeError, ValueError):
        return 0

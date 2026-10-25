"""How a dream-list company's job board is read (#90, ADR 0056).

A dream company's board is read like any other, with two limits that keep a large board from
costing a run (see docs/how-it-works/discovery.md):

- Boards that take a search (Workday) are queried by the candidate's title keep-terms
  (`discovery.title_keep_terms`), never by `adzuna_queries`, which are tuned for an aggregator.
  Workday and Dayforce read at most `DREAM_MAX_PAGES` pages and fetch at most
  `DREAM_MAX_POSTINGS` descriptions.
- Boards that list everything in one call (Greenhouse, Lever, Ashby) are wrapped in
  `CappedBoardAdapter`: the title prefilter runs first, THEN the cap, so the cap can never
  drop a leadership role in favour of an engineer.

A capped read is a window, not a full snapshot: a posting missing from one run proves nothing,
so the wrapper ages postings out on a TTL like Workday does.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from blastjob.discovery.base import RawPosting
from blastjob.discovery.workday import STALE_AFTER_DAYS, title_wanted

DREAM_MAX_PAGES = 3
DREAM_MAX_POSTINGS = 100
DREAM_MAX_QUERIES = 8


class CappedBoardAdapter:
    """Reads a whole board through `inner`, keeps the postings whose title the prefilter wants,
    and takes at most `max_postings` of them."""

    snapshot = True
    stale_after_days = STALE_AFTER_DAYS

    def __init__(
        self,
        inner: Any,
        keep_terms: Sequence[str],
        drop_terms: Sequence[str],
        max_postings: int = DREAM_MAX_POSTINGS,
    ) -> None:
        self.inner = inner
        self.source: str = inner.source
        self.keep_terms = list(keep_terms)
        self.drop_terms = list(drop_terms)
        self.max_postings = max_postings

    async def fetch(self) -> list[RawPosting]:
        postings: list[RawPosting] = await self.inner.fetch()
        wanted = [p for p in postings if title_wanted(p.title, self.keep_terms, self.drop_terms)]
        return wanted[: self.max_postings]


def dream_queries(keep_terms: Sequence[str]) -> list[str]:
    """The search queries for a dream board that takes them: the keep-terms, bounded."""
    return [t for t in keep_terms if t.strip()][:DREAM_MAX_QUERIES]

"""Which job rows get the dream-list geo exception (#90, ADR 0056).

One helper, shared by matching, `/funnel`, the All page and the weekly Dream list, so they
cannot disagree. A row is eligible only when ALL of these hold:

- its company is on the dream tier now (`company_key()`), and
- it came from a configured `discovery.target_boards` entry for that company, and
- that entry carries an identity basis (the board was proven to be the company's).

The one other way in (#91, ADR 0066): a job with a CURRENT hit from an enabled dream category
(`category_store.current_dream_job_ids`). The exception is then about the category, not the
employer, so the company and source do not matter. It still only keeps a role whose place is
known to be far (`geo.is_dream_bypass`), so an unknown place is never helped.

A company NAME alone is not enough: an aggregator, alert-mail, Apple or search-firm row can
carry any name. Those rows are rejected on location as before. The Dream badge is separate and
stays name-based (current membership).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from blastjob.core import category_store
from blastjob.core.company import company_key
from blastjob.core.target_store import dream_keys
from blastjob.discovery import ashby, dayforce, greenhouse, lever, workday
from blastjob.models.config import DiscoveryConfig, TargetBoard


def board_source(board: TargetBoard) -> str:
    """The `jobs.source` value rows from this board carry; "" when the token is unusable."""
    ats, token = board.ats.lower(), board.token
    try:
        if ats == "greenhouse":
            return greenhouse.source_for(token)
        if ats == "lever":
            return lever.source_for(token)
        if ats == "ashby":
            return ashby.source_for(token)
        if ats == "workday":
            return workday.source_for(workday.parse_board_token(token))
        if ats == "dayforce":
            return dayforce.source_for(dayforce.parse_board_token(token))
    except ValueError:
        return ""
    return ""


@dataclass(frozen=True)
class DreamRule:
    dreams: frozenset[str]
    proven: frozenset[tuple[str, str]]  # (source, company_key) of proven boards
    category_jobs: frozenset[int] = frozenset()  # jobs with a current dream-category hit

    @classmethod
    def load(cls, conn: sqlite3.Connection, disc: DiscoveryConfig) -> DreamRule:
        proven = {
            (src, company_key(b.company))
            for b in disc.target_boards
            if b.identity_basis and (src := board_source(b))
        }
        return cls(dream_keys(conn), frozenset(proven), category_store.current_dream_job_ids(conn))

    def applies(self, company: str, source: str, job_id: int | None = None) -> bool:
        if job_id is not None and job_id in self.category_jobs:
            return True
        key = company_key(company or "")
        return key in self.dreams and (source or "", key) in self.proven

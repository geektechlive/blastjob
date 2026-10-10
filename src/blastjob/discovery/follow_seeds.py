"""Companies the user follows on LinkedIn that the pipeline has never seen.

A real export carried over a hundred company follows and most of them had
produced no job at all -- the user told LinkedIn they care about these
employers and the cockpit had never shown a single opening from any of them.
That is the cheapest coverage expansion available: the intent is already
recorded, it just was not connected to anything.

Deliberately read-only with respect to config. It presents boards to add; a
board token is exactly the thing that must not be written unverified, because
a live token once turned out to point at an unrelated, same-named company in
another state, and nothing noticed for a week. Verify a hit by the LOCATION of
a sample posting, never by the name string.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from blastjob.core.company import company_key
from blastjob.models.config import BlastJobConfig

# Follows that are not employers the user could work for. Media, communities and
# professional bodies dominate a long-lived LinkedIn account's follow list, and
# probing them burns Jina calls for nothing.
_NOT_AN_EMPLOYER = re.compile(
    r"\b(magazine|journal|news|media|podcast|conference|summit|expo|community|"
    r"association|society|institute|foundation|chapter|network(ing)?|meetup|"
    r"alumni|university|college|school|academy|club|legion|fandom|"
    r"group on|jobs?|careers?|hiring|recruit\w*|staffing|talent solutions|"
    r"methodolog\w*|certification|training|coaching|newsletter|blog)\b",
    re.I,
)

# A follow that is a concept, a hashtag or a stray character rather than a company.
_NOT_A_COMPANY_NAME = re.compile(r"^[^A-Za-z]*$|^.{1,2}$")


@dataclass
class FollowSeed:
    company: str
    company_key: str
    followed_at: str
    likely_employer: bool
    note: str


def unresolved_follows(conn: sqlite3.Connection, cfg: BlastJobConfig) -> list[FollowSeed]:
    """Followed companies with no job in the database and no configured board."""
    configured = {company_key(b.company) for b in cfg.discovery.target_boards}
    seen = {
        company_key(r["company"] or "") for r in conn.execute("SELECT DISTINCT company FROM jobs")
    }

    out: list[FollowSeed] = []
    for row in conn.execute(
        "SELECT company, company_key, followed_at FROM company_follows ORDER BY company"
    ):
        name = (row["company"] or "").strip()
        if not name:
            continue
        key = row["company_key"] or company_key(name)
        if key in configured:
            continue
        if key in seen:
            continue
        bad = _NOT_AN_EMPLOYER.search(name)
        note = f"looks like {bad.group(0).lower()}, not an employer" if bad else ""
        if not note and _NOT_A_COMPANY_NAME.match(name):
            note = "not a company name"
        out.append(
            FollowSeed(
                company=name,
                company_key=key,
                # LinkedIn's export writes follow dates as "Tue Feb 14" with no
                # year, so this is shown verbatim rather than pretending to be
                # an ISO date the data does not contain.
                followed_at=(row["followed_at"] or "").strip(),
                likely_employer=not note,
                note=note,
            )
        )
    # Employers first, then alphabetical, so the probe list is the useful half.
    out.sort(key=lambda s: (not s.likely_employer, s.company.lower()))
    return out


def summary(seeds: list[FollowSeed]) -> dict[str, int]:
    employers = sum(1 for s in seeds if s.likely_employer)
    return {"total": len(seeds), "employers": employers, "filtered": len(seeds) - employers}

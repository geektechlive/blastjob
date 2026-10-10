"""Resolve a company name to its public ATS board token, if it has one.

Greenhouse/Lever/Ashby board tokens are usually a slugified company name, but not
always — so we probe a few slug variants against each ATS and keep whatever
actually returns jobs.

Workday tenants can't be guessed (tenant, wdN shard and site slug are all
arbitrary: "mcw/wd503/ExternalCareers"), so `probe_workday` asks Jina search for
`site:myworkdayjobs.com <company>`, extracts every (tenant, wd, site) triple
from the result URLs, and confirms each with one listing POST.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from dataclasses import dataclass

import httpx

from blastjob.discovery.dayforce import BoardRef as DayforceRef
from blastjob.discovery.dayforce import count_postings
from blastjob.discovery.http import USER_AGENT
from blastjob.discovery.workday import BoardRef as WorkdayRef
from blastjob.secrets import load_secrets_env, secrets_file_hint

_WORKDAY_URL = re.compile(
    r"https?://([a-z0-9\-]+)\.(wd\d+)\.myworkdayjobs\.com/"
    r"(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_\-]+)(?=[/\"'\s?#)]|$)"
)
# Path segments that are never a site slug.
_NOT_SITES = {"job", "wday", "login", "en-US", "en-us", "es", "fr-CA", "search"}

# jobs.dayforcehcm.com/{clientNamespace}/{jobBoardCode}[/...]; client slots that
# are API/infra paths, not tenants, are excluded so a search hit on the API host
# itself never gets treated as a candidate board.
_DAYFORCE_URL = re.compile(
    r"https?://jobs\.dayforcehcm\.com/([a-z0-9\-]+)/([A-Za-z0-9_\-]+)(?=[/\"'\s?#)]|$)"
)
_DAYFORCE_NOT_CLIENTS = {"api", "jobs", "login", "auth"}
_DAYFORCE_NOT_BOARDS = {"api", "jobs", "job"}

# Legacy per-tenant redirect origin: usNNN.dayforcehcm.com/CandidatePortal/en-US/{client}.
# This form never carries a jobBoardCode, so it always resolves to the default
# CANDIDATEPORTAL board.
_DAYFORCE_LEGACY = re.compile(
    r"https?://[a-z0-9]+\.dayforcehcm\.com/CandidatePortal/[a-z]{2}-[A-Z]{2}/([a-z0-9]+)"
)

_STOP = re.compile(r"\b(inc|llc|corp|corporation|co|company|group|holdings|ltd)\b", re.I)


@dataclass
class BoardHit:
    company: str
    ats: str
    token: str
    job_count: int


def slug_variants(name: str) -> list[str]:
    base = _STOP.sub("", name).strip()
    low = base.lower()
    compact = re.sub(r"[^a-z0-9]", "", low)
    hyphen = re.sub(r"[^a-z0-9]+", "-", low).strip("-")
    variants = [compact, hyphen, low.replace(" ", "")]
    # de-dupe, keep order, drop empties
    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _count(url: str, key: str) -> int | None:
    """Return job count if the board exists and returns the expected shape, else None."""
    try:
        r = httpx.get(url, timeout=10, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
            return None
        data = r.json()
        if key == "":  # lever returns a bare list
            return len(data) if isinstance(data, list) else None
        items = data.get(key)
        return len(items) if isinstance(items, list) else None
    except Exception:
        return None


def workday_candidates(text: str) -> list[WorkdayRef]:
    """Every distinct (tenant, wd, site) referenced in `text` (search results,
    a careers page), in first-seen order. Pure; fixture-tested."""
    seen: set[tuple[str, str, str]] = set()
    out: list[WorkdayRef] = []
    for tenant, wd, site in _WORKDAY_URL.findall(text):
        if site in _NOT_SITES:
            continue
        key = (tenant, wd, site)
        if key in seen:
            continue
        seen.add(key)
        out.append(WorkdayRef(tenant=tenant, wd=wd, site=site))
    return out


def _workday_count(ref: WorkdayRef, search_text: str = "director") -> int | None:
    try:
        r = httpx.post(
            ref.jobs_url,
            json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": search_text},
            timeout=10,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        if r.status_code != 200:
            return None
        total = r.json().get("total")
        return int(total) if isinstance(total, int) else None
    except Exception:
        return None


def probe_workday(name: str, jina_api_key: str) -> list[BoardHit]:
    """Find a company's Workday board(s) via Jina search + a live confirmation.
    Returns confirmed boards with their 'director' match count (0 is still a
    valid board). Empty when the company isn't on Workday or search found nothing."""
    try:
        r = httpx.get(
            "https://s.jina.ai/",
            params={"q": f"{name} careers site:myworkdayjobs.com"},
            headers={
                "Authorization": f"Bearer {jina_api_key}",
                "Accept": "application/json",
                "X-Respond-With": "no-content",
            },
            timeout=40,
        )
        text = r.text
    except Exception:
        return []
    hits: list[BoardHit] = []
    for ref in workday_candidates(text)[:6]:
        n = _workday_count(ref)
        if n is not None:
            hits.append(BoardHit(name, "workday", ref.token, n))
    return hits


def probe_company(name: str) -> list[BoardHit]:
    """Probe Greenhouse/Lever/Ashby for a company. Returns every board that
    responded with jobs (usually zero or one)."""
    hits: list[BoardHit] = []
    for slug in slug_variants(name):
        gh = _count(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", "jobs")
        if gh:
            hits.append(BoardHit(name, "greenhouse", slug, gh))
        lv = _count(f"https://api.lever.co/v0/postings/{slug}?mode=json", "")
        if lv:
            hits.append(BoardHit(name, "lever", slug, lv))
        ash = _count(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", "jobs")
        if ash:
            hits.append(BoardHit(name, "ashby", slug, ash))
        if hits:  # first slug that resolves is enough
            break
    return hits


def dayforce_candidates(text: str) -> list[DayforceRef]:
    """Every distinct (client, board) referenced in `text` (search results, a
    careers page), in first-seen order. Covers both the current
    jobs.dayforcehcm.com/{client}/{board} form and the legacy per-tenant
    usNNN.dayforcehcm.com/CandidatePortal/{culture}/{client} redirect origin,
    which always implies the default CANDIDATEPORTAL board. Pure; fixture-tested."""
    found: list[tuple[int, str, str]] = []
    for m in _DAYFORCE_URL.finditer(text):
        client, board = m.group(1), m.group(2)
        if client in _DAYFORCE_NOT_CLIENTS or board in _DAYFORCE_NOT_BOARDS:
            continue
        found.append((m.start(), client, board))
    for m in _DAYFORCE_LEGACY.finditer(text):
        found.append((m.start(), m.group(1), "CANDIDATEPORTAL"))
    found.sort(key=lambda t: t[0])
    seen: set[tuple[str, str]] = set()
    out: list[DayforceRef] = []
    for _, client, board in found:
        key = (client, board)
        if key in seen:
            continue
        seen.add(key)
        out.append(DayforceRef(client=client, board=board))
    return out


def probe_dayforce(name: str, jina_api_key: str) -> list[BoardHit]:
    """Find a company's Dayforce board(s) via two Jina searches + a live
    confirmation. Returns confirmed boards with their total posting count (0 is
    still a valid board). Empty when the company isn't on Dayforce or search
    found nothing."""
    texts: list[str] = []
    for query in (
        f'"{name}" site:jobs.dayforcehcm.com',
        f'"{name}" careers dayforcehcm.com',
    ):
        try:
            r = httpx.get(
                "https://s.jina.ai/",
                params={"q": query},
                headers={
                    "Authorization": f"Bearer {jina_api_key}",
                    "Accept": "application/json",
                    "X-Respond-With": "no-content",
                },
                timeout=40,
            )
            texts.append(r.text)
        except Exception:
            continue
    hits: list[BoardHit] = []
    seen: set[tuple[str, str]] = set()
    candidates: list[DayforceRef] = []
    for text in texts:
        for ref in dayforce_candidates(text):
            key = (ref.client, ref.board)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(ref)
    for ref in candidates[:6]:
        n = asyncio.run(count_postings(ref))
        if n is not None:
            hits.append(BoardHit(name, "dayforce", ref.token, n))
    return hits


def main(argv: list[str] | None = None) -> int:
    """CLI: probe Workday, Dayforce, and Greenhouse/Lever/Ashby for each company
    name given, printing one line per confirmed board (or a not-found line)."""
    names = list(argv if argv is not None else sys.argv[1:])
    if not names:
        print("usage: python -m blastjob.discovery.resolve <company> [<company> ...]")
        return 1
    load_secrets_env()
    jina_api_key = os.environ.get("JINA_API_KEY", "")
    if not jina_api_key:
        print(
            "note: JINA_API_KEY is not set, so Workday and Dayforce boards are NOT probed"
            f" (only Greenhouse/Lever/Ashby). Add it to {secrets_file_hint()} to probe them.",
            file=sys.stderr,
        )
    for name in names:
        hits: list[BoardHit] = []
        if jina_api_key:
            hits.extend(probe_workday(name, jina_api_key))
            hits.extend(probe_dayforce(name, jina_api_key))
        hits.extend(probe_company(name))
        if not hits:
            print(f"{name}\t-\tno boards found")
            continue
        for hit in hits:
            print(f"{hit.company}\t{hit.ats}\t{hit.token}\t{hit.job_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

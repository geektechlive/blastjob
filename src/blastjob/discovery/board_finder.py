"""Find the job board of a dream-list company, with the evidence that it is theirs (#90).

`find_boards` runs the existing resolve probes (Greenhouse, Lever, Ashby by slug; Workday and
Dayforce through a Jina search) and, for each board found, gathers identity evidence. A
board is only ever SAVED with an identity basis (ADR 0056), because a same-named company can
own the slug a probe guesses, and the geo exception would then apply to its postings:

- `domain_link`: the company's own careers page, on the domain the owner entered, links to
  the board (read through Jina).
- `ats_name`: the ATS reports an organization name that names the company as a whole phrase
  (`research_match.names_company`). Only Greenhouse reports one.
- no basis: the owner sees the evidence and must tick that the board belongs to the company
  (`owner_confirmed`, decided in the route, never here).

Sample postings are shown for the owner's judgement; they are never evidence by themselves.

Everything here is blocking (httpx, and Dayforce's `asyncio.run`), so the web route runs the
whole function in a worker thread (`web/routes_boards.py`). Nothing is added to settings by
this module, and it never contacts LinkedIn or scrapes a board it cannot read through an
ATS's own public API. The five network leaves are `resolve.probe_*`, `fetch_samples` and
`read_careers_page`; tests patch them (tests/conftest.py::_no_real_board_finder).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from pydantic import BaseModel, ValidationError, field_validator

from blastjob.core.company import company_key
from blastjob.core.employer_domains import normalize_domain
from blastjob.core.jina import READER_URL
from blastjob.discovery import resolve
from blastjob.discovery.http import USER_AGENT
from blastjob.discovery.resolve import BoardHit, dayforce_candidates, workday_candidates
from blastjob.discovery.workday import BoardRef as WorkdayRef

log = logging.getLogger(__name__)

TIMEOUT_S = 10
READER_TIMEOUT_S = 40
MAX_SAMPLES = 2
MAX_FIELD_CHARS = 120
MAX_CAREERS_CHARS = 200_000
CAREERS_PATHS = ("/careers", "/jobs")

DOMAIN_LINK = "domain_link"
ATS_NAME = "ats_name"
NO_BASIS = ""

_GH_BOARD = "https://boards-api.greenhouse.io/v1/boards/{token}"


@dataclass(frozen=True)
class Sample:
    title: str
    location: str


@dataclass(frozen=True)
class Candidate:
    company: str
    ats: str
    token: str
    job_count: int
    samples: tuple[Sample, ...] = ()
    ats_name: str = ""
    identity_basis: str = NO_BASIS


@dataclass(frozen=True)
class Findings:
    """One search's result. `notes` are coverage gaps: a search that did not look somewhere is
    never an all-clear for it."""

    company: str
    candidates: tuple[Candidate, ...] = ()
    notes: tuple[str, ...] = ()


class _Posting(BaseModel):
    """What a sample needs of a board's posting; anything else on it is ignored."""

    title: str = ""
    location: str = ""

    @field_validator("title", "location", mode="before")
    @classmethod
    def _text(cls, value: object) -> str:
        return " ".join(str(value or "").split())[:MAX_FIELD_CHARS]


def _samples(items: list[object], title_key: str, loc: Callable[[dict], object]) -> list[Sample]:
    out: list[Sample] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            p = _Posting.model_validate({"title": item.get(title_key), "location": loc(item)})
        except ValidationError:
            continue
        if p.title:
            out.append(Sample(p.title, p.location))
        if len(out) >= MAX_SAMPLES:
            break
    return out


# ------------------------------------------------------------------ identity evidence


def _board_url_pattern(ats: str, token: str) -> re.Pattern[str] | None:
    t = re.escape(token)
    # Anchored to the URL's host: a board URL inside another site's query string is no link.
    scheme = r"https?://(?:[\w-]+\.)*"
    patterns = {
        "greenhouse": rf"{scheme}greenhouse\.io/(?:embed/job_board\?for=|v1/boards/)?{t}(?![\w-])",
        "lever": rf"{scheme}jobs\.lever\.co/{t}(?![\w-])",
        "ashby": rf"{scheme}jobs\.ashbyhq\.com/{t}(?![\w-])",
    }
    pattern = patterns.get(ats)
    return re.compile(pattern, re.IGNORECASE) if pattern else None


def links_to_board(text: str, ats: str, token: str) -> bool:
    """Does `text` (a careers page as the reader returned it) link to this exact board?"""
    want = token.strip().lower()
    if ats == "workday":
        return any(r.token.lower() == want for r in workday_candidates(text))
    if ats == "dayforce":
        return any(r.token.lower() == want for r in dayforce_candidates(text))
    pattern = _board_url_pattern(ats, token.strip())
    return bool(pattern and pattern.search(text))


def _ats_basis(company: str, ats_name: str) -> str:
    """Evidence only when the reported name IS the company's name (same `company_key`).
    A name that merely contains it ("Acme Staffing" for "Acme") is shown, not trusted."""
    same = bool(ats_name) and company_key(ats_name) == company_key(company)
    return ATS_NAME if same else NO_BASIS


def _basis(company: str, hit: BoardHit, careers: str, ats_name: str) -> str:
    if careers and links_to_board(careers, hit.ats, hit.token):
        return DOMAIN_LINK
    return _ats_basis(company, ats_name)


# ------------------------------------------------------------------ network leaves


def _get_json(url: str) -> object:
    r = httpx.get(url, timeout=TIMEOUT_S, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    return r.json()


def _greenhouse(token: str) -> tuple[list[Sample], str]:
    data = _get_json(_GH_BOARD.format(token=token) + "/jobs")
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    samples = _samples(jobs, "title", lambda j: (j.get("location") or {}).get("name"))
    try:
        board = _get_json(_GH_BOARD.format(token=token))
    except (httpx.HTTPError, ValueError):
        return samples, ""
    name = board.get("name") if isinstance(board, dict) else ""
    return samples, " ".join(str(name or "").split())[:MAX_FIELD_CHARS]


def _lever(token: str) -> tuple[list[Sample], str]:
    data = _get_json(f"https://api.lever.co/v0/postings/{token}?mode=json")
    items = data if isinstance(data, list) else []
    return _samples(items, "text", lambda j: (j.get("categories") or {}).get("location")), ""


def _ashby(token: str) -> tuple[list[Sample], str]:
    data = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    return _samples(jobs, "title", lambda j: j.get("location")), ""


def _workday(token: str) -> tuple[list[Sample], str]:
    parts = token.split("/")
    if len(parts) != 3:
        return [], ""
    ref = WorkdayRef(tenant=parts[0], wd=parts[1], site=parts[2])
    r = httpx.post(
        ref.jobs_url,
        json={"appliedFacets": {}, "limit": MAX_SAMPLES, "offset": 0, "searchText": ""},
        timeout=TIMEOUT_S,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    r.raise_for_status()
    data = r.json()
    items = data.get("jobPostings", []) if isinstance(data, dict) else []
    return _samples(items, "title", lambda j: j.get("locationsText")), ""


_SAMPLERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby, "workday": _workday}


def fetch_samples(hit: BoardHit) -> tuple[tuple[Sample, ...], str]:
    """(up to two sample postings, the ATS-reported organization name or ""). Best effort: a
    board that cannot be read again returns nothing, and the owner is told there is no sample.
    Dayforce has no cheap public listing, so it has none."""
    sampler = _SAMPLERS.get(hit.ats)
    if sampler is None:
        return (), ""
    try:
        samples, name = sampler(hit.token)
    except (httpx.HTTPError, ValueError) as e:
        log.info("board samples failed for %s %s (%s)", hit.ats, hit.token, type(e).__name__)
        return (), ""
    return tuple(samples), name


def read_careers_page(domain: str, key: str) -> str:
    """The company's careers pages on the owner-entered domain as the Jina reader returns them
    (markdown, with link targets), concatenated. "" when nothing could be read."""
    try:
        host = normalize_domain(domain)
    except ValueError:
        return ""
    urls = [f"https://{host}{p}" for p in CAREERS_PATHS] + [f"https://careers.{host}"]
    chunks: list[str] = []
    for url in urls:
        try:
            r = httpx.get(
                f"{READER_URL}{url}",
                headers={"Authorization": f"Bearer {key}", "Accept": "text/plain"},
                timeout=READER_TIMEOUT_S,
            )
        except httpx.HTTPError:
            continue
        if r.status_code == 200:
            chunks.append(r.text)
    return "\n".join(chunks)[:MAX_CAREERS_CHARS]


# ------------------------------------------------------------------ the search


def _probe(label: str, notes: list[str], call: Callable[[], list[BoardHit]]) -> list[BoardHit]:
    try:
        return call()
    except Exception as e:  # noqa: BLE001 - one failed probe must not lose the others
        log.warning("board probe %s failed (%s)", label, type(e).__name__)
        notes.append(f"The {label} search failed ({type(e).__name__}); it was not checked.")
        return []


def find_boards(company: str, domain: str, jina_key: str) -> Findings:
    """Every supported board that answers for `company`, each with its identity evidence."""
    notes: list[str] = []
    hits: list[BoardHit] = []
    if jina_key:
        hits += _probe("Workday", notes, lambda: resolve.probe_workday(company, jina_key))
        hits += _probe("Dayforce", notes, lambda: resolve.probe_dayforce(company, jina_key))
    else:
        notes.append(
            "No Jina key is set, so Workday and Dayforce boards were not searched. "
            "Greenhouse, Lever and Ashby were."
        )
    hits += _probe("Greenhouse, Lever and Ashby", notes, lambda: resolve.probe_company(company))

    careers = ""
    if domain and jina_key:
        careers = read_careers_page(domain, jina_key)
        if not careers:
            notes.append(f"The careers page on {domain} could not be read, so no link was checked.")
    elif not domain:
        notes.append(
            "No website was entered for this company, so a link from its own careers page "
            "could not be checked."
        )

    out: list[Candidate] = []
    seen: set[tuple[str, str]] = set()
    for hit in hits:
        ident = (hit.ats, hit.token.lower())
        if ident in seen:
            continue
        seen.add(ident)
        samples, ats_name = fetch_samples(hit)
        out.append(
            Candidate(
                company=company,
                ats=hit.ats,
                token=hit.token,
                job_count=hit.job_count,
                samples=samples,
                ats_name=ats_name,
                identity_basis=_basis(company, hit, careers, ats_name),
            )
        )
    return Findings(company=company, candidates=tuple(out), notes=tuple(notes))

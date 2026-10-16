"""LinkedIn guest pages read through Jina's reader: the full JD, posted pay, and
the company of a whole-card alert (moved out of mailalerts.py, which re-exports
these names).

A plain GET of linkedin.com returns an auth wall to bots; `r.jina.ai` reads the
public guest page. Fail-soft per posting: the alert snippet stays otherwise.
"""

from __future__ import annotations

import logging
import re

import httpx

from blastjob.comp import parse_salary_range
from blastjob.discovery.base import RawPosting
from blastjob.discovery.linkedin_cards import SPLIT_KNOWN, guest_page_company, split_card

log = logging.getLogger(__name__)

JINA_KEY_ENV = "JINA_API_KEY"
_JINA_READER = "https://r.jina.ai/"
# Guest job pages end with these blocks; the JD is everything before.
_LI_TAIL = re.compile(
    r"\n(?:Seniority level|Referrals increase your chances|Set alert for similar jobs|"
    r"Similar jobs|Sign in to set job alerts|People also viewed|Looking for a job\?)",
    re.I,
)
_LI_PAY = re.compile(r"Base pay range\s*\n?\s*(\$[\d,\.]+(?:/yr)?\s*-\s*\$[\d,\.]+(?:/yr)?)", re.I)
_LI_APPLICANTS = re.compile(r"\n([^\n]*applicants?)\s*\n", re.I)

# Employer-authored pay range written straight into the JD body (no LinkedIn
# "Base pay range" widget at all) — e.g. "Acme Pay Range\n$165,000.00 -
# $190,000.00*". Requires a heading naming the employer directly; a bare
# "Salary Range"/"Base Salary" heading only counts when the employer's name
# also appears nearby, so we don't credit an unrelated heading to this job.
_EMPLOYER_PAY_HEADING = re.compile(
    r"\n[ \t]*([A-Za-z][A-Za-z0-9&.,'\-]*(?:[ \t]+[A-Za-z0-9&.,'\-]+){0,4})[ \t]*Pay Range[ \t]*\n"
)
_GENERIC_PAY_HEADING = re.compile(r"\n[ \t]*(?:Salary Range|Base Salary)[ \t]*\n", re.I)
# A range attributed to a third party (aggregator/platform), not the employer
# itself — must never be read as POSTED, even when it sits in the JD body.
_AGGREGATOR_MARK = re.compile(r"provided by|provided pay range|estimated by|\bestimate\b", re.I)
_HOURLY_MARK = re.compile(r"/\s*hr\b|per\s+hour|hourly", re.I)
_MONEY_SPAN = re.compile(r"\$[\d,]+(?:\.\d+)?\*?\s*(?:-|to)\s*\$[\d,]+(?:\.\d+)?\*?", re.I)
# Zero-width / bidi formatting marks some ATS pages sprinkle around headings.
_INVISIBLE_MARKS = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")


def _strip_invisible(text: str) -> str:
    return _INVISIBLE_MARKS.sub("", text)


def _normalize_company(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").casefold())


def _employer_body_pay(body: str, company: str) -> tuple[str, str]:
    """(pay text, matched source span) for an employer-authored pay range
    found directly in the JD body -- POSTED-eligible. Returns ("", "") when no
    qualifying range exists: no recognizable employer label, a label naming a
    platform/agency instead of the employer, an hourly-looking figure, an
    ambiguous magnitude (parse_salary_range's own $10k-floor rule), or more
    than one distinct qualifying range (picking one would be a guess)."""
    text = _strip_invisible(body)
    norm_company = _normalize_company(company)
    if not norm_company:
        return "", ""
    candidates: list[str] = []

    for m in _EMPLOYER_PAY_HEADING.finditer(text):
        tail = text[m.end() : m.end() + 120]
        money = _MONEY_SPAN.search(tail)
        if not money:
            continue
        span = text[m.start() : m.end() + money.end()]
        head = text[max(0, m.start() - 200) : m.start()]
        if (
            _AGGREGATOR_MARK.search(head)
            or _AGGREGATOR_MARK.search(span)
            or _HOURLY_MARK.search(span)
        ):
            continue
        norm_label = _normalize_company(m.group(1))
        if norm_label in norm_company or norm_company in norm_label:
            candidates.append(span.strip())

    for m in _GENERIC_PAY_HEADING.finditer(text):
        tail = text[m.end() : m.end() + 120]
        money = _MONEY_SPAN.search(tail)
        if not money:
            continue
        span = text[m.start() : m.end() + money.end()]
        head = text[max(0, m.start() - 200) : m.start()]
        if (
            _AGGREGATOR_MARK.search(head)
            or _AGGREGATOR_MARK.search(span)
            or _HOURLY_MARK.search(span)
        ):
            continue
        if norm_company in _normalize_company(head):
            candidates.append(span.strip())

    if len(candidates) != 1:
        return "", ""
    span = candidates[0]
    lo, hi, _cur = parse_salary_range(span)
    if not (lo and hi):
        return "", ""
    return f"${lo:,}-${hi:,}", span


def parse_linkedin_guest_page(text: str, company: str = "") -> tuple[str, str, str]:
    """(job description, pay text, matched pay source span) from the reader's
    plain text of a public LinkedIn job page. Empty JD when the page is an
    auth wall or has no body.

    Pay resolution order: LinkedIn's own structured "Base pay range" widget
    first (unchanged -- whoever it's attributed to, that provenance call is
    out of scope here; `span` is left empty for this path, matching prior
    behavior); when that widget is absent, fall back to an employer-authored
    range written straight into the JD body (`_employer_body_pay`), which
    does carry a source span."""
    if not text:
        return "", "", ""
    has_body_marker = bool(
        re.search(r"\n\s*(?:Description|About the job)\s*\n", text) or _LI_APPLICANTS.search(text)
    )
    if not has_body_marker and "Sign in" in text[:600] and "Join now" in text[:600]:
        return "", "", ""  # auth wall: header chrome only
    pay = ""
    span = ""
    m = _LI_PAY.search(text)
    if m:
        pay = m.group(1).replace("/yr", "").replace(" ", "")
    # The body starts at the "Description" heading when present, else after the
    # applicants line (title/company/meta block).
    start = 0
    d = re.search(r"\n\s*(?:Description|About the job)\s*\n", text)
    if d:
        start = d.end()
    else:
        a = _LI_APPLICANTS.search(text)
        if a:
            start = a.end()
    body = text[start:]
    t = _LI_TAIL.search(body)
    if t:
        body = body[: t.start()]
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if len(body) < 300:
        return "", pay, span
    if not pay:
        pay, span = _employer_body_pay(body, company)
    return body, pay, span


def _apply_guest_company(p: RawPosting, page_text: str) -> None:
    """An unsplit whole card (company still empty) takes its company from the
    guest page header, which confirms it structurally (`guest_page_company`)."""
    head = p.extra.get("card_head") if not p.company else None
    if not head:
        return
    company = guest_page_company(page_text, str(head))
    if not company:
        return
    card = split_card(f"{head} · {p.location_raw}", [company])
    if card.basis != SPLIT_KNOWN:
        return
    p.title, p.company = card.title, card.company
    if card.remote:
        p.remote = True
    p.extra.pop("card_head", None)


async def enrich_linkedin_via_jina(
    postings: list[RawPosting], api_key: str, *, timeout_s: int = 60, concurrency: int = 3
) -> int:
    """Replace alert snippets with the full JD (and posted pay) for LinkedIn
    postings, reading the public job page through Jina's reader (a plain GET
    of linkedin.com returns an auth wall to bots). Fail-soft per posting."""
    import asyncio

    targets = [p for p in postings if p.source == "linkedin" and p.url]
    if not targets or not api_key:
        return 0
    sem = asyncio.Semaphore(concurrency)
    upgraded = 0

    async def one(client: httpx.AsyncClient, p: RawPosting) -> None:
        nonlocal upgraded
        async with sem:
            try:
                r = await client.get(
                    f"{_JINA_READER}{p.url}",
                    headers={"Authorization": f"Bearer {api_key}", "X-Return-Format": "text"},
                )
                if r.status_code != 200:
                    return
                _apply_guest_company(p, r.text)
                jd, pay, span = parse_linkedin_guest_page(r.text, p.company)
            except Exception as e:  # noqa: BLE001 — keep the snippet
                log.debug("linkedin jina enrich failed for %s: %s", p.url, e)
                return
        if jd and len(jd) > len(p.description):
            p.description = jd
            upgraded += 1
        if pay:
            lo, hi, cur = parse_salary_range(pay)
            if lo or hi:
                p.comp_min, p.comp_max, p.comp_currency = lo, hi, cur
                # The matched source span, when we have one (body-scan path
                # only), is more traceable than the normalized "$lo-$hi"
                # string -- carries the literal heading + amount text. Not
                # persisted by normalize.py today (comp_summary has no DB
                # column); kept here so it's at least available in-process.
                p.comp_summary = span or pay

    async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
        await asyncio.gather(*(one(client, p) for p in targets))
    return upgraded

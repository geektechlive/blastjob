"""Review-site summaries kept as verbatim evidence (issue #108, ADR 0069).

A search result from Glassdoor, Indeed or Comparably carries a short description such
as "3.9 out of 5 from 120 reviews". This module turns those results into evidence
items in plain Python: the model never sees them and never writes about them, and the
pages are never read. An item is a verbatim record of what a site's summary said, not a
profile fact (docs/truth-guarantees.md). Truth rules:

- the URL must be a plain https URL on an allowlisted registrable domain (or a subdomain);
- the company is named as a whole name AND the job's location sits near that mention (or
  the employer's own domain appears), else the result is dropped; a result that fails this
  is still admitted when another allowlisted result of the SAME site and SAME site employer
  id passes it (a Glassdoor E-id, an Indeed or Comparably company slug);
- no "N out of 5" rating in the snippet, or one outside 0..5, means no item; the kind is
  "overall" only on explicit overall wording, else "culture" or "other".
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from blastjob.core import research_match
from blastjob.core.page_fetch import host_of
from blastjob.core.research_facts import strip_instructions

SNIPPET_CHARS = 400
MAX_ITEMS = 6
CULTURE_LOOKBEHIND = 40
OVERALL_LOOKBEHIND = 60
OVERALL_LOOKAHEAD = 60
SUBRATING_WINDOW = 40
MAX_RATING = 5.0
MAX_REVIEW_COUNT = 10_000_000
HTTPS_PORT = 443

_GLASSDOOR = frozenset(
    f"glassdoor.{tld}" for tld in ("com", "ca", "co.uk", "co.in", "com.au", "ie", "sg", "de", "fr")
)
_INDEED = frozenset({"indeed.com"})
_COMPARABLY = frozenset({"comparably.com"})
# (site label, domains, URL path must start with, site employer id pattern on the path)
_SITES = (
    ("Glassdoor", _GLASSDOOR, "", re.compile(r"(?:-E|_IE)(\d+)")),
    ("Indeed", _INDEED, "/cmp/", re.compile(r"^/cmp/([^/?#]+)")),
    ("Comparably", _COMPARABLY, "", re.compile(r"^/companies/([^/?#]+)")),
)
_RATING = re.compile(r"(?<![\d.,])(\d(?:\.\d)?)\s*out of 5(?![\d])", re.IGNORECASE)
_BASED_ON = re.compile(r"based on\s+\d[\d,]*\s+(?:company\s+)?reviews", re.IGNORECASE)
_REVIEW_COUNT = re.compile(r"(\d[\d,]*)\s+(?:company\s+)?reviews", re.IGNORECASE)
_SUB_RATING = re.compile(
    r"work[\s-]*life|balance|\bpay\b|benefits|compensation|\bceo\b|approval|recommend"
    r"|management|diversity|career|opportunities",
    re.IGNORECASE,
)
# A two-letter state code right after a city: "Racine, NJ" or "Racine NJ".
_STATE_AFTER = re.compile(r",?\s*([A-Z]{2})\b")
_SPACE = re.compile(r"\s+")


def _plain_https(url: str) -> bool:
    """A plain https URL whose host two parsers agree on: no whitespace, control
    character or backslash, no userinfo, no port but 443."""
    if any(c.isspace() or c == "\\" or ord(c) < 32 or ord(c) == 127 for c in url):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
        parsed_host = httpx.URL(url).host
    except (ValueError, httpx.InvalidURL):
        return False
    if parts.scheme != "https" or "@" in parts.netloc or port not in (None, HTTPS_PORT):
        return False
    return parsed_host.lower().rstrip(".") == host_of(url)


def _classify(url: str) -> tuple[str, str]:
    """(site label, site employer id) for an allowlisted review URL, else ("", "")."""
    if not _plain_https(url):
        return "", ""
    host = host_of(url)
    path = urlsplit(url).path or ""
    for site, domains, prefix, id_pattern in _SITES:
        if research_match.on_domain(host, domains) and path.lower().startswith(prefix):
            m = id_pattern.search(path)
            return site, (m.group(1).lower() if m else "")
    return "", ""


def review_site(url: str) -> str:
    """'Glassdoor' / 'Indeed' / 'Comparably' for an allowlisted review URL, else ''."""
    return _classify(url)[0]


def _city_near(text: str, company: str, anchors: research_match.Anchors) -> bool:
    """The job's city within `NEAR_CHARS` of a company mention, with no other
    state's code beside it. Review sites title their location pages "<Co> Reviews in
    <City>" without the state (ADR 0069 addendum); pages, unlike these labeled
    quotes, still need the state."""
    if not anchors.city:
        return False
    city = re.compile(rf"(?<![A-Za-z]){re.escape(anchors.city)}(?![A-Za-z])", re.IGNORECASE)
    # Any "<City>, <other state>" anywhere in the result means another place: refuse.
    for m in city.finditer(text):
        other = _STATE_AFTER.match(text, m.end())
        if other is not None and other.group(1) != anchors.state_code:
            return False
    for s, e in research_match.company_mentions(text, company):
        window = text[max(0, s - research_match.NEAR_CHARS) : e + research_match.NEAR_CHARS]
        if city.search(window):
            return True
    return False


def _located(text: str, company: str, anchors: research_match.Anchors) -> bool:
    """The company named with the job's location near the mention (or its city, with
    no other state beside it), or named with the employer's own domain."""
    if research_match.located_mention(text, company, anchors):
        return True
    if _city_near(text, company, anchors):
        return True
    return research_match.names_company(text, company) and research_match.names_domain(
        text, anchors.strong_domains
    )


def _kind(snippet: str, start: int, end: int) -> str:
    before = snippet[max(0, start - CULTURE_LOOKBEHIND) : start].lower()
    if "culture" in before:
        return "culture"
    near = (
        snippet[max(0, start - SUBRATING_WINDOW) : start]
        + " "
        + snippet[end : end + SUBRATING_WINDOW]
    )
    if _SUB_RATING.search(near):
        return "other"
    wide = snippet[max(0, start - OVERALL_LOOKBEHIND) : start].lower()
    if "employee rating" in wide or "overall" in wide:
        return "overall"
    if _BASED_ON.search(snippet[end : end + OVERALL_LOOKAHEAD]):
        return "overall"
    return "other"


def _rating(snippet: str) -> tuple[float, str] | None:
    """(value, kind) from the first "N out of 5" in `snippet`, or None."""
    m = _RATING.search(snippet)
    if m is None:
        return None
    value = float(m.group(1))
    if not 0 <= value <= MAX_RATING:
        return None
    return value, _kind(snippet, m.start(1), m.end())


def _review_count(snippet: str) -> int | None:
    m = _REVIEW_COUNT.search(snippet)
    if m is None:
        return None
    count = int(m.group(1).replace(",", ""))
    return count if count <= MAX_REVIEW_COUNT else None


def _item(result: Mapping[str, Any], site: str, observed_at: str) -> dict[str, Any] | None:
    description = str(result.get("description") or "")
    snippet = _SPACE.sub(" ", strip_instructions(description)).strip()[:SNIPPET_CHARS]
    rated = _rating(snippet)
    if rated is None:
        return None
    value, kind = rated
    item: dict[str, Any] = {
        "site": site,
        "rating_kind": kind,
        "value": value,
        "url": str(result.get("url") or ""),
        "snippet": snippet,
        "observed_at": observed_at,
    }
    count = _review_count(snippet)
    if count is not None:
        item["review_count"] = count
    return item


def review_evidence(
    results: Mapping[str, Mapping[str, Any]],
    company: str,
    anchors: research_match.Anchors,
    *,
    today: dt.date | None = None,
) -> list[dict[str, Any]]:
    """Evidence items from every search result, deduped by (site, kind, value),
    at most `MAX_ITEMS`, in result order."""
    observed_at = (today or dt.date.today()).isoformat()
    candidates: list[tuple[Mapping[str, Any], str, str, bool]] = []
    for result in results.values():
        url = str(result.get("url") or "")
        site, site_id = _classify(url)
        if not site:
            continue
        text = f"{result.get('title') or ''}\n{result.get('description') or ''}\n{url}"
        named = research_match.names_company(text, company)
        candidates.append((result, site, site_id, named and _located(text, company, anchors)))
    # A located page vouches for the same site's pages about the same site employer id.
    vouched = {(site, sid) for _, site, sid, ok in candidates if ok and sid}
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str, float]] = set()
    for result, site, site_id, ok in candidates:
        text = (
            f"{result.get('title') or ''}\n{result.get('description') or ''}\n{result.get('url')}"
        )
        sibling = bool(site_id) and (site, site_id) in vouched
        if not (ok or (sibling and research_match.names_company(text, company))):
            continue
        item = _item(result, site, observed_at)
        if item is None:
            continue
        ident = (item["site"], item["rating_kind"], item["value"])
        if ident in seen:
            continue
        seen.add(ident)
        items.append(item)
        if len(items) == MAX_ITEMS:
            break
    return items


def glassdoor_overall(items: list[dict[str, Any]]) -> float | None:
    """The first Glassdoor overall rating, the only source of a new profile's culture_rating."""
    for item in items:
        if item["site"] == "Glassdoor" and item["rating_kind"] == "overall":
            return float(item["value"])
    return None

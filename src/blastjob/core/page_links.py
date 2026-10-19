"""Employer careers pages as a closure link: `Link("page:<host>", url, url)`.

For employers on no board we can list (JazzHR on a custom domain, iCIMS,
BambooHR...), the posting's own page on the employer's site can still prove
it open (schema.org JobPosting JSON-LD naming this employer and title) or gone
(that same host answering 404/410). A link type only, never a discovery
source, and not authoritative.

Every step leans towards "no link", as in core/closure_links: the page must be
off every aggregator, bound to the company by its own ATS tenant, name ONE
employer, and hold exactly one JobPosting matching the title. linkedin.com is
never contacted: it is on page_fetch.AGGREGATOR_HOSTS, which every hop
(redirects too) is checked against before it is requested.

Trust level (issue #23): anyone can put an employer's ATS `<form action>` on a
page. So FULL trust needs the page host to BE the ATS tenant host, or a domain
the user listed for the employer (`discovery.employer_domains`, see
core/employer_domains). Any other page bound only through a form action is
LIMITED: its link shows the ATS tenant root (`Link.url`), never the page; it
is no evidence either way (neither open nor closed); its text is never
adopted. The level is recomputed from the stored page URL (`Link.external_id`)
and the installed settings every time, so rows stored before #23 follow the
rule too.

`_safe_get` and `_jina_search` are the only network calls, looked up at call
time so tests/conftest.py can forbid them suite-wide.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
from collections.abc import Iterator, Mapping
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from blastjob.core import employer_domains
from blastjob.core.closure_links import Link, PageCheck, _compact, match_posting
from blastjob.core.company import company_key
from blastjob.core.page_fetch import Fetch, Page, Search, fetchable, host_of, is_aggregator_host
from blastjob.core.page_fetch import jina_search as _jina_search
from blastjob.core.page_fetch import safe_get as _safe_get
from blastjob.discovery.base import RawPosting

log = logging.getLogger(__name__)

# ATS host families. Single-employer per tenant, which is what makes a page
# bound to one the employer's own. Subdomain-tenanted: the tenant is the first
# label. Path-tenanted: the tenant is the first path segment.
SUBDOMAIN_ATS: tuple[str, ...] = (
    "applytojob.com", "icims.com", "bamboohr.com", "breezy.hr", "ultipro.com",
    "myworkdayjobs.com", "recruitee.com", "teamtailor.com", "applicantpro.com",
    "isolvedhire.com",
)  # fmt: skip
PATH_ATS: frozenset[str] = frozenset(
    {
        "apply.workable.com", "jobs.smartrecruiters.com", "jobs.jobvite.com",
        "recruiting.paylocity.com", "boards.greenhouse.io", "job-boards.greenhouse.io",
        "jobs.lever.co", "jobs.ashbyhq.com",
    }
)  # fmt: skip
# Every host family that serves many employers' postings, for "is this host the
# employer's own?" (core/research_match). A host here is never an anchor: a
# Dayforce posting on jobs.dayforcehcm.com would otherwise vouch for every
# Dayforce tenant's pages. Covers the families above plus hosts closure never
# needed (Dayforce, UKG/UltiPro, Taleo, Rippling, ADP, Workday's own domain).
SHARED_ATS_SUFFIXES: frozenset[str] = frozenset(
    {
        *SUBDOMAIN_ATS, *PATH_ATS,
        "dayforcehcm.com", "ultipro.com", "ukg.net", "myworkdayjobs.com", "myworkday.com",
        "workday.com", "taleo.net", "ats.rippling.com", "workforcenow.adp.com",
        "recruiting.adp.com", "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com",
        "smartrecruiters.com", "jobvite.com", "paylocity.com", "paycomonline.net",
        "successfactors.com", "oraclecloud.com", "jazzhr.com", "applytojob.com",
    }
)  # fmt: skip


def is_shared_ats_host(host: str) -> bool:
    """`host` is (a subdomain of) a multi-employer ATS host."""
    host = host.lower().rstrip(".")
    return any(host == s or host.endswith("." + s) for s in SHARED_ATS_SUFFIXES)


MAX_CANDIDATES = 3  # search results fetched per job
GONE_STATUSES = frozenset({404, 410})


# --- parsing ------------------------------------------------------------------


class _PageParser(HTMLParser):
    """JSON-LD script bodies and <form action> values; nothing else counts."""

    def __init__(self) -> None:
        super().__init__()
        self.ld_blocks: list[str] = []
        self.form_actions: list[str] = []
        self._in_ld = False
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k.lower(): (v or "") for k, v in attrs}
        if tag == "script" and "application/ld+json" in values.get("type", "").lower():
            self._in_ld, self._buf = True, []
        elif tag == "form" and values.get("action"):
            self.form_actions.append(values["action"])

    def handle_data(self, data: str) -> None:
        if self._in_ld:
            self._buf.append(data)  # a script body can arrive in several pieces

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_ld:
            self.ld_blocks.append("".join(self._buf))
            self._in_ld = False


def _parse(html: str) -> _PageParser:
    parser = _PageParser()
    parser.feed(html or "")
    parser.close()
    return parser


def _walk(obj: Any) -> Iterator[dict]:
    if isinstance(obj, list):
        for item in obj:
            yield from _walk(item)
    elif isinstance(obj, dict):
        if "@graph" in obj:
            yield from _walk(obj["@graph"])
        kind = obj.get("@type")
        if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
            yield obj


def job_postings(html: str) -> list[dict]:
    """Every schema.org JobPosting in the page's JSON-LD. Pure."""
    out: list[dict] = []
    for block in _parse(html).ld_blocks:
        try:
            out.extend(_walk(json.loads(block)))
        except ValueError:
            continue
    return out


def _first(value: Any) -> Any:
    return value[0] if isinstance(value, list) and value else value


def org_name(posting: dict) -> str:
    org = _first(posting.get("hiringOrganization"))
    name = org.get("name") if isinstance(org, dict) else org
    return name if isinstance(name, str) else ""


def _region(posting: dict) -> str:
    place = _first(posting.get("jobLocation"))
    address = place.get("address") if isinstance(place, dict) else None
    region = address.get("addressRegion") if isinstance(address, dict) else ""
    return region if isinstance(region, str) else ""


def _as_raw(posting: dict) -> RawPosting:
    title, url = posting.get("title"), posting.get("url")
    return RawPosting(
        source="page",
        external_id=url if isinstance(url, str) else "",
        title=title if isinstance(title, str) else "",
        region=_region(posting),
    )


def canonical_url(posting: dict, final_url: str) -> str:
    """The JobPosting's own url when it is on the page's host, else the page."""
    url = posting.get("url")
    if isinstance(url, str) and url and host_of(url) == host_of(final_url):
        return url
    return final_url


# --- trust ----------------------------------------------------------------------


def _tenant(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host in PATH_ATS:
        return parts.path.strip("/").split("/")[0]
    for family in SUBDOMAIN_ATS:
        if host.endswith("." + family):
            return host.split(".")[0]
    return ""


def _tenant_matches(tenant: str, key: str, *, exact: bool) -> bool:
    """Does an ATS tenant belong to the company key? The closure rule (`exact`
    False) accepts any tenant that STARTS with the key, which is enough to prove a
    posting open or gone. Adopting a page's TEXT needs more: a lookalike tenant
    ("acmeholdings", "acmeevil") must not pass as "acme". So `exact` accepts the
    key itself, optionally followed by digits (JazzHR names tenants "acme1")."""
    if not tenant:
        return False
    if not exact:
        return tenant.startswith(key)
    return re.fullmatch(re.escape(key) + r"\d*", tenant) is not None


def _binding_url(url: str, html: str, company: str, *, exact: bool = False) -> str:
    """The URL (the page itself or a resolved <form action>) whose ATS tenant
    binds this page to `company`, or ""."""
    key = _compact(company_key(company or ""))
    if not key:
        return ""
    for candidate in (url, *(urljoin(url, a) for a in _parse(html).form_actions)):
        if _tenant_matches(_compact(_tenant(candidate)), key, exact=exact):
            return candidate
    return ""


def ats_binding(url: str, html: str, company: str, *, exact: bool = False) -> str:
    """The ATS host binding this page to `company` (the page host itself or a
    <form action> host whose tenant starts with the company key; with `exact`,
    whose tenant IS the key, see `_tenant_matches`), or ""."""
    bound = _binding_url(url, html, company, exact=exact)
    return host_of(bound) if bound else ""


NEUTRAL_PAGE_LABEL = "an employer careers page"


def _on_company_tenant(url: str, company: str) -> bool:
    key = _compact(company_key(company or ""))
    return bool(key) and _tenant_matches(_compact(_tenant(url)), key, exact=False)


def full_trust(page_url: str, company: str) -> bool:
    """Is the page at `page_url` the employer's own: served by the company's
    ATS tenant, or on a domain listed for it in the installed settings? False =
    limited trust (see the module docstring)."""
    if _on_company_tenant(page_url, company):
        return True
    return employer_domains.listed(host_of(page_url), company)


def is_limited(link: Link, company: str) -> bool:
    """A `page:` link without full trust (#23)."""
    return link.source.startswith("page:") and not full_trust(page_url(link), company)


def tenant_root(ats_url: str) -> str:
    """The ATS tenant's own root: https://<tenant-host>/ (plus the tenant path
    segment on a path-tenanted host). "" when `ats_url` is on no ATS tenant."""
    parts = urlsplit(ats_url)
    host = (parts.hostname or "").lower()
    tenant = _tenant(ats_url)
    if not tenant:
        return ""
    return f"https://{host}/{tenant}" if host in PATH_ATS else f"https://{host}/"


def page_url(link: Link) -> str:
    """The page a `page:` link was bound on (external_id; url on very old rows)."""
    return link.external_id or link.url


def apply_url(link: Link, company: str) -> str:
    """The URL to SHOW for a stored employer link. A page link with full trust
    shows the page; a limited one shows only its ATS tenant root, and "" when
    the row predates #23 and still stores the intermediary page."""
    if not link.source.startswith("page:"):
        return link.url
    if full_trust(page_url(link), company):
        return page_url(link)
    return link.url if _on_company_tenant(link.url, company) else ""


def link_label(link: Link, company: str) -> str:
    """The link source to PRINT: a limited page never names its intermediary
    host; it shows its ATS tenant host, or a neutral label until that is known."""
    if not is_limited(link, company):
        return link.source
    root = apply_url(link, company)
    return host_of(root) if root else NEUTRAL_PAGE_LABEL


def _job_link(job: Mapping[str, Any] | Any) -> tuple[Link, str]:
    def field(key: str) -> str:
        try:
            return str(job[key] or "")
        except (KeyError, IndexError, TypeError):
            return ""

    link = Link(
        field("employer_link_source"),
        field("employer_link_external_id"),
        field("employer_link_url"),
    )
    return link, field("company")


def employer_href(job: Mapping[str, Any] | Any) -> str:
    """Jinja filter: `apply_url` for a job row/mapping with the employer_link_*
    columns (and company). The template still renders it through safe_url."""
    return apply_url(*_job_link(job))


def employer_label(job: Mapping[str, Any] | Any) -> str:
    """Jinja filter: `link_label` for a job row/mapping."""
    return link_label(*_job_link(job))


def _root_on(page: Page, company: str) -> str:
    """The company's own ATS tenant root bound on `page` ("" = none)."""
    bound = _binding_url(page.url, page.html, company, exact=True)
    return tenant_root(bound) if bound else ""


def trusted_employer_page(
    url: str,
    html: str,
    posting: dict,
    company: str,
    title: str,
    region: str,
    *,
    exact_tenant: bool = False,
) -> bool:
    """Is `posting` on this page the company's own, confidently this title?
    `exact_tenant` is the stricter binding used before ADOPTING the text."""
    if is_aggregator_host(host_of(url)) or not ats_binding(url, html, company, exact=exact_tenant):
        return False
    postings = job_postings(html)
    key = company_key(company or "")
    orgs = {company_key(org_name(p)) for p in postings} - {""}
    if len(orgs) > 1 or not key or company_key(org_name(posting)) != key:
        return False  # a multi-employer board, or someone else's posting
    matching = [p for p in postings if match_posting(title, [_as_raw(p)], region) is not None]
    return len(matching) == 1 and matching[0] == posting  # ambiguity before any dedupe


def _expired(posting: dict, today: datetime.date) -> bool:
    raw = posting.get("validThrough")
    try:
        return datetime.date.fromisoformat(str(raw or "")[:10]) < today
    except ValueError:
        return False


async def probe_page(
    link: Link,
    company: str,
    title: str,
    region: str,
    *,
    fetch: Fetch | None = None,
    today: datetime.date | None = None,
) -> PageCheck:
    """The page's verdict (see `check_page`) plus, for a limited page (#23)
    whose row has no tenant root yet, the root read off the page itself, so
    the caller can store it. A limited page's verdict is always None; once its
    root is known it is not fetched at all. Network errors raise."""
    target = page_url(link)
    limited = not full_trust(target, company)
    if limited and apply_url(link, company):
        return PageCheck(None)  # nothing to learn, and no evidence either way
    page = await (fetch or _safe_get)(target)
    if page is None or page.cross_host:
        return PageCheck(None)
    if limited:
        root = _root_on(page, company) if page.status == httpx.codes.OK else ""
        return PageCheck(None, root)
    if page.status in GONE_STATUSES:
        return PageCheck(False if host_of(page.url) == host_of(target) else None)
    if page.status != httpx.codes.OK:
        return PageCheck(None)
    p = _verified(page, link, company, title, region)
    if p is None:
        return PageCheck(None)
    return PageCheck(None if _expired(p, today or datetime.date.today()) else True)


async def check_page(
    link: Link,
    company: str,
    title: str,
    region: str,
    *,
    fetch: Fetch | None = None,
    today: datetime.date | None = None,
) -> bool | None:
    """True = still posted, False = the employer's own host says gone, None =
    cannot tell. Re-verifies company, title and region on every check. A
    limited page (#23) is always None. Network errors raise; the closure pass
    treats that as no evidence."""
    checked = await probe_page(link, company, title, region, fetch=fetch, today=today)
    return checked.verdict


def _linked_posting(page: Page, link: Link) -> dict | None:
    """The page's JobPosting whose canonical url is the stored link."""
    for p in job_postings(page.html):
        if canonical_url(p, page.url) == link.external_id:
            return p
    return None


def _verified(
    page: Page, link: Link, company: str, title: str, region: str, *, exact_tenant: bool = False
) -> dict | None:
    """The linked JobPosting, only if it passes every trust check for this
    company, title and region (the shared core of check_page and
    page_description). None = absent or untrusted."""
    p = _linked_posting(page, link)
    if p is None:
        return None
    trusted = trusted_employer_page(
        page.url, page.html, p, company, title, region, exact_tenant=exact_tenant
    )
    return p if trusted else None


async def page_description(
    link: Link,
    company: str,
    title: str,
    region: str,
    *,
    fetch: Fetch | None = None,
    today: datetime.date | None = None,
) -> tuple[str | None, str]:
    """(text, reason) for the JD on an employer page, after re-running every
    trust check of `check_page`. The text is THAT trusted posting's own
    description, never the first posting on the page. Reasons: 'ok', 'gone'
    (the host says 404/410, the posting left the page, or it expired),
    'company_mismatch', 'title_drift', 'no_text'. Not normalized (the text is
    usually HTML). A limited-trust page (#23) is 'company_mismatch' before
    any request. Network errors raise."""
    target = page_url(link)
    if not full_trust(target, company):
        return None, "company_mismatch"
    page = await (fetch or _safe_get)(target)
    if page is None or page.cross_host:
        return None, "no_text"
    if page.status in GONE_STATUSES:
        gone = host_of(page.url) == host_of(target)
        return None, "gone" if gone else "no_text"
    if page.status != httpx.codes.OK:
        return None, "no_text"
    p = _linked_posting(page, link)
    if p is None or _expired(p, today or datetime.date.today()):
        return None, "gone"
    if _verified(page, link, company, title, region, exact_tenant=True) is None:
        return None, _untrusted_reason(page, p, company)
    text = p.get("description")
    return (text, "ok") if isinstance(text, str) and text.strip() else (None, "no_text")


def _untrusted_reason(page: Page, posting: dict, company: str) -> str:
    """Why a linked posting failed the trust test: a different employer (or no
    ATS tenant binding the page to this one), else the title moved."""
    employer_ok = company_key(org_name(posting)) == company_key(company or "")
    if not employer_ok or not ats_binding(page.url, page.html, company, exact=True):
        return "company_mismatch"
    return "title_drift"


async def find_page_link(
    company: str,
    title: str,
    region: str,
    jina_api_key: str,
    *,
    search: Search | None = None,
    fetch: Fetch | None = None,
    today: datetime.date | None = None,
) -> Link | None:
    """One Jina search; link only if exactly one trusted, unexpired posting is
    found (an expired one would be "verified open" now and no evidence later)."""
    urls = await (search or _jina_search)(f'"{company}" "{title}"', jina_api_key)
    candidates = [u for u in dict.fromkeys(urls) if fetchable(u)][:MAX_CANDIDATES]
    found: dict[str, str] = {}
    for url in candidates:
        try:
            page = await (fetch or _safe_get)(url)
        except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError) as e:
            log.info("page link: %s unreadable (%s); no link", url, type(e).__name__)
            return None  # an unread candidate might have been a second match
        if page is None or page.status != httpx.codes.OK:
            continue
        postings = job_postings(page.html)
        # Only a single-job page, linked at the URL actually fetched: a 404 on a
        # multi-role page, or on a self-reported url never requested, would not
        # prove THIS role closed.
        if len(postings) != 1 or canonical_url(postings[0], page.url) != page.url:
            continue
        (p,) = postings
        if _expired(p, today or datetime.date.today()):
            continue
        if trusted_employer_page(page.url, page.html, p, company, title, region):
            # The company's own tenant only (never a lookalike) may be shown.
            found[page.url] = _binding_url(page.url, page.html, company, exact=True)
    if len(found) != 1:
        return None
    ((canonical, ats),) = found.items()
    # Limited trust (#23): the link shows the ATS tenant root, never this page
    # ("" hides it when only a lookalike tenant binds the page).
    shown = canonical if full_trust(canonical, company) else tenant_root(ats) if ats else ""
    log.info("page link: %s / %s -> %s (shown %s)", company, title, canonical, shown or "-")
    return Link(f"page:{host_of(canonical)}", canonical, shown)

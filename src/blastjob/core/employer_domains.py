"""Domains the user confirms belong to an employer (`discovery.employer_domains`).

A careers page bound to its employer only through an off-domain `<form action>`
ATS host gets limited trust (core/page_links, issue #23): anyone can put that
form on a page. A page on a domain listed here for that employer (or a
subdomain of one) gets full trust back, like a page served by the ATS itself.

The mapping is installed by `config.apply_runtime_settings` on every config load,
the same way `company.set_company_aliases` is, so config-free helpers (the apply
link, templates) read the current value at call time.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping

from blastjob.core.company import company_key
from blastjob.core.page_fetch import is_aggregator_host

# A bare DNS hostname with at least one dot: no scheme, path, port or userinfo.
_HOSTNAME = re.compile(r"(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")

# Hosts shared by many unrelated sites: listing one would trust every site on it.
SHARED_HOSTS: tuple[str, ...] = (
    "github.io", "sites.google.com", "wixsite.com", "squarespace.com", "wordpress.com",
    "webflow.io", "netlify.app", "vercel.app",
)  # fmt: skip

_DOMAINS: dict[str, tuple[str, ...]] = {}


def _under(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _refusal(host: str) -> str:
    """Why `host` cannot be an employer's own domain, or ""."""
    from blastjob.core import page_links  # late: page_links imports this module

    if is_aggregator_host(host):
        return "a job board or aggregator"
    if host in page_links.PATH_ATS or any(_under(host, f) for f in page_links.SUBDOMAIN_ATS):
        return "an ATS host (a page served by the employer's ATS tenant is already trusted)"
    if any(_under(host, s) for s in SHARED_HOSTS):
        return "a shared hosting domain used by many unrelated sites"
    return ""


def normalize_domain(raw: str) -> str:
    """`raw` as a lowercase bare hostname; ValueError for anything else."""
    host = (raw or "").strip().lower().rstrip(".")
    if not _HOSTNAME.fullmatch(host):
        raise ValueError(
            f"employer domain {raw!r} must be a bare hostname such as careers.example.com "
            "(no https://, path or port)"
        )
    if why := _refusal(host):
        raise ValueError(f"employer domain {host!r} cannot be listed: it is {why}")
    return host


def normalize(mapping: Mapping[str, Collection[str]]) -> dict[str, list[str]]:
    """Validated copy of a company -> domains mapping (order kept, duplicates dropped)."""
    out: dict[str, list[str]] = {}
    for company, domains in mapping.items():
        name = (company or "").strip()
        if not name:
            raise ValueError("an employer domain entry has an empty company name")
        if "," in name:  # the /settings form is comma-separated; it could never round-trip
            raise ValueError(f"employer domain company {name!r} must not contain a comma")
        out[name] = list(dict.fromkeys(normalize_domain(d) for d in domains))
    return out


def set_employer_domains(mapping: Mapping[str, Collection[str]]) -> None:
    """Install the mapping, keyed by `company_key` so naming variants match."""
    global _DOMAINS
    merged: dict[str, tuple[str, ...]] = {}
    for company, domains in normalize(mapping).items():
        key = company_key(company)
        if key:
            merged[key] = (*merged.get(key, ()), *domains)
    _DOMAINS = merged


def listed(host: str, company: str) -> bool:
    """Is `host` a domain (or a subdomain of one) listed for `company` in the
    installed settings?"""
    key = company_key(company or "")
    if not key or not host:
        return False
    host = host.lower().rstrip(".")
    return any(_under(host, d) for d in _DOMAINS.get(key, ()))

"""Ashby public job-board adapter (no key).

Endpoint (verified live):
  GET api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true
Each job carries: id, title, location, address.postalAddress
(addressRegion/Locality/Country), isRemote, workplaceType, jobUrl, applyUrl,
descriptionPlain, and compensation.compensationTierSummary. Ashby gives
structured location, a remote flag, a plain-text JD, an apply URL, AND posted
compensation — the richest of the free ATS feeds.

`source` is per board ("ashby:{token}") rather than a bare "ashby":
with two boards configured, one board's failure used to put the shared source
in `ok_sources` on the other's success, stale-marking every posting from the
board that was down.
"""

from __future__ import annotations

from blastjob.comp import parse_salary_range
from blastjob.discovery.base import RawPosting
from blastjob.discovery.http import get_json

SOURCE = "ashby"


def source_for(token: str) -> str:
    slug = (token or "").strip().lower()
    return f"{SOURCE}:{slug}" if slug else SOURCE


def _postal(job: dict) -> dict:
    return ((job.get("address") or {}).get("postalAddress")) or {}


def parse_jobs(company: str, payload: dict, source: str = SOURCE) -> list[RawPosting]:
    out: list[RawPosting] = []
    for j in payload.get("jobs", []):
        if j.get("isListed") is False:
            continue
        postal = _postal(j)
        comp = j.get("compensation") or {}
        summary = comp.get("compensationTierSummary", "") or ""
        cmin, cmax, ccur = parse_salary_range(summary)
        out.append(
            RawPosting(
                source=source,
                external_id=str(j.get("id", "")),
                company=company,
                title=(j.get("title") or "").strip(),
                url=j.get("applyUrl") or j.get("jobUrl", ""),
                description=j.get("descriptionPlain", "") or "",
                location_raw=j.get("location", "") or "",
                city=postal.get("addressLocality", "") or "",
                region=postal.get("addressRegion", "") or "",
                country=postal.get("addressCountry", "") or "",
                remote=bool(j.get("isRemote")) if "isRemote" in j else None,
                workplace_type=j.get("workplaceType", "") or "",
                comp_min=cmin,
                comp_max=cmax,
                comp_currency=ccur,
                comp_is_estimated=False,  # Ashby posts real ranges
                comp_summary=summary,
                posted_at=j.get("publishedAt", ""),
            )
        )
    return out


class AshbyAdapter:
    source = SOURCE

    def __init__(self, company: str, token: str, *, timeout_s: int = 15, max_retries: int = 3):
        self.company = company
        self.token = token
        self.source = source_for(token)
        self.timeout_s = timeout_s
        self.max_retries = max_retries

    async def fetch(self) -> list[RawPosting]:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{self.token}"
        payload = await get_json(
            url,
            params={"includeCompensation": "true"},
            timeout_s=self.timeout_s,
            max_retries=self.max_retries,
        )
        return parse_jobs(self.company, payload, self.source)

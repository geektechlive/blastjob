"""Greenhouse public job-board adapter (no key).

Endpoint (verified live): GET boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
-> {"jobs": [{id, title, location:{name}, absolute_url, content(HTML), updated_at}]}

`source` is per board ("greenhouse:{token}") rather than a bare "greenhouse":
with two boards configured, one board's failure used to put the shared source
in `ok_sources` on the other's success, stale-marking every posting from the
board that was down.
"""

from __future__ import annotations

from blastjob.discovery.base import RawPosting
from blastjob.discovery.http import get_json
from blastjob.discovery.textutil import html_to_text, split_location

SOURCE = "greenhouse"


def source_for(token: str) -> str:
    """Per-board source id. Falls back to the bare prefix for a blank token so
    the value is never a dangling "greenhouse:"."""
    slug = (token or "").strip().lower()
    return f"{SOURCE}:{slug}" if slug else SOURCE


def parse_jobs(company: str, payload: dict, source: str = SOURCE) -> list[RawPosting]:
    out: list[RawPosting] = []
    for j in payload.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "") or ""
        city, region = split_location(loc)
        out.append(
            RawPosting(
                source=source,
                external_id=str(j.get("id", "")),
                company=company,
                title=(j.get("title") or "").strip(),
                url=j.get("absolute_url", ""),
                description=html_to_text(j.get("content", "")),
                location_raw=loc,
                city=city,
                region=region,
                remote="remote" in loc.lower() or None,
                posted_at=j.get("updated_at", ""),
            )
        )
    return out


class GreenhouseAdapter:
    source = SOURCE

    def __init__(self, company: str, token: str, *, timeout_s: int = 15, max_retries: int = 3):
        self.company = company
        self.token = token
        self.source = source_for(token)
        self.timeout_s = timeout_s
        self.max_retries = max_retries

    async def fetch(self) -> list[RawPosting]:
        url = f"https://boards-api.greenhouse.io/v1/boards/{self.token}/jobs"
        payload = await get_json(
            url,
            params={"content": "true"},
            timeout_s=self.timeout_s,
            max_retries=self.max_retries,
        )
        return parse_jobs(self.company, payload, self.source)

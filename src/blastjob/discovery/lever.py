"""Lever public postings adapter (no key).

Endpoint: GET api.lever.co/v0/postings/{token}?mode=json
-> [{id, text(title), categories:{location, team, commitment}, hostedUrl,
     descriptionPlain, description(HTML), workplaceType, country, createdAt}]
Shape is documented + stable; verify against a real token when one is added
(the parse is fixture-tested regardless).

`source` is per board ("lever:{token}") rather than a bare "lever":
with two boards configured, one board's failure used to put the shared source
in `ok_sources` on the other's success, stale-marking every posting from the
board that was down.
"""

from __future__ import annotations

from blastjob.discovery.base import RawPosting
from blastjob.discovery.http import get_json
from blastjob.discovery.textutil import html_to_text, split_location

SOURCE = "lever"


def source_for(token: str) -> str:
    slug = (token or "").strip().lower()
    return f"{SOURCE}:{slug}" if slug else SOURCE


def parse_jobs(company: str, payload: list, source: str = SOURCE) -> list[RawPosting]:
    out: list[RawPosting] = []
    for j in payload:
        cats = j.get("categories") or {}
        loc = cats.get("location", "") or ""
        city, region = split_location(loc)
        wtype = j.get("workplaceType", "") or ""
        desc = j.get("descriptionPlain") or html_to_text(j.get("description", ""))
        out.append(
            RawPosting(
                source=source,
                external_id=str(j.get("id", "")),
                company=company,
                title=(j.get("text") or "").strip(),
                url=j.get("hostedUrl", ""),
                description=desc,
                location_raw=loc,
                city=city,
                region=region,
                country=j.get("country", "") or "",
                remote=(wtype.lower() == "remote") or ("remote" in loc.lower()) or None,
                workplace_type=wtype,
                posted_at=str(j.get("createdAt", "") or ""),
            )
        )
    return out


class LeverAdapter:
    source = SOURCE

    def __init__(self, company: str, token: str, *, timeout_s: int = 15, max_retries: int = 3):
        self.company = company
        self.token = token
        self.source = source_for(token)
        self.timeout_s = timeout_s
        self.max_retries = max_retries

    async def fetch(self) -> list[RawPosting]:
        url = f"https://api.lever.co/v0/postings/{self.token}"
        payload = await get_json(
            url,
            params={"mode": "json"},
            timeout_s=self.timeout_s,
            max_retries=self.max_retries,
        )
        return parse_jobs(self.company, payload, self.source)

"""Adzuna aggregator adapter (free-tier key).

Query-based, so it finds roles at companies not on your curated list. Endpoint:
GET api.adzuna.com/v1/api/jobs/us/search/{page}
    ?app_id=&app_key=&what=&where=&distance=&salary_min=&...
-> {"results": [{id, title, company:{display_name}, location:{display_name, area[]},
                 redirect_url, description, salary_min, salary_max, salary_is_predicted,
                 latitude, longitude, created}]}
Keys come from the environment (names configurable); absent -> MissingCredentials,
which the pipeline catches and skips so a missing optional key never crashes a run.
"""

from __future__ import annotations

import os

from blastjob.discovery.base import RawPosting
from blastjob.discovery.http import get_json

SOURCE = "adzuna"
BASE = "https://api.adzuna.com/v1/api/jobs/us/search"

# Windowed snapshot, NOT a full board listing: `fetch` reads page 1 only, 25
# results per query, so the set a run sees is whatever Adzuna ranks highest
# today. Treating that window as authoritative is what left 64 stale rows
# against 87 active with the fetched count pinned at 86-90 (live DB, 2026-09-12)
# — the same roles flapping in and out, not closing. Postings therefore age out
# on silence rather than on one absence; Adzuna has no per-posting detail
# endpoint we can probe, so the TTL is the only evidence available.
STALE_AFTER_DAYS = 14


class MissingCredentials(RuntimeError):
    """Raised when Adzuna is enabled but its key env vars are unset."""


def parse_jobs(payload: dict) -> list[RawPosting]:
    out: list[RawPosting] = []
    for j in payload.get("results", []):
        loc = j.get("location") or {}
        area = loc.get("area") or []  # e.g. ["US", "Ohio", "Springfield"]
        region = area[1] if len(area) >= 2 else ""
        city = area[-1] if len(area) >= 3 else ""
        predicted = str(j.get("salary_is_predicted", "0")) == "1"
        smin = int(j.get("salary_min") or 0)
        smax = int(j.get("salary_max") or 0)
        loc_disp = loc.get("display_name", "") or ""
        out.append(
            RawPosting(
                source=SOURCE,
                external_id=str(j.get("id", "")),
                company=(j.get("company") or {}).get("display_name", "") or "",
                title=(j.get("title") or "").strip(),
                url=j.get("redirect_url", ""),
                description=j.get("description", "") or "",
                location_raw=loc_disp,
                city=city,
                region=region,
                country="USA",
                remote="remote" in loc_disp.lower() or None,
                comp_min=smin,
                comp_max=smax,
                comp_currency="USD",
                comp_is_estimated=predicted,
                posted_at=j.get("created", "") or "",
                extra={"lat": j.get("latitude"), "lon": j.get("longitude")},
            )
        )
    return out


class AdzunaAdapter:
    source = SOURCE
    snapshot = True
    stale_after_days = STALE_AFTER_DAYS

    def __init__(
        self,
        *,
        queries: list[str],
        where: str,
        distance_km: int,
        salary_min: int,
        app_id_env: str,
        app_key_env: str,
        results_per_page: int = 25,
        max_days_old: int = 40,
        timeout_s: int = 15,
        max_retries: int = 3,
    ):
        self.queries = queries
        self.where = where
        self.distance_km = distance_km
        self.salary_min = salary_min
        self.app_id_env = app_id_env
        self.app_key_env = app_key_env
        self.results_per_page = results_per_page
        self.max_days_old = max_days_old
        self.timeout_s = timeout_s
        self.max_retries = max_retries

    async def fetch(self) -> list[RawPosting]:
        app_id = os.environ.get(self.app_id_env)
        app_key = os.environ.get(self.app_key_env)
        if not app_id or not app_key:
            raise MissingCredentials(
                f"Adzuna enabled but {self.app_id_env}/{self.app_key_env} not set"
            )
        # Run each targeted title query, merge, and de-dupe by Adzuna id.
        by_id: dict[str, RawPosting] = {}
        for what in self.queries:
            params = {
                "app_id": app_id,
                "app_key": app_key,
                "what": what,
                "where": self.where,
                "distance": self.distance_km,
                "results_per_page": self.results_per_page,
                "max_days_old": self.max_days_old,
                "content-type": "application/json",
            }
            # Only constrain salary when explicitly set — Adzuna's salary_min also
            # filters on PREDICTED pay, which would hide good undisclosed-comp roles.
            if self.salary_min > 0:
                params["salary_min"] = self.salary_min
            payload = await get_json(
                f"{BASE}/1", params=params, timeout_s=self.timeout_s, max_retries=self.max_retries
            )
            for p in parse_jobs(payload):
                if p.external_id:
                    by_id[p.external_id] = p
        postings = list(by_id.values())
        # Upgrade the ~500-char API snippets to full JDs from each details page.
        from blastjob.discovery.enrich import enrich_full_jds

        await enrich_full_jds(postings, timeout_s=self.timeout_s)
        return postings

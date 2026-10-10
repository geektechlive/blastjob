"""An agentic pass over the LinkedIn import, to read titles properly.

The user asked for this directly: "the import should have a full agentic pass on it
to really figure out the connections for me. titles etc need to be looked at
deeply." The deterministic classifier in `core/network.py` is the floor -- it
now gets SVP/EVP/AVP right and stops promoting Product Owners -- but a regex will
never read "AVP Strategic Sales (Enterprise, Client Sales)" correctly. That is a
senior SALES person, and for referral purposes the function matters more than
the tier.

PRIVACY, and the reason this design is also the cheap one: the model never sees
a person. It is sent DISTINCT title and company strings only -- 1,103 and 907 of
them against 1,364 contacts -- and the results are mapped back locally by string.
No names, no message history, no relationship metadata leaves the machine. That
was going to be the shape anyway for cost and idempotency; it just happens to
also remove the question of disclosing third parties' data to a provider.

TRUST: `canonical_company` is a SUGGESTION, never an identity join. If the model
maps an ambiguous employer to the wrong brand, `company_key` would happily join a
real person to a target employer and the cockpit would recommend a warm
introduction that does not exist -- exactly the fabricated-vouch failure the
name-collision fix exists to prevent. Only deterministic aliases or the user's own
approval may affect warm-path matching.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel, Field, ValidationError

from blastjob.llm.caching import cached_text, plain_text
from blastjob.llm.cost import CostTracker, cost_from_usage
from blastjob.llm.providers import make_stream
from blastjob.models.config import BlastJobConfig

BATCH_SIZE = 40
MODEL_TAG = "enrich-v1"

TIERS = ("c_suite", "evp_svp", "vp", "director", "manager", "ic")
FUNCTIONS = (
    "technology",
    "engineering",
    "sales",
    "hr",
    "finance",
    "operations",
    "consulting",
    "legal",
    "marketing",
    "clinical",
    "other",
)
RECRUITER_KINDS = ("none", "in_house", "agency", "exec_search")

ENRICH_SYSTEM = """You classify job titles. You are given a numbered list of
title/company pairs and you return one JSON object per line item.

You are NOT given names, and you must not ask for or infer any. You are reading
job titles only.

For each item return:
  tier: one of c_suite, evp_svp, vp, director, manager, ic
  function: one of technology, engineering, sales, hr, finance, operations,
            consulting, legal, marketing, clinical, other
  recruiter: one of none, in_house, agency, exec_search
  decision_maker: true only if this title plausibly owns budget or headcount
  canonical_company: the employer's common brand name, or "" if unsure

Rules that matter:
- SVP, EVP and AVP are senior executive titles, not individual contributors.
- "Product Owner", "Process Owner" and "Data Owner" are individual-contributor
  or manager roles, NOT executives, despite the word "owner".
- A title's FUNCTION is what the person does, not what the company does. "AVP
  Strategic Sales" at a tech firm is sales, not technology.
- recruiter=agency for staffing and contingency firms; exec_search for retained
  search (Acme Executive Search, Globex Retained Search and the like); in_house
  for a talent-acquisition role inside a normal employer.
- If a title is unreadable, abbreviated beyond recognition, or in a language you
  cannot parse, return tier "ic", function "other" and canonical_company "".
  NEVER guess. An unknown is fine; a confident wrong answer is not.

Return a JSON array. Each element: {"n": <the item number>, "tier": ..., 
"function": ..., "recruiter": ..., "decision_maker": ..., "canonical_company": ...}
Return nothing but the JSON array."""


class TitleVerdict(BaseModel):
    """One classified title/company pair, validated at the boundary."""

    n: int
    tier: str = "ic"
    function: str = "other"
    recruiter: str = "none"
    decision_maker: bool = False
    canonical_company: str = Field(default="", max_length=120)

    def normalized(self) -> TitleVerdict:
        return TitleVerdict(
            n=self.n,
            tier=self.tier if self.tier in TIERS else "ic",
            function=self.function if self.function in FUNCTIONS else "other",
            recruiter=self.recruiter if self.recruiter in RECRUITER_KINDS else "none",
            decision_maker=self.decision_maker,
            canonical_company=self.canonical_company.strip(),
        )


@dataclass
class EnrichResult:
    pairs_total: int = 0
    pairs_sent: int = 0
    pairs_classified: int = 0
    contacts_updated: int = 0
    batches: int = 0
    failed_batches: int = 0
    skipped: list[str] = field(default_factory=list)


def pending_pairs(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Distinct (position, company) pairs that still need classifying.

    A pair is pending when any contact holding it has no `enriched_at`, so a
    re-import enriches only the delta and a crash mid-pass resumes where it
    stopped.
    """
    rows = conn.execute(
        "SELECT DISTINCT position, company FROM network_contacts "
        "WHERE TRIM(COALESCE(position,'')) <> '' AND COALESCE(enriched_at,'') = ''"
    ).fetchall()
    return [(r["position"], r["company"] or "") for r in rows]


def _parse(raw: str) -> list[TitleVerdict]:
    """Extract and validate the model's array. A malformed element is skipped,
    never written."""
    match = re.search(r"\[.*\]", raw, re.S)
    if not match:
        return []
    try:
        payload = json.loads(match.group(0))
    except ValueError:
        return []
    if not isinstance(payload, list):
        return []
    out = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            out.append(TitleVerdict.model_validate(item).normalized())
        except ValidationError:
            continue
    return out


def build_messages(batch: list[tuple[str, str]]) -> tuple[list[dict], list[dict]]:
    """The rubric is cached; the batch is not. Never interpolate per-call data
    into a cached block."""
    listing = "\n".join(
        f"{i}. title: {title} | company: {company}" for i, (title, company) in enumerate(batch, 1)
    )
    return [cached_text(ENRICH_SYSTEM)], [plain_text(listing)]


def apply_verdicts(
    conn: sqlite3.Connection,
    batch: list[tuple[str, str]],
    verdicts: Iterable[TitleVerdict],
    *,
    now: str | None = None,
) -> int:
    """Write each verdict onto every contact holding that title/company pair."""
    stamp = now or datetime.now().isoformat(timespec="seconds")
    updated = 0
    for v in verdicts:
        if not 1 <= v.n <= len(batch):
            continue
        title, company = batch[v.n - 1]
        cur = conn.execute(
            "UPDATE network_contacts SET enrich_tier=?, enrich_function=?, "
            "enrich_recruiter=?, enrich_decision_maker=?, enrich_canonical_company=?, "
            "enriched_at=?, enrich_model=? "
            "WHERE position=? AND COALESCE(company,'')=?",
            (
                v.tier,
                v.function,
                v.recruiter,
                int(v.decision_maker),
                v.canonical_company,
                stamp,
                MODEL_TAG,
                title,
                company,
            ),
        )
        updated += cur.rowcount
    conn.commit()
    return updated


async def run_enrichment(
    cfg: BlastJobConfig,
    conn: sqlite3.Connection,
    cost_tracker: CostTracker,
    *,
    batch_size: int = BATCH_SIZE,
    on_progress: Callable[[int, int], None] | None = None,
    limit: int | None = None,
) -> EnrichResult:
    """Classify every pending title/company pair. Resumable and idempotent."""
    res = EnrichResult()
    pairs = pending_pairs(conn)
    res.pairs_total = len(pairs)
    if limit is not None:
        pairs = pairs[:limit]
    res.pairs_sent = len(pairs)

    batches = [pairs[i : i + batch_size] for i in range(0, len(pairs), batch_size)]
    for idx, batch in enumerate(batches, 1):
        res.batches += 1
        if on_progress:
            on_progress(idx, len(batches))
        system, user = build_messages(batch)
        text = ""
        try:
            stream = await make_stream(
                cfg, system, [{"role": "user", "content": user}], max_tokens=4096
            )
            async with stream:
                async for chunk in stream.text_stream:
                    text += chunk
                final = await stream.get_final_message()
            cost_tracker.record(cost_from_usage(final.usage, cfg.pricing))
        except Exception as exc:  # noqa: BLE001 — one bad batch must not lose the run
            res.failed_batches += 1
            res.skipped.append(f"batch {idx}: {type(exc).__name__}")
            continue
        verdicts = _parse(text)
        if not verdicts:
            res.failed_batches += 1
            res.skipped.append(f"batch {idx}: no parseable verdicts")
            continue
        res.pairs_classified += len(verdicts)
        res.contacts_updated += apply_verdicts(conn, batch, verdicts)
    return res

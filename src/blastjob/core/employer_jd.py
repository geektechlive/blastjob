"""Employer full-description upgrade for aggregator snippet jobs (issue #17).

Aggregator rows (Adzuna, LinkedIn/Indeed alert mail) carry a ~500-character
snippet. The closure pass already finds the employer's own posting; this module
keeps its text, with provenance, so the row can be scored and tailored on the
whole description. Steps, cheapest first:

1. LOCAL revalidation of descriptions already adopted from a posting we hold:
   the proof is re-run (same posting still reached by the sibling / company+title
   route or a stored link, and still this role on its CURRENT title and company);
   a miss reverts it, a revised text is adopted. No network.
2. FREE copy from a held authoritative sibling (`closure_links.db_link` or the
   stored link), full text only, no score floor.
3. NETWORK (opt-in, request-metered): kept rows always, undecided rows at or
   above `min_score`; agencies and company-less rows are skipped before any
   request is spent. Undecided copies of a role already applied to (same
   cluster as an application) are skipped in whole-table runs: they are hidden
   behind the application everywhere. Identical searches (same company, title,
   region) run once per run and are shared.
3b. Adoptions proven only over the network (a posting we do not hold) are
   re-verified, capped and budgeted, oldest first; a refusal reverts.
4. A stored non-manual link not held in the DB is re-verified and its text read.
5. An unlinked row is searched (`resolve_match`); the link is saved for closure.

Every adoption is one compare-and-set over what the match was proven against
(`set_employer_jd` + a local proof re-check, in one `BEGIN IMMEDIATE`), and the
connection is committed before every network await. `write=False` writes
nothing at all: no JD, no link, no lookup record.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import Counter
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from datetime import datetime

from blastjob.core import closure_links as cl
from blastjob.core import role_clusters as rc
from blastjob.core.agencies import is_agency
from blastjob.core.closure_links import BudgetExhausted, Link, LinkDeps, RequestBudget
from blastjob.core.company import company_key
from blastjob.db import client as dbc
from blastjob.matching import rescore
from blastjob.models.config import BlastJobConfig, DiscoveryConfig
from blastjob.web.apply_link import MANUAL_LINK

log = logging.getLogger(__name__)

# The `source` values of every aggregator adapter (adzuna.SOURCE; mailalerts and
# the pipeline store alert mail under its real origin, "linkedin" / "indeed").
AGGREGATOR_SOURCES = ("adzuna", "linkedin", "indeed")

# A body that names none of the job's title words in its first 2k chars is an
# error or blocked page, not the posting.
NON_JD_WINDOW = 2000
_STOPWORDS = frozenset({"of", "and", "the", "for", "a", "an", "at", "in", "to"})
_WORD = re.compile(r"[a-z0-9]+")

# Adoptions proven only over the network (a posting we do not hold) are re-judged
# this many per run, oldest `jd_fetched_at` first; a check that comes back clean
# moves the row to the back, so the whole set rotates through.
REVERIFY_PER_RUN = 10
# verified_description reasons that mean the posting no longer backs the text.
_REFUSALS = frozenset({"title_drift", "company_mismatch", "gone"})

STORED = frozenset({"upgraded", "revised"})
WOULD_STORE = frozenset({"would_upgrade", "would_revise"})
_REVERTED = frozenset({"reverted", "would_revert"})


@dataclass(frozen=True)
class RowOutcome:
    job_id: int
    company: str
    title: str
    score: int | None
    review_state: str
    status: str
    via: str = ""  # revalidate | copy | link | search
    jd_source: str = ""
    before_chars: int = 0
    after_chars: int = 0
    note: str = ""


@dataclass
class UpgradeResult:
    outcomes: list[RowOutcome] = field(default_factory=list)
    upgraded: list[int] = field(default_factory=list)  # JD stored or revised
    reverted: list[int] = field(default_factory=list)
    requests_used: int = 0
    stale_kept: list[int] = field(default_factory=list)

    def summary(self) -> str:
        stored = [o for o in self.outcomes if o.status in STORED]
        would = [o for o in self.outcomes if o.status in WOULD_STORE]
        reverted = [o for o in self.outcomes if o.status in _REVERTED]
        parts: list[str] = []
        if stored:
            parts.append(f"{len(stored)} stored{_by_via(stored)}")
        if would:
            parts.append(f"would store {len(would)}{_by_via(would)}")
        if reverted:
            word = "reverted" if self.reverted else "would revert"
            parts.append(f"{len(reverted)} {word}")
        other = Counter(
            o.status for o in self.outcomes if o.status not in STORED | WOULD_STORE | _REVERTED
        )
        if other:
            parts.append("not stored: " + ", ".join(f"{n} {s}" for s, n in sorted(other.items())))
        head = "; ".join(parts) if parts else "nothing to upgrade"
        return f"Employer descriptions: {head}; {self.requests_used} requests used."


def _by_via(outcomes: Iterable[RowOutcome]) -> str:
    counts = Counter(o.via for o in outcomes)
    return " (" + ", ".join(f"{n} {v}" for v, n in sorted(counts.items())) + ")"


def format_report(res: UpgradeResult, *, applied: bool) -> str:
    """Plain-text report for the CLI: one line per row, then the summary."""
    head = "Employer description upgrade" + ("" if applied else " (report only, nothing written)")
    lines = [head, res.summary(), ""]
    for o in sorted(res.outcomes, key=lambda o: (o.status not in STORED | WOULD_STORE, o.job_id)):
        score = "-" if o.score is None else str(o.score)
        route = f" via {o.via}" if o.via else ""
        size = f" {o.before_chars} -> {o.after_chars} chars" if o.after_chars else ""
        note = f" ({o.note})" if o.note else ""
        lines.append(
            f"#{o.job_id} [{o.review_state} {score}] {o.company} - {o.title}: "
            f"{o.status}{route}{size}{note}"
        )
    if res.stale_kept:
        ids = ", ".join(f"#{i}" for i in res.stale_kept)
        lines += ["", f"Kept rows with a stale score awaiting a re-score: {ids}"]
    return "\n".join(lines)


# --- candidate selection ---------------------------------------------------------------


def _id_filter(job_ids: Collection[int] | None) -> tuple[str, list[int]]:
    if job_ids is None:
        return "", []
    ids = list(job_ids)
    return f" AND id IN ({','.join('?' for _ in ids)})", ids


def candidates(
    conn: sqlite3.Connection, *, job_ids: Collection[int] | None = None
) -> list[sqlite3.Row]:
    """Aggregator snippet rows with no paste and no employer text yet: kept, or
    undecided and still active at the source. Archived rows never."""
    if job_ids is not None and not job_ids:
        return []
    src = ",".join("?" for _ in AGGREGATOR_SOURCES)
    undecided = ",".join("?" for _ in dbc.UNDECIDED_STATES)
    flt, ids = _id_filter(job_ids)
    rows = conn.execute(
        f"SELECT * FROM jobs WHERE source IN ({src}) AND COALESCE(jd_pasted_at,'')='' "
        "AND COALESCE(jd_source,'')='' AND (review_state='kept' OR "
        f"(review_state IN ({undecided}) AND COALESCE(NULLIF(source_state,''),'active')='active'))"
        f"{flt} ORDER BY id",
        (*AGGREGATOR_SOURCES, *dbc.UNDECIDED_STATES, *ids),
    ).fetchall()
    return [r for r in rows if dbc.is_jd_snippet(r["raw_jd"], r["jd_pasted_at"])]


def _adopted_rows(conn: sqlite3.Connection, job_ids: Collection[int] | None) -> list[sqlite3.Row]:
    if job_ids is not None and not job_ids:
        return []
    src = ",".join("?" for _ in AGGREGATOR_SOURCES)
    undecided = ",".join("?" for _ in dbc.UNDECIDED_STATES)
    flt, ids = _id_filter(job_ids)
    return conn.execute(
        f"SELECT * FROM jobs WHERE source IN ({src}) AND COALESCE(jd_pasted_at,'')='' "
        "AND jd_source LIKE ? AND (review_state='kept' OR "
        f"review_state IN ({undecided})){flt} ORDER BY id",
        (*AGGREGATOR_SOURCES, dbc.EMPLOYER_JD_PREFIX + "%", *dbc.UNDECIDED_STATES, *ids),
    ).fetchall()


# --- run context -------------------------------------------------------------------------


@dataclass
class _Ctx:
    conn: sqlite3.Connection
    radius: float
    geo: DiscoveryConfig
    now_dt: datetime
    now: str
    write: bool
    network: bool
    min_score: int | None
    respect_backoff: bool
    skip_search: Collection[int]
    budget: RequestBudget
    deps: LinkDeps | None
    applied_clusters: frozenset[int] = frozenset()
    searches: dict[tuple[str, str, str], cl.Match | None] = field(default_factory=dict)
    index: dict = field(default_factory=dict)
    out: list[RowOutcome] = field(default_factory=list)
    upgraded: list[int] = field(default_factory=list)
    reverted: list[int] = field(default_factory=list)


def _emit(
    ctx: _Ctx,
    row: sqlite3.Row,
    status: str,
    *,
    via: str = "",
    jd_source: str = "",
    after: int = 0,
    note: str = "",
) -> None:
    ctx.out.append(
        RowOutcome(
            job_id=row["id"],
            company=row["company"] or "",
            title=row["title"] or "",
            score=row["alignment_score"],
            review_state=row["review_state"] or "",
            status=status,
            via=via,
            jd_source=jd_source,
            before_chars=len((row["raw_jd"] or "").strip()),
            after_chars=after,
            note=note,
        )
    )


# --- text checks -------------------------------------------------------------------------


def looks_like_jd(body: str, title: str | None) -> bool:
    """False for an obvious non-posting: none of the title's words (stopwords
    dropped, whole words) appear in the first 2k chars."""
    wanted = {w for w in _WORD.findall((title or "").lower()) if w not in _STOPWORDS}
    if not wanted:
        return True
    return bool(wanted & set(_WORD.findall(body[:NON_JD_WINDOW].lower())))


def _prepare(text: str, title: str | None) -> tuple[str | None, str]:
    """(normalized body, '') or (None, 'short_text' | 'not_a_jd')."""
    body = dbc.normalize_employer_jd(text)
    if body is None:
        return None, "short_text"
    if not looks_like_jd(body, title):
        return None, "not_a_jd"
    return body, ""


# --- proof and the adoption write ---------------------------------------------------------


def _held(conn: sqlite3.Connection, source: str, external_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM jobs WHERE source=? AND external_id=?", (source, external_id)
    ).fetchone()


def _proof_ok(ctx: _Ctx, row: sqlite3.Row, link: Link) -> bool:
    """The local proof behind `link` for `row`: not a rejected pair, and a held
    posting still justifies the pairing (sibling proof / decisions)."""
    if cl.rejected_link(ctx.conn, row["id"], link):
        return False
    held = _held(ctx.conn, link.source, link.external_id)
    if held is None:
        return True
    return cl.link_still_proof(ctx.conn, row, held["id"], ctx.radius, ctx.geo)


def _user_confirmed(conn: sqlite3.Connection, a: int, b: int) -> bool:
    job_a, job_b = rc.pair_of(a, b)
    found = conn.execute(
        "SELECT decision FROM same_role_decisions WHERE job_a=? AND job_b=?", (job_a, job_b)
    ).fetchone()
    return found is not None and found["decision"] == rc.SAME


def _same_role(ctx: _Ctx, row: sqlite3.Row, held: sqlite3.Row) -> bool:
    """Is the held posting, on its CURRENT title and company, this job's role? A
    retitled or id-reused posting must not supply another role's text. The user's
    'same role' decision overrides: they have said these are one posting."""
    if _user_confirmed(ctx.conn, row["id"], held["id"]):
        return True
    key = company_key(row["company"] or "")
    if not key or key != company_key(held["company"] or ""):
        return False
    posting = cl.posting_of_row(held)
    return cl.match_posting(row["title"] or "", [posting], row["geo_region"] or "") is not None


_LANDED = "landed"
_LOST_RACE = "lost_race"
_NO_PROOF = "no_proof"


def _write_adoption(
    ctx: _Ctx,
    snap: dbc.JdSnapshot,
    body: str,
    link: Link,
    *,
    jd_source: str,
    jd_ref: str,
    save: bool,
    lookup: bool,
) -> str:
    """CAS the text in under BEGIN IMMEDIATE: re-check local proof, then
    `set_employer_jd` against the pre-await snapshot, then (new link) save it.
    The link save is best-effort: `save_link` refuses a row with a manual posting
    verdict, and that must never keep a proven text out. Returns `_LANDED`,
    `_NO_PROOF` (local proof gone for a reason other than a 'not the same role'
    verdict: structural, so a network lookup records a backed-off miss rather than
    searching again tomorrow) or `_LOST_RACE` (anything moved; record nothing)."""
    conn = ctx.conn
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        status = _attempt(ctx, snap, body, link, jd_source, jd_ref, save=save, lookup=lookup)
        if status == _LANDED:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        raise
    if status == _NO_PROOF and lookup:
        _record(ctx, snap.job_id, "no_match")
    return status


def _attempt(
    ctx: _Ctx,
    snap: dbc.JdSnapshot,
    body: str,
    link: Link,
    jd_source: str,
    jd_ref: str,
    *,
    save: bool,
    lookup: bool,
) -> str:
    conn = ctx.conn
    fresh = conn.execute("SELECT * FROM jobs WHERE id=?", (snap.job_id,)).fetchone()
    if fresh is None:
        return _LOST_RACE
    if not _proof_ok(ctx, fresh, link):
        return _LOST_RACE if cl.rejected_link(conn, snap.job_id, link) else _NO_PROOF
    if not dbc.set_employer_jd(
        conn, snap, text=body, jd_source=jd_source, jd_source_ref=jd_ref, now=ctx.now
    ):
        return _LOST_RACE
    if save:
        cl.save_link(conn, snap.job_id, link)  # best-effort: never gates the text
    if lookup:
        dbc.record_jd_lookup(conn, snap.job_id, "found", ctx.now)
    return _LANDED


def _land(
    ctx: _Ctx,
    row: sqlite3.Row,
    snap: dbc.JdSnapshot,
    body: str,
    link: Link,
    *,
    via: str,
    save: bool,
    lookup: bool,
) -> None:
    jd_source, jd_ref = cl.employer_jd_ref(link)
    if not ctx.write:
        _emit(ctx, row, "would_upgrade", via=via, jd_source=jd_source, after=len(body))
        return
    status = _write_adoption(
        ctx, snap, body, link, jd_source=jd_source, jd_ref=jd_ref, save=save, lookup=lookup
    )
    if status == _LANDED:
        ctx.upgraded.append(row["id"])
        _emit(ctx, row, "upgraded", via=via, jd_source=jd_source, after=len(body))
    else:
        _emit(ctx, row, status, via=via, jd_source=jd_source)


# --- step 1: local revalidation ------------------------------------------------------------


def _is_full_authoritative(held: sqlite3.Row) -> bool:
    return (
        cl.is_authoritative(held["source"])
        and not held["jd_pasted_at"]
        and dbc.jd_class(held["raw_jd"], held["jd_pasted_at"], held["jd_source"]) == dbc.JD_FULL
    )


def _revalidate_all(ctx: _Ctx, job_ids: Collection[int] | None) -> None:
    for row in _adopted_rows(ctx.conn, job_ids):
        held = _held(
            ctx.conn, row["jd_source"][len(dbc.EMPLOYER_JD_PREFIX) :], row["jd_source_ref"]
        )
        if held is None:
            continue  # network-proven: `_reverify` re-judges it, with a budget
        if _still_proven(ctx, row, held):
            _revise(ctx, row, held)
        else:
            _revert(ctx, row)


def _still_proven(ctx: _Ctx, row: sqlite3.Row, held: sqlite3.Row) -> bool:
    """Re-run the proof the adoption used, not just 'no verdict against it': the
    held posting must still be this role AND be reached by the same route, the
    sibling / company+title proof (`db_link` returns that very posting) or a stored
    link to it that is still justified. A row whose link could not be stored (a
    manual posting verdict) is judged on the held row's own proof alone."""
    if not _same_role(ctx, row, held):
        return False
    ref = (held["source"], held["external_id"])
    found = cl.db_link(ctx.conn, row, ctx.index, ctx.radius, ctx.geo)
    if found is not None and (found[0].source, found[0].external_id) == ref:
        return True
    stored = _stored_link(row)
    if stored is not None and (stored.source, stored.external_id) == ref:
        return _proof_ok(ctx, row, stored)
    return bool(row["posting_check_manual"]) and _proof_ok(ctx, row, Link(*ref))


def _revert(ctx: _Ctx, row: sqlite3.Row, via: str = "revalidate") -> None:
    if not ctx.write:
        _emit(ctx, row, "would_revert", via=via, jd_source=row["jd_source"])
        return
    ref = (row["jd_source"], row["jd_source_ref"])
    if not dbc.revert_employer_jd(ctx.conn, row["id"], ref=ref):
        ctx.conn.rollback()
        return
    # The lookup that found this text is void; let a later search try again.
    ctx.conn.execute(
        "UPDATE jobs SET jd_lookup_at='', jd_lookup_status='', jd_lookup_failures=0 WHERE id=?",
        (row["id"],),
    )
    ctx.conn.commit()
    ctx.reverted.append(row["id"])
    _emit(ctx, row, "reverted", via=via, jd_source=row["jd_source"])


def _revise(ctx: _Ctx, row: sqlite3.Row, held: sqlite3.Row) -> None:
    """The held posting's text changed and is still a full description for this
    role. A revision that no longer reads as this job's posting reverts."""
    if not _is_full_authoritative(held):
        return
    body, why = _prepare(held["raw_jd"], row["title"])
    if body is None:
        if why == "not_a_jd":
            _revert(ctx, row)
        return
    if body == (row["raw_jd"] or ""):
        return
    snap = dbc.jd_snapshot(ctx.conn, row["id"])
    if snap is None:
        return
    link = Link(held["source"], held["external_id"], held["url"] or "")
    if not ctx.write:
        _emit(
            ctx, row, "would_revise", via="revalidate", jd_source=row["jd_source"], after=len(body)
        )
        return
    status = _write_adoption(
        ctx, snap, body, link, jd_source=row["jd_source"], jd_ref=row["jd_source_ref"],
        save=False, lookup=False,
    )  # fmt: skip
    landed = status == _LANDED
    if landed:
        ctx.upgraded.append(row["id"])
    _emit(
        ctx, row, "revised" if landed else status, via="revalidate",
        jd_source=row["jd_source"], after=len(body) if landed else 0,
    )  # fmt: skip


# --- step 2: free copy from a held sibling ---------------------------------------------------


def _stored_link(row: sqlite3.Row) -> Link | None:
    source = row["employer_link_source"] or ""
    if not source:
        return None
    return Link(source, row["employer_link_external_id"] or "", row["employer_link_url"] or "")


def _free_source(ctx: _Ctx, row: sqlite3.Row) -> tuple[Link, sqlite3.Row] | str | None:
    """(link, held row) to copy from; a terminal status string when this row must
    not go on to the network; None when there is nothing free to copy. A held
    posting that is no longer this role (retitled, id reused, another company) is
    nothing free: the network step judges it on the live board."""
    link = _stored_link(row)
    if link is not None:
        if link.source == MANUAL_LINK:
            return "manual_link"  # display-only: never judged, fetched or copied over
        held = _held(ctx.conn, link.source, link.external_id)
        if held is None:
            return None  # the network step re-verifies it
        if not _proof_ok(ctx, row, link):
            return "no_proof"
        return (link, held) if _same_role(ctx, row, held) else None
    found = cl.db_link(ctx.conn, row, ctx.index, ctx.radius, ctx.geo)
    if found is None:
        return None
    held = _held(ctx.conn, found[0].source, found[0].external_id)
    if held is None or not _same_role(ctx, row, held):
        return None
    return found[0], held


def _copy(ctx: _Ctx, row: sqlite3.Row) -> bool:
    """Try the free copy. True = this row is settled (outcome emitted)."""
    found = _free_source(ctx, row)
    if found is None:
        return False
    if isinstance(found, str):
        _emit(ctx, row, found)
        return True
    link, held = found
    if not _is_full_authoritative(held):
        if ctx.network:
            return False  # a snippet/pasted sibling is no text, not the end of the road
        _emit(ctx, row, "sibling_not_full", via="copy")
        return True
    body, why = _prepare(held["raw_jd"], row["title"])
    snap = dbc.jd_snapshot(ctx.conn, row["id"])
    if body is None or snap is None:
        _emit(ctx, row, why or "lost_race", via="copy")
        return True
    _land(ctx, row, snap, body, link, via="copy", save=False, lookup=False)
    return True


# --- steps 3-5: the network ------------------------------------------------------------------


def _due(ctx: _Ctx, row: sqlite3.Row) -> bool:
    # A 'found' record on a row that is a candidate again means its text was
    # reverted; the old success must not block a fresh lookup.
    return row["jd_lookup_status"] == "found" or dbc.jd_lookup_due(row, ctx.now)


def _gate(ctx: _Ctx, row: sqlite3.Row) -> str:
    """Why this row gets no network work ('' = eligible). Checked before any
    request is spent."""
    if row["review_state"] != "kept" and row["cluster_id"] in ctx.applied_clusters:
        return "applied_role"
    if row["review_state"] != "kept" and ctx.min_score is not None:
        score = row["alignment_score"]
        if score is None:
            return "unscored"
        if score < ctx.min_score:
            return "below_floor"
    company = row["company"] or ""
    if is_agency(company):
        return "agency"
    if not company_key(company):
        return "no_company"
    if ctx.respect_backoff and not _due(ctx, row):
        return "backoff"
    return ""


def _net_order(row: sqlite3.Row) -> tuple[int, float, int]:
    score = row["alignment_score"]
    return (
        0 if row["review_state"] == "kept" else 1,
        -(score if score is not None else -1),
        row["id"],
    )


def _record(ctx: _Ctx, job_id: int, lookup: str, save: Link | None = None) -> None:
    """Record a lookup outcome (and keep a found link for closure). No write in
    report mode."""
    if not ctx.write:
        return
    conn = ctx.conn
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        fresh = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if fresh is not None:
            blank = not (fresh["employer_link_source"] or "")
            if save is not None and blank and _proof_ok(ctx, fresh, save):
                cl.save_link(conn, job_id, save)
            dbc.record_jd_lookup(conn, job_id, lookup, ctx.now)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _no_text(
    ctx: _Ctx, row: sqlite3.Row, status: str, lookup: str, *, via: str, link: Link | None,
    note: str = "",
) -> None:  # fmt: skip
    _record(ctx, row["id"], lookup, link)
    _emit(ctx, row, status, via=via, note=note)


def _conclude(
    ctx: _Ctx,
    row: sqlite3.Row,
    snap: dbc.JdSnapshot,
    link: Link,
    text: str | None,
    reason: str,
    *,
    via: str,
    save: bool,
) -> None:
    keep = link if save else None
    if text is None:
        if reason.startswith("error:"):
            _no_text(ctx, row, "transient", "transient", via=via, link=None, note=reason)
        else:
            # The fresh re-check REFUSED the match (drift, wrong company, gone): the
            # link it would have saved is not trusted, so closure never sees it.
            kept = None if reason in _REFUSALS else keep
            _no_text(ctx, row, reason, "no_match", via=via, link=kept)
        return
    body, why = _prepare(text, row["title"])
    if body is None:
        _no_text(ctx, row, why, "no_match", via=via, link=keep)
        return
    _land(ctx, row, snap, body, link, via=via, save=save, lookup=True)


async def _from_stored(ctx: _Ctx, row: sqlite3.Row, snap: dbc.JdSnapshot, link: Link) -> None:
    assert ctx.deps is not None
    if cl.rejected_link(ctx.conn, row["id"], link):
        _no_text(ctx, row, "rejected", "no_match", via="link", link=None)
        return
    text, reason = await cl.verified_description(
        link, row["company"] or "", row["title"] or "", row["geo_region"] or "", ctx.deps
    )
    _conclude(ctx, row, snap, link, text, reason, via="link", save=False)


async def _from_search(ctx: _Ctx, row: sqlite3.Row, snap: dbc.JdSnapshot) -> None:
    assert ctx.deps is not None
    company, title, region = row["company"] or "", row["title"] or "", row["geo_region"] or ""
    key = (company, title, region)
    if key in ctx.searches:
        # The same inputs give the same answer; each row still runs its own
        # rejection check, verification and compare-and-set below.
        match = ctx.searches[key]
    else:
        try:
            match = await cl.resolve_match(company, title, region, ctx.deps)
        except BudgetExhausted:
            raise
        except Exception as e:  # noqa: BLE001 -- a failed search proves nothing either way
            log.warning("employer-jd: search failed for job %s (%s)", row["id"], type(e).__name__)
            _no_text(
                ctx, row, "transient", "transient", via="search", link=None, note=type(e).__name__
            )
            return
        ctx.searches[key] = match
    if match is None:
        _no_text(ctx, row, "no_match", "no_match", via="search", link=None)
        return
    if cl.rejected_link(ctx.conn, row["id"], match.link):
        _no_text(ctx, row, "rejected", "no_match", via="search", link=None)
        return
    own = match.posting.description if match.posting is not None else ""
    if dbc.normalize_employer_jd(own or "") is not None:
        _conclude(ctx, row, snap, match.link, own, "ok", via="search", save=True)
        return
    text, reason = await cl.verified_description(match.link, company, title, region, ctx.deps)
    _conclude(ctx, row, snap, match.link, text, reason, via="search", save=True)


async def _network_row(ctx: _Ctx, row: sqlite3.Row) -> None:
    gate = _gate(ctx, row)
    if gate:
        _emit(ctx, row, gate)
        return
    link = _stored_link(row)
    if link is None and row["id"] in ctx.skip_search:
        _emit(ctx, row, "closure_searched")
        return
    if ctx.budget.exhausted:
        _emit(ctx, row, "cap")
        return
    snap = dbc.jd_snapshot(ctx.conn, row["id"])  # BEFORE any await
    if snap is None:
        return
    ctx.conn.commit()
    if link is not None:
        await _from_stored(ctx, row, snap, link)
    else:
        await _from_search(ctx, row, snap)


def _applied_clusters(conn: sqlite3.Connection) -> frozenset[int]:
    """Clusters holding an application (`role_clusters.has_application`). Their
    undecided members are hidden behind it in every list and the digest."""
    rows = conn.execute(
        "SELECT cluster_id, review_state, application_state, applied_at FROM jobs "
        "WHERE review_state='kept' AND cluster_id IS NOT NULL"
    ).fetchall()
    return frozenset(r["cluster_id"] for r in rows if rc.has_application(r))


def _reverify_link(row: sqlite3.Row) -> Link | None:
    """The link behind a network-proven adoption, rebuilt from its provenance: a
    page link carries its own url; a board needs the stored link's url (Workday and
    Dayforce recover the board from it). None = cannot be re-checked, left alone."""
    source = row["jd_source"][len(dbc.EMPLOYER_JD_PREFIX) :]
    ref = row["jd_source_ref"] or ""
    stored = _stored_link(row)
    url = stored.url if stored and (stored.source, stored.external_id) == (source, ref) else ""
    if source.startswith("page:"):
        return Link(source, ref, ref)
    link = Link(source, ref, url)
    return link if cl.board_token(link) is not None else None


def _reverify_picks(ctx: _Ctx, job_ids: Collection[int] | None) -> list[tuple[sqlite3.Row, Link]]:
    held_free: list[tuple[sqlite3.Row, Link]] = []
    for row in _adopted_rows(ctx.conn, job_ids):
        if _held(ctx.conn, row["jd_source"][len(dbc.EMPLOYER_JD_PREFIX) :], row["jd_source_ref"]):
            continue  # a held posting is judged locally by `_revalidate_all`
        link = _reverify_link(row)
        if link is not None:
            held_free.append((row, link))
    held_free.sort(key=lambda pair: (pair[0]["jd_fetched_at"] or "", pair[0]["id"]))
    return held_free[:REVERIFY_PER_RUN]


def _rotate(ctx: _Ctx, row: sqlite3.Row) -> None:
    """A clean re-check moves the row to the back of the oldest-first order."""
    if not ctx.write:
        return
    ctx.conn.execute(
        "UPDATE jobs SET jd_fetched_at=? WHERE id=? AND jd_source=? AND jd_source_ref=?",
        (ctx.now, row["id"], row["jd_source"], row["jd_source_ref"]),
    )
    ctx.conn.commit()


async def _reverify(ctx: _Ctx, job_ids: Collection[int] | None) -> None:
    """Re-judge adoptions whose posting we do not hold (a `page:` link, an unheld
    board or Workday posting) with `verified_description`: the same fresh trust
    revalidation that adopted them. A refusal (title_drift, company_mismatch, gone)
    reverts; an error or blank text is no evidence and changes nothing."""
    assert ctx.deps is not None
    for row, link in _reverify_picks(ctx, job_ids):
        if ctx.budget.exhausted:
            return
        ctx.conn.commit()
        try:
            _text, reason = await cl.verified_description(
                link, row["company"] or "", row["title"] or "", row["geo_region"] or "", ctx.deps
            )
        except BudgetExhausted:
            return
        except Exception as e:  # noqa: BLE001 -- one bad row must not sink the run
            log.warning("employer-jd: reverify of job %s failed (%s)", row["id"], type(e).__name__)
            continue
        if reason in _REFUSALS:
            _revert(ctx, row, via="reverify")
        elif reason in ("ok", "no_text"):
            _rotate(ctx, row)


async def _network_step(ctx: _Ctx, rows: list[sqlite3.Row]) -> None:
    for row in sorted(rows, key=_net_order):
        try:
            await _network_row(ctx, row)
        except BudgetExhausted:
            ctx.conn.rollback()
            _emit(ctx, row, "cap")
        except Exception as e:  # noqa: BLE001 -- one bad row must not sink the run
            log.warning("employer-jd: job %s failed (%s)", row["id"], type(e).__name__)
            ctx.conn.rollback()
            _emit(ctx, row, "error", note=type(e).__name__)


# --- entry point ----------------------------------------------------------------------------


async def run_upgrade(
    cfg: BlastJobConfig,
    conn: sqlite3.Connection,
    *,
    deps: LinkDeps | None = None,
    job_ids: Collection[int] | None = None,
    min_score: int | None,
    network: bool,
    write: bool,
    request_limit: int,
    skip_search_ids: Collection[int] = frozenset(),
    respect_backoff: bool = True,
    now: datetime | None = None,
) -> UpgradeResult:
    """Upgrade aggregator snippet rows to the employer's full description.

    `network=False` runs only the free steps. `write=False` computes the same
    outcomes ('would_upgrade', 'would_revert') and writes nothing. The network
    runs through one `RequestBudget(request_limit)` (request UNITS, see
    `closure_links.budgeted`); when it is spent the
    remaining rows are 'cap'. `skip_search_ids` are rows closure already searched
    this run. A whole-table run (`job_ids` None) skips undecided copies of a role
    already applied to; an explicit `job_ids` request never does. The caller
    re-scores `upgraded`, `reverted` and `stale_kept`."""
    when = now or datetime.now()
    budget = RequestBudget(request_limit)
    live = cl.budgeted(deps or cl.default_deps(), budget) if network else None
    ctx = _Ctx(
        conn=conn,
        radius=cfg.discovery.commute_radius_miles,
        geo=cfg.discovery,
        now_dt=when,
        now=when.isoformat(timespec="seconds"),
        write=write,
        network=network,
        min_score=min_score,
        respect_backoff=respect_backoff,
        skip_search=skip_search_ids,
        budget=budget,
        deps=live,
        applied_clusters=_applied_clusters(conn) if job_ids is None else frozenset(),
    )
    ctx.index = cl.authoritative_index(conn)
    _revalidate_all(ctx, job_ids)
    rest = [r for r in candidates(conn, job_ids=job_ids) if not _copy(ctx, r)]
    if network:
        await _reverify(ctx, job_ids)
        await _network_step(ctx, rest)
    else:
        seen = {o.job_id for o in ctx.out}
        for row in rest:
            if row["id"] not in seen:
                _emit(ctx, row, "no_free_copy")
    conn.commit()
    return UpgradeResult(
        outcomes=ctx.out,
        upgraded=ctx.upgraded,
        reverted=ctx.reverted,
        requests_used=budget.used,
        stale_kept=rescore.stale_kept_ids(conn),
    )

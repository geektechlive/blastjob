"""Digest formatting for `blastjob-daily` (issue #58).

`build_digest` and the `format_*` helpers turn a run's results into the Discord
message and the run footer. Pure formatting plus the digest-window ordering; no
network and no sends. See docs/how-it-works/daily.md.

Kept out of `daily.py`, which is already past the file-size budget; `daily`
re-exports every public name here so `daily.build_digest` and friends still work.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote

from blastjob.core.outreach_asks import KIND_LABELS
from blastjob.core.recruiter_status import Reminder
from blastjob.core.target_store import BASIS_LABELS, EVENT_LABELS
from blastjob.daily_dream import MAX_DREAM_JOBS, rendered_dream_jobs
from blastjob.llm.cost import describe_cost
from blastjob.web.apply_link import ApplyLink

if TYPE_CHECKING:
    from blastjob.daily import DailyResult
    from blastjob.discovery.replies import ReplyMatch

HEADER = "🎯 **blastjob**"
COCKPIT_BASE = "http://127.0.0.1:8765"  # fallback default; the run derives it: cockpit_base()
MAX_DIGEST_JOBS = 25
MAX_CLOSED_NAMES = 5
MAX_TARGET_NEWS = 8
MAX_TARGET_SPAN_CHARS = 160
MAX_ASK_NAME_CHARS = 60
# Page text is untrusted: no markdown, links, mentions or mass-ping syntax get through.
_UNSAFE_SPAN_CHARS = re.compile(r"[<>@*_`\[\]()|~\\#]")


_BARE_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)


@dataclass(frozen=True)
class TargetNewsItem:
    """One target-company event as the digest shows it (core/target_store row + contacts)."""

    id: int
    company_key: str
    company: str
    event_type: str
    published_on: str
    span: str
    host: str
    basis: str
    contacts: int


_COMP_TAGS = {"posted": "posted", "inferred": "inferred (from the posting)", "estimated": "est."}


def _comp_badge(row: sqlite3.Row) -> str:
    lo, hi = int(row["comp_min"] or 0), int(row["comp_max"] or 0)
    if not (lo or hi):
        return "comp n/a"
    top = hi or lo
    # A lookup, not a ternary: the old `else` swept `inferred` and `unknown` into
    # the "posted" bucket and shipped a figure no employer published to Discord,
    # where it reads as fact on a phone.
    tag = _COMP_TAGS.get(row["comp_provenance"], "unverified")
    return f"${top // 1000}K {tag}"


def _where(row: sqlite3.Row) -> str:
    loc = (row["geo_location_raw"] or "").strip()
    if row["geo_remote"]:
        return f"Remote{' · ' + loc if loc else ''}"
    return loc or "location n/a"


def format_job_line(
    row: sqlite3.Row, base_url: str = COCKPIT_BASE, *, warm: bool = False, url: str = ""
) -> str:
    """`url` is the role's best apply link (web/apply_link); the row's own URL
    when there is none."""
    flag = " (review)" if row["review_state"] == "queued" else ""
    marker = " 🤝 you know someone here" if warm else ""
    score = row["alignment_score"] if row["alignment_score"] is not None else "?"
    return (
        f"• **{score}** {row['company']} — {row['title']}{flag}{marker}\n"
        f"   {_where(row)} · {_comp_badge(row)} · <{url or row['url']}>"
        f" · cockpit <{base_url}/jobs/{row['id']}>"
    )


def warm_job_ids(conn: sqlite3.Connection, jobs: list[sqlite3.Row]) -> frozenset[int]:
    """Which of `jobs` have a network warm path — used to jump the digest
    queue ahead of higher-scoring cold roles. Deterministic SQL only."""
    from blastjob.core import network as net
    from blastjob.core.company import company_key

    keys = {company_key(r["company"] or "") for r in jobs}
    counts = net.warm_counts(conn, keys)
    return frozenset(
        int(r["id"]) for r in jobs if counts.get(company_key(r["company"] or ""), 0) > 0
    )


def order_for_digest(
    jobs: list[sqlite3.Row], warm_ids: frozenset[int] = frozenset()
) -> list[sqlite3.Row]:
    """Digest order: warm-path jobs jump ahead of higher-scoring cold roles. A
    stable sort keeps each group's own score-descending order, so a warm role
    never loses its spot to a colder one. Shared by `build_digest` (what gets
    shown) and `run_daily` (what gets stamped) so the two never disagree about
    which jobs were actually rendered."""
    return sorted(jobs, key=lambda r: 0 if int(r["id"]) in warm_ids else 1)


def rendered_jobs(
    jobs: list[sqlite3.Row], warm_ids: frozenset[int] = frozenset()
) -> list[sqlite3.Row]:
    """The subset of `jobs` that `build_digest` actually renders (the digest
    window, `MAX_DIGEST_JOBS` wide). Anything outside this slice must not be
    stamped `notified_at` — see the overflow-stamping bug this guards against
    in tests/test_daily_notify.py."""
    return order_for_digest(jobs, warm_ids)[:MAX_DIGEST_JOBS]


def build_digest(
    jobs: list[sqlite3.Row],
    errors: dict[str, str],
    followups: list[dict],
    *,
    base_url: str = COCKPIT_BASE,
    heartbeat: str = "",
    footer: str = "",
    warm_ids: frozenset[int] = frozenset(),
    closed_postings: list[dict] | None = None,
    replies: list[ReplyMatch] | None = None,
    apply_links: Mapping[int, ApplyLink] | None = None,
    notes: Mapping[str, str] | None = None,
    target_news: Sequence[TargetNewsItem] = (),
    target_failed: int = 0,
    dream_jobs: Sequence[sqlite3.Row] = (),
    stopped: str = "",
    ask_followups: Sequence[sqlite3.Row] = (),
    recruiter_touches: Sequence[Reminder] = (),
    pace: str = "",
    pace_is_news: bool = False,
    triage_pending: int = 0,
) -> str:
    """The Discord message. Empty string means "nothing to say". `apply_links`
    (`web.apply_link.apply_links_for`) gives each job its cluster's best link;
    a job missing from it links its own URL. `pace` is the weekly pace line; it rides on any
    digest that is sent, but breaks a quiet day's silence only when `pace_is_news` (its day).
    `triage_pending` counts recruiter-mail messages waiting on the Recruiter mail page; it is
    news by itself, and the line carries the count and a link, never a sender or subject."""
    links = apply_links or {}
    closed = closed_postings or []
    replied = replies or []
    noted = notes or {}
    quiet = not (
        jobs
        or dream_jobs
        or errors
        or followups
        or ask_followups
        or recruiter_touches
        or heartbeat
        or closed
        or replied
        or noted
        or stopped
        or triage_pending
        or (pace and pace_is_news)
    )
    if quiet and not (target_news or target_failed):
        return ""
    n = len(jobs)
    title = f"{HEADER} — {n} new role{'s' if n != 1 else ''}" if n else HEADER
    lines = [title]
    if stopped:
        lines.append(f"**{stopped}**")
    for row in rendered_jobs(jobs, warm_ids):
        link = links.get(int(row["id"]))
        lines.append(
            format_job_line(
                row, base_url, warm=int(row["id"]) in warm_ids, url=link.url if link else ""
            )
        )
    if n > MAX_DIGEST_JOBS:
        # /all lists every active job (surfaced + queued + kept + archived),
        # so it contains the overflow regardless of the surfaced/queued mix —
        # unlike "/" (surfaced only) or "/queue" (queued only), either of which
        # can miss part of what got left out of the digest.
        lines.append(f"…and {n - MAX_DIGEST_JOBS} more in the cockpit: <{base_url}/all>")
    if dream_jobs:
        lines.append("")
        lines += format_dream_block(list(dream_jobs), base_url, links)
    if closed:
        lines.append("")
        lines.append(format_closed_line(closed))
    if replied:
        lines.append("")
        lines.append("**Replies**")
        for r in replied:
            lines.append(format_reply_line(r, base_url))
    if triage_pending:
        lines.append("")
        lines.append(
            f"**Recruiter mail**: {triage_pending} new to triage <{base_url}/recruiter-mail>"
        )
    if target_news or target_failed:
        lines.append("")
        lines += format_target_block(target_news, target_failed, base_url)
    if followups:
        lines.append("")
        lines.append("**Follow-ups due**")
        for f in followups:
            who = f"{_plain_name(f['company'])} — {_plain_name(f['title'])}"
            lines.append(f"• {who}: {f['followup_reason']} <{base_url}/jobs/{f['id']}>")
    if ask_followups:
        lines.append("")
        lines += format_ask_block(ask_followups, base_url)
    if recruiter_touches:
        lines.append("")
        lines += format_recruiter_block(recruiter_touches, base_url)
    if pace:
        lines.append("")
        lines.append(pace)
    if errors:
        lines.append("")
        lines.append("**Source errors**")
        for src, err in sorted(errors.items()):
            lines.append(f"• {src}: {err[:160]}")
    if noted:
        lines.append("")
        lines.append("**Notes**")
        for src, note in sorted(noted.items()):
            lines.append(f"• {src}: {note[:200]}")
    if heartbeat:
        lines.append("")
        lines.append(heartbeat)
    if footer:
        lines.append("")
        lines.append(footer)
    return "\n".join(lines)


def format_dream_block(
    jobs: list[sqlite3.Row], base_url: str, links: Mapping[int, ApplyLink]
) -> list[str]:
    """The weekly "Dream list" section (#90): far on-site roles at companies the owner named,
    best fit first, capped at `MAX_DREAM_JOBS`."""
    lines = ["**Dream list** (on-site, far from home)"]
    for row in rendered_dream_jobs(jobs):
        link = links.get(int(row["id"]))
        lines.append(format_job_line(row, base_url, url=link.url if link else ""))
    if len(jobs) > MAX_DREAM_JOBS:
        lines.append(f"…and {len(jobs) - MAX_DREAM_JOBS} more: <{base_url}/all>")
    return lines


def _plain_name(text: str) -> str:
    """A person or employer name from a feed or an import, defanged for Discord."""
    return _plain_span(text or "")[:MAX_ASK_NAME_CHARS]


def format_ask_line(row: sqlite3.Row, base_url: str) -> str:
    n = int(row["days_overdue"] or 0)
    overdue = f"{n} day{'s' if n != 1 else ''} overdue"
    who = _plain_name(row["recipient_name"])
    label = KIND_LABELS.get(row["kind"], row["kind"])
    if row["job_id"] is None:
        where = _plain_name(row["network_company"] or "")
        who += f" ({where})" if where else ""
        return f"• {who} · {label} · {overdue} · cockpit <{base_url}/network>"
    role = f"{_plain_name(row['title'])} at {_plain_name(row['company'])}"
    return f"• {who} · {label} · {role} · {overdue} · cockpit <{base_url}/jobs/{row['job_id']}>"


def format_ask_block(rows: Sequence[sqlite3.Row], base_url: str) -> list[str]:
    """The "Asks to follow up" section (#97): waiting outreach asks whose follow-up date has
    come. `rows` is `outreach_asks.rendered_due_followups`, the same list that gets stamped."""
    return ["**Asks to follow up**", *(format_ask_line(r, base_url) for r in rows)]


def format_recruiter_line(r: Reminder, base_url: str) -> str:
    who = _plain_name(r.name)
    firm = _plain_name(r.company)
    where = f" ({firm})" if firm else ""
    n = r.days_since
    age = f"{n} day{'s' if n != 1 else ''} since last touch"
    return f"• {who}{where} · {age} · cockpit <{base_url}/network>"


def format_recruiter_block(rows: Sequence[Reminder], base_url: str) -> list[str]:
    """The "Recruiters to touch" section (#98): recruiters you have spoken with whose last touch
    is old. `rows` is `recruiter_status.due_reminders`, the same list that gets stamped."""
    return ["**Recruiters to touch**", *(format_recruiter_line(r, base_url) for r in rows)]


def rendered_target_news(news: Sequence[TargetNewsItem]) -> list[TargetNewsItem]:
    """The events `build_digest` renders (`MAX_TARGET_NEWS`); the rest stay unnotified."""
    return list(news)[:MAX_TARGET_NEWS]


def _plain_span(span: str) -> str:
    """The span with links, markup and mention syntax removed (Discord embeds a bare URL)."""
    no_links = _BARE_URL.sub("link omitted", span)
    cleaned = " ".join(_UNSAFE_SPAN_CHARS.sub(" ", no_links).split())
    return cleaned[:MAX_TARGET_SPAN_CHARS]


def format_target_line(item: TargetNewsItem, base_url: str) -> str:
    n = item.contacts
    who = f"{n} contact{'s' if n != 1 else ''}"
    source = f"{BASIS_LABELS.get(item.basis, item.basis)}, {item.host}"
    label = EVENT_LABELS.get(item.event_type, item.event_type)
    return (
        f"• {item.company} · {label} · published {item.published_on} · {source}\n"
        f'   "{_plain_span(item.span)}" · {who} · '
        f"cockpit <{base_url}/targets/{quote(item.company_key, safe='')}>"
    )


def format_target_block(
    news: Sequence[TargetNewsItem], failed: int, base_url: str = COCKPIT_BASE
) -> list[str]:
    """The "Target news" section: new events (capped) and, when scans failed, one line
    saying so rather than nothing."""
    lines = ["**Target news**"]
    lines += [format_target_line(i, base_url) for i in rendered_target_news(news)]
    if len(news) > MAX_TARGET_NEWS:
        lines.append(f"…and {len(news) - MAX_TARGET_NEWS} more: <{base_url}/targets>")
    if failed:
        lines.append(
            f"{failed} target scan{'s' if failed != 1 else ''} failed: <{base_url}/targets>"
        )
    return lines


def format_reply_line(r: ReplyMatch, base_url: str = COCKPIT_BASE) -> str:
    """One entry: who replied, about what, and a one-line snippet -- surface
    only, never a draft or a suggested response (the user answers this themselves)."""
    return (
        f"• {r.company} — {r.title}: reply from {r.sender} ({r.when})\n"
        f'   "{r.snippet}" · cockpit <{base_url}/jobs/{r.job_id}>'
    )


def format_closed_line(closed: list[dict]) -> str:
    """One line naming kept jobs whose posting closed this run."""
    n = len(closed)
    names = ", ".join(f"{c['company']} {c['title']}" for c in closed[:MAX_CLOSED_NAMES])
    more = ", …" if n > MAX_CLOSED_NAMES else ""
    return f"{n} kept posting{'s' if n != 1 else ''} closed: {names}{more}"


def _top_sources(per_source: dict[str, int], limit: int = 5) -> str:
    """Top N sources by fetch count, comma-joined, with a trailing '…' if more."""
    parts = sorted(per_source.items(), key=lambda kv: kv[1], reverse=True)
    src = ", ".join(f"{k} {v}" for k, v in parts[:limit])
    if len(parts) > limit:
        src += ", …"
    return src


def format_duration(s: float) -> str:
    """'12m 03s' under an hour, '1h 34m' at/above an hour."""
    total = int(round(max(s, 0.0)))
    if total < 3600:
        m, sec = divmod(total, 60)
        return f"{m}m {sec:02d}s"
    h, rem = divmod(total, 3600)
    m = rem // 60
    return f"{h}h {m:02d}m"


def format_run_footer(res: DailyResult) -> str:
    """One italic line summarizing a run: timing, fetch mix, scoring throughput."""
    src = _top_sources(res.per_source)
    fetched_str = f"fetched {res.fetched}" + (f" ({src})" if src else "")
    scoring_str = ""
    if res.scoring_batches:
        scoring_str = (
            f" · scored {res.scored} in {res.scoring_batches} batches"
            f" / {format_duration(res.scoring_s)}"
        )
    if res.has_cost:
        scoring_str += f" · cost {describe_cost(res.total_cost)}"
    return f"_Run {format_duration(res.duration_s)} · {fetched_str}{scoring_str}_"

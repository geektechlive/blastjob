"""Watch for human replies about applications the user has already made -- so
a recruiter's answer cannot sit unread for days.

This is deliberately narrow: SURFACE ONLY. It never drafts and never sends a
reply; it reports that one exists and who it is from, and the user answers it
themselves in their own mail client. That is the whole feature.

Reads the user's PERSONAL mailbox (`personal_account`), which is a different
account from the alert feed's. This is the correction that made the feature
work at all: v1 reused `mailalerts`' alert account, where LinkedIn/Indeed
alerts land -- but recruiters write to the user's personal address instead.
Against the live alert mailbox the scanner fetched 58 messages and matched
none, while a real recruiter's reply sat in the personal mailbox, unseeable
by construction. A blank `personal_mail_*` field falls back to the alert
account, so an unconfigured install behaves as before.

It reuses `mailalerts.fetch_alert_messages` verbatim (imported, not
duplicated) for the connection + header-then-body fetch loop. The only new
IMAP call here is `fetch_message_dates`, a small supplementary header fetch
for "when a match replied" -- `fetch_alert_messages` doesn't carry the Date
header (mailalerts.py is out of scope for this feature: no schema changes,
no touching adapters that already work).

Matching is conservative on purpose -- a false positive here is worse than a
miss, because it would put a stranger's name in front of the user as if a
real lead. Two signals, in descending reliability:

1. The sender's email address exactly equals (case-insensitively) an address
   found in the job's `app_notes` free text, never a substring of one (this
   is how a recruiter reply is catchable today: the user pastes a recruiter's
   address into notes when they first reach out).
2. The sender's domain, squashed and stripped of its TLD, exactly equals the
   job's company name run through the same normalization
   (`core.company.company_key`) -- exact match only, common webmail domains
   excluded. This is still a guess (a domain can coincidentally look like a
   company name), so it is held to a strict equality bar rather than a
   substring one.

What this deliberately does NOT catch (v1, by design -- see CLAUDE.md):
company name mentioned only in the subject line, or a domain that merely
resembles the company loosely. Those are lower-confidence signals this
module skips rather than guesses at.

Only jobs the tracker considers "in process" are scanned
(`review_state='kept'` and `application_state` in applied/screen/interview/
offer) -- a reply about a job the user never applied to has nothing to match
against. Known alert senders (LinkedIn, Indeed), obvious automated senders
(no-reply/notifications/mailer-daemon/etc.) AND anything that matches no
applied-to job are all excluded at the header, before a body is ever
fetched -- so an ATS acknowledgment never becomes a "reply", and the user's
bank, medical and personal mail is never read into this process at all.

Bookkeeping reuses `mail_ledger` (no migration) with a distinct
`source='reply'` value, message_id-keyed like every other consumer of that
table, so a surfaced reply is reported once and never again.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import html as html_mod
import imaplib
import logging
import os
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

from blastjob.core.company import company_key
from blastjob.discovery import mailalerts as ma
from blastjob.discovery.mailalerts import LedgerEntry, MailMessage, fetch_alert_messages
from blastjob.discovery.pipeline import record_ledger
from blastjob.models.config import BlastJobConfig, DiscoveryConfig

log = logging.getLogger(__name__)

REPLY_SOURCE = "reply"
# Matches the alert path's window. A short window is a silent-loss path: four
# missed runs (laptop asleep, a PAUSE file left in place, a launchd hiccup) and
# a reply ages out having never been ledgered -- which is the exact failure
# this module exists to prevent. The mail_ledger dedupe makes a wide window
# free, and the header gate means a wider window costs no extra body fetches.
REPLY_LOOKBACK_DAYS = 14
MAX_SNIPPET_CHARS = 220
APPLIED_STATES = ("applied", "screen", "interview", "offer")

_AUTOMATED_SENDER = re.compile(
    r"no.?reply|donotreply|notifications?@|mailer-daemon|postmaster|automated@", re.I
)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
# Where the reply stops and the quoted original begins. Mail clients mark this
# boundary a handful of well-known ways; cutting at the first one keeps the
# digest snippet to what the human actually wrote, instead of the user's own
# message quoted back at them. Conservative: an unrecognized style just means
# the snippet is as long as it was before, never truncated wrongly.
_QUOTED_TAIL_RE = re.compile(
    r"-{2,}\s*Original Message\s*-{2,}"
    r"|\bFrom:\s.*?\bSent:\s"
    r"|\bOn\b.{0,80}?\bwrote:"
    r"|\b_{5,}\b",
    re.I | re.S,
)
_WS_RE = re.compile(r"\s+")
_WEBMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "yahoo.com",
        "outlook.com",
        "hotmail.com",
        "icloud.com",
        "me.com",
        "live.com",
        "aol.com",
        "protonmail.com",
    }
)
_MIN_DOMAIN_LABEL_LEN = 4


@dataclass(frozen=True)
class MailAccount:
    """One IMAP account, resolved from config. `user_env`/`password_env` are
    env var NAMES -- values live in the secrets file and are read at use."""

    host: str
    user_env: str
    password_env: str
    mailbox: str

    def credentials(self) -> tuple[str, str]:
        return os.environ.get(self.user_env, ""), os.environ.get(self.password_env, "")


def personal_account(d: DiscoveryConfig) -> MailAccount:
    """The mailbox recruiters actually write to. Every field falls back to the
    alert account when blank, so this is a no-op until the user configures it."""
    return MailAccount(
        host=d.personal_mail_imap_host or d.mail_imap_host,
        user_env=d.personal_mail_user_env or d.mail_user_env,
        password_env=d.personal_mail_password_env or d.mail_password_env,
        mailbox=d.personal_mail_mailbox or d.mail_mailbox,
    )


def replies_configured(cfg: BlastJobConfig) -> bool:
    """Whether reply scanning can run at all. Deliberately NOT gated on
    `mail_alerts_enabled`: that flag is about the alert account's LinkedIn/
    Indeed feed, and hanging replies off it means turning the job feed off
    silently stops watching for a recruiter's answer. Credentials decide."""
    user, password = personal_account(cfg.discovery).credentials()
    return bool(user and password)


def _is_reply_candidate(sender: str, subject: str) -> bool:
    """Header-only gate: not a known alert sender, not an obviously automated
    address. Necessary but not sufficient -- see `reply_gate`."""
    s = ma.normalize_sender(sender)
    if any(domain in s for domain in ma.ALERT_SENDER_DOMAINS):
        return False
    return not _AUTOMATED_SENDER.search(s)


def reply_gate(jobs: list[sqlite3.Row]) -> Callable[[str, str], bool]:
    """The gate `fetch_alert_messages` applies to headers before fetching a
    body. Matching needs only the sender, so it belongs HERE rather than after
    the body fetch: against the live mailbox 58 messages cleared the sender
    check -- bank, medical, shipping, personal -- and every one had its full
    body pulled into this process to be discarded. Deciding at the header
    means only mail that matches an applied-to job is ever read."""

    def gate(sender: str, subject: str) -> bool:
        if not _is_reply_candidate(sender, subject):
            return False
        job, _matched_by = match_job(sender, jobs)
        return job is not None

    return gate


def processed_reply_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT message_id FROM mail_ledger WHERE source=? AND status='ok'", (REPLY_SOURCE,)
    ).fetchall()
    return {str(r["message_id"]) for r in rows}


def _reply_candidate_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in APPLIED_STATES)
    return conn.execute(
        f"SELECT id, company, title, url, app_notes FROM jobs "
        f"WHERE review_state='kept' AND application_state IN ({placeholders}) "
        f"ORDER BY applied_at DESC, id DESC",
        APPLIED_STATES,
    ).fetchall()


def _sender_address(sender: str) -> str:
    m = _EMAIL_RE.search(sender or "")
    return m.group(0).lower() if m else ""


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _domain_label(domain: str) -> str:
    """Domain minus its final (TLD) segment, squashed -- 'acmecu.com' ->
    'acmecu'. Good enough for a v1 exact-match check; multi-part TLDs
    (co.uk) are not special-cased."""
    parts = domain.split(".")
    return _squash(".".join(parts[:-1])) if len(parts) > 1 else _squash(domain)


def match_job(sender: str, jobs: list[sqlite3.Row]) -> tuple[sqlite3.Row | None, str]:
    """(job, matched_by) for the first job this sender matches, or (None, "").
    `matched_by` is "app_notes" or "domain" -- see module docstring for what
    each requires and why case 3 (subject-line company mention) is not
    attempted here."""
    addr = _sender_address(sender)
    if not addr:
        return None, ""
    for job in jobs:
        notes = (job["app_notes"] or "").lower()
        if notes and addr in set(_EMAIL_RE.findall(notes)):
            return job, "app_notes"
    if "@" not in addr:
        return None, ""
    domain = addr.rsplit("@", 1)[-1]
    if domain in _WEBMAIL_DOMAINS:
        return None, ""
    domain_squash = _domain_label(domain)
    if len(domain_squash) < _MIN_DOMAIN_LABEL_LEN:
        return None, ""
    for job in jobs:
        if _squash(company_key(job["company"] or "")) == domain_squash:
            return job, "domain"
    return None, ""


def _snippet(text: str, limit: int = MAX_SNIPPET_CHARS) -> str:
    """First readable words of the reply, for the digest the user reads on
    their phone. `<style>`/`<script>` bodies are dropped whole, not just their
    tags: Outlook ships a stylesheet ahead of the text, so tag-stripping alone
    opened a live snippet with "P {margin-top:0;...}". Entities are
    decoded so quoted mail reads as "<user@...>" rather than "&lt;user@...&gt;"."""
    plain = _SCRIPT_STYLE_RE.sub(" ", text or "")
    plain = _TAG_RE.sub(" ", plain)
    plain = html_mod.unescape(plain)
    plain = _WS_RE.sub(" ", plain).strip()
    plain = _QUOTED_TAIL_RE.split(plain, maxsplit=1)[0].strip()
    return plain[:limit]


def fetch_message_dates(
    *,
    host: str,
    user: str,
    password: str,
    mailbox: str,
    uids: list[str],
    connect: Callable[[str], imaplib.IMAP4] | None = None,
) -> dict[str, str]:
    """Supplementary IMAP call for the (typically 0-3) surfaced matches only:
    `fetch_alert_messages` doesn't carry the Date header, and extending it is
    out of scope here (mailalerts.py belongs to the mail-alerts feature).
    Best-effort: a failure here must not lose an already-found match, so
    callers treat a missing uid as "date unknown" rather than retrying."""
    if not uids:
        return {}
    conn = (connect or ma.open_imap)(host)
    out: dict[str, str] = {}
    try:
        conn.login(user, password)
        conn.select(mailbox, readonly=True)
        for uid in uids:
            typ, data = conn.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (DATE)])")
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                continue
            hdr = email.message_from_bytes(data[0][1], policy=email.policy.default)
            raw = str(hdr.get("Date") or "")
            try:
                parsed = email.utils.parsedate_to_datetime(raw) if raw else None
            except (TypeError, ValueError):
                parsed = None
            out[uid] = parsed.date().isoformat() if parsed else ""
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
    return out


@dataclass(frozen=True)
class ReplyMatch:
    message_id: str
    job_id: int
    company: str
    title: str
    job_url: str
    sender: str
    subject: str
    when: str
    snippet: str
    matched_by: str  # "app_notes" | "domain"


async def scan_replies(
    cfg: BlastJobConfig,
    conn: sqlite3.Connection,
    *,
    fetch_messages: Callable[..., list[MailMessage]] = fetch_alert_messages,
    fetch_dates: Callable[..., dict[str, str]] = fetch_message_dates,
    connect: Callable[[str], imaplib.IMAP4] | None = None,
    lookback_days: int = REPLY_LOOKBACK_DAYS,
    today: date | None = None,
    record: bool = True,
) -> list[ReplyMatch]:
    """Scan the mailbox for human replies to jobs already applied to. Returns
    the surfaced matches; also records a `mail_ledger` row (source='reply',
    status='ok') per match so the next run doesn't re-surface it. Missing IMAP
    credentials or an empty applied-jobs list is a quiet no-op, not an error
    -- the caller (`daily._replies_pass`) decides whether to even call this
    based on `mail_alerts_enabled`."""
    import asyncio

    acct = personal_account(cfg.discovery)
    user, password = acct.credentials()
    if not user or not password:
        return []
    jobs = _reply_candidate_jobs(conn)
    if not jobs:
        return []

    since = (today or date.today()) - timedelta(days=lookback_days)
    already = processed_reply_ids(conn)
    messages = await asyncio.to_thread(
        fetch_messages,
        host=acct.host,
        user=user,
        password=password,
        since=since,
        skip_message_ids=already,
        mailbox=acct.mailbox,
        connect=connect,
        is_alert=reply_gate(jobs),
    )

    found: list[tuple[MailMessage, sqlite3.Row, str]] = []
    for m in messages:
        job, matched_by = match_job(m.sender, jobs)
        if job is not None:
            found.append((m, job, matched_by))
    if not found:
        return []

    try:
        dates = fetch_dates(
            host=acct.host,
            user=user,
            password=password,
            mailbox=acct.mailbox,
            uids=[m.uid for m, _job, _mb in found],
            connect=connect,
        )
    except Exception as e:  # noqa: BLE001 -- a match is worth reporting even undated
        log.debug("reply date fetch failed: %s", e)
        dates = {}

    fallback_when = (today or date.today()).isoformat()
    matches = [
        ReplyMatch(
            message_id=m.message_id,
            job_id=int(job["id"]),
            company=job["company"] or "",
            title=job["title"] or "",
            job_url=job["url"] or "",
            sender=m.sender,
            subject=m.subject,
            when=dates.get(m.uid) or fallback_when,
            snippet=_snippet(m.html),
            matched_by=matched_by,
        )
        for m, job, matched_by in found
    ]

    if record:
        record_ledger(
            conn,
            [LedgerEntry(m.message_id, REPLY_SOURCE, m.uid, "ok", 1) for m, _job, _mb in found],
        )
        _record_reply_events(conn, matches)
        conn.commit()
    return matches


def _record_reply_events(conn: sqlite3.Connection, matches: list[ReplyMatch]) -> None:
    """One `recruiter_reply` application event per surfaced reply, in the
    ledger's transaction. Only on a recording run: a dry run must leave no
    trace, or the real run's timeline would differ from what it surfaced."""
    from blastjob.core import events

    for m in matches:
        if events.has_event(conn, m.job_id, "recruiter_reply", ref=m.message_id):
            continue
        try:
            at = events.normalize_at(m.when)
        except ValueError:
            at = date.today().isoformat()
        events.insert_event(
            conn,
            m.job_id,
            "recruiter_reply",
            at,
            note=f"{m.sender}: {m.subject}"[:300],
            ref=m.message_id,
            source="replies",
        )

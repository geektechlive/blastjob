"""LinkedIn / Indeed job-alert emails as a discovery source (incremental).

Why email: LinkedIn is where most exec IT roles get posted, and the standing
decision (no browser automation, no unofficial APIs) rules out scraping it.
Job-alert emails are the sanctioned feed. Indeed alerts ride the same path.

How: the mailbox (any IMAP host works) is read over IMAP
(stdlib `imaplib`, TLS) with an app-specific password. Messages are selected
by header only — sender domain OR an alert-looking subject — because Indeed
mail arrives through an Apple Hide My Email relay that may rewrite the From
header. Then the alert HTML is parsed for job links and the text around them;
each job becomes a RawPosting whose description is the alert snippet, later
upgraded by `enrich.enrich_full_jds` from the posting's public JSON-LD when
that page is reachable (fail-soft: the snippet stays otherwise, and the
cockpit already badges short JDs).

Bookkeeping is a per-message ledger keyed by RFC Message-ID (a plain UID
watermark would skip a message that failed to parse and breaks on
UIDVALIDITY changes). The adapter is `snapshot = False`: it only reports what
is new, so the pipeline never stale-marks on its behalf.

Ledger status policy (`discovery.pipeline.processed_message_ids` only skips
'ok' and 'ignored' on the next run -- anything else is handed back and
re-fetched): a message that raises during parsing is 'error'; a recognized
sender/body that resolves to no source at all is 'ignored'; a *recognized*
alert (sender/body matched) that parses to exactly 0 postings is 'empty', not
'ok' -- a template restyle produces exactly this shape, and marking it 'ok'
would burn that Message-ID forever with no recovery short of hand-editing
`mail_ledger`. 'empty' is retried on every run while the message is still
within `mail_lookback_days` of `since`; once it ages past that window,
`fetch_alert_messages`'s IMAP SEARCH SINCE simply stops returning its UID, so
retries are bounded by the lookback window rather than by an attempt counter.
Only a genuinely non-empty parse records 'ok'.

Parsers are fixture-tested against the link/text shapes these alerts use;
they are tolerant (anchor href pattern + nearby text), because the vendors
restyle their templates without notice.
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import logging
import os
import re
import ssl
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from email.utils import parseaddr
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from blastjob.comp import parse_salary_range
from blastjob.discovery.base import RawPosting
from blastjob.discovery.linkedin_cards import SPLIT_KNOWN as SPLIT_KNOWN
from blastjob.discovery.linkedin_cards import SPLIT_VOCAB as SPLIT_VOCAB
from blastjob.discovery.linkedin_cards import guest_page_company as guest_page_company
from blastjob.discovery.linkedin_cards import is_card_text, split_card
from blastjob.discovery.linkedin_cards import known_company_names as known_company_names
from blastjob.discovery.linkedin_cards import split_card_text as split_card_text
from blastjob.discovery.linkedin_guest import JINA_KEY_ENV as JINA_KEY_ENV
from blastjob.discovery.linkedin_guest import _employer_body_pay as _employer_body_pay
from blastjob.discovery.linkedin_guest import enrich_linkedin_via_jina
from blastjob.discovery.linkedin_guest import (
    parse_linkedin_guest_page as parse_linkedin_guest_page,
)
from blastjob.discovery.textutil import normalize_region, split_location

log = logging.getLogger(__name__)

# Sender DOMAINS (IMAP FROM is a substring match; relays may rewrite the
# local part but keep the domain somewhere in the header).
ALERT_SENDER_DOMAINS = ("linkedin.com", "indeed.com")
# Subject fallback when the From header gives nothing away (relay rewrite).
# Deliberately no generic "new role"/"new position": LinkedIn's own
# notifications ("Congratulate X on their new role") would read as alerts and
# get their bodies fetched. Title words an alert subject leads with ("New
# director jobs ...") are a setting, `discovery.mail_alert_subject_terms`.
_ALERT_SUBJECT_BASE = (
    r"job alert|new jobs?|jobs? for you|jobs? (?:match|matching)|recommended jobs?|jobs? in\b"
)


def _alert_subject(terms: Iterable[str] = ()) -> re.Pattern[str]:
    words = [re.escape(t.strip()) for t in terms if t.strip()]
    extra = f"|new (?:{'|'.join(words)})" if words else ""
    return re.compile(_ALERT_SUBJECT_BASE + extra, re.I)


_ALERT_SUBJECT = _alert_subject()


def set_alert_subject_terms(terms: Iterable[str]) -> None:
    """Replace the configured subject title words (installed on config load)."""
    global _ALERT_SUBJECT
    _ALERT_SUBJECT = _alert_subject(terms)


_LI_VIEW = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?#]*-)?(\d{6,})")
_IN_JK = re.compile(r"\bjk=([0-9a-f]{8,})", re.I)
_WS = re.compile(r"\s+")
MAX_SNIPPET_CHARS = 600


@dataclass
class _Anchor:
    href: str
    text: str
    after: list[str] = field(default_factory=list)  # text lines until the next anchor


class _AnchorWalker(HTMLParser):
    """Collect (href, anchor text, following text lines) in document order."""

    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[_Anchor] = []
        self._in_a: _Anchor | None = None
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("style", "script"):
            self._skip += 1
        if tag == "a":
            href = dict(attrs).get("href") or ""
            self._in_a = _Anchor(href=href, text="")
            self.anchors.append(self._in_a)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("style", "script") and self._skip:
            self._skip -= 1
        if tag == "a":
            self._in_a = None

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        text = _WS.sub(" ", data).strip()
        if not text:
            return
        if self._in_a is not None:
            self._in_a.text = (self._in_a.text + " " + text).strip()
        elif self.anchors:
            self.anchors[-1].after.append(text)


def _walk(html: str) -> list[_Anchor]:
    w = _AnchorWalker()
    w.feed(html)
    return w.anchors


def _company_location(lines: list[str]) -> tuple[str, str]:
    """Alerts put 'Company · Location' (LinkedIn) or 'Company' / 'Location'
    on separate lines (Indeed) right after the title link."""
    for i, line in enumerate(lines[:4]):
        if "·" in line:
            company, _, loc = line.partition("·")
            return company.strip(), loc.strip()
        if " - " in line and i == 1 and lines[0]:
            return lines[0].strip(), line.strip()
    if len(lines) >= 2:
        return lines[0].strip(), lines[1].strip()
    if lines:
        return lines[0].strip(), ""
    return "", ""


def _snippet(lines: Iterable[str]) -> str:
    return " ".join(lines)[:MAX_SNIPPET_CHARS]


# LinkedIn labels nationwide-remote postings with the bare country as location.
_COUNTRY_ONLY = {"united states", "usa", "us"}


def _is_remote(title: str, loc: str) -> bool:
    text = f"{title} {loc}".lower()
    return "remote" in text or loc.strip().lower() in _COUNTRY_ONLY


def _is_whole_card(a: _Anchor) -> bool:
    """The anchor wraps the whole card (title, company, location) and nothing
    after it names a company."""
    return is_card_text(a.text) and not any("·" in line for line in a.after[:4])


def _card_posting(
    source: str, ext_id: str, url: str, a: _Anchor, known: Iterable[str]
) -> RawPosting:
    """Whole-card anchor. Only a KNOWN split is applied; a vocabulary split is a
    guess, so the title stays the card's head and the company stays empty until
    the guest page or the user confirms it (see `linkedin_cards`)."""
    card = split_card(a.text, known)
    applied = card.basis == SPLIT_KNOWN
    city, region = split_location(card.location)
    return RawPosting(
        source=source,
        external_id=ext_id,
        company=card.company if applied else "",
        title=card.title if applied else card.head,
        url=url,
        description=_snippet(a.after),
        location_raw=card.location,
        city=city,
        region=region,
        remote=True if card.remote or _is_remote("", card.location) else None,
        extra={"from_alert": True, "whole_card": True}
        | ({} if applied else {"card_head": card.head}),
    )


def _posting(source: str, ext_id: str, url: str, a: _Anchor) -> RawPosting:
    company, loc = _company_location(a.after)
    city, region = split_location(loc)
    return RawPosting(
        source=source,
        external_id=ext_id,
        company=company,
        title=a.text.strip(),
        url=url,
        description=_snippet(a.after),
        location_raw=loc,
        city=city,
        region=region,
        remote=True if _is_remote(a.text, loc) else None,
        extra={"from_alert": True},
    )


def _title_link_beats_card(prev: RawPosting, a: _Anchor) -> bool:
    """A later title-only link replaces an earlier whole-card one for the same
    job, but only when it carries the 'Company · Location' line after it: a bare
    "See job" anchor must never become the title."""
    if not prev.extra.get("whole_card") or is_card_text(a.text):
        return False
    company, _loc = _company_location(a.after)
    return bool(company) and any("·" in line for line in a.after[:4])


def parse_linkedin_alert(html: str, known_companies: Iterable[str] = ()) -> list[RawPosting]:
    out: dict[str, RawPosting] = {}
    known = frozenset(known_companies)
    for a in _walk(html):
        m = _LI_VIEW.search(a.href)
        if not m or not a.text.strip():
            continue
        jid = m.group(1)
        if jid in out and not _title_link_beats_card(out[jid], a):
            continue
        url = f"https://www.linkedin.com/jobs/view/{jid}/"
        if _is_whole_card(a):
            out[jid] = _card_posting("linkedin", jid, url, a, known)
        else:
            out[jid] = _posting("linkedin", jid, url, a)
    return list(out.values())


_INDEED_CHROME_TEXT = re.compile(
    r"^(?:more jobs|see all jobs|unsubscribe|edit alert|manage alerts)$", re.I
)
_INDEED_SEARCH_LINK = re.compile(r"indeed\.com/jobs\?q=", re.I)
_INDEED_CHROME_LINE = re.compile(
    r"^(?:easily apply|posted \d+\+?\s*days?\s*ago|just posted|urgently hiring|"
    r"responsive employer|hiring multiple candidates|active \d+\+?\s*days?\s*ago)$",
    re.I,
)
# Tracking wrappers (cts.indeed.com) carry the real viewjob URL, jk and all,
# percent-encoded in one of these query params.
_INDEED_WRAPPED_URL_KEYS = ("url", "u", "redirect", "target")
_INDEED_REMOTE_PREFIX = re.compile(r"^(?:hybrid remote in|remote in|hybrid in)\s+(.+)$", re.I)
_INDEED_JK_URL = re.compile(r"indeed\.com/(?:viewjob\?jk=|rc/clk/dl\?jk=)([0-9a-f]{8,})", re.I)


def _extract_indeed_jk(href: str) -> str | None:
    """jk from a direct Indeed link, or from the nested viewjob URL inside a
    click-tracking wrapper (cts.indeed.com/...&url=<encoded viewjob url>)."""
    q = parse_qs(urlparse(href).query)
    jk = next(iter(q.get("jk", [])), None)
    if jk:
        return jk
    for key in _INDEED_WRAPPED_URL_KEYS:
        nested = next(iter(q.get(key, [])), None)
        if not nested:
            continue
        m = _IN_JK.search(nested)
        if m:
            return m.group(1)
    m = _IN_JK.search(href)
    return m.group(1) if m else None


def _indeed_location(loc_line: str) -> tuple[bool | None, str, str, str]:
    """(remote, location_raw, city, region) for an Indeed location line —
    handles bare 'Remote', 'Hybrid remote in <City, ST>' and 'Remote in
    <State>' in addition to the plain 'City, ST' shape."""
    raw = loc_line.strip()
    if not raw:
        return None, "", "", ""
    low = raw.lower()
    if low == "remote":
        return True, raw, "", ""
    m = _INDEED_REMOTE_PREFIX.match(raw)
    if m:
        rest = m.group(1).strip()
        if "," in rest:
            city, region = split_location(rest)
        else:
            city, region = "", normalize_region(rest)
        return True, raw, city, region
    remote = True if "remote" in low else None
    city, region = split_location(raw)
    return remote, raw, city, region


def _indeed_comp(lines: list[str]) -> tuple[int, int, str, str]:
    """First line among the anchor's following lines that looks like pay
    ('$' plus a digit) becomes comp_summary; parse_salary_range fills the
    bounds (hourly rates fall below its $10k floor, so comp stays 0 but the
    summary text is kept)."""
    for line in lines:
        if "$" in line and re.search(r"\d", line):
            lo, hi, cur = parse_salary_range(line)
            return lo, hi, line.strip(), cur
    return 0, 0, "", "USD"


def _indeed_posting(jk: str, a: _Anchor) -> RawPosting:
    lines = [ln for ln in a.after if not _INDEED_CHROME_LINE.match(ln.strip())]
    company, loc_line = _company_location(lines)
    remote, location_raw, city, region = _indeed_location(loc_line)
    comp_min, comp_max, comp_summary, comp_currency = _indeed_comp(a.after)
    return RawPosting(
        source="indeed",
        external_id=jk,
        company=company,
        title=a.text.strip(),
        url=f"https://www.indeed.com/viewjob?jk={jk}",
        description=_snippet(lines),
        location_raw=location_raw,
        city=city,
        region=region,
        remote=remote,
        comp_min=comp_min,
        comp_max=comp_max,
        comp_currency=comp_currency,
        comp_summary=comp_summary,
        extra={"from_alert": True},
    )


# Title given to a text/plain alert link with no title line before it. pipeline.py
# keeps such a posting out of fingerprint clustering (see docs/how-it-works/same-role.md).
INDEED_PLACEHOLDER_TITLE = "Indeed posting"


def _parse_indeed_plaintext(text: str) -> list[RawPosting]:
    """Fallback for a text/plain alert (no anchors at all): scan raw lines for
    a viewjob/rc-click URL carrying jk=, using the nearest preceding
    non-URL, non-blank line as the title. Any URL line (matched or not)
    clears the pending title, so one posting's title never bleeds into the
    next block's when no title line precedes it."""
    out: dict[str, RawPosting] = {}
    pending_title = ""
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _INDEED_JK_URL.search(line)
        if m:
            jk = m.group(1)
            if jk not in out:
                title = pending_title or INDEED_PLACEHOLDER_TITLE
                out[jk] = RawPosting(
                    source="indeed",
                    external_id=jk,
                    title=title,
                    url=f"https://www.indeed.com/viewjob?jk={jk}",
                    description=title,
                    extra={"from_alert": True},
                )
            pending_title = ""
            continue
        if re.match(r"https?://", line, re.I):
            pending_title = ""
        else:
            pending_title = line
    return list(out.values())


def parse_indeed_alert(html: str) -> list[RawPosting]:
    anchors = _walk(html)
    if not anchors:
        return _parse_indeed_plaintext(html)
    out: dict[str, RawPosting] = {}
    for a in anchors:
        if "indeed.com" not in a.href:
            continue
        text = a.text.strip()
        if not text or _INDEED_CHROME_TEXT.match(text) or _INDEED_SEARCH_LINK.search(a.href):
            continue
        jk = _extract_indeed_jk(a.href)
        if not jk or jk in out:
            continue
        out[jk] = _indeed_posting(jk, a)
    return list(out.values())


_RELAY_AT = re.compile(r"([a-z0-9.+-]+)_at_([a-z0-9_]+)@")


def normalize_sender(sender: str) -> str:
    """Undo Apple Hide My Email's rewrite of a forwarded sender: the original
    address is folded into the relay's local part with '@' -> '_at_' and
    '.' -> '_' (e.g. 'donotreply_at_jobalert_indeed_com_abc123@example.com').
    Returns the lowercased header with the reconstructed address appended, so
    plain domain substring checks keep working. Anything else passes through."""
    s = (sender or "").lower()
    m = _RELAY_AT.search(s)
    if not m:
        return s
    return f"{s} {m.group(1)}@{m.group(2).replace('_', '.')}"


# The relay appends an opaque id after the folded domain ('..._indeed_com_a1b2c3d4e5_f6a7b8c').
# Those trailing labels hold a digit (or are one character); a real TLD or host label such
# as 'au' or 'evil' does not, so 'indeed.com.evil' is not mistaken for 'indeed.com'.
_RELAY_ID_LABELS = r"(?:\.(?=[0-9a-z]*\d|[0-9a-z]$)[0-9a-z]{1,16}){0,2}"


def sender_domains(sender: str) -> tuple[str, ...]:
    """Domains of the sender ADDRESS only (display names are attacker text): the
    address's own domain, plus the original domain when Hide My Email folded it into
    the local part. Lowercased; empty when no address parses."""
    addr = parseaddr(sender or "")[1].lower()
    if "@" not in addr:
        return ()
    domains = [addr.rsplit("@", 1)[1]]
    m = _RELAY_AT.search(addr)
    if m:
        domains.append(m.group(2).replace("_", "."))
    return tuple(domains)


def _from_domain(sender: str, domain: str) -> bool:
    """True when the sender address is at `domain` or one of its subdomains, or a Hide
    My Email relay folded such an address into its local part."""
    domains = sender_domains(sender)
    if not domains:
        return False
    addr_domain, *folded = domains
    if addr_domain == domain or addr_domain.endswith("." + domain):
        return True
    pattern = re.compile(rf"(?:^|\.){re.escape(domain)}{_RELAY_ID_LABELS}$")
    return any(pattern.search(f) for f in folded)


def looks_like_alert(sender: str, subject: str) -> bool:
    """Header-only gate before a body fetch. LinkedIn sends many notification
    mails from linkedin.com (impressions, comments, messages), so for LinkedIn
    the sender must be the job-alerts mailbox; Indeed mail is alerts only."""
    if _from_domain(sender, "linkedin.com"):
        s = normalize_sender(sender)
        return "jobalerts" in s or "jobs-noreply" in s or bool(_ALERT_SUBJECT.search(subject or ""))
    if _from_domain(sender, "indeed.com"):
        return True
    return bool(_ALERT_SUBJECT.search(subject or ""))


def parse_alert(
    sender: str, html: str, known_companies: Iterable[str] = ()
) -> tuple[str, list[RawPosting]]:
    """Route by sender domain when it's recognizable; otherwise let the body
    decide (a relay-forwarded Indeed alert still contains indeed.com job links).
    Returns (source, postings); nothing recognizable yields ('', [])."""
    if _from_domain(sender, "linkedin.com"):
        return "linkedin", parse_linkedin_alert(html, known_companies)
    if _from_domain(sender, "indeed.com"):
        return "indeed", parse_indeed_alert(html)
    found = parse_linkedin_alert(html, known_companies)
    if found:
        return "linkedin", found
    found = parse_indeed_alert(html)
    if found:
        return "indeed", found
    return "", []


# ---------- IMAP ----------


@dataclass(frozen=True)
class MailMessage:
    message_id: str
    uid: str
    sender: str
    subject: str
    html: str


def _html_body(msg: email.message.EmailMessage) -> str:
    part = msg.get_body(preferencelist=("html", "plain"))
    if part is None:
        return ""
    try:
        return part.get_content()
    except Exception:  # noqa: BLE001 — odd charset; treat as empty
        return ""


def _imap_date(d: date) -> str:
    return d.strftime("%d-%b-%Y")


# Per-socket-operation timeout. imaplib's default is none, so one stalled read
# held the whole discovery run (a thread cannot be cancelled from asyncio).
DEFAULT_IMAP_TIMEOUT_S = 30
# Wall-clock budget for one mailbox sweep, checked inside the fetch loop: a
# server that answers each command slowly never trips the socket timeout.
DEFAULT_IMAP_DEADLINE_S = 300.0


def open_imap(host: str, timeout: float = DEFAULT_IMAP_TIMEOUT_S) -> imaplib.IMAP4:
    """IMAP over TLS with the certificate and hostname verified. Without an
    explicit context imaplib uses an UNVERIFIED one, which would hand the app
    password to anyone able to intercept the connection. `timeout` bounds every
    socket operation."""
    return imaplib.IMAP4_SSL(host, ssl_context=ssl.create_default_context(), timeout=timeout)


def _close_imap(conn: imaplib.IMAP4) -> None:
    """Log out; if the server will not answer, drop the socket instead."""
    try:
        conn.logout()
    except Exception:  # noqa: BLE001
        try:
            conn.shutdown()
        except Exception:  # noqa: BLE001
            pass


def fetch_alert_messages(
    *,
    host: str,
    user: str,
    password: str,
    since: date,
    skip_message_ids: set[str],
    mailbox: str = "INBOX",
    connect: Callable[[str], imaplib.IMAP4] | None = None,
    is_alert: Callable[[str, str], bool] = looks_like_alert,
    timeout_s: float = DEFAULT_IMAP_TIMEOUT_S,
    deadline_s: float = DEFAULT_IMAP_DEADLINE_S,
    clock: Callable[[], float] = time.monotonic,
) -> list[MailMessage]:
    """Return alert-looking messages newer than `since` whose Message-ID isn't
    in `skip_message_ids`. Headers are fetched for every recent message (cheap),
    bodies only for the ones that pass `is_alert`. Read-only (BODY.PEEK):
    nothing is flagged, moved or deleted — the mailbox stays the user's.

    Bounded two ways: `timeout_s` per socket operation, and `deadline_s` for the
    whole sweep, checked before each message. Past the deadline
    the sweep stops and returns the messages read so far (TimeoutError only when
    none were read); the connection is closed on the way out and messages not
    returned are simply not recorded in the ledger, so the next run fetches them."""
    conn = connect(host) if connect else open_imap(host, timeout=timeout_s)
    deadline = clock() + deadline_s
    try:
        conn.login(user, password)
        conn.select(mailbox, readonly=True)
        # No charset argument: imaplib skipped the None this used to pass.
        typ, data = conn.uid("SEARCH", "SINCE", _imap_date(since))
        uids = data[0].split() if typ == "OK" and data and data[0] else []
        out: list[MailMessage] = []
        for uid in sorted(uids, key=lambda b: int(b)):
            if clock() > deadline:
                msg = f"IMAP sweep exceeded {deadline_s:g}s; closing the connection"
                if not out:
                    raise TimeoutError(msg)
                # Keep what was read: unrecorded messages retry next run, and
                # discarding them would let a slow mailbox never make progress.
                log.warning("%s; returning %d message(s) read so far", msg, len(out))
                break
            typ, data = conn.uid(
                "FETCH", uid.decode(), "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT)])"
            )
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                continue
            hdr = email.message_from_bytes(data[0][1], policy=email.policy.default)
            mid = (hdr.get("Message-ID") or "").strip()
            sender = str(hdr.get("From") or "")
            subject = str(hdr.get("Subject") or "")
            if not mid or mid in skip_message_ids or not is_alert(sender, subject):
                continue
            typ, data = conn.uid("FETCH", uid.decode(), "(BODY.PEEK[])")
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                continue
            full = email.message_from_bytes(data[0][1], policy=email.policy.default)
            out.append(
                MailMessage(
                    message_id=mid,
                    uid=uid.decode(),
                    sender=sender,
                    subject=subject,
                    html=_html_body(full),  # type: ignore[arg-type]
                )
            )
        return out
    finally:
        _close_imap(conn)


@dataclass(frozen=True)
class LedgerEntry:
    message_id: str
    source: str
    uid: str
    status: str  # "ok" | "error" | "ignored" | "empty" -- see module docstring
    postings: int
    error: str = ""


class MailAlertAdapter:
    """Incremental discovery source over alert emails. `fetch` is network-only;
    the ledger entries it produces are applied by the pipeline afterwards."""

    source = "mail-alerts"
    snapshot = False

    def __init__(
        self,
        *,
        host: str,
        user: str,
        password: str,
        skip_message_ids: set[str],
        lookback_days: int = 14,
        mailbox: str = "INBOX",
        enrich: bool = True,
        fetch_messages: Callable[..., list[MailMessage]] = fetch_alert_messages,
        timeout_s: int = 15,
        debug_dir: Path | None = None,
        known_companies: Iterable[str] = (),
        deadline_s: float = DEFAULT_IMAP_DEADLINE_S,
    ):
        # Company names already in the DB: a whole-card alert whose text ends
        # with one splits on it (see linkedin_cards).
        self.known_companies = frozenset(known_companies)
        self.host = host
        self.user = user
        self.password = password
        self.skip_message_ids = set(skip_message_ids)
        self.lookback_days = lookback_days
        self.mailbox = mailbox
        self.enrich = enrich
        self._fetch_messages = fetch_messages
        self.timeout_s = timeout_s
        self.deadline_s = deadline_s
        self.debug_dir = debug_dir
        self.ledger: list[LedgerEntry] = []
        # One (ledger entry, that message's postings) per message, in mailbox
        # order. The pipeline persists each batch with its ledger row in ONE
        # transaction, so a message whose postings fail to save is rolled back
        # and stays retryable instead of being marked done with its jobs lost.
        # Postings shared by several messages are the same object in each batch,
        # so enrichment (which edits them in place) reaches every copy.
        self.batches: list[tuple[LedgerEntry, list[RawPosting]]] = []

    def _dump_debug_html(self, m: MailMessage) -> None:
        """Best-effort capture of a message that parsed to 0 postings, so a
        template change can be diagnosed later. Never raises — a debug aid
        must not be able to sink the fetch."""
        if self.debug_dir is None:
            return
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            if len(list(self.debug_dir.glob("*.html"))) >= 20:
                return
            name = re.sub(r"[^A-Za-z0-9._-]+", "_", m.message_id)[:80] + ".html"
            (self.debug_dir / name).write_text(m.html, encoding="utf-8")
        except Exception as e:  # noqa: BLE001 — fail-soft debug capture
            log.debug("mail-alerts debug dump failed: %s", e)

    async def fetch(self) -> list[RawPosting]:
        import asyncio

        since = date.today() - timedelta(days=self.lookback_days)
        messages = await asyncio.to_thread(
            self._fetch_messages,
            host=self.host,
            user=self.user,
            password=self.password,
            since=since,
            skip_message_ids=self.skip_message_ids,
            mailbox=self.mailbox,
            timeout_s=self.timeout_s,
            deadline_s=self.deadline_s,
        )
        postings: dict[tuple[str, str], RawPosting] = {}
        ledger: list[LedgerEntry] = []
        batches: list[tuple[LedgerEntry, list[RawPosting]]] = []

        def record(entry: LedgerEntry, found: list[RawPosting] | None = None) -> None:
            ledger.append(entry)
            batches.append((entry, [postings[(p.source, p.external_id)] for p in found or []]))

        for m in messages:
            try:
                source, found = parse_alert(m.sender, m.html, self.known_companies)
            except Exception as e:  # noqa: BLE001 — one bad email never sinks the run
                record(LedgerEntry(m.message_id, "", m.uid, "error", 0, repr(e)[:200]))
                continue
            if not source:
                record(LedgerEntry(m.message_id, "", m.uid, "ignored", 0))
                continue
            if not found:
                log.warning(
                    "alert parsed to 0 postings: source=%s subject=%r uid=%s "
                    "-- status 'empty', retried until it ages past the %d-day lookback window",
                    source,
                    m.subject,
                    m.uid,
                    self.lookback_days,
                )
                self._dump_debug_html(m)
                record(LedgerEntry(m.message_id, source, m.uid, "empty", 0))
                continue
            for p in found:
                postings.setdefault((p.source, p.external_id), p)
            record(LedgerEntry(m.message_id, source, m.uid, "ok", len(found)), found)
        self.ledger = ledger
        self.batches = batches
        out = list(postings.values())
        if self.enrich and out:
            from blastjob.discovery.enrich import enrich_full_jds

            # LinkedIn: guest pages need the reader; everything else: JSON-LD.
            n_li = await enrich_linkedin_via_jina(out, os.environ.get(JINA_KEY_ENV, ""))
            others = [p for p in out if p.source != "linkedin"]
            n_other = await enrich_full_jds(others, timeout_s=self.timeout_s) if others else 0
            log.info("alert enrichment: %d linkedin, %d other postings upgraded", n_li, n_other)
        return out

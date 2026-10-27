"""Read recruiter mail the owner filed under ONE label, for the Recruiter mail page (#83).

The reply scanner (`replies.py`) sees only mail that matches a job already applied to, so a
recruiter's first message never reaches the app. This reader closes that gap without widening
what the app can see: it opens exactly the label the owner named in settings, read-only, and
no other folder is selected, listed or searched. The owner decides what is in scope by filing
the mail there. See docs/how-it-works/discovery.md.

What it does and does not do:

- Credentials, host and the account are the reply scanner's (`replies.personal_account`).
  An empty label or missing credentials return before any connection is opened.
- Every message in the label is taken; there is no sender gate. Automated senders are marked
  on the page, never hidden. Nothing is replied to, moved, labelled or flagged: every fetch is
  `BODY.PEEK`, and no model reads the mail.
- Identity is (mailbox, UIDVALIDITY, UID). Each run takes the UIDs above the stored watermark,
  oldest first, at most `MAX_PER_RUN`, so a backlog drains over runs and nothing is skipped
  past a failure: a message that cannot be fetched stops the sweep and is retried next run.
  The watermark lives in `mail_triage_cursor` (see `core/mail_triage.py`).
- Size: headers and the server's own receive time come first. A message up to
  `MAX_FULL_FETCH_BYTES` is then fetched whole; a larger one is recorded from its headers
  only, with a fixed snippet. No partial MIME parsing.
- The full body is never stored: only a plain-text snippet.
- `Authentication-Results` is advisory: only the topmost header, and only when it names
  `mx.google.com` as its authserv-id, is read. See `parse_auth_results`.
"""

from __future__ import annotations

import asyncio
import email
import email.policy
import email.utils
import imaplib
import logging
import re
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from blastjob.core import mail_triage
from blastjob.discovery import mailalerts as ma
from blastjob.discovery.mailalerts import open_imap
from blastjob.discovery.replies import _snippet, personal_account
from blastjob.label_names import recruiter_label_problem
from blastjob.models.config import BlastJobConfig

log = logging.getLogger(__name__)

MAX_PER_RUN = 50
MAX_FULL_FETCH_BYTES = 2 * 1024 * 1024
TOO_LARGE_SNIPPET = "Too large to preview here; open it in your mail."
UNREADABLE_SNIPPET = "Could not read this message here; open it in your mail."
MAX_SUBJECT_CHARS = 300
MAX_SNIPPET_CHARS = 500
MAX_NAME_CHARS = 120
MAX_ADDRESS_CHARS = 254
MAX_MESSAGE_ID_CHARS = 300
MAX_HEADER_BYTES = 65536
MAX_AUTH_HEADER_CHARS = 4096
MAX_FAILED_RUNS = 3
GMAIL_AUTHSERV_ID = "mx.google.com"
UNKNOWN = "unknown"
_RESULT_WORDS = frozenset({"pass", "fail", "softfail", "neutral", "none"})

PROBLEM_MISSING = "Recruiter label not found in the mailbox"
PROBLEM_UNREADABLE = "Recruiter label could not be read (the server gave no UIDVALIDITY)"

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")
_SIZE_RE = re.compile(rb"RFC822\.SIZE\s+(\d+)")
_INTERNALDATE_RE = re.compile(rb'INTERNALDATE\s+"([^"]+)"')
_AUTH_RESULT_RE = re.compile(r"(?<![\w.-])(spf|dkim|dmarc)=([A-Za-z]+)", re.I)


@dataclass(frozen=True)
class AuthResults:
    spf: str = UNKNOWN
    dkim: str = UNKNOWN
    dmarc: str = UNKNOWN


@dataclass(frozen=True)
class LabelMessage:
    uid: int
    message_id: str
    received_at: str
    header_date: str
    sender_name: str
    sender_address: str
    subject: str
    snippet: str
    auth: AuthResults
    received_estimated: bool = False


@dataclass(frozen=True)
class LabelBatch:
    """What one sweep read. `last_uid` is the highest UID passed (recorded or vanished); the
    watermark moves to it. `problem` is set when the label could not be read at all."""

    mailbox: str
    uidvalidity: int
    messages: tuple[LabelMessage, ...]
    last_uid: int
    problem: str = ""
    failed_uid: int = 0
    given_up: tuple[int, ...] = ()


@dataclass(frozen=True)
class LabelScan:
    recorded: int = 0
    ran: bool = False
    problem: str = ""

    @property
    def label_missing(self) -> bool:
        return self.problem == PROBLEM_MISSING


def _strip_comments(text: str) -> str:
    """`text` without RFC 5322 parenthesised comments (they nest). One pass, linear: a sender
    controls what is inside a comment, so a result word there must not be read as a result."""
    out: list[str] = []
    depth = 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
        elif not depth:
            out.append(ch)
    return "".join(out)


def parse_auth_results(headers: Sequence[str]) -> AuthResults:
    """spf, dkim and dmarc results from the topmost `Authentication-Results` header, but only
    when its authserv-id is `mx.google.com`. Gmail prepends its own header, and a sender can
    forge any lower one (or a top one naming another server), so nothing else is trusted:
    no header, or a top header from another server, gives all `unknown`. A result word outside
    pass, fail, softfail, neutral and none is `unknown`."""
    if not headers:
        return AuthResults()
    top = " ".join(headers[0][:MAX_AUTH_HEADER_CHARS].split())
    head, _, rest = top.partition(";")
    served_by = head.split()[0].lower() if head.split() else ""
    if served_by != GMAIL_AUTHSERV_ID:
        return AuthResults()
    found: dict[str, str] = {}
    for method, word in _AUTH_RESULT_RE.findall(_strip_comments(rest)):
        found.setdefault(method.lower(), word.lower())
    return AuthResults(
        **{
            m: (found[m] if found.get(m) in _RESULT_WORDS else UNKNOWN)
            for m in ("spf", "dkim", "dmarc")
        }
    )


def _clean(text: str, limit: int) -> str:
    return _WS_RE.sub(" ", _CONTROL_RE.sub(" ", text or "")).strip()[:limit]


def _received_at(meta: bytes) -> str | None:
    """The server's own receive time (ISO, UTC), or None when it is absent or unreadable."""
    m = _INTERNALDATE_RE.search(meta)
    if not m:
        return None
    try:
        parsed = datetime.strptime(m.group(1).decode("ascii").strip(), "%d-%b-%Y %H:%M:%S %z")
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


def _header_date(raw: str) -> str:
    """The Date header as an ISO day, or "" when it does not parse. Untrusted: display only."""
    try:
        parsed = email.utils.parsedate_to_datetime(raw) if raw else None
    except (TypeError, ValueError, IndexError):
        return ""
    return parsed.date().isoformat() if parsed else ""


def _body_text(full: bytes) -> str:
    msg = email.message_from_bytes(full, policy=email.policy.default)
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    return str(part.get_content())


def _literal(data: object) -> tuple[bytes, bytes] | None:
    """(response prefix, literal bytes) from an imaplib FETCH reply, or None when the server
    returned nothing for the message (it was expunged)."""
    if not isinstance(data, list) or not data or not isinstance(data[0], tuple):
        return None
    prefix, body = data[0][0], data[0][1]
    if not isinstance(prefix, bytes) or not isinstance(body, bytes):
        return None
    return prefix, body


def _build_message(
    uid: int,
    head: bytes,
    full: bytes | None,
    received_at: str,
    snippet_override: str,
    estimated: bool = False,
) -> LabelMessage:
    """A recorded message from its headers (and body, when read). A message that will not
    parse still becomes a row with blank fields, so the watermark never has to skip it."""
    try:
        hdr = email.message_from_bytes(head, policy=email.policy.default)
        name, address = email.utils.parseaddr(str(hdr.get("From") or ""))
        address = address.strip().lower()
        if "@" not in address or len(address) > MAX_ADDRESS_CHARS:
            address = ""
        if snippet_override:
            snippet = snippet_override
        else:
            snippet = _clean(
                _snippet(_body_text(full or b""), MAX_SNIPPET_CHARS), MAX_SNIPPET_CHARS
            )
        return LabelMessage(
            uid=uid,
            message_id=_clean(str(hdr.get("Message-ID") or ""), MAX_MESSAGE_ID_CHARS),
            received_at=received_at,
            header_date=_header_date(str(hdr.get("Date") or "")),
            sender_name=_clean(name, MAX_NAME_CHARS),
            sender_address=address,
            subject=_clean(str(hdr.get("Subject") or ""), MAX_SUBJECT_CHARS),
            snippet=snippet,
            auth=parse_auth_results([str(h) for h in hdr.get_all("Authentication-Results") or []]),
            received_estimated=estimated,
        )
    except Exception:  # noqa: BLE001 -- one odd message must not stall the whole label
        log.warning("label mail uid %d could not be parsed; recorded without details", uid)
        return LabelMessage(
            uid, "", received_at, "", "", "", "", UNREADABLE_SNIPPET, AuthResults(), estimated
        )


_OK = "ok"
_VANISHED = "vanished"
_FAILED = "failed"


def _meta(data: object) -> bytes | None:
    """The size and receive-time reply (no literal), or None when the server returned nothing
    for the message (it was expunged)."""
    if not isinstance(data, list) or not data or not isinstance(data[0], bytes):
        return None
    return data[0]


def _fetch_one(conn: imaplib.IMAP4, uid: int) -> tuple[str, LabelMessage | None]:
    """Size and receive time first (a request with no literal, so they parse reliably), then a
    capped header fetch, then the whole message only when it is known to be small. A missing
    size is treated as too large: header-only is the safe side. A missing receive time falls
    back to the time this row is recorded, and the row says so."""
    typ, data = conn.uid("FETCH", str(uid), "(RFC822.SIZE INTERNALDATE)")
    if typ != "OK":
        return _FAILED, None
    meta = _meta(data)
    if meta is None:
        return _VANISHED, None
    received = _received_at(meta)
    estimated = received is None
    stamp = received or datetime.now(UTC).isoformat(timespec="seconds")
    typ, data = conn.uid("FETCH", str(uid), f"(BODY.PEEK[HEADER]<0.{MAX_HEADER_BYTES}>)")
    if typ != "OK":
        return _FAILED, None
    head = _literal(data)
    if head is None:
        return _VANISHED, None
    header_bytes = head[1][:MAX_HEADER_BYTES]
    size_match = _SIZE_RE.search(meta)
    if size_match is None or int(size_match.group(1)) > MAX_FULL_FETCH_BYTES:
        return _OK, _build_message(uid, header_bytes, None, stamp, TOO_LARGE_SNIPPET, estimated)
    typ, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
    if typ != "OK":
        return _FAILED, None
    body = _literal(data)
    if body is None:
        return _VANISHED, None
    raw = body[1]
    if len(raw) > MAX_FULL_FETCH_BYTES:
        return _OK, _build_message(uid, header_bytes, None, stamp, TOO_LARGE_SNIPPET, estimated)
    return _OK, _build_message(uid, header_bytes, raw, stamp, "", estimated)


def _uidvalidity(conn: imaplib.IMAP4) -> int | None:
    _typ, data = conn.response("UIDVALIDITY")
    raw = data[0] if data else None
    if isinstance(raw, bytes) and raw.strip().isdigit():
        return int(raw)
    return None


def fetch_label(
    *,
    host: str,
    user: str,
    password: str,
    label: str,
    cursors: Mapping[int, int],
    failures: Mapping[tuple[int, int], int] | None = None,
    connect: Callable[[str], imaplib.IMAP4] | None = None,
    timeout_s: float = ma.DEFAULT_IMAP_TIMEOUT_S,
    deadline_s: float = ma.DEFAULT_IMAP_DEADLINE_S,
    clock: Callable[[], float] = time.monotonic,
    limit: int = MAX_PER_RUN,
) -> LabelBatch:
    """Sweep the label once. Selects exactly `label` (quoted, read-only); `cursors` maps a
    UIDVALIDITY to the highest UID already recorded under it. Bounded per socket operation
    (`timeout_s`) and for the whole sweep (`deadline_s`), checked before each message."""
    conn = connect(host) if connect else open_imap(host, timeout=timeout_s)
    deadline = clock() + deadline_s
    try:
        conn.login(user, password)
        typ, _data = conn.select(f'"{label}"', readonly=True)
        if typ != "OK":
            log.warning("recruiter label %r was not found in the mailbox; nothing read", label)
            return LabelBatch(label, 0, (), 0, problem=PROBLEM_MISSING)
        validity = _uidvalidity(conn)
        if validity is None:
            log.warning("recruiter label %r gave no UIDVALIDITY; nothing read", label)
            return LabelBatch(label, 0, (), 0, problem=PROBLEM_UNREADABLE)
        after = cursors.get(validity, 0)
        typ, found = conn.uid("SEARCH", "UID", f"{after + 1}:*")
        if typ != "OK":
            raise imaplib.IMAP4.error("the label search failed")
        listed = found[0].split() if found and isinstance(found[0], bytes) else []
        # `N:*` always returns the newest message even when it is below N, so filter here.
        uids = sorted(u for u in (int(x) for x in listed) if u > after)[:limit]
        out: list[LabelMessage] = []
        gave_up: list[int] = []
        failed = 0
        last = 0
        for uid in uids:
            if clock() > deadline:
                log.warning("label sweep passed %gs; stopping with %d read", deadline_s, len(out))
                break
            if (failures or {}).get((validity, uid), 0) >= MAX_FAILED_RUNS:
                log.warning(
                    "label mail uid %d failed %d runs; giving up on it", uid, MAX_FAILED_RUNS
                )
                gave_up.append(uid)
                last = uid
                continue
            state, message = _fetch_one(conn, uid)
            if state == _FAILED:
                log.warning("label mail uid %d could not be fetched; stopping to retry it", uid)
                failed = uid
                break
            last = uid
            if message is not None:
                out.append(message)
        return LabelBatch(
            label, validity, tuple(out), last, failed_uid=failed, given_up=tuple(gave_up)
        )
    finally:
        ma._close_imap(conn)


async def scan_label(
    cfg: BlastJobConfig,
    conn: sqlite3.Connection,
    *,
    connect: Callable[[str], imaplib.IMAP4] | None = None,
    record: bool = True,
    clock: Callable[[], float] = time.monotonic,
    deadline_s: float = ma.DEFAULT_IMAP_DEADLINE_S,
) -> LabelScan:
    """Read the label and record new messages. An empty label, a label the rules refuse, or
    missing credentials return before any connection. A label missing on the server is a
    warning and a `problem` the caller reports; it never raises. With `record=False` (a dry
    run) nothing is written and the count is 0."""
    d = cfg.discovery
    label = d.recruiter_label.strip()
    if not label:
        return LabelScan()
    if recruiter_label_problem(label, d.personal_mail_mailbox, d.mail_mailbox):
        log.warning("recruiter label is not allowed; recruiter mail triage is off")
        return LabelScan()
    acct = personal_account(d)
    user, password = acct.credentials()
    if not user or not password:
        return LabelScan()
    batch = await asyncio.to_thread(
        fetch_label,
        host=acct.host,
        user=user,
        password=password,
        label=label,
        cursors=mail_triage.cursors(conn, label),
        failures=mail_triage.failure_counts(conn, label),
        connect=connect,
        clock=clock,
        deadline_s=deadline_s,
    )
    if batch.problem:
        return LabelScan(ran=True, problem=batch.problem)
    if not record:
        return LabelScan(ran=True)
    return LabelScan(ran=True, recorded=mail_triage.record_batch(conn, batch))

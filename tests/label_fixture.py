"""Invented fixtures for the recruiter-label triage tests (#83): a strict fake IMAP object and a
message builder. Every address is example.com, every name is made up."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field

import pytest

from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig

LABEL = "Inbound/Leads"
USER_ENV = "TEST_LABEL_MAIL_USER"
PASS_ENV = "TEST_LABEL_MAIL_PASSWORD"
GOOD_AUTH = "mx.google.com; spf=pass smtp.mailfrom=firm.example.com; dkim=pass; dmarc=pass"


def raw_message(
    *,
    sender: str = "Ria Fox <ria@firm.example.com>",
    subject: str = "A VP role you might like",
    body: str = "Hello, I have a role for you.",
    message_id: str | None = "<m1@firm.example.com>",
    date: str | None = "Mon, 26 Oct 2026 09:00:00 +0000",
    auth: tuple[str, ...] = (GOOD_AUTH,),
    html: bool = False,
) -> bytes:
    lines = [f"Authentication-Results: {a}" for a in auth]
    lines.append(f"From: {sender}")
    lines.append(f"Subject: {subject}")
    if message_id is not None:
        lines.append(f"Message-ID: {message_id}")
    if date is not None:
        lines.append(f"Date: {date}")
    lines.append("MIME-Version: 1.0")
    kind = "text/html" if html else "text/plain"
    lines.append(f'Content-Type: {kind}; charset="utf-8"')
    return ("\r\n".join(lines) + "\r\n\r\n" + body + "\r\n").encode()


@dataclass
class Msg:
    raw: bytes
    internaldate: str = "26-Oct-2026 10:00:00 +0000"
    size: int | None = None
    omit_size: bool = False
    omit_internaldate: bool = False


@dataclass
class FakeImap:
    """Allows exactly the commands the reader may use; anything else raises."""

    messages: dict[int, Msg]
    labels: tuple[str, ...] = (LABEL,)
    uidvalidity: int | None = 7
    fail_uids: set[int] = field(default_factory=set)
    vanished_uids: set[int] = field(default_factory=set)
    calls: list[tuple] = field(default_factory=list)
    fetch_specs: list[str] = field(default_factory=list)
    fetched_full: list[int] = field(default_factory=list)

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.calls.append(("login",))
        return "OK", [b"ok"]

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.calls.append(("select", mailbox, readonly))
        name = mailbox[1:-1] if mailbox.startswith('"') and mailbox.endswith('"') else mailbox
        if name not in self.labels:
            return "NO", [b"Unknown Mailbox"]
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code: str) -> tuple[str, list[bytes | None]]:
        self.calls.append(("response", code))
        if code == "UIDVALIDITY" and self.uidvalidity is not None:
            return code, [str(self.uidvalidity).encode()]
        return code, [None]

    def uid(self, command: str, *args: str) -> tuple[str, list]:
        self.calls.append(("uid", command, *args))
        if command == "SEARCH":
            assert args[0] == "UID", args
            low = int(args[1].split(":")[0])
            found = sorted(u for u in self.messages if u >= low)
            if not found and self.messages:
                found = [max(self.messages)]  # the real server's `N:*` quirk
            return "OK", [" ".join(str(u) for u in found).encode()]
        assert command == "FETCH", command
        uid = int(args[0])
        spec = args[1]
        self.fetch_specs.append(spec)
        assert "BODY" not in spec or "PEEK" in spec, f"a fetch that would mark mail read: {spec}"
        if uid in self.fail_uids:
            return "NO", [b"fetch failed"]
        if uid in self.vanished_uids or uid not in self.messages:
            return "OK", [None]
        msg = self.messages[uid]
        size = msg.size if msg.size is not None else len(msg.raw)
        if "RFC822.SIZE" in spec:
            assert "BODY" not in spec, f"size and date must come without a literal: {spec}"
            parts = [f"UID {uid}"]
            if not msg.omit_size:
                parts.append(f"RFC822.SIZE {size}")
            if not msg.omit_internaldate:
                parts.append(f'INTERNALDATE "{msg.internaldate}"')
            return "OK", [f"{uid} ({' '.join(parts)})".encode()]
        if "HEADER" in spec:
            assert "<0.65536>" in spec, f"the header fetch must be capped: {spec}"
            head = msg.raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
            prefix = f"{uid} (UID {uid} BODY[HEADER]<0> {{{len(head)}}}".encode()
            return "OK", [(prefix, head[:65536]), b")"]
        self.fetched_full.append(uid)
        return "OK", [(f"{uid} (BODY[] {{{len(msg.raw)}}}".encode(), msg.raw), b")"]

    def logout(self) -> tuple[str, list[bytes]]:
        self.calls.append(("logout",))
        return "BYE", [b"bye"]

    def __getattr__(self, name: str):
        raise AssertionError(f"the label reader called IMAP {name!r}, which it may not")


def make_cfg(label: str = LABEL) -> BlastJobConfig:
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    d = cfg.discovery
    d.personal_mail_imap_host = "imap.example.com"
    d.personal_mail_user_env = USER_ENV
    d.personal_mail_password_env = PASS_ENV
    d.recruiter_label = label
    return cfg


def connector(fake: FakeImap):
    def connect(host: str) -> FakeImap:
        assert host == "imap.example.com"
        return fake

    return connect


def no_connection(host: str):
    raise AssertionError("a connection was opened")


@pytest.fixture
def mail_env(monkeypatch):
    monkeypatch.setenv(USER_ENV, "owner@example.com")
    monkeypatch.setenv(PASS_ENV, "not-a-real-password")
    cfg = make_cfg()
    conn = dbc.init_db(cfg)
    yield cfg, conn
    conn.close()


def insert_triage(conn, uid: int = 1, **over: object) -> int:
    row: dict[str, object] = {
        "mailbox": LABEL,
        "uidvalidity": 7,
        "uid": uid,
        "message_id": f"<t{uid}@firm.example.com>",
        "received_at": "2026-10-26T10:00:00+00:00",
        "sender_name": "Ria Fox",
        "sender_address": "ria@firm.example.com",
        "subject": "A role",
        "snippet": "Hello",
        "auth_dmarc": "pass",
        "created_at": "2026-10-26T10:05:00",
    }
    row.update(over)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    cur = conn.execute(f"INSERT INTO mail_triage ({cols}) VALUES ({marks})", tuple(row.values()))
    conn.commit()
    return int(cur.lastrowid or 0)

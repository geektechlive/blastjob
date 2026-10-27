"""Migration 72: the recruiter-mail triage tables (#83), kept out of `schema.py` for size.

`mail_triage` is the label reader's own ledger: identity is (mailbox, uidvalidity, uid), the
Message-ID is stored but not unique, and no message body is ever stored. `status` and the two
foreign keys move together (the CHECKs make a half-linked row impossible), and `version` is an
integer compare-and-set counter bumped by every decision. `mail_triage_cursor` holds the
watermark per (mailbox, uidvalidity): the highest uid the reader has passed. It is separate
from the rows because a message skipped as already-seen after a UIDVALIDITY change leaves no
row, and the watermark must still move past it. `mail_triage_failures` counts consecutive runs
in which one message could not be fetched; after three the reader gives up on it, moves the
watermark past it and the digest says so once (`notified_at`). `received_estimated` marks a
row whose receive time the server did not give (the recording time is used).
The migration runner splits on semicolons, so none appear inside a statement.
"""

from __future__ import annotations

TRIAGE_MIGRATION = """
CREATE TABLE IF NOT EXISTS mail_triage (
    id                 INTEGER PRIMARY KEY,
    mailbox            TEXT NOT NULL,
    uidvalidity        INTEGER NOT NULL,
    uid                INTEGER NOT NULL,
    message_id         TEXT NOT NULL DEFAULT '',
    received_at        TEXT NOT NULL,
    received_estimated INTEGER NOT NULL DEFAULT 0 CHECK (received_estimated IN (0, 1)),
    header_date        TEXT NOT NULL DEFAULT '',
    sender_name        TEXT NOT NULL DEFAULT '',
    sender_address     TEXT NOT NULL DEFAULT '',
    subject            TEXT NOT NULL DEFAULT '' CHECK (length(subject) <= 300),
    snippet            TEXT NOT NULL DEFAULT '' CHECK (length(snippet) <= 500),
    auth_spf           TEXT NOT NULL DEFAULT 'unknown',
    auth_dkim          TEXT NOT NULL DEFAULT 'unknown',
    auth_dmarc         TEXT NOT NULL DEFAULT 'unknown',
    status             TEXT NOT NULL DEFAULT 'new'
                       CHECK (status IN ('new', 'linked', 'dismissed')),
    version            INTEGER NOT NULL DEFAULT 0,
    job_id             INTEGER REFERENCES jobs(id),
    network_contact_id INTEGER REFERENCES network_contacts(id),
    decided_at         TEXT,
    notified_at        TEXT,
    created_at         TEXT NOT NULL,
    UNIQUE (mailbox, uidvalidity, uid),
    CHECK (job_id IS NULL OR network_contact_id IS NULL),
    CHECK ((status = 'linked') = (job_id IS NOT NULL OR network_contact_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_mail_triage_status ON mail_triage(status, received_at);
CREATE INDEX IF NOT EXISTS idx_mail_triage_message ON mail_triage(message_id);
CREATE TABLE IF NOT EXISTS mail_triage_cursor (
    mailbox     TEXT NOT NULL,
    uidvalidity INTEGER NOT NULL,
    last_uid    INTEGER NOT NULL,
    PRIMARY KEY (mailbox, uidvalidity)
);
CREATE TABLE IF NOT EXISTS mail_triage_failures (
    mailbox     TEXT NOT NULL,
    uidvalidity INTEGER NOT NULL,
    uid         INTEGER NOT NULL,
    runs        INTEGER NOT NULL DEFAULT 0,
    given_up    INTEGER NOT NULL DEFAULT 0 CHECK (given_up IN (0, 1)),
    notified_at TEXT,
    PRIMARY KEY (mailbox, uidvalidity, uid)
);
"""

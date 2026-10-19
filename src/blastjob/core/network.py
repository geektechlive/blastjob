"""The user's own LinkedIn connections export -> warm paths into target companies.

The user is many years into one metro area across a long employer history; everyone
they have ever worked with is still in that market, now years further into
their careers. At a senior level, roles often move through referral and
retained search rather than cold applications. This module turns the user's
LinkedIn "Connections.csv"
export (their data, downloaded through LinkedIn's own sanctioned data-export
path -- no scraping, no automation) into two signals per job: who they already
know there, and whether they already worked there themselves.

Everything here is a deterministic SQL join or a regex. No LLM calls, no
network calls.
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import AliasChoices, BaseModel, Field, ValidationError, field_validator

from blastjob.core.company import company_key
from blastjob.models.config import CandidateConfig

# --------------------------------------------------------------------------
# CSV import
# --------------------------------------------------------------------------


class _ConnectionRow(BaseModel):
    """One row of LinkedIn's `Connections.csv` -- an external boundary, so it
    gets a schema rather than a bare dict. Company and Position are CURRENT
    only; the export carries no employment history."""

    model_config = {"extra": "ignore"}

    first_name: str = Field(default="", alias="First Name")
    last_name: str = Field(default="", alias="Last Name")
    url: str = Field(default="", alias="URL")
    email: str = Field(default="", alias="Email Address")
    company: str = Field(default="", alias="Company")
    position: str = Field(default="", alias="Position")
    connected_on: str = Field(default="", alias="Connected On")

    @field_validator("*", mode="before")
    @classmethod
    def _blank_for_none(cls, v: object) -> object:
        return "" if v is None else v

    @field_validator(
        "first_name", "last_name", "url", "email", "company", "position", "connected_on"
    )
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


@dataclass(frozen=True)
class ImportResult:
    """Outcome of one `import_connections_csv` run."""

    added: int = 0
    updated: int = 0
    skipped: int = 0


_MONTH_ABBREVS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def _parse_connected_on(raw: str) -> str:
    """'05 Jun 2023' -> '2023-06-05'; unparseable input -> ''.

    Deliberately not `strptime("%b")`: month abbreviations there are locale
    dependent, which would make this non-deterministic across machines.
    """
    parts = raw.strip().split()
    if len(parts) != 3:
        return ""
    day_s, mon_s, year_s = parts
    month = _MONTH_ABBREVS.get(mon_s[:3].lower())
    if month is None:
        return ""
    try:
        return date(int(year_s), month, int(day_s)).isoformat()
    except ValueError:
        return ""


def _skip_preamble(text: str) -> str:
    """LinkedIn's export starts with a short 'Notes:' preamble before the real
    header row -- scan for the first line starting with 'First Name,' rather
    than assuming line 1 is the header."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("First Name,"):
            return "\n".join(lines[i:])
    raise ValueError("Connections.csv: no 'First Name,' header row found")


def _find_existing(conn: sqlite3.Connection, url: str, full_name: str) -> sqlite3.Row | None:
    """Identity is the profile URL; a blank URL falls back to a lowercased
    full-name match. The `url = ''` clause is required in the fallback branch
    -- without it a blank-URL row could match someone who already has a URL,
    silently merging two different people."""
    if url:
        return conn.execute("SELECT id FROM network_contacts WHERE url=?", (url,)).fetchone()
    return conn.execute(
        "SELECT id FROM network_contacts WHERE url='' AND lower(full_name)=?",
        (full_name.lower(),),
    ).fetchone()


def _import_csv_text(conn: sqlite3.Connection, text: str) -> ImportResult:
    body = _skip_preamble(text)
    reader = csv.DictReader(io.StringIO(body))
    added = updated = skipped = 0
    now = datetime.now(UTC).isoformat()

    for raw_row in reader:
        try:
            row = _ConnectionRow.model_validate(raw_row)
        except ValidationError:
            skipped += 1
            continue

        full_name = f"{row.first_name} {row.last_name}".strip()
        if not full_name:
            skipped += 1
            continue

        ckey = company_key(row.company)
        seniority = classify_seniority(row.position)
        recruiter = 1 if is_recruiter(row.position) else 0
        connected_iso = _parse_connected_on(row.connected_on)

        existing = _find_existing(conn, row.url, full_name)
        if existing is None:
            conn.execute(
                "INSERT INTO network_contacts "
                "(full_name, first_name, last_name, url, email, company, company_key, "
                " position, connected_on, seniority, is_recruiter, notes, imported_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    full_name,
                    row.first_name,
                    row.last_name,
                    row.url,
                    row.email,
                    row.company,
                    ckey,
                    row.position,
                    connected_iso,
                    seniority,
                    recruiter,
                    "",
                    now,
                ),
            )
            added += 1
        else:
            # notes is deliberately excluded from this UPDATE -- a re-import
            # must never clobber anything the user wrote by hand.
            conn.execute(
                "UPDATE network_contacts SET full_name=?, first_name=?, last_name=?, "
                "email=?, company=?, company_key=?, position=?, connected_on=?, "
                "seniority=?, is_recruiter=?, imported_at=? WHERE id=?",
                (
                    full_name,
                    row.first_name,
                    row.last_name,
                    row.email,
                    row.company,
                    ckey,
                    row.position,
                    connected_iso,
                    seniority,
                    recruiter,
                    now,
                    existing["id"],
                ),
            )
            updated += 1

    conn.commit()
    return ImportResult(added=added, updated=updated, skipped=skipped)


def import_connections_csv(conn: sqlite3.Connection, path: str | Path) -> ImportResult:
    """Parse LinkedIn's `Connections.csv`, normalize, and upsert by `url`
    (falling back to lowercased full name when the URL is blank). Re-importing
    a newer export UPDATEs company/position/seniority rather than duplicating
    rows, and never clobbers a manually written `notes` value."""
    text = Path(path).read_text(encoding="utf-8-sig")
    return _import_csv_text(conn, text)


# --------------------------------------------------------------------------
# Full data-export archive: Recommendations_Received.csv, messages.csv,
# Company Follows.csv, layered on top of Connections.csv above.
# --------------------------------------------------------------------------


class _RecommendationRow(BaseModel):
    """One row of LinkedIn's `Recommendations_Received.csv`."""

    model_config = {"extra": "ignore"}

    first_name: str = Field(default="", alias="First Name")
    last_name: str = Field(default="", alias="Last Name")

    @field_validator("*", mode="before")
    @classmethod
    def _blank_for_none(cls, v: object) -> object:
        return "" if v is None else v

    @field_validator("first_name", "last_name")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class _MessageRow(BaseModel):
    """One row of LinkedIn's `messages.csv`. Deliberately does NOT declare a
    field for the message body/subject column -- `extra='ignore'` means that
    content is parsed by csv.DictReader into the raw dict but never touches
    this model, so it can never be forwarded anywhere downstream."""

    model_config = {"extra": "ignore"}

    from_name: str = Field(default="", alias="FROM")
    to_name: str = Field(default="", alias="TO")
    date: str = Field(default="", alias="DATE")

    @field_validator("*", mode="before")
    @classmethod
    def _blank_for_none(cls, v: object) -> object:
        return "" if v is None else v

    @field_validator("from_name", "to_name", "date")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class _FollowRow(BaseModel):
    """One row of LinkedIn's `Company Follows.csv`.

    The real export names the column **Organization**, not Company -- verified
    against a live archive on 2026-09-10, where assuming "Company" silently
    parsed every row to blank and imported nothing. Both spellings are accepted
    because the header has varied by export vintage.
    """

    model_config = {"extra": "ignore", "populate_by_name": True}

    company: str = Field(
        default="", validation_alias=AliasChoices("Organization", "Company", "company")
    )
    followed_on: str = Field(
        default="",
        validation_alias=AliasChoices("Followed On", "Followed_On", "followed_on"),
    )

    @field_validator("*", mode="before")
    @classmethod
    def _blank_for_none(cls, v: object) -> object:
        return "" if v is None else v

    @field_validator("company", "followed_on")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


def _normalize_name(name: str) -> str:
    """Lowercased, whitespace-collapsed -- the join key between the archive's
    name-only files (Recommendations_Received.csv, messages.csv) and
    `network_contacts.full_name`. These files carry names, not profile URLs,
    so this is the only identity we have; a name that matches no connection
    is skipped, never used to invent a new contact."""
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def _contact_ids_by_normalized_name(conn: sqlite3.Connection) -> dict[str, list[int]]:
    mapping: dict[str, list[int]] = {}
    for row in conn.execute("SELECT id, full_name FROM network_contacts").fetchall():
        mapping.setdefault(_normalize_name(row["full_name"]), []).append(row["id"])
    return mapping


def _import_recommendations_text(conn: sqlite3.Connection, text: str) -> tuple[int, int, int]:
    """Sets `recommended_me=1` on connections named in the recommendation
    file. Returns (matched, skipped, ambiguous). A recommender who is not an
    existing connection is skipped -- never inserted as a new contact. A name
    that matches MORE THAN ONE connection is also credited to nobody: two
    distinct people can share a full name (each with their own profile URL),
    and inventing a vouch for the wrong one is worse than missing it."""
    name_map = _contact_ids_by_normalized_name(conn)
    matched = skipped = ambiguous = 0
    for raw_row in csv.DictReader(io.StringIO(text)):
        try:
            row = _RecommendationRow.model_validate(raw_row)
        except ValidationError:
            skipped += 1
            continue
        full_name = f"{row.first_name} {row.last_name}".strip()
        ids = name_map.get(_normalize_name(full_name), [])
        if not ids:
            skipped += 1
            continue
        if len(ids) > 1:
            ambiguous += 1
            continue
        conn.execute("UPDATE network_contacts SET recommended_me=1 WHERE id=?", (ids[0],))
        matched += 1
    conn.commit()
    return matched, skipped, ambiguous


_MESSAGE_DATE_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def _normalize_message_date(raw: str) -> str:
    """'2024-03-15 09:23:00 UTC' -> '2024-03-15'; anything else is kept
    as-is (still comparable well enough for a max() to pick the latest)."""
    raw = (raw or "").strip()
    m = _MESSAGE_DATE_PREFIX.match(raw)
    return m.group(1) if m else raw


def _import_messages_text(conn: sqlite3.Connection, text: str) -> tuple[int, int]:
    """PRIVACY: `messages.csv` contains the full text of the user's private
    LinkedIn conversations, including other people's words. This function
    must NEVER store a message body and NEVER log one. For each row it reads
    only the FROM/TO names and the DATE, derives a per-correspondent (name,
    most-recent-date, message-count) aggregate in memory, and then the parsed
    row is discarded -- nothing else may reach the database or a log line.
    Do not "optimize" this by keeping the row around.

    A name matching more than one connection touches NEITHER of them --
    same rationale as the recommendations import: crediting the wrong person
    with a message thread they never had is worse than missing the signal.

    Returns (matched, ambiguous) -- matched is the number of distinct
    connections whose recency/count got updated; ambiguous is the number of
    distinct ambiguous names encountered (never resolved to any contact)."""
    name_map = _contact_ids_by_normalized_name(conn)
    agg: dict[int, tuple[int, str]] = {}
    ambiguous_names: set[str] = set()

    def _touch(name: str, when: str) -> None:
        key = _normalize_name(name)
        ids = name_map.get(key, [])
        if len(ids) > 1:
            ambiguous_names.add(key)
            return
        if len(ids) == 1:
            cid = ids[0]
            count, last = agg.get(cid, (0, ""))
            agg[cid] = (count + 1, max(last, when))

    for raw_row in csv.DictReader(io.StringIO(text)):
        try:
            row = _MessageRow.model_validate(raw_row)
        except ValidationError:
            continue
        when = _normalize_message_date(row.date)
        _touch(row.from_name, when)
        for name in row.to_name.split(","):
            _touch(name, when)
        # `row` and `raw_row` go out of scope here -- nothing beyond the
        # (name, date, count) aggregate above survives this loop iteration.

    for cid, (count, last) in agg.items():
        conn.execute(
            "UPDATE network_contacts SET last_message_at=?, message_count=? WHERE id=?",
            (last, count, cid),
        )
    conn.commit()
    return len(agg), len(ambiguous_names)


def _import_follows_text(conn: sqlite3.Connection, text: str) -> int:
    """Populates `company_follows`, deduped by `company_key` (first
    occurrence wins). Returns the number of new rows added."""
    added = 0
    for raw_row in csv.DictReader(io.StringIO(text)):
        try:
            row = _FollowRow.model_validate(raw_row)
        except ValidationError:
            continue
        if not row.company:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO company_follows (company, company_key, followed_at) "
            "VALUES (?,?,?)",
            (row.company, company_key(row.company), row.followed_on),
        )
        if cur.rowcount:
            added += 1
    conn.commit()
    return added


# A 10MB cap on the whole upload used to reject the archive this feature
# exists for (a real LinkedIn export runs to tens of MB); the web upload cap
# is now 400MB (blastjob.web.app.MAX_NETWORK_UPLOAD_BYTES). That doesn't make
# decompression safe on its own -- a small ZIP can still declare (or, with a
# highly compressible payload, actually deliver) gigabytes of decompressed
# content. These caps sit under the *decompressed* side of an archive member:
# generous enough that no real export gets truncated, tight enough that a
# bomb can't land in memory whole.
MAX_ARCHIVE_MEMBER_BYTES = 200 * 1024 * 1024  # 200MB: no single real export file gets near this
MAX_ARCHIVE_TOTAL_BYTES = 400 * 1024 * 1024  # 400MB: matches the web upload cap

# Filenames vary by export vintage and live at arbitrary ZIP/folder depth --
# match case-insensitively on the basename, tolerating spaces vs underscores.
_KNOWN_ARCHIVE_MEMBERS = {
    "connections.csv": "connections",
    "recommendations_received.csv": "recommendations",
    "messages.csv": "messages",
    "company_follows.csv": "follows",
}


def _match_known_member(basename: str) -> str | None:
    key = basename.strip().lower().replace(" ", "_")
    return _KNOWN_ARCHIVE_MEMBERS.get(key)


@dataclass(frozen=True)
class ArchiveResult:
    """Outcome of one `import_archive` run."""

    connections: ImportResult = field(default_factory=ImportResult)
    recommendations_matched: int = 0
    recommendations_skipped: int = 0
    recommendations_ambiguous: int = 0
    messages_matched: int = 0
    messages_ambiguous: int = 0
    follows_added: int = 0
    unrecognized: tuple[str, ...] = ()
    oversized: tuple[str, ...] = ()


def _accept_member(
    label: str,
    kind: str,
    declared_size: int,
    read_capped: Callable[[int], bytes],
    found: dict[str, str],
    oversized: list[str],
    total_read: int,
) -> int:
    """Shared accept/reject logic for one archive member, used by both the ZIP
    and directory collectors. Never trusts `declared_size` for anything but a
    fast pre-check -- the real cap is enforced against bytes actually read, so
    a header (or a stale `stat()`) that lies about its size cannot smuggle more
    bytes into memory than the cap allows. Returns the updated running total.
    """
    if kind in found:
        return total_read  # first occurrence wins, matching prior setdefault
    if declared_size > MAX_ARCHIVE_MEMBER_BYTES or total_read + declared_size > (
        MAX_ARCHIVE_TOTAL_BYTES
    ):
        oversized.append(label)
        return total_read
    try:
        data = read_capped(MAX_ARCHIVE_MEMBER_BYTES + 1)
    except (zipfile.BadZipFile, OSError, EOFError):
        # A header that doesn't match what was actually written -- lying
        # about its own size corrupts the CRC check on read -- costs only
        # this member rather than the whole import.
        oversized.append(label)
        return total_read
    if len(data) > MAX_ARCHIVE_MEMBER_BYTES or total_read + len(data) > MAX_ARCHIVE_TOTAL_BYTES:
        oversized.append(label)
        return total_read
    found[kind] = data.decode("utf-8-sig")
    return total_read + len(data)


def _collect_archive_members_zip(path: Path) -> tuple[dict[str, str], list[str], list[str]]:
    found: dict[str, str] = {}
    unrecognized: list[str] = []
    oversized: list[str] = []
    total_read = 0
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            basename = info.filename.rsplit("/", 1)[-1]
            if not basename:
                continue
            kind = _match_known_member(basename)
            if kind is None:
                unrecognized.append(info.filename)
                continue

            def _read_capped(cap_plus_one: int, info=info) -> bytes:
                # info.file_size (the declared, central-directory size) is NOT
                # trusted here -- a forged header could claim anything. Reading
                # through zf.open() decompresses the real DEFLATE stream and
                # bounding the read means a lying-small header can't hide an
                # actually-huge payload, and a genuinely huge one is never
                # materialized past the cap.
                with zf.open(info) as fh:
                    return fh.read(cap_plus_one)

            total_read = _accept_member(
                info.filename, kind, info.file_size, _read_capped, found, oversized, total_read
            )
    return found, unrecognized, oversized


def _collect_archive_members_dir(path: Path) -> tuple[dict[str, str], list[str], list[str]]:
    found: dict[str, str] = {}
    unrecognized: list[str] = []
    oversized: list[str] = []
    total_read = 0
    root = path.resolve()
    for entry in sorted(p for p in path.rglob("*") if p.is_file()):
        # A symlink inside the archive directory could point anywhere on disk.
        # There is no legitimate reason for a LinkedIn export to contain one,
        # so refuse to follow anything that resolves outside the archive root
        # rather than trusting the tree the user pointed us at.
        resolved = entry.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        label = str(entry.relative_to(path))
        kind = _match_known_member(entry.name)
        if kind is None:
            unrecognized.append(label)
            continue
        declared_size = entry.stat().st_size

        def _read_capped(cap_plus_one: int, entry=entry) -> bytes:
            with entry.open("rb") as fh:
                return fh.read(cap_plus_one)

        total_read = _accept_member(
            label, kind, declared_size, _read_capped, found, oversized, total_read
        )
    return found, unrecognized, oversized


def import_archive(conn: sqlite3.Connection, path: str | Path) -> ArchiveResult:
    """Import LinkedIn's full data-export archive: a ZIP (or an already
    unzipped directory) that may contain `Connections.csv`,
    `Recommendations_Received.csv`, `messages.csv`, and `Company Follows.csv`
    at any depth. A missing optional file is skipped silently -- archive
    contents differ per account. Connections is always imported first,
    regardless of member order in the ZIP, because the other three files
    match onto existing connections by name. A member whose declared or
    actual decompressed size exceeds the caps in this module is skipped and
    listed in `oversized` rather than aborting the whole import -- one absurd
    file should not cost the user the rest of a real export."""
    p = Path(path)
    if p.is_dir():
        found, unrecognized, oversized = _collect_archive_members_dir(p)
    else:
        found, unrecognized, oversized = _collect_archive_members_zip(p)

    connections = (
        _import_csv_text(conn, found["connections"]) if "connections" in found else ImportResult()
    )

    rec_matched = rec_skipped = rec_ambiguous = 0
    if "recommendations" in found:
        rec_matched, rec_skipped, rec_ambiguous = _import_recommendations_text(
            conn, found["recommendations"]
        )

    msg_matched = msg_ambiguous = 0
    if "messages" in found:
        msg_matched, msg_ambiguous = _import_messages_text(conn, found["messages"])

    follows_added = _import_follows_text(conn, found["follows"]) if "follows" in found else 0

    return ArchiveResult(
        connections=connections,
        recommendations_matched=rec_matched,
        recommendations_skipped=rec_skipped,
        recommendations_ambiguous=rec_ambiguous,
        messages_matched=msg_matched,
        messages_ambiguous=msg_ambiguous,
        follows_added=follows_added,
        unrecognized=tuple(unrecognized),
        oversized=tuple(oversized),
    )


# --------------------------------------------------------------------------
# Deterministic classification -- no LLM, no network
# --------------------------------------------------------------------------

# (keyword, label) pairs. Longest keyword match wins; ties break exec >
# director > manager (see _TIER_RANK) so e.g. "Partnerships Manager" -- which
# contains both "partner" and "manager" at equal length -- resolves to exec.
_SENIORITY_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("vice president", "exec"),
    ("president", "exec"),
    ("founder", "exec"),
    ("owner", "exec"),
    ("partner", "exec"),
    ("chief", "exec"),
    ("cio", "exec"),
    ("cto", "exec"),
    ("ciso", "exec"),
    ("vp", "exec"),
    # "\bvp\b" cannot match inside "SVP"/"EVP"/"AVP" -- there's no word
    # boundary between the leading letter and "vp". Verified against live
    # data: 7 senior executives filed as IC because of exactly this.
    ("svp", "exec"),
    ("evp", "exec"),
    ("avp", "exec"),
    ("head of", "director"),
    ("director", "director"),
    ("supervisor", "manager"),
    ("manager", "manager"),
    ("lead", "manager"),
)

_TIER_RANK = {"exec": 0, "director": 1, "manager": 2, "ic": 3}


# "Partner" means executive at a law or consulting firm, but "Talent Acquisition
# Partner", "HR Business Partner" and "People Partner" are not executives. Without
# this guard a recruiter ranks top of the intro list, which is exactly backwards --
# the point of the ranking is to surface the person who can actually refer the user.
_PARTNER_NOT_EXEC = re.compile(
    r"\b(talent|business|hr|human resources|people|channel|sales|delivery)\s+partner\b"
)

# "Product Owner" is an Agile/Scrum IC role, not a business owner -- but
# "\bowner\b" matched it anyway. Verified against live data: 32 of 269
# contacts classified "exec" were Product Owner titles, ~12% pollution of the
# exec tier the whole warm-intro ranking is built on. Deliberately narrow
# (only the verified false-positive shape) rather than a broad "owner" guard
# that would risk reclassifying genuine business owners.
_OWNER_NOT_EXEC = re.compile(r"\bproduct\s+owner\b")


def classify_seniority(position: str) -> str:
    """Deterministic, no LLM. Longest keyword match wins; a tie breaks toward
    the more senior label. Falls back to 'ic'."""
    text = (position or "").lower()
    if _PARTNER_NOT_EXEC.search(text) or is_recruiter(text):
        # Strip the misleading token, then classify on whatever else is there.
        text = re.sub(r"\bpartner\b", " ", text)
    if _OWNER_NOT_EXEC.search(text):
        text = re.sub(r"\bowner\b", " ", text)
    best: tuple[int, int, str] | None = None
    for keyword, label in _SENIORITY_KEYWORDS:
        pattern = r"\b" + re.escape(keyword) + r"\b"
        if re.search(pattern, text):
            candidate = (-len(keyword), _TIER_RANK[label], label)
            if best is None or candidate < best:
                best = candidate
    return best[2] if best else "ic"


_RECRUITER_KEYWORDS = (
    "recruiter",
    "talent acquisition",
    "talent partner",
    "executive search",
    "headhunter",
    "staffing",
)


def is_recruiter(position: str) -> bool:
    """True for recruiter / talent-acquisition / executive-search titles."""
    text = (position or "").lower()
    return any(keyword in text for keyword in _RECRUITER_KEYWORDS)


# The candidate's own former employers, as they appear in work_history.md. A
# job at one is the strongest warm signal the network view can show. Lives in
# config (`[candidate] former_employers`), not here -- a legal-entity vs
# brand-name mismatch belongs in core/company.py::_COMPANY_ALIASES.


def alumni_employers(candidate: CandidateConfig) -> frozenset[str]:
    """Computed at call time from `[candidate] former_employers` -- never
    cached at import time, so a fresh install (empty config) and a filled-in
    one both see the right set without a restart."""
    return frozenset(company_key(name) for name in candidate.former_employers)


def is_alumni_company(key: str, candidate: CandidateConfig | None = None) -> bool:
    """True when a JOB is at a place the candidate already worked -- the
    strongest possible warm signal. `key` may be a raw company name or an
    already-normalized company_key; company_key() is idempotent either way.
    `candidate` is `CandidateConfig`. Callers should pass the loaded config's
    `.candidate` explicitly; omitting it is treated as "no former employers
    configured" rather than loading the settings from the database here,
    which would make this read real machine state as a side
    effect of an innocuous-looking default argument."""
    if candidate is None:
        candidate = CandidateConfig()
    return company_key(key) in alumni_employers(candidate)


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

# Prefer the agentic pass's verdict when it has one, else the deterministic
# classifier. A regex cannot read "AVP Strategic Sales (Enterprise, Client
# Sales)" -- that is a senior SALES person, and for a referral the function
# matters more than the tier -- but the regex is a fine floor for anything the
# pass has not reached yet.
_EFFECTIVE_TIER_SQL = (
    "CASE WHEN COALESCE(enrich_tier,'') <> '' THEN "
    "  CASE enrich_tier WHEN 'c_suite' THEN 'exec' WHEN 'evp_svp' THEN 'exec' "
    "  WHEN 'vp' THEN 'exec' WHEN 'director' THEN 'director' "
    "  WHEN 'manager' THEN 'manager' ELSE 'ic' END "
    "ELSE seniority END"
)

_SENIORITY_ORDER_SQL = (
    f"CASE {_EFFECTIVE_TIER_SQL} "
    "WHEN 'exec' THEN 0 WHEN 'director' THEN 1 WHEN 'manager' THEN 2 WHEN 'ic' THEN 3 "
    "ELSE 4 END"
)


def warm_paths(conn: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    """Connections whose company_key equals the job's, ordered recommenders
    first, then exec -> director -> manager -> ic, then most recently
    messaged, then by name. A VP who recommended the user outranks a VP who never
    spoke to them -- the whole point of this feature: "you know three people
    at this company, one is a VP, and one of them already vouched for you.\""""
    job = conn.execute("SELECT company FROM jobs WHERE id=?", (job_id,)).fetchone()
    if job is None:
        return []
    key = company_key(job["company"] or "")
    if not key:
        return []
    return conn.execute(
        "SELECT * FROM network_contacts WHERE company_key=? "
        f"ORDER BY recommended_me DESC, {_SENIORITY_ORDER_SQL}, last_message_at DESC, full_name",
        (key,),
    ).fetchall()


def warm_counts(conn: sqlite3.Connection, company_keys: Iterable[str]) -> dict[str, int]:
    """Connection counts for a batch of company_keys in one query -- the
    batched-lookup pattern list pages need for the 'N in your network' badge
    (imitates `web.service.cluster_overrides`; never do this per row)."""
    keys = sorted({k for k in company_keys if k})
    if not keys:
        return {}
    placeholders = ",".join("?" for _ in keys)
    rows = conn.execute(
        "SELECT company_key, COUNT(*) n FROM network_contacts "
        f"WHERE company_key IN ({placeholders}) GROUP BY company_key",
        keys,
    ).fetchall()
    return {r["company_key"]: r["n"] for r in rows}


def recruiters(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every connection who is a recruiter -- a standing channel independent
    of any single job."""
    return conn.execute(
        "SELECT * FROM network_contacts WHERE is_recruiter=1 ORDER BY full_name"
    ).fetchall()


def advocates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Everyone who publicly recommended the user, most recently messaged first
    -- their warm-intro shortlist independent of any single job."""
    return conn.execute(
        "SELECT * FROM network_contacts WHERE recommended_me=1 "
        "ORDER BY last_message_at DESC, full_name"
    ).fetchall()


def network_summary(conn: sqlite3.Connection) -> dict:
    """Totals by seniority, recruiter count, and the companies with the most
    connections. Feeds the /network page."""
    by_seniority = {
        r["seniority"]: r["n"]
        for r in conn.execute(
            "SELECT seniority, COUNT(*) n FROM network_contacts GROUP BY seniority"
        ).fetchall()
    }
    recruiter_count = conn.execute(
        "SELECT COUNT(*) n FROM network_contacts WHERE is_recruiter=1"
    ).fetchone()["n"]
    top_companies = conn.execute(
        "SELECT company, company_key, COUNT(*) n FROM network_contacts "
        "WHERE company_key != '' GROUP BY company_key ORDER BY n DESC, company LIMIT 10"
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) n FROM network_contacts").fetchone()["n"]
    return {
        "total": total,
        "by_seniority": by_seniority,
        "recruiter_count": recruiter_count,
        "top_companies": top_companies,
    }

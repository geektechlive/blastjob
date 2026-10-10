"""The whole-card LinkedIn alert layout: one <a> around the entire job card.

Its text is "<Title> <Company> · <City, ST>[ (On-site|Hybrid|Remote)][ $<pay> /
year][ Easy Apply]...", with no delimiter between title and company. Two ways
to split it, and only one of them is trusted:

- KNOWN: the text before " · " ends with a company name we already hold (the
  DB's companies at ingest, a cluster sibling's in the repair). Structure, not
  a guess, so it is applied.
- VOCAB: the longest prefix ending on a title word ("...Technology") is the
  title, the rest the company. "Director of IT Acme Technologies" shows why it
  is never applied on its own: it is a suggestion the user confirms
  (`blastjob-daily --repair-linkedin-titles --accept <ids>`).

The LinkedIn guest page read through Jina (`guest_page_company`) can also
confirm the company structurally: its header has the title on one line and
the company on the next, and the two must rebuild the card text exactly.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

SPLIT_KNOWN = "known"
SPLIT_VOCAB = "vocab"

CARD_SEP = " · "
# Where the location ends: a workplace tag, pay, or one of LinkedIn's badges.
_LOC_END = re.compile(
    r"\s*(?:\((?:On-site|Hybrid|Remote)\)|\$|Easy Apply|Be an early applicant|"
    r"Actively recruiting|Promoted)",
    re.I,
)
_PAY_OR_BADGE = re.compile(
    r"\s*(?:\$|Easy Apply|Be an early applicant|Actively recruiting|Promoted)", re.I
)
_REMOTE_TAG = re.compile(r"\s*\((?:On-site|Hybrid|Remote)\)\s*", re.I)
_TITLE_WORDS = frozenset(
    "officer president vp director head manager lead architect engineer technology "
    "technologies it infrastructure operations systems security information digital "
    "transformation cio cto ciso".split()
)
# A known company shorter than this is too likely to be a coincidental suffix.
_MIN_KNOWN_LEN = 3
_GUEST_HEADER_LINES = 40


@dataclass(frozen=True)
class CardSplit:
    """One whole-card anchor, parsed. `basis` is SPLIT_KNOWN, SPLIT_VOCAB, or ""
    when no split was found (title = head, company = "")."""

    head: str  # the text before " · ", pay and badges stripped, tags intact
    title: str
    company: str
    location: str
    remote: bool
    basis: str


def is_card_text(text: str) -> bool:
    return CARD_SEP in (text or "")


def _location(tail: str) -> tuple[str, bool]:
    m = _LOC_END.search(tail)
    loc = (tail[: m.start()] if m else tail).split(CARD_SEP)[0].strip()
    return loc, bool(re.search(r"\(Remote\)", tail, re.I))


def _clean_head(head: str) -> str:
    """Pay and badges never belong to a title; a workplace tag stays (it is
    lifted into the remote hint later)."""
    m = _PAY_OR_BADGE.search(head)
    cut = head[: m.start()].strip() if m else head.strip()
    return cut or head.strip()


def _known_split(body: str, known: Iterable[str]) -> tuple[str, str] | None:
    """(title, company) for the longest known company `body` ends with on a word
    boundary, leaving a non-empty title."""
    low = body.casefold()
    best: tuple[str, str] | None = None
    for name in known:
        name = (name or "").strip()
        if len(name) < _MIN_KNOWN_LEN or name.casefold() in _TITLE_WORDS:
            continue
        if not low.endswith(" " + name.casefold()):
            continue
        title = body[: len(body) - len(name)].strip()
        if title and (best is None or len(name) > len(best[1])):
            best = (title, body[len(body) - len(name) :])
    return best


def _vocab_split(body: str) -> tuple[str, str] | None:
    words = body.split()
    last = -1
    for i, w in enumerate(words):
        if w.endswith(")") or w.strip(",;:").casefold() in _TITLE_WORDS:
            last = i
    if last < 0 or last == len(words) - 1:
        return None
    return " ".join(words[: last + 1]), " ".join(words[last + 1 :])


def split_card(text: str, known_companies: Iterable[str] = ()) -> CardSplit:
    """Parse whole-card text (module doc). Never guesses: no clean split gives
    title = head and company = ""."""
    head_raw, _, tail = (text or "").partition(CARD_SEP)
    location, remote = _location(tail)
    head = _clean_head(head_raw)
    tag = _REMOTE_TAG.search(head)
    if tag and re.search(r"remote", tag.group(0), re.I):
        remote = True
    body = _REMOTE_TAG.sub(" ", head).strip()
    for basis, found in (
        (SPLIT_KNOWN, _known_split(body, known_companies)),
        (SPLIT_VOCAB, _vocab_split(body)),
    ):
        if found:
            return CardSplit(head, found[0], found[1], location, remote, basis)
    return CardSplit(head, head, "", location, remote, "")


def split_card_text(text: str, known_companies: Iterable[str] = ()) -> tuple[str, str, str, bool]:
    """(title, company, location, remote_hint), vocabulary split included. Callers
    that write data use `split_card` and check `basis`."""
    card = split_card(text, known_companies)
    return card.title, card.company, card.location, card.remote


def guest_page_company(page_text: str, head: str) -> str:
    """The company from a LinkedIn guest page header, or "" unless the header's
    title line plus the company on the next line rebuild `head` exactly."""
    head = (head or "").strip()
    lines = [ln.strip() for ln in (page_text or "").splitlines()[:_GUEST_HEADER_LINES]]
    lines = [ln for ln in lines if ln]
    for i, line in enumerate(lines[:-1]):
        if not head.startswith(line + " "):
            continue
        company = re.split(r"\s{2,}", lines[i + 1])[0].strip()
        if company and head == f"{line} {company}":
            return company
    return ""


def known_company_names(conn: sqlite3.Connection) -> frozenset[str]:
    """Every non-empty company name in the DB, loaded once per discovery run."""
    rows = conn.execute("SELECT DISTINCT company FROM jobs WHERE TRIM(company) <> ''")
    return frozenset(str(r[0]).strip() for r in rows.fetchall())

import hashlib
import json
import os
import re
from datetime import date
from pathlib import Path


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text[:64].strip("-")


def name_token(text: str) -> str:
    """Filename-safe Title_Case token, e.g. 'Fabrikam Equipment' -> 'Fabrikam_Equipment'."""
    cleaned = re.sub(r"[^\w\s-]", "", text).strip()
    return re.sub(r"[\s-]+", "_", cleaned)[:48].strip("_") or "Unknown"


# Indeed refuses an upload whose name is 90+ characters (issue #19); 80 leaves room
# for stricter boards. The longest uploadable name is `{person}_Resume_-_{token}
# _ATS.ats.txt` (person + 22 + token), so a 40-character token keeps every
# document at or under 80 for a person slug of up to 18 characters.
DOCUMENT_TOKEN_MAX = 40
_HASH_LEN = 4
_COMPANY_SUFFIXES = frozenset(
    "inc incorporated corp corporation llc ltd co company plc lp llp".split()
)
_FILLER_WORDS = frozenset("of and the for remote hybrid onsite".split())
# Applied in order; whole words only. The second tier runs only when the first
# plus a shorter company still does not fit.
_ROLE_ABBREVIATIONS = (
    ("chief information security officer", "CISO"),
    ("chief information officer", "CIO"),
    ("chief technology officer", "CTO"),
    ("chief operating officer", "COO"),
    ("chief executive officer", "CEO"),
    ("chief financial officer", "CFO"),
    ("executive vice president", "EVP"),
    ("senior vice president", "SVP"),
    ("vice president", "VP"),
    ("information technology", "IT"),
    ("senior", "Sr"),
)
_ROLE_ABBREVIATIONS_TIGHT = (
    ("director", "Dir"),
    ("technology", "Tech"),
    ("information", "Info"),
    ("management", "Mgmt"),
    ("manager", "Mgr"),
    ("operations", "Ops"),
    ("infrastructure", "Infra"),
    ("engineering", "Eng"),
    ("manufacturing", "Mfg"),
)
# Earlier versions of the tight table, newest first. A role whose token they
# hashed differently still names files on disk that were never renamed, and
# `document_tokens` must keep matching them (`core/packet_renames.py`).
_PRIOR_TIGHT_TABLES: tuple[tuple[tuple[str, str], ...], ...] = (
    tuple(p for p in _ROLE_ABBREVIATIONS_TIGHT if p[0] != "manufacturing"),
)


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[\s_-]+", re.sub(r"[^\w\s-]", " ", text)) if w]


def _company_words(company: str) -> list[str]:
    words = _words(company)
    while len(words) > 1 and words[-1].lower() in _COMPANY_SUFFIXES:
        words.pop()
    if len(words) > 1 and words[0].lower() == "the":
        words.pop(0)
    return words


def _abbreviate(text: str, table: tuple[tuple[str, str], ...]) -> str:
    for phrase, short in table:
        text = re.sub(rf"\b{phrase}\b", short, text, flags=re.IGNORECASE)
    return text


def _role_words(
    role: str, *, tight: bool, tight_table: tuple[tuple[str, str], ...] = _ROLE_ABBREVIATIONS_TIGHT
) -> list[str]:
    text = _abbreviate(re.sub(r"\([^)]*\)", " ", role), _ROLE_ABBREVIATIONS)
    if tight:
        text = _abbreviate(text, tight_table)
    return [w for w in _words(text) if w.lower() not in _FILLER_WORDS]


def document_token(company: str, role: str) -> str:
    """The `{Co}_{Role}` part of a packet document's name, at most
    DOCUMENT_TOKEN_MAX characters.

    A token that already fits is returned unchanged, so packets named before
    this cap keep their names. Otherwise: drop company legal suffixes and role
    parentheticals, abbreviate title words, drop filler, then drop trailing
    company words (safe: a packet folder holds one company). Only a cut into
    the role itself is lossy, and it carries a hash of the raw company + role
    so two roles at one company never share a name."""
    return _document_token(company, role, _ROLE_ABBREVIATIONS_TIGHT)


def _document_token(company: str, role: str, tight_table: tuple[tuple[str, str], ...]) -> str:
    full = f"{name_token(company)}_{name_token(role)}"
    if len(full) <= DOCUMENT_TOKEN_MAX:
        return full
    company_words = _company_words(company) or ["Unknown"]
    for tight in (False, True):
        role_words = _role_words(role, tight=tight, tight_table=tight_table) or _words(role)
        for keep in range(len(company_words), 0, -1):
            token = "_".join(company_words[:keep] + role_words)
            if len(token) <= DOCUMENT_TOKEN_MAX:
                return token
    # Hash the raw text, not `full`: name_token cuts at 48 characters, so two
    # roles that differ past that point share a legacy token.
    raw = f"{company}\x00{role}".encode()
    digest = hashlib.sha256(raw).hexdigest()[:_HASH_LEN]
    cut = token[: DOCUMENT_TOKEN_MAX - _HASH_LEN - 1].rstrip("_")
    return f"{cut}_{digest}"


def document_tokens(company: str, role: str) -> tuple[str, ...]:
    """Every token a packet document for this company+role may carry: the
    current capped one first, the ones earlier abbreviation tables produced
    (a file not yet renamed), and, for packets built before the cap, the
    uncapped one."""
    legacy = f"{name_token(company)}_{name_token(role)}"
    prior = (_document_token(company, role, table) for table in _PRIOR_TIGHT_TABLES)
    return tuple(dict.fromkeys((document_token(company, role), *prior, legacy)))


def document_tokens_regex(company: str, role: str) -> str:
    """`document_tokens` as one regex alternation group, each token escaped."""
    return "(?:" + "|".join(re.escape(t) for t in document_tokens(company, role)) + ")"


def person_slug(full_name: str) -> str:
    """'Alexander R. Rivera' -> 'Alexander_Rivera' (first + last, drop middle)."""
    parts = [p for p in re.sub(r"[^\w\s]", "", full_name).split() if len(p) > 1]
    if len(parts) >= 2:
        return f"{parts[0]}_{parts[-1]}"
    return name_token(full_name) or "Candidate"


# --- the filed layout (issue #20) ----------------------------------------------
#
# The output folder files itself: a top-level ENTRY (a direct child folder such
# as `acme-2026-10-16`) sits at the root while its packet is still to send, and
# `core/packet_filing.py` moves it whole into one of these by one rename.
SUBMITTED = "Submitted"
CLOSED_BEFORE_APPLYING = "Closed before applying"
ARCHIVE = "Archive"
BUCKETS: tuple[str, ...] = (SUBMITTED, CLOSED_BEFORE_APPLYING, ARCHIVE)


def _norm(path: str | Path) -> Path:
    return Path(os.path.normpath(os.path.expanduser(str(path))))


def _under(path: Path, output_root: str | Path) -> tuple[Path, tuple[str, ...]] | None:
    """(the root as `path` spells it, the parts below it), or None when `path`
    is not inside the output dir. Tries the configured spelling and the
    resolved one: `artifacts.register` stores resolved paths, `finish_build`
    the folder as built."""
    root = _norm(output_root)
    for base in dict.fromkeys((root, root.resolve())):
        try:
            return base, path.relative_to(base).parts
        except ValueError:
            continue
    return None


def canonical(path: str | Path, output_root: str | Path) -> Path:
    """The ROOT form of a packet path: home-expanded, with a bucket segment
    directly under the output dir removed. Every path written to the database
    goes through this, so a stored path never names a bucket and filing an
    entry never has to rewrite a row. A path outside the output dir comes back
    expanded and otherwise untouched."""
    p = _norm(path)
    split = _under(p, output_root)
    if split is None:
        return p
    base, parts = split
    if len(parts) >= 2 and parts[0] in BUCKETS:
        return base.joinpath(*parts[1:])
    return p


def locate(path: str | Path, output_root: str | Path) -> Path:
    """Where a stored packet path is NOW: the root or bucket form (root first)
    where the FULL path exists; failing that, the first place its entry folder
    exists, so a stray empty root folder never shadows the real filed entry.
    Accepts either form. When the entry is nowhere the root form comes back (a
    reader then reports it missing). Pure: never creates, moves or locks
    anything, and never returns a path outside the output dir for one inside
    it."""
    canon = canonical(path, output_root)
    split = _under(canon, output_root)
    # parts[0] is a bucket only when `path` is the bucket folder itself.
    if split is None or not split[1] or split[1][0] in BUCKETS:
        return canon
    base, parts = split
    places = [base, *(base / b for b in BUCKETS)]
    for place in places:
        if place.joinpath(*parts).exists():
            return place.joinpath(*parts)
    for place in places:
        if (place / parts[0]).exists():
            return place.joinpath(*parts)
    return canon


def entry_name(path: str | Path, output_root: str | Path) -> str | None:
    """The top-level entry a stored path lives in (`acme-2026-10-16`), or None
    for a path outside the output dir, the dir itself, or a bucket folder."""
    split = _under(canonical(path, output_root), output_root)
    if split is None or not split[1] or split[1][0] in BUCKETS:
        return None
    return split[1][0]


def application_dir(output_root: Path, company: str) -> Path:
    """One flat folder per company per day: {root}/{company-slug}-{date}/.

    Shared across roles applied to the same company that day — files inside carry
    the role in their name, so multiple postings don't collide.

    A build always lands at the ROOT. When today's name is already filed in a
    bucket (the user rebuilt a submitted job today), the next free `-2`, `-3`
    ... is used instead, so the new packet never merges into a filed one; the
    post-build filing hook then files it by the job's state. A name that exists
    at the root is reused (that is the shared same-day folder).
    """
    today = date.today().isoformat()
    company_slug = slugify(company) or "unknown-company"
    n = 1
    while True:
        name = f"{company_slug}-{today}" if n == 1 else f"{company_slug}-{today}-{n}"
        base = output_root / name
        if base.is_dir() or not any((output_root / b / name).exists() for b in BUCKETS):
            base.mkdir(parents=True, exist_ok=True)
            return base
        n += 1


def _normalize_role(role: str) -> str:
    """Collapse whitespace and case so trivially-different spellings of the
    same role (re-submitted title, different capitalization, double spaces)
    compare equal."""
    return " ".join(role.split()).casefold()


def _slug_claimed(out_dir: Path, slug: str) -> bool:
    """True if any sidecar already uses this exact slug as its stem.

    Uses a glob anchored on `{slug}.` so a longer sibling slug that happens to
    start with this one (`vp-it-platforms.metadata.json`) never makes a
    shorter, distinct slug (`vp-it`) look taken.
    """
    return out_dir.exists() and any(out_dir.glob(f"{slug}.*"))


def _slug_owner(out_dir: Path, slug: str) -> str | None:
    """The role recorded in `{slug}.metadata.json`, if that marker exists and
    parses. `core.build.run_build` writes this file with the exact role text
    it was given, so it doubles as this slug's ownership record — no new file
    format needed. Returns None if there is no marker or it can't be read,
    which `resolve_role_slug` treats as "claimed but ownership unknown" and
    therefore not safe to reuse.
    """
    marker = out_dir / f"{slug}.metadata.json"
    if not marker.exists():
        return None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("role")
    return str(value) if value is not None else None


def resolve_role_slug(out_dir: Path, role: str) -> str:
    """Collision-safe slug for one role's sidecar family inside `out_dir`.

    `application_dir` deliberately shares one flat folder across every role
    tailored for a company on a given day; `{role_slug}.job_description.md`
    / `.fit_score.json` / `.fit_score.md` / `.metadata.json` /
    `.interview_prep.md` are what keep the roles apart in that shared folder.
    `slugify` is a lossy, human-readable transform — punctuation is dropped
    rather than preserved, and long strings are truncated to 64 chars — so
    two genuinely different role titles can land on the identical base slug
    ('VP, IT' and 'VP IT' both slugify to 'vp-it'; 'C++ Engineer' and
    'C# Engineer' both slugify to 'c-engineer'; two long titles can agree up
    to the 64-char cut). Silently reusing a claimed slug would let one role's
    sidecars overwrite another's — a document that then reads as fact about
    the wrong job, which is the failure class this project treats as
    unacceptable.

    Returns the plain slug in the common case: an empty folder, or a folder
    where that slug is already owned by this same role (re-tailoring the same
    job is *meant* to keep overwriting its own files). Otherwise appends a
    short, deterministic disambiguator derived from the full role text, so
    the same colliding title always resolves to the same slug.

    A slug that is claimed (some `{slug}.*` file exists) but has no readable
    `{slug}.metadata.json` — e.g. a build that crashed after writing the job
    description but before the metadata step — is treated as owned by an
    unknown role and NOT reused: a stray file from disambiguating is a far
    smaller problem than silently overwriting whatever crashed build left
    behind.
    """
    base = slugify(role) or "role"
    normalized = _normalize_role(role)

    def usable(slug: str) -> bool:
        if not _slug_claimed(out_dir, slug):
            return True
        owner = _slug_owner(out_dir, slug)
        return owner is not None and _normalize_role(owner) == normalized

    if usable(base):
        return base

    digest = hashlib.blake2s(normalized.encode("utf-8"), digest_size=4).hexdigest()
    candidate = f"{base}-{digest}"
    if usable(candidate):
        return candidate

    # Vanishingly unlikely (the digest also collided with a third role's
    # slug) — a counter guarantees termination without getting cute about it.
    n = 2
    while not usable(f"{candidate}-{n}"):
        n += 1
    return f"{candidate}-{n}"

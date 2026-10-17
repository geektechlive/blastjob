import hashlib
import json
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


def _role_words(role: str, *, tight: bool) -> list[str]:
    text = _abbreviate(re.sub(r"\([^)]*\)", " ", role), _ROLE_ABBREVIATIONS)
    if tight:
        text = _abbreviate(text, _ROLE_ABBREVIATIONS_TIGHT)
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
    full = f"{name_token(company)}_{name_token(role)}"
    if len(full) <= DOCUMENT_TOKEN_MAX:
        return full
    company_words = _company_words(company) or ["Unknown"]
    for tight in (False, True):
        role_words = _role_words(role, tight=tight) or _words(role)
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
    capped one and, for packets built before the cap, the uncapped one."""
    legacy = f"{name_token(company)}_{name_token(role)}"
    return tuple(dict.fromkeys((document_token(company, role), legacy)))


def document_tokens_regex(company: str, role: str) -> str:
    """`document_tokens` as one regex alternation group, each token escaped."""
    return "(?:" + "|".join(re.escape(t) for t in document_tokens(company, role)) + ")"


def person_slug(full_name: str) -> str:
    """'Alexander R. Rivera' -> 'Alexander_Rivera' (first + last, drop middle)."""
    parts = [p for p in re.sub(r"[^\w\s]", "", full_name).split() if len(p) > 1]
    if len(parts) >= 2:
        return f"{parts[0]}_{parts[-1]}"
    return name_token(full_name) or "Candidate"


def application_dir(output_root: Path, company: str) -> Path:
    """One flat folder per company per day: {root}/{company-slug}-{date}/.

    Shared across roles applied to the same company that day — files inside carry
    the role in their name, so multiple postings don't collide.
    """
    today = date.today().isoformat()
    company_slug = slugify(company) or "unknown-company"
    base = output_root / f"{company_slug}-{today}"
    base.mkdir(parents=True, exist_ok=True)
    return base


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

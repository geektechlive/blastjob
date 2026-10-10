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

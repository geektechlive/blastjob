import re
from datetime import date
from pathlib import Path


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text[:64].strip("-")


def make_output_dir(output_root: Path, company: str, role: str) -> Path:
    today = date.today().isoformat()
    company_slug = slugify(company) or "unknown-company"
    role_slug = slugify(role) or "unknown-role"

    base = output_root / today / company_slug / role_slug
    if not base.exists():
        base.mkdir(parents=True, exist_ok=True)
        return base

    # Collision — append numeric suffix
    i = 2
    while True:
        candidate = output_root / today / company_slug / f"{role_slug}-{i}"
        if not candidate.exists():
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        i += 1

from pathlib import Path


def write(resume_md: str, out_dir: Path) -> Path:
    dest = out_dir / "resume.md"
    dest.write_text(resume_md, encoding="utf-8")
    return dest

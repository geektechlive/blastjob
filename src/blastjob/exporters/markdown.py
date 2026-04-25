from pathlib import Path


def write(content: str, out_dir: Path, stem: str = "resume") -> Path:
    dest = out_dir / f"{stem}.md"
    dest.write_text(content, encoding="utf-8")
    return dest

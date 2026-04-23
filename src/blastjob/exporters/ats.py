import re
import textwrap
from pathlib import Path


def write(resume_md: str, out_dir: Path) -> Path:
    text = _strip_markdown(resume_md)
    wrapped = _wrap(text)
    dest = out_dir / "resume.ats.txt"
    dest.write_text(wrapped, encoding="ascii", errors="ignore")
    return dest


def _strip_markdown(text: str) -> str:
    # Convert ## headings to UPPERCASE:
    text = re.sub(r"^#{1,3}\s+(.+)$", lambda m: m.group(1).upper() + ":", text, flags=re.MULTILINE)
    # Remove bold/italic
    text = re.sub(r"\*{1,3}(.+?)\*{1,3}", r"\1", text)
    text = re.sub(r"_{1,3}(.+?)_{1,3}", r"\1", text)
    # Remove links — keep text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Remove inline code
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    # Remove YAML frontmatter
    text = re.sub(r"^---.*?---\s*", "", text, flags=re.DOTALL)
    return text


def _wrap(text: str) -> str:
    lines = text.splitlines()
    result = []
    for line in lines:
        if len(line) > 80:
            result.extend(textwrap.wrap(line, width=80))
        else:
            result.append(line)
    return "\n".join(result)

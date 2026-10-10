import logging
import re
import textwrap
import unicodedata
from pathlib import Path

log = logging.getLogger(__name__)

# Greenhouse's parser documentedly stops parsing beyond this size; warn, never raise.
_MAX_BYTES = 2_500_000

# The exact character set behind the logged Workday rejection ("Contains illegal
# characters", 2026-09-10): `< > [ ] { } " \`. `_strip_markdown` already clears most
# of these as a side effect of converting headings/links/code, but anything that
# survives -- a literal bracket in body text, an unmatched angle bracket -- is
# stripped here so the banned set can never reach the exported file. Double quotes
# become single quotes rather than vanishing, matching V13's "strip or replace,
# never raise" directive.
_BANNED_CHARS = str.maketrans(
    {
        "<": "",
        ">": "",
        "[": "",
        "]": "",
        "{": "",
        "}": "",
        "\\": "",
        '"': "'",
    }
)


def write(resume_md: str, out_dir: Path, stem: str = "resume") -> Path:
    """Plain-text ATS copy. `stem` matches the md/docx/pdf exporters, which all
    take one -- without it `core.build` raised TypeError the moment the "ats"
    format was enabled, and every role in a shared folder overwrote the same
    `resume.ats.txt`.
    """
    text = _transliterate(_strip_markdown(resume_md))
    text = _strip_banned_chars(text)
    wrapped = _wrap(text)
    dest = out_dir / f"{stem}.ats.txt"
    # `errors="ignore"` silently DELETED every non-ASCII character, so
    # "Senior Director - IT" (em dash) became "Senior Director  IT" with a
    # double space, and bullets vanished mid-line. Transliterate first, then
    # assert the text really is ASCII rather than dropping what is left.
    # `_transliterate` already guarantees pure-ASCII output for any input (every
    # character is either kept ascii, a dropped combining mark, or replaced with
    # a space), so this should never raise -- but ats.py must be fail-soft like
    # docx.py/pdf.py rather than crash a build on a character it did not
    # anticipate (D2/V13), so a last-resort replace-on-encode net backs it up.
    try:
        dest.write_text(wrapped, encoding="ascii")
    except UnicodeEncodeError:
        dest.write_bytes(wrapped.encode("ascii", errors="replace"))
    _warn_if_oversized(dest)
    return dest


def _strip_banned_chars(text: str) -> str:
    return text.translate(_BANNED_CHARS)


def _warn_if_oversized(dest: Path) -> None:
    size = dest.stat().st_size
    if size > _MAX_BYTES:
        log.warning("%s is %d bytes, over Greenhouse's documented ~2.5MB parse limit", dest, size)


# Typography an LLM reaches for, mapped to what an ATS parser can read. Anything
# not listed falls back to NFKD decomposition, then a space -- never deletion,
# because a silently removed character closes up a word boundary.
_ASCII_MAP = {
    "\u2014": " - ",
    "\u2013": "-",
    "\u2012": "-",
    "\u2212": "-",
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": ",",
    "\u201b": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u00ab": '"',
    "\u00bb": '"',
    "\u2022": "*",
    "\u00b7": "*",
    "\u25cf": "*",
    "\u25aa": "*",
    "\u2023": "*",
    "\u2026": "...",
    "\u00a0": " ",
    "\u2009": " ",
    "\u200a": " ",
    "\u202f": " ",
    "\u200b": "",
    "\ufeff": "",
    "\ufffd": " ",
    "\u00ae": "(R)",
    "\u2122": "(TM)",
    "\u00a9": "(C)",
    "\u00b0": " deg",
    "\u2192": "->",
    "\u2190": "<-",
    "\u00d7": "x",
    "\u2044": "/",
    "\u2032": "'",
    "\u2033": '"',
    "\u20ac": " EUR",
    "\u00a3": " GBP",
    "\u00bd": " 1/2",
    "\u00bc": " 1/4",
    "\u00be": " 3/4",
}


def _transliterate(text: str) -> str:
    """Make the text ASCII-safe without losing information."""
    for src, dst in _ASCII_MAP.items():
        text = text.replace(src, dst)
    if text.isascii():
        return text
    decomposed = unicodedata.normalize("NFKD", text)
    out = []
    for ch in decomposed:
        if ch.isascii():
            out.append(ch)
        elif unicodedata.combining(ch):
            continue  # an accent whose base letter we just kept
        else:
            out.append(" ")
    return "".join(out)


def _strip_markdown(text: str) -> str:
    # Convert ## headings to UPPERCASE:
    text = re.sub(r"^#{1,3}\s+(.+)$", lambda m: m.group(1).upper() + ":", text, flags=re.MULTILINE)
    # Remove bold/italic
    text = re.sub(r"\*{1,3}(.+?)\*{1,3}", r"\1", text)
    text = re.sub(r"_{1,3}(.+?)_{1,3}", r"\1", text)
    # Markdown links -> bare URL. A plain-text ATS field can't carry a hyperlink,
    # so the visible link text is dropped in favor of the URL itself -- also the
    # only way to keep `[` `]` `(` `)` from surviving as literal characters.
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\2", text)
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

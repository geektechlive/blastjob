"""Markdown -> professional .docx.

Renders a clean single-font (Calibri) document with tight spacing and manually
styled headings — not Word's oversized default Heading styles. Handles the
markdown the generator emits: `#` name, `##` sections (ruled), `###` roles,
`*italic*` dates, `- ` bullets, `**bold**`, and `[text](url)` links (shown as
their text — the raw link syntax was previously leaking into the document).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .ats import _ASCII_MAP

log = logging.getLogger(__name__)

# Greenhouse's parser documentedly stops parsing beyond this size; warn, never raise.
_MAX_BYTES = 2_500_000

FONT = "Calibri"
BODY_PT = 10.0
NAME_PT = 20.0
CONTACT_PT = 9.5
SECTION_PT = 11.0
ROLE_PT = 10.5
DATE_PT = 9.5

_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")


_MARKER = re.compile(r"^\s*(#{1,6} |[-*+] |\d+\. |>|\||---)")


def _unwrap_paragraphs(md: str) -> str:
    """Join soft-wrapped lines back into one paragraph each.

    Markdown treats a single newline inside a paragraph as a space; a blank line
    is what separates paragraphs. This renderer emitted one Word paragraph per
    SOURCE line, so a body paragraph hard-wrapped at 100 characters came out with
    a line break every 100 characters instead of wrapping at the page margin.
    The user saw exactly that in a real cover letter on 2026-09-10.

    Lines that open a markdown construct (heading, bullet, quote, rule) stay on
    their own line, so resume bullets and headings are untouched.
    """
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            out.append(" ".join(buf))
            buf.clear()

    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip() or _MARKER.match(line):
            flush()
            out.append(line)
            continue
        buf.append(line.strip())
    flush()
    return "\n".join(out)


def _normalize_text(text: str) -> str:
    """Typography normalized the same way exporters/ats.py does it (D9): smart
    quotes, dashes, bullets and ellipses become their plain-text equivalents, and
    the mojibake replacement character U+FFFD is scrubbed rather than passed
    through -- two literal U+FFFD chars shipped in a real packet
    (~/Documents/blastjob/acme-corp-2026-09-10/). Markdown structure
    (`*`, `_`, `[`, `]`, `#`) is untouched by `_ASCII_MAP`, so this is safe to run
    on the raw markdown before parsing. Unlike ats.py, this does not force full
    ASCII -- Word/OOXML is Unicode-native and an accented name should not be
    decomposed into a space; only the mapped typography (and U+FFFD) changes.
    """
    for src, dst in _ASCII_MAP.items():
        text = text.replace(src, dst)
    return text


def write(content_md: str, out_dir: Path, stem: str = "resume") -> Path:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    content_md = _normalize_text(content_md)

    doc = Document()
    section = doc.sections[0]
    section.top_margin = section.bottom_margin = Pt(54)  # 0.75"
    section.left_margin = section.right_margin = Pt(54)

    normal = doc.styles["Normal"]
    normal.font.name = FONT
    normal.font.size = Pt(BODY_PT)
    normal.paragraph_format.space_after = Pt(3)
    normal.paragraph_format.line_spacing = 1.0

    dark = RGBColor(0x1A, 0x1A, 0x1A)
    accent = RGBColor(0x33, 0x33, 0x33)

    # Header mode is ENTERED by a `# ` title, never assumed. Starting it True meant a
    # document with no headings at all -- a cover letter -- never left header mode, so
    # every paragraph rendered centered in 9.5pt contact styling. Resumes are
    # unaffected: they open with a `## ` heading, which already cleared the flag.
    in_header = False
    for raw in _unwrap_paragraphs(content_md).splitlines():
        line = raw.rstrip()
        if not line or line.startswith("---") or _is_frontmatter(line, in_header):
            continue

        if line.startswith("# "):
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.space_after = Pt(2)
            r = p.add_run(_strip_links(line[2:]))
            _font(r, NAME_PT, bold=True, color=dark)
            in_header = True

        elif line.startswith("## "):
            in_header = False
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(9)
            p.paragraph_format.space_after = Pt(3)
            r = p.add_run(line[3:].upper())
            _font(r, SECTION_PT, bold=True, color=dark)
            _bottom_border(p)

        elif line.startswith("### "):
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(6)
            p.paragraph_format.space_after = Pt(0)
            r = p.add_run(_strip_links(line[4:]))
            _font(r, ROLE_PT, bold=True, color=dark)

        elif line.startswith("- "):
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Pt(14)
            p.paragraph_format.first_line_indent = Pt(-9)
            p.paragraph_format.space_after = Pt(2)
            bullet = p.add_run("•  ")
            _font(bullet, BODY_PT, color=accent)
            _add_inline(p, line[2:], size=BODY_PT)

        elif in_header:
            # contact line(s): small, centered, links shown as text
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.space_after = Pt(1)
            r = p.add_run(_strip_links(line))
            _font(r, CONTACT_PT, color=accent)

        elif line.startswith("*") and line.endswith("*") and not line.startswith("**"):
            # a dates line like *January 2025 - Present*
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(2)
            r = p.add_run(line.strip("*"))
            _font(r, DATE_PT, italic=True, color=accent)

        else:
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(7)
            _add_inline(p, line, size=BODY_PT)

    dest = out_dir / f"{stem}.docx"
    doc.save(str(dest))
    _warn_if_oversized(dest)
    return dest


def _warn_if_oversized(dest: Path) -> None:
    size = dest.stat().st_size
    if size > _MAX_BYTES:
        log.warning("%s is %d bytes, over Greenhouse's documented ~2.5MB parse limit", dest, size)


def _is_frontmatter(line: str, in_header: bool) -> bool:
    # Defensive: skip stray YAML `key: value` if a work_history-style header sneaks in.
    return in_header and bool(re.match(r"^(name|email|phone|location|linkedin|github):", line))


def _strip_links(text: str) -> str:
    return _LINK.sub(r"\1", text)


def _font(run, size: float, *, bold: bool = False, italic: bool = False, color=None) -> None:
    from docx.shared import Pt

    run.font.name = FONT
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    if color is not None:
        run.font.color.rgb = color


def _bottom_border(paragraph) -> None:
    """Add a thin bottom rule under a section heading (no direct python-docx API)."""
    from docx.oxml.ns import qn

    p_pr = paragraph._p.get_or_add_pPr()
    borders = p_pr.makeelement(qn("w:pBdr"), {})
    bottom = borders.makeelement(
        qn("w:bottom"),
        {qn("w:val"): "single", qn("w:sz"): "6", qn("w:space"): "2", qn("w:color"): "999999"},
    )
    borders.append(bottom)
    p_pr.append(borders)


def _add_inline(paragraph, text: str, *, size: float = BODY_PT) -> None:
    """Render **bold** / *italic* and strip markdown links to their text."""
    text = _strip_links(text)
    parts = re.split(r"(\*{1,2}[^*]+\*{1,2})", text)
    for part in parts:
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            _font(paragraph.add_run(part[2:-2]), size, bold=True)
        elif part.startswith("*") and part.endswith("*"):
            _font(paragraph.add_run(part[1:-1]), size, italic=True)
        else:
            _font(paragraph.add_run(part), size)

from pathlib import Path

from blastjob.core.docx_text import extract_lines


def extract(path: Path) -> str:
    """All text in the .docx: body, tables (skills grids, role/date columns) and headers/footers.

    Uses the document-order walker in `core/docx_text.py` so table-layout resumes are not
    silently truncated at ingestion.
    """
    return "\n".join(line.text for line in extract_lines(path))

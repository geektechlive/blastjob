from collections.abc import Callable
from pathlib import Path

from blastjob.importers import docx, pdf, text

_DISPATCH = {
    ".pdf": pdf.extract,
    ".docx": docx.extract,
    ".doc": docx.extract,
    ".txt": text.extract,
    ".md": text.extract,
}


def collect(path: Path) -> list[tuple[Path, str]]:
    """Return (file_path, text_content) pairs for all supported files under path."""
    if path.is_file():
        return _extract_file(path)
    results = []
    for child in sorted(path.rglob("*")):
        if child.is_file():
            results.extend(_extract_file(child))
    return results


def _extract_file(path: Path) -> list[tuple[Path, str]]:
    ext = path.suffix.lower()
    fn = _DISPATCH.get(ext)
    if fn is None:
        return []
    try:
        content = fn(path)
        if content.strip():
            return [(path, content)]
    except Exception:
        pass
    return []


def labeled_chunks(
    path: Path,
    on_file: Callable[[int, int, str], None] | None = None,
) -> list[str]:
    """Return XML-tagged text chunks. Calls on_file(index, total, filename) per file."""
    pairs = collect(path)
    total = len(pairs)
    chunks = []
    for i, (file_path, content) in enumerate(pairs, 1):
        if on_file:
            on_file(i, total, file_path.name)
        chunks.append(f'<file path="{file_path}">\n{content}\n</file>')
    return chunks

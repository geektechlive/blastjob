"""A one-tap link to the apply page, saved beside a tailored packet (issue #5).

Written as `{role-slug}.apply_link.html` next to the role's
`{role-slug}.metadata.json`, so `artifacts.role_files` indexes it with the rest
of the packet. The URL comes only from `apply_link.apply_links_for` (the
cluster's best route, already through `safe_external_url`); with no link there
is no file. A plain HTML page with one link opens from Finder and from Files
Quick Look on another device.
"""

from __future__ import annotations

import html
import sqlite3
from pathlib import Path

from blastjob.web.apply_link import APPLY_COLS, apply_links_for

_META_SUFFIX = ".metadata.json"
_QUARANTINE_DIRNAME = "quarantine"  # core/quarantine.py's held-file folder

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Apply: {company} | {role}</title>
<style>
body {{ font: 18px/1.5 -apple-system, system-ui, sans-serif; margin: 2rem 1rem; }}
a.apply {{ display: inline-block; padding: .8rem 1.2rem; background: #0b5cad; color: #fff;
  border-radius: 8px; text-decoration: none; font-weight: 600; }}
.url {{ color: #555; word-break: break-all; font-size: 14px; }}
</style></head>
<body>
<h1>{company}</h1>
<p>{role}</p>
<p><a class="apply" href="{url}">Open the posting ({label})</a></p>
<p class="url">{url}</p>
</body></html>
"""


def _role_slug(files: list[Path]) -> tuple[Path, str] | None:
    """The packet folder + role slug, from the role's own metadata sidecar
    (`role_files` can also list a held copy under `quarantine/`, never used)."""
    for f in files:
        if f.name.endswith(_META_SUFFIX) and f.parent.name != _QUARANTINE_DIRNAME:
            return f.parent, f.name[: -len(_META_SUFFIX)]
    return None


def write_apply_file(conn: sqlite3.Connection, job_id: int, files: list[Path]) -> Path | None:
    """Write the apply-link page for `job_id` beside its packet `files`."""
    found = _role_slug(files)
    row = conn.execute(f"SELECT {APPLY_COLS}, title FROM jobs WHERE id=?", (job_id,)).fetchone()
    if found is None or row is None:
        return None
    link = apply_links_for(conn, [row]).get(job_id)
    if link is None:
        return None
    folder, slug = found
    path = folder / f"{slug}.apply_link.html"
    path.write_text(
        _PAGE.format(
            company=html.escape(row["company"] or ""),
            role=html.escape(row["title"] or ""),
            url=html.escape(link.url, quote=True),
            label=html.escape(link.label),
        ),
        encoding="utf-8",
    )
    return path

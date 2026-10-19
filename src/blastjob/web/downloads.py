"""Which file `GET /jobs/{job_id}/files/{name}` serves -- the one resolver.

The download route and the job page's Files list (`web/routes_activity.py`)
both call `resolve_download`, so the list links an indexed artifact only when
the route would serve exactly that file. A basename alone is not enough: the
route searches every packet folder of the job, newest first, so a same-named
file elsewhere (or in an older packet) would be served in its place.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from blastjob import config as cfg_mod
from blastjob.core.artifacts import stored_path
from blastjob.core.quarantine import held_names
from blastjob.models.config import BlastJobConfig
from blastjob.web import packets


def resolve_download(
    conn: sqlite3.Connection, cfg: BlastJobConfig, job_id: int, name: str
) -> Path | None:
    """The file the download route serves for (job_id, name), or None.

    Every packet this job ever produced, newest first, so an older row on the
    Packets page still opens its own documents after a re-tailor. Only
    non-quarantined files directly inside a packet folder under the output
    directory qualify (`packets.packet_file_path`)."""
    for folder in packets.job_packet_dirs(conn, job_id):
        target = packets.packet_file_path(cfg, folder, name)
        if target is None:
            continue
        # A QUARANTINED report is authoritative wherever the file sits: a
        # document the gate held must not be served from the public folder
        # even if a move missed it (a shared folder, a failed rename).
        if name in held_names(target.parent):
            continue
        return target
    return None


def is_downloadable(
    conn: sqlite3.Connection, cfg: BlastJobConfig, job_id: int, path: str | Path
) -> bool:
    """True when the route, asked for this file's basename, serves THIS file.

    Compared entry-relative (root form, `paths.canonical`): the index stores
    root form and the route serves the file wherever filing put its entry."""
    root = cfg_mod.output_dir(cfg)
    want = stored_path(path, root)
    served = resolve_download(conn, cfg, job_id, want.name)
    return served is not None and stored_path(served, root) == want

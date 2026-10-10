"""What a route handler needs from the app, as one explicitly-typed object.

`web/app.py` grew to 1,309 lines with 66 routes inside a single 1,194-line
`create_app` closure. The routes could not simply move to other modules because
every handler captured that closure -- `conn`, `load_config`, `render`, the
nav-count callable, the commute radius -- so there was nothing to pass them.

`web/manual_job.py` and `web/same_role.py` had already solved this in the small,
with `register(app, conn, ...)` taking each dependency as an argument. This is
that pattern with the arguments collected into one frozen object, so a route
module declares `deps: Deps` instead of accepting five positional callables that
must stay in the same order everywhere.

Typed rather than implicit, deliberately: mypy checks a route module that takes
`Deps`, and could check nothing at all about a captured closure.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import HTMLResponse

from blastjob.models.config import BlastJobConfig

Render = Callable[..., HTMLResponse]


@dataclass(frozen=True)
class Deps:
    """Injected once in `create_app`, handed to every `register()`.

    `conn` returns a NEW connection per call; the caller closes it. That is the
    existing contract everywhere in this package and it is not changed here --
    every route still opens and closes its own, which is what keeps a failed
    request from leaking one.
    """

    conn: Callable[[], sqlite3.Connection]
    load_config: Callable[[], BlastJobConfig]
    render: Render
    nav_counts: Callable[[], dict[str, int]]
    radius: Callable[[], float]


__all__ = ["Deps", "Render", "Request"]

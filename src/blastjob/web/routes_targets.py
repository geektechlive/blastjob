"""The targets screen: the owner's target-company list and the news found for it (#85).

Web is the only surface, so everything the weekly watch does is visible and editable
here. The watch itself runs in the daily job (`daily_targets`), never from a request.
Every POST goes through `require_csrf` (token plus Origin check). Event spans are page
text and are only ever rendered through Jinja autoescaping; links go through `safe_url`.
See docs/how-it-works/employer-data.md.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from blastjob.core import company as co
from blastjob.core import network as net
from blastjob.core import target_store as ts
from blastjob.core.page_fetch import host_of
from blastjob.web.deps import Deps
from blastjob.web.security import require_csrf

MAX_PASTE_CHARS = 20_000
_BACK_OK = re.compile(r"/targets(?:/[^/?#\s]+)?")


def _event_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        **dict(row),
        "label": ts.EVENT_LABELS.get(row["event_type"], row["event_type"]),
        "basis_label": ts.BASIS_LABELS.get(row["source_basis"], row["source_basis"]),
        "host": host_of(row["source_url"]),
    }


def _target_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every target with its undismissed events and warm-path counts, the one with the
    newest event first, then the rest by name."""
    targets = ts.list_targets(conn)
    keys = [t["company_key"] for t in targets]
    events = ts.all_events(conn)
    contacts = net.warm_counts(conn, keys)
    deciders = net.decision_maker_counts(conn, keys)
    rows = [
        {
            **dict(t),
            "events": [_event_view(e) for e in events.get(t["company_key"], [])],
            "contacts": contacts.get(t["company_key"], 0),
            "decision_makers": deciders.get(t["company_key"], 0),
        }
        for t in targets
    ]
    with_events = sorted(
        (r for r in rows if r["events"]), key=lambda r: r["events"][0]["published_on"], reverse=True
    )
    without = sorted((r for r in rows if not r["events"]), key=lambda r: r["company"].lower())
    return with_events + without


def _back(value: str, fallback: str = "/targets") -> str:
    """Where to send the owner after a button: only our own targets pages."""
    return value if _BACK_OK.fullmatch(value) else fallback


def register(app: FastAPI, deps: Deps) -> None:
    def _page(request: Request, conn: sqlite3.Connection, **extra: Any) -> HTMLResponse:
        return deps.render(request, "targets.html", targets=_target_rows(conn), **extra)

    @app.get("/targets", response_class=HTMLResponse)
    async def targets_page(request: Request):
        c = deps.conn()
        try:
            return _page(request, c)
        finally:
            c.close()

    @app.post("/targets/add", response_class=HTMLResponse)
    async def targets_add(
        request: Request,
        companies: str = Form(""),
        tier: str = Form("core"),
        _: None = Depends(require_csrf),
    ):
        if tier not in ts.TIERS:
            return HTMLResponse("Invalid tier", status_code=400)
        errors: list[str] = []
        parsed = ts.BulkParse((), ())
        if len(companies) > MAX_PASTE_CHARS:
            errors.append(f"That paste is too long ({MAX_PASTE_CHARS} characters at most).")
        else:
            parsed = ts.parse_bulk(companies)
        c = deps.conn()
        try:
            result = ts.add_targets(c, parsed.items, tier)
            return _page(
                request, c, add_result=result, add_errors=[*errors, *parsed.errors], added_tier=tier
            )
        finally:
            c.close()

    @app.get("/targets/{key}", response_class=HTMLResponse)
    async def target_page(request: Request, key: str):
        c = deps.conn()
        try:
            target = ts.get_target(c, key)
            if target is None:
                return HTMLResponse("Not found", status_code=404)
            return deps.render(
                request,
                "target.html",
                target=dict(target),
                events=[_event_view(e) for e in ts.events_for(c, key)],
                contacts=net.contacts_for_key(c, key),
                has_profile=co.get_profile_by_key(c, key) is not None,
                key_path=quote(key, safe=""),
            )
        finally:
            c.close()

    @app.post("/targets/{key}/remove")
    async def target_remove(key: str, _: None = Depends(require_csrf)):
        c = deps.conn()
        try:
            if not ts.remove_target(c, key):
                return HTMLResponse("Not found", status_code=404)
            return RedirectResponse("/targets", status_code=303)
        finally:
            c.close()

    @app.post("/targets/{key}/tier")
    async def target_tier(key: str, tier: str = Form(...), _: None = Depends(require_csrf)):
        if tier not in ts.TIERS:
            return HTMLResponse("Invalid tier", status_code=400)
        c = deps.conn()
        try:
            if not ts.set_tier(c, key, tier):
                return HTMLResponse("Not found", status_code=404)
            return RedirectResponse("/targets", status_code=303)
        finally:
            c.close()

    @app.post("/events/{event_id}/dismiss")
    async def event_dismiss(
        event_id: int, back: str = Form("/targets"), _: None = Depends(require_csrf)
    ):
        c = deps.conn()
        try:
            if not ts.dismiss_event(c, event_id):
                return HTMLResponse("Not found", status_code=404)
            return RedirectResponse(_back(back), status_code=303)
        finally:
            c.close()

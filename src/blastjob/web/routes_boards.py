"""Finding a dream company's job board from its target page (#90, ADR 0056).

`POST /targets/{key}/boards/find` starts a search in the background and
`GET /targets/{key}/boards` shows it: the boards found, each with its identity evidence and
two sample postings. `POST /targets/{key}/boards/use` adds ONE of them to
`discovery.target_boards`, only after the owner's click, and only with an identity basis the
server holds (never one the browser sent).

- The resolver is blocking (httpx, and Dayforce's `asyncio.run`), so the whole search runs in
  a worker thread (`asyncio.to_thread`), one at a time. The cockpit stays responsive.
- The search result is kept here on the server, per company. The confirm POST only NAMES a
  candidate (ats and token); its basis comes from the kept result, or is `owner_confirmed`
  when the owner ticked the box. A forged field changes nothing.
- Confirming reloads the latest settings, dedupes by normalized (ats, token) and saves with
  the store's revision check; on `StaleSettings` the page says to try again.
- Every POST goes through `require_csrf`. Page text (titles, locations, the ATS-reported
  name) is only rendered through Jinja autoescaping.

See docs/how-it-works/discovery.md.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from blastjob import logging as applog
from blastjob.core import target_store as ts
from blastjob.core.company import company_key
from blastjob.discovery import board_finder as bf
from blastjob.discovery.board_finder import Candidate, Findings
from blastjob.models.config import BlastJobConfig, TargetBoard
from blastjob.settings_store import StaleSettings
from blastjob.web.deps import Deps
from blastjob.web.runtime import RUNTIME, Run
from blastjob.web.security import require_csrf

RUN_NAME = "board_find"
MAX_KEPT_FINDINGS = 50
FINDINGS_MAX_AGE_S = 30 * 60  # a search older than this is not trusted at confirm
OWNER_CONFIRMED = "owner_confirmed"

BASIS_LABELS = {
    "domain_link": "linked from the company's own careers page",
    "ats_name": "the board reports the company's name",
    "owner_confirmed": "confirmed by you",
    "": "entered by hand",
}
NOTICES = {
    "added": "Board added. It is read from the next Find run on.",
    "exists": "That board is already on your list.",
    "stale": "The settings changed while you were looking, so nothing was saved. Try again.",
    "needs_confirm": "Nothing was saved: tick that the board belongs to this company first.",
    "no_findings": "That search is no longer available. Search again.",
    "exists_other": "That board is already on your list under another company.",
    "busy": "A search is already running. Try again when it finishes.",
}

SaveCfg = Callable[[BlastJobConfig], None]

# Searches the cockpit has run, per company, newest last. Bounded: the cockpit is a long-lived
# service. A result is the server's record of what evidence each board came with.
_FINDINGS: OrderedDict[str, tuple[float, Findings]] = OrderedDict()
_FINDINGS_LOCK = threading.Lock()
# One resolution at a time: each makes many outbound requests.
_SEARCH_LOCK = threading.Lock()


def _now() -> float:
    return time.monotonic()


def _keep(key: str, findings: Findings) -> None:
    with _FINDINGS_LOCK:
        _FINDINGS[key] = (_now(), findings)
        _FINDINGS.move_to_end(key)
        while len(_FINDINGS) > MAX_KEPT_FINDINGS:
            _FINDINGS.popitem(last=False)


def _kept(key: str) -> Findings | None:
    with _FINDINGS_LOCK:
        entry = _FINDINGS.get(key)
        if entry is None:
            return None
        stamp, findings = entry
        if _now() - stamp > FINDINGS_MAX_AGE_S:
            del _FINDINGS[key]
            return None
        return findings


def _notice_text(notice: str, other: str) -> str:
    """The visible message for a notice code. `other` is a company name from the owner's own
    settings; the template autoescapes it."""
    if notice == "exists_other":
        return f"That board is already on your list under {other[:80] or 'another company'}."
    return NOTICES.get(notice, "")


def _jina_key() -> str:
    from blastjob import secrets as secrets_mod

    secrets_mod.load_secrets_env()
    return os.environ.get("JINA_API_KEY", "").strip()


def _search(company: str, domain: str) -> Findings:
    """The blocking search, run in a worker thread."""
    with _SEARCH_LOCK:
        return bf.find_boards(company, domain, _jina_key())


def _norm_board(ats: str, token: str) -> tuple[str, str]:
    return ats.strip().lower(), token.strip().lower()


def _board_key(b: TargetBoard) -> tuple[str, str]:
    return _norm_board(b.ats, b.token)


def _candidate(findings: Findings, ats: str, token: str) -> Candidate | None:
    want = _norm_board(ats, token)
    return next((c for c in findings.candidates if _norm_board(c.ats, c.token) == want), None)


def existing_company(load: Callable[[], BlastJobConfig], cand: Candidate) -> str:
    """The company an identical (ats, token) board is already listed under, or ""."""
    want = _norm_board(cand.ats, cand.token)
    for b in load().discovery.target_boards:
        if _board_key(b) == want:
            return b.company
    return ""


def add_board(
    load: Callable[[], BlastJobConfig],
    save: SaveCfg,
    company: str,
    cand: Candidate,
    basis: str,
) -> str:
    """Add the board to the LATEST settings: 'added', 'exists' or 'stale'. Idempotent."""
    cfg = load()
    boards = cfg.discovery.target_boards
    if any(_board_key(b) == _norm_board(cand.ats, cand.token) for b in boards):
        return "exists"
    new = cfg.model_copy(deep=True)
    new.discovery.target_boards = [
        *boards,
        TargetBoard.model_validate(
            {
                "company": company,
                "ats": cand.ats,
                "token": cand.token,
                "identity_basis": basis,
            }
        ),
    ]
    try:
        save(new.with_revision(cfg.settings_revision))
    except StaleSettings:
        return "stale"
    return "added"


def _basis_for(cand: Candidate, owner_confirmed: bool) -> str:
    """The identity basis to store: the server's own evidence, else the owner's tick."""
    if cand.identity_basis:
        return cand.identity_basis
    return OWNER_CONFIRMED if owner_confirmed else ""


def register(app: FastAPI, deps: Deps, save_config: SaveCfg) -> None:
    def _dream_target(key: str) -> Any:
        c = deps.conn()
        try:
            row = ts.get_target(c, key)
            return dict(row) if row is not None and row["tier"] == "dream" else None
        finally:
            c.close()

    def _configured(company: str) -> list[dict[str, str]]:
        want = company_key(company)
        return [
            {
                "ats": b.ats,
                "token": b.token,
                "basis": BASIS_LABELS.get(b.identity_basis, b.identity_basis),
            }
            for b in deps.load_config().discovery.target_boards
            if company_key(b.company) == want
        ]

    @app.get("/targets/{key}/boards", response_class=HTMLResponse)
    async def boards_fragment(request: Request, key: str, notice: str = "", other: str = ""):
        target = _dream_target(key)
        if target is None:
            return HTMLResponse("")
        running = RUNTIME.running_keyed(RUN_NAME, key) is not None
        return deps.render(
            request,
            "_board_search.html",
            target=target,
            key_path=quote(key, safe=""),
            running=running,
            findings=None if running else _kept(key),
            configured=_configured(target["company"]),
            notice=_notice_text(notice, other),
            basis_labels=BASIS_LABELS,
        )

    @app.post("/targets/{key}/boards/find")
    async def boards_find(key: str, _: None = Depends(require_csrf)):
        target = _dream_target(key)
        if target is None:
            return HTMLResponse("Only a dream-list company can be searched", status_code=404)

        if RUNTIME.active((RUN_NAME,), recent_seconds=0) is not None:
            # Never queue threads behind the lock: say so instead.
            return RedirectResponse(f"/targets/{quote(key, safe='')}?notice=busy", status_code=303)

        async def work(run: Run) -> None:
            run.emit("log", "searching")
            try:
                found = await asyncio.to_thread(_search, target["company"], target["domain"] or "")
            except Exception as e:  # noqa: BLE001 - shown on the page, never lost
                applog.log_exception("board search", e)
                found = Findings(
                    company=target["company"],
                    notes=(f"The search failed ({type(e).__name__}); nothing was checked.",),
                )
            _keep(key, found)

        RUNTIME.start(RUN_NAME, work, key=key)
        return RedirectResponse(f"/targets/{quote(key, safe='')}", status_code=303)

    @app.post("/targets/{key}/boards/use")
    async def boards_use(
        key: str,
        ats: str = Form(...),
        token: str = Form(...),
        owner_confirmed: str = Form(""),
        _: None = Depends(require_csrf),
    ):
        target = _dream_target(key)
        if target is None:
            return HTMLResponse("Only a dream-list company can have a board", status_code=404)
        back = f"/targets/{quote(key, safe='')}"
        findings = _kept(key)
        if findings is None:
            return RedirectResponse(f"{back}?notice=no_findings", status_code=303)
        cand = _candidate(findings, ats, token)
        if cand is None:
            return HTMLResponse("That board was not in the search", status_code=400)
        owner = existing_company(deps.load_config, cand)
        if owner and company_key(owner) != key:
            return RedirectResponse(
                f"{back}?notice=exists_other&other={quote(owner[:80], safe='')}", status_code=303
            )
        basis = _basis_for(cand, bool(owner_confirmed))
        if not basis:
            return RedirectResponse(f"{back}?notice=needs_confirm", status_code=303)
        outcome = add_board(deps.load_config, save_config, target["company"], cand, basis)
        return RedirectResponse(f"{back}?notice={outcome}", status_code=303)

"""The Category searches section of /targets (#91): add, edit, enable or disable, delete.

The weekly run itself lives in `discovery/category_search.py` and only runs inside the daily job;
nothing here searches. Every POST goes through `require_csrf` (token plus Origin/Host checks).
Labels and terms are owner text and are rendered only through Jinja autoescaping. An edit,
toggle or delete carries the category's `version`; a stale one answers 409 and the newer stored
row stays. See docs/how-it-works/discovery.md#category-searches.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, Form, Path, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from blastjob.core import category_store as cs
from blastjob.web.deps import Deps
from blastjob.web.routes_targets import render_targets
from blastjob.web.security import require_csrf

MAX_ROW = 2**62  # SQLite integers are 63-bit; a larger id or version is out of range, not a 500

STALE_MESSAGE = (
    "This category changed since you opened it, so nothing was saved. The newer version is "
    "shown below; your unsaved text is kept in the box."
)


def register(app: FastAPI, deps: Deps) -> None:
    @app.post("/targets/categories", response_class=HTMLResponse)
    async def category_add(
        request: Request,
        label: str = Form(""),
        terms: str = Form(""),
        is_dream: str = Form(""),
        _: None = Depends(require_csrf),
    ):
        draft = {"label": label, "terms": terms, "is_dream": bool(is_dream)}
        c = deps.conn()
        try:
            try:
                cs.add_category(c, label=label, terms=terms, is_dream=bool(is_dream))
            except cs.CategoryError as e:
                return render_targets(
                    deps, request, c, 400, category_error=str(e), category_draft=draft
                )
            return RedirectResponse("/targets", status_code=303)
        finally:
            c.close()

    @app.post("/targets/categories/{category_id}", response_class=HTMLResponse)
    async def category_edit(
        request: Request,
        category_id: Annotated[int, Path(ge=0, le=MAX_ROW)],
        version: Annotated[int, Form(ge=0, le=MAX_ROW)],
        label: str = Form(""),
        terms: str = Form(""),
        is_dream: str = Form(""),
        _: None = Depends(require_csrf),
    ):
        draft = {"label": label, "terms": terms, "is_dream": bool(is_dream)}
        c = deps.conn()
        try:
            try:
                cs.update_category(
                    c, category_id, version, label=label, terms=terms, is_dream=bool(is_dream)
                )
            except cs.CategoryNotFound:
                return HTMLResponse("Not found", status_code=404)
            except cs.StaleCategory:
                return render_targets(
                    deps,
                    request,
                    c,
                    409,
                    category_error=STALE_MESSAGE,
                    category_draft=draft,
                    category_draft_id=category_id,
                )
            except cs.CategoryError as e:
                return render_targets(
                    deps,
                    request,
                    c,
                    400,
                    category_error=str(e),
                    category_draft=draft,
                    category_draft_id=category_id,
                )
            return RedirectResponse("/targets", status_code=303)
        finally:
            c.close()

    @app.post("/targets/categories/{category_id}/enabled", response_class=HTMLResponse)
    async def category_enabled(
        request: Request,
        category_id: Annotated[int, Path(ge=0, le=MAX_ROW)],
        version: Annotated[int, Form(ge=0, le=MAX_ROW)],
        enabled: str = Form("1"),
        _: None = Depends(require_csrf),
    ):
        c = deps.conn()
        try:
            try:
                cs.set_enabled(c, category_id, version, enabled == "1")
            except cs.CategoryNotFound:
                return HTMLResponse("Not found", status_code=404)
            except cs.StaleCategory:
                return render_targets(deps, request, c, 409, category_error=STALE_MESSAGE)
            return RedirectResponse("/targets", status_code=303)
        finally:
            c.close()

    @app.post("/targets/categories/{category_id}/delete", response_class=HTMLResponse)
    async def category_delete(
        request: Request,
        category_id: Annotated[int, Path(ge=0, le=MAX_ROW)],
        version: Annotated[int, Form(ge=0, le=MAX_ROW)],
        _: None = Depends(require_csrf),
    ):
        c = deps.conn()
        try:
            try:
                cs.delete_category(c, category_id, version)
            except cs.CategoryNotFound:
                return HTMLResponse("Not found", status_code=404)
            except cs.StaleCategory:
                msg = "This category changed since you opened it, so it was not deleted."
                return render_targets(deps, request, c, 409, category_error=msg)
            return RedirectResponse("/targets", status_code=303)
        finally:
            c.close()

"""The story bank page (#101): add, edit and delete saved interview stories.

Storage only: nothing here reaches the prep pack or a model. Every POST goes through
`require_csrf` (token plus Origin/Host checks). Story text is owner-typed but still rendered
only through Jinja autoescaping. An edit or delete carries the story's `version`; a stale one
answers 409 and the newer stored text stays. See docs/how-it-works/stories.md.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, Path, Request
from fastapi.responses import HTMLResponse

from blastjob.core import stories as st
from blastjob.web import workhistory
from blastjob.web.deps import Deps
from blastjob.web.security import require_csrf

MAX_ROW = 2**62  # SQLite integers are 63-bit; a larger id or version is out of range, not a 500

STALE_MESSAGE = (
    "This story changed since you opened it, so nothing was saved. The newer text is shown "
    "below; your unsaved text is kept in the box."
)


def register(app: FastAPI, deps: Deps) -> None:
    def _cards(conn: sqlite3.Connection) -> tuple[list[dict[str, Any]], bool]:
        """The story cards and whether the figure check ran. When the work history can't be
        read the check did not run, and the page says so (never an all-clear)."""
        cards = [dict(row) for row in st.list_stories(conn)]
        try:
            history = workhistory.read_work_history(deps.load_config()).content
        except (OSError, UnicodeDecodeError):
            return [{**card, "unverified": []} for card in cards], False
        checked = [{**card, "unverified": st.unverified_figures(card, history)} for card in cards]
        return checked, True

    def _page(
        request: Request,
        conn: sqlite3.Connection,
        status: int = 200,
        **extra: Any,
    ) -> HTMLResponse:
        cards, figures_checked = _cards(conn)
        response = deps.render(
            request, "stories.html", stories=cards, figures_checked=figures_checked, **extra
        )
        response.status_code = status
        return response

    @app.get("/stories", response_class=HTMLResponse)
    async def stories_page(request: Request):
        c = deps.conn()
        try:
            return _page(request, c)
        finally:
            c.close()

    @app.post("/stories", response_class=HTMLResponse)
    async def story_add(
        request: Request,
        title: str = Form(""),
        employer: str = Form(""),
        year: str = Form(""),
        situation: str = Form(""),
        task: str = Form(""),
        action: str = Form(""),
        result: str = Form(""),
        tags: str = Form(""),
        _: None = Depends(require_csrf),
    ):
        form = dict(
            title=title,
            employer=employer,
            year=year,
            situation=situation,
            task=task,
            action=action,
            result=result,
            tags=tags,
        )
        c = deps.conn()
        try:
            try:
                st.add_story(c, **form)
            except st.StoryError as e:
                return _page(request, c, 400, error=str(e), draft=form)
            return _page(request, c)
        finally:
            c.close()

    @app.post("/stories/{story_id}", response_class=HTMLResponse)
    async def story_edit(
        request: Request,
        story_id: Annotated[int, Path(ge=0, le=MAX_ROW)],
        version: Annotated[int, Form(ge=0, le=MAX_ROW)],
        title: str = Form(""),
        employer: str = Form(""),
        year: str = Form(""),
        situation: str = Form(""),
        task: str = Form(""),
        action: str = Form(""),
        result: str = Form(""),
        tags: str = Form(""),
        _: None = Depends(require_csrf),
    ):
        form = dict(
            title=title,
            employer=employer,
            year=year,
            situation=situation,
            task=task,
            action=action,
            result=result,
            tags=tags,
        )
        c = deps.conn()
        try:
            try:
                st.update_story(c, story_id, version, **form)
            except st.StoryNotFound:
                return HTMLResponse("Not found", status_code=404)
            except st.StaleStory:
                return _page(request, c, 409, error=STALE_MESSAGE, draft=form, draft_id=story_id)
            except st.StoryError as e:
                return _page(request, c, 400, error=str(e), draft=form, draft_id=story_id)
            return _page(request, c)
        finally:
            c.close()

    @app.post("/stories/{story_id}/delete", response_class=HTMLResponse)
    async def story_delete(
        request: Request,
        story_id: Annotated[int, Path(ge=0, le=MAX_ROW)],
        version: Annotated[int, Form(ge=0, le=MAX_ROW)],
        _: None = Depends(require_csrf),
    ):
        c = deps.conn()
        try:
            try:
                st.delete_story(c, story_id, version)
            except st.StoryNotFound:
                return HTMLResponse("Not found", status_code=404)
            except st.StaleStory:
                msg = "This story changed since you opened it, so it was not deleted."
                return _page(request, c, 409, error=msg)
            return _page(request, c)
        finally:
            c.close()

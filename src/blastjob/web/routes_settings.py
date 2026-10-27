"""Settings, work history, and ingest routes.

`/settings` and `/work-history` moved here verbatim out of `web/app.py`
(2026-09-17) alongside the new `/work-history/ingest`, which is the route that
makes `core/ingest.py` reachable from the cockpit at all -- previously its only
caller was a retired terminal UI.
"Web is the only surface. A capability with no route does not exist."

`register` takes `save_config` as a fourth argument alongside `deps`, because
`Deps` (frozen, shared by every route module) only carries `load_config` --
`/settings` POST is the one route in the whole cockpit that writes config, so
it is not worth widening `Deps` for. `web/manual_job.py` already sets the
precedent of a route module taking extra callables positionally.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from blastjob import config as cfg_mod
from blastjob.label_names import recruiter_label_problem
from blastjob.llm import providers
from blastjob.models.config import BlastJobConfig
from blastjob.settings_store import StaleSettings
from blastjob.web import local_guard as lg
from blastjob.web import mail_settings as ms
from blastjob.web import packets
from blastjob.web import settings_form as sf
from blastjob.web import workhistory as wh
from blastjob.web.deps import Deps
from blastjob.web.runtime import RUNTIME, Run
from blastjob.web.security import require_csrf
from blastjob.web.upload_guard import MAX_NETWORK_UPLOAD_BYTES, parse_content_length

SaveCfg = Callable[[BlastJobConfig], None]

# Mirrors importers/folder.py's `_DISPATCH` keys. Kept as a literal set here
# (rather than importing `_DISPATCH`, a private name) so an upload the
# importer would silently skip is instead rejected with a clear message
# before any run starts.
SUPPORTED_SUFFIXES = {".pdf", ".docx", ".doc", ".txt", ".md"}

STALE_MESSAGE = (
    "Someone else changed the settings after this page was loaded, so nothing was saved. "
    "Reload the page to see the current values, then make your change again."
)


def register(app: FastAPI, deps: Deps, save_config: SaveCfg) -> None:
    def _settings_page(
        request: Request, cfg: BlastJobConfig, status_code: int = 200, **extra
    ) -> HTMLResponse:
        page = deps.render(
            request,
            "settings.html",
            cfg=cfg,
            is_local=packets.is_local_request(request),
            mail=ms.status(cfg),
            active_model=providers.active_model(cfg),
            towns_text=sf.towns_text(cfg.discovery.towns),
            boards_text=sf.boards_text(cfg.discovery.target_boards),
            employer_domains_text=sf.employer_domains_text(cfg.discovery.employer_domains),
            revision=cfg.settings_revision or "",
            **extra,
        )
        page.status_code = status_code
        return page

    def _save(request: Request, cfg: BlastJobConfig, form: sf.Form) -> HTMLResponse | None:
        """Save `cfg` as an edit of the revision the form was loaded at; the
        page to show instead when that revision is no longer current."""
        revision = form.get("revision")
        try:
            save_config(cfg.with_revision(revision) if revision is not None else cfg)
        except StaleSettings:
            return _settings_page(request, deps.load_config(), 409, settings_error=STALE_MESSAGE)
        return None

    @app.get("/settings", response_class=HTMLResponse)
    async def settings(request: Request):
        return _settings_page(request, deps.load_config())

    @app.post("/settings")
    async def save_settings(request: Request, _: None = Depends(require_csrf)):
        form = {k: str(v) for k, v in (await request.form()).items()}
        cfg = deps.load_config()
        try:
            new = sf.apply_form(cfg, form)
        except sf.FormError as e:
            return _settings_page(request, cfg, 400, settings_error=str(e))
        if blocked := lg.guarded_changes(request, cfg, new):
            return _settings_page(request, cfg, 403, settings_error=lg.refusal_message(blocked))
        if (stale := _save(request, new, form)) is not None:
            return stale
        return RedirectResponse("/settings", status_code=303)

    # ----- mail accounts (env var NAMES only; values stay in secrets.env) -----

    @app.post("/settings/mail", response_class=HTMLResponse)
    async def save_mail_settings(
        request: Request,
        mail_alerts_enabled: bool = Form(False),
        mail_imap_host: str = Form(...),
        mail_user_env: str = Form(...),
        mail_password_env: str = Form(...),
        mail_mailbox: str = Form("INBOX"),
        personal_mail_imap_host: str = Form(""),
        personal_mail_user_env: str = Form(""),
        personal_mail_password_env: str = Form(""),
        personal_mail_mailbox: str = Form(""),
        _: None = Depends(require_csrf),
    ):
        """Env var NAMES are validated and REJECTED on mismatch -- never
        sanitized. A pasted app password would otherwise land in the settings
        table, which the daily backup copies and `setup get` prints; the page re-renders
        with an error and nothing is written. The rejected value is not echoed back."""
        current = deps.load_config()
        cfg = current.model_copy(deep=True)
        error = ms.validate_names(
            mail_user_env=mail_user_env.strip(),
            mail_password_env=mail_password_env.strip(),
            personal_mail_user_env=personal_mail_user_env.strip(),
            personal_mail_password_env=personal_mail_password_env.strip(),
            cfg=cfg,
        )
        if error:
            return _settings_page(request, current, mail_error=error)
        d = cfg.discovery
        d.mail_alerts_enabled = mail_alerts_enabled
        d.mail_imap_host = mail_imap_host.strip()
        d.mail_user_env = mail_user_env.strip()
        d.mail_password_env = mail_password_env.strip()
        d.mail_mailbox = mail_mailbox.strip() or "INBOX"
        d.personal_mail_imap_host = personal_mail_imap_host.strip()
        d.personal_mail_user_env = personal_mail_user_env.strip()
        d.personal_mail_password_env = personal_mail_password_env.strip()
        d.personal_mail_mailbox = personal_mail_mailbox.strip()
        form = {k: str(v) for k, v in (await request.form()).items()}
        if "recruiter_label" in form:
            # Absent = keep the stored label (older forms); empty = off. Read from the raw form
            # because a declared optional Form field cannot tell the two apart.
            d.recruiter_label = form["recruiter_label"].strip()
        if problem := recruiter_label_problem(
            d.recruiter_label, d.personal_mail_mailbox, d.mail_mailbox
        ):
            return _settings_page(request, current, mail_error=f"Recruiter label: {problem}")
        if blocked := lg.guarded_changes(request, current, cfg):
            return _settings_page(request, current, 403, mail_error=lg.refusal_message(blocked))
        if (stale := _save(request, cfg, form)) is not None:
            return stale
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/mail/test/{role}", response_class=HTMLResponse)
    async def test_mail_account(
        request: Request,
        role: str,
        _: None = Depends(require_csrf),
    ):
        """Read-only login probe against the SAVED config, so diagnosing an app
        password never requires printing it. Deliberately not driven by the
        submitted host: that would make this an outbound-connection primitive
        on a surface with no auth."""
        if role not in (ms.ALERT, ms.PERSONAL):
            raise HTTPException(status_code=404, detail="unknown mail account")
        ok, detail = ms.probe(deps.load_config(), role)
        return deps.render(request, "_mail_probe.html", ok=ok, detail=detail, role=role)

    # ----- work history (single canonical input for every generated doc) -----

    @app.get("/work-history", response_class=HTMLResponse)
    async def work_history_page(request: Request):
        return deps.render(
            request, "work_history.html", view=wh.read_work_history(deps.load_config())
        )

    @app.post("/work-history", response_class=HTMLResponse)
    async def save_work_history_route(
        request: Request,
        content: str = Form(...),
        base_version: str = Form(""),
        _: None = Depends(require_csrf),
    ):
        cfg = deps.load_config()
        try:
            view = wh.save_work_history(cfg, content, expected_version=base_version)
        except wh.WorkHistoryConflictError as exc:
            return deps.render(
                request,
                "work_history.html",
                view=wh.with_content(exc.current, content),
                error=str(exc),
            )
        except wh.EmptyWorkHistoryError as exc:
            current = wh.read_work_history(cfg)
            return deps.render(
                request,
                "work_history.html",
                view=wh.with_content(current, content),
                error=str(exc),
            )
        except OSError as exc:
            current = wh.read_work_history(cfg)
            return deps.render(
                request,
                "work_history.html",
                view=wh.with_content(current, content),
                error=f"Save failed: {exc}",
            )
        return deps.render(request, "work_history.html", view=view, saved=True)

    # ----- ingest (source documents -> work_history.md + templates) -----

    @app.get("/work-history/ingest", response_class=HTMLResponse)
    async def ingest_page(request: Request):
        run = RUNTIME.active(("ingest",), recent_seconds=0)
        return deps.render(request, "ingest.html", run=run)

    @app.post("/work-history/ingest", response_class=HTMLResponse)
    async def ingest_route(
        request: Request,
        files: list[UploadFile] = File(...),
        _: None = Depends(require_csrf),
    ):
        already = RUNTIME.active(("ingest",), recent_seconds=0)
        if already is not None:
            return deps.render(
                request,
                "ingest.html",
                run=already,
                error="An ingest is already running -- wait for it to finish.",
            )

        cfg = deps.load_config()
        names = [Path(f.filename or "").name for f in files]
        bad = [n for n in names if not n or Path(n).suffix.lower() not in SUPPORTED_SUFFIXES]
        if not names or bad:
            supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
            return deps.render(
                request,
                "ingest.html",
                run=None,
                error=(
                    "Unsupported file(s): "
                    + (", ".join(bad) if bad else "no files selected")
                    + f". Supported types: {supported}."
                ),
            )

        declared = parse_content_length(request.headers.get("content-length"))
        if declared is not None and declared > MAX_NETWORK_UPLOAD_BYTES:
            return deps.render(
                request,
                "ingest.html",
                run=None,
                error=f"Upload too large ({MAX_NETWORK_UPLOAD_BYTES // (1024 * 1024)}MB max).",
            )

        # Spooled in the handler (not inside the background work function) so the
        # directory outlives the request that scheduled it -- a
        # TemporaryDirectory context manager here would be cleaned up before the
        # background task ever reads it.
        spool_dir = Path(tempfile.mkdtemp(prefix="blastjob-ingest-"))
        scheduled = False
        try:
            for f, name in zip(files, names, strict=True):
                data = await f.read()
                (spool_dir / name).write_bytes(data)

            # Back up work_history.md synchronously, before scheduling the run: the
            # backup then exists even if the run is never scheduled or crashes
            # before it would otherwise get around to it, and a test can assert on
            # it without waiting on a background task.
            wh_path = cfg_mod.data_dir(cfg) / "work_history.md"
            if wh_path.exists():
                wh.backup_work_history(wh_path, cfg_mod.data_dir(cfg), label="ingest")

            run, scheduled = RUNTIME.start_run(
                "ingest", _make_ingest_work(spool_dir, deps.load_config)
            )
        finally:
            # The work function owns the directory once it is scheduled. Otherwise
            # (an error above, or a concurrent upload won the race and start handed
            # back its run) nothing will ever read or delete the resumes in it.
            if not scheduled:
                shutil.rmtree(spool_dir, ignore_errors=True)
        return deps.render(request, "_build_panel.html", run=run, heading="Ingesting")


def _make_ingest_work(spool_dir: Path, load_config: Callable[[], BlastJobConfig]):
    async def work(run: Run):
        from blastjob.core.ingest import run_ingestion
        from blastjob.llm.cost import CostTracker, describe_cost

        cfg = load_config()
        try:
            tracker = CostTracker()
            path = await run_ingestion(
                spool_dir, cfg, tracker, on_text=lambda t: run.emit("output", t)
            )
            run.emit(
                "log",
                f"Work history saved to {path}. Cost {describe_cost(tracker.total_call_cost)}.",
            )
            return {"path": str(path)}
        finally:
            shutil.rmtree(spool_dir, ignore_errors=True)

    return work


__all__ = ["register"]

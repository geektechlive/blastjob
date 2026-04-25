import subprocess

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Footer, Input, Label, TextArea

from blastjob.tui.widgets.coverage_panel import CoveragePanel
from blastjob.tui.widgets.nav_sidebar import NavSidebar
from blastjob.tui.widgets.stream_log import StreamLog


class BuildResumeScreen(Screen):
    DEFAULT_CSS = """
    #build-main {
        layout: grid;
        grid-size: 2;
        grid-columns: 3fr 2fr;
        height: 100%;
    }
    #build-left {
        padding: 2;
        border-right: solid $primary-darken-2;
        overflow-y: auto;
    }
    #build-right {
        padding: 1;
    }
    #build-left Label {
        margin-top: 1;
        color: $text-muted;
    }
    #build-left Input {
        margin-bottom: 1;
    }
    #jd-area {
        height: 12;
        margin-bottom: 1;
    }
    #build-error {
        color: $error;
    }
    #build-result {
        color: $success;
    }
    .format-row {
        layout: horizontal;
        height: auto;
        margin-bottom: 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield NavSidebar(active="build")
        with Horizontal(id="build-main"):
            with Vertical(id="build-left"):
                yield Label("[bold]Build Resume[/bold]", markup=True)
                yield Label("Job Description:")
                yield TextArea(id="jd-area")
                yield Label("Company name:")
                yield Input(placeholder="Acme Corp", id="inp-company")
                yield Checkbox(
                    "Company is confidential (skip research)", id="chk-confidential", value=False
                )
                yield Label("Role / Job title:")
                yield Input(placeholder="Senior Engineer", id="inp-role")
                yield Label("Output formats:")
                with Horizontal(classes="format-row"):
                    yield Checkbox("Markdown", id="fmt-md", value=True)
                    yield Checkbox("PDF", id="fmt-pdf", value=True)
                with Horizontal(classes="format-row"):
                    yield Checkbox("DOCX", id="fmt-docx", value=True)
                    yield Checkbox("ATS .txt", id="fmt-ats", value=True)
                yield Checkbox("ATS-optimized resume", id="use-ats", value=False)
                yield Checkbox("Include cover letter", id="chk-cover-letter", value=True)
                with Horizontal(classes="format-row"):
                    yield Button(
                        "Check coverage",
                        id="btn-coverage",
                        variant="default",
                        disabled=True,
                    )
                    yield Button("Generate", id="btn-generate", variant="success")
                yield Label("", id="build-error")
                yield Label("", id="build-result")
                yield Button(
                    "Open Output Folder",
                    id="btn-open-output",
                    variant="default",
                    disabled=True,
                )
                yield CoveragePanel(id="coverage-panel")
            with Vertical(id="build-right"):
                yield StreamLog(id="build-log", highlight=True, markup=True)
        yield Footer()

    def on_screen_resume(self) -> None:
        pending = getattr(self.app, "pending_build", None)
        if pending:
            self.app.pending_build = None
            self.query_one("#inp-company", Input).value = pending.get("company", "")
            self.query_one("#inp-role", Input).value = pending.get("role", "")
            self.query_one("#jd-area", TextArea).load_text(pending.get("jd", ""))
        from blastjob import config as cfg_mod

        data_path = cfg_mod.data_dir(self.app.config)  # type: ignore[attr-defined]
        if not (data_path / "work_history.md").exists():
            self.query_one("#build-error", Label).update("No work history yet. Go to Ingest first.")
            self.query_one("#btn-generate", Button).disabled = True
        else:
            self.query_one("#build-error", Label).update("")
            self.query_one("#btn-generate", Button).disabled = False

        has_jd = bool(self.query_one("#jd-area", TextArea).text.strip())
        self.query_one("#btn-coverage", Button).disabled = not has_jd

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id != "jd-area":
            return
        has_jd = bool(event.text_area.text.strip())
        self.query_one("#btn-coverage", Button).disabled = not has_jd

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-open-output":
            if hasattr(self, "_last_out_dir"):
                subprocess.run(["open", str(self._last_out_dir)], check=False)
            return
        if event.button.id == "btn-coverage":
            self._run_coverage()
            return
        if event.button.id != "btn-generate":
            return

        jd = self.query_one("#jd-area", TextArea).text.strip()
        company = self.query_one("#inp-company", Input).value.strip()
        role = self.query_one("#inp-role", Input).value.strip()

        if not jd:
            self.query_one("#build-error", Label).update("Paste a job description first.")
            return
        if not company:
            self.query_one("#build-error", Label).update("Enter a company name.")
            return
        if not role:
            self.query_one("#build-error", Label).update("Enter a role/job title.")
            return

        formats: set[str] = set()
        if self.query_one("#fmt-md", Checkbox).value:
            formats.add("md")
        if self.query_one("#fmt-pdf", Checkbox).value:
            formats.add("pdf")
        if self.query_one("#fmt-docx", Checkbox).value:
            formats.add("docx")
        if self.query_one("#fmt-ats", Checkbox).value:
            formats.add("ats")

        use_ats = self.query_one("#use-ats", Checkbox).value
        confidential = self.query_one("#chk-confidential", Checkbox).value
        include_cover_letter = self.query_one("#chk-cover-letter", Checkbox).value

        self.query_one("#build-error", Label).update("")
        self.query_one("#build-result", Label).update("")
        self.query_one("#btn-generate", Button).disabled = True
        self.query_one("#build-log", StreamLog).clear()
        self.run_worker(
            self._do_build(company, role, jd, formats, use_ats, confidential, include_cover_letter),
            exclusive=True,
        )

    async def _do_build(
        self,
        company: str,
        role: str,
        jd: str,
        formats: set,
        use_ats: bool,
        confidential: bool = False,
        include_cover_letter: bool = False,
    ) -> None:
        from blastjob.core.build import run_build

        log = self.query_one("#build-log", StreamLog)

        # Async worker runs on the event loop — direct widget calls, no call_from_thread
        def on_text(text: str) -> None:
            log.append_text(text)

        cfg = self.app.config  # type: ignore[attr-defined]
        tracker = self.app.cost_tracker  # type: ignore[attr-defined]
        try:
            out_dir, fit_score = await run_build(
                company,
                role,
                jd,
                formats,
                use_ats,
                cfg,
                tracker,
                on_text,
                confidential=confidential,
                include_cover_letter=include_cover_letter,
            )
            log.flush()
            self._last_out_dir = out_dir
            self.query_one("#btn-open-output", Button).disabled = False
            result = f"Saved to: {out_dir}"
            if fit_score is not None:
                result += (
                    f" · Fit: {fit_score.overall_score}/100"
                    f" · {fit_score.unsupported_count} unsupported"
                )
            else:
                result += " · Score unavailable"
            self.query_one("#build-result", Label).update(result)
            self.app.query_one("CostBar").update_cost(tracker.session_summary)
        except Exception as e:
            import blastjob.logging as applog

            applog.log_exception("build", e)
            self.query_one("#build-error", Label).update(f"Error: {e}  (see {applog.log_path()})")
        finally:
            self.query_one("#btn-generate", Button).disabled = False

    def _run_coverage(self) -> None:
        jd = self.query_one("#jd-area", TextArea).text.strip()
        if not jd:
            self.query_one("#build-error", Label).update("Paste a job description first.")
            return
        self.query_one("#build-error", Label).update("")
        self.query_one("#btn-coverage", Button).disabled = True
        self.query_one("#build-log", StreamLog).clear()
        self.query_one("#coverage-panel", CoveragePanel).clear_report()
        self.run_worker(self._do_coverage(jd), exclusive=True)

    async def _do_coverage(self, jd: str) -> None:
        from blastjob.core.coverage import analyze_coverage

        log = self.query_one("#build-log", StreamLog)
        panel = self.query_one("#coverage-panel", CoveragePanel)

        def on_text(text: str) -> None:
            log.append_text(text)

        cfg = self.app.config  # type: ignore[attr-defined]
        tracker = self.app.cost_tracker  # type: ignore[attr-defined]
        try:
            report = await analyze_coverage(jd, cfg, tracker, on_text)
            log.flush()
            panel.render_report(report)
            self.app.query_one("CostBar").update_cost(tracker.session_summary)
        except Exception as e:
            import blastjob.logging as applog

            applog.log_exception("coverage", e)
            self.query_one("#build-error", Label).update(
                f"Coverage check failed: {e}  (see {applog.log_path()})"
            )
        finally:
            self.query_one("#btn-coverage", Button).disabled = False

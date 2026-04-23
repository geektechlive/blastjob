import subprocess

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Footer, Input, Label, TextArea

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
                yield Button("Generate", id="btn-generate", variant="success")
                yield Label("", id="build-error")
                yield Label("", id="build-result")
                yield Button(
                    "Open Output Folder",
                    id="btn-open-output",
                    variant="default",
                    disabled=True,
                )
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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-open-output":
            if hasattr(self, "_last_out_dir"):
                subprocess.run(["open", str(self._last_out_dir)], check=False)
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

        self.query_one("#build-error", Label).update("")
        self.query_one("#build-result", Label).update("")
        self.query_one("#btn-generate", Button).disabled = True
        self.query_one("#build-log", StreamLog).clear()
        self.run_worker(
            self._do_build(company, role, jd, formats, use_ats, confidential),
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

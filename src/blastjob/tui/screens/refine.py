from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Label, TextArea

from blastjob.tui.widgets.nav_sidebar import NavSidebar
from blastjob.tui.widgets.stream_log import StreamLog


class RefineScreen(Screen):
    DEFAULT_CSS = """
    #refine-main {
        layout: grid;
        grid-size: 2;
        grid-columns: 3fr 2fr;
        height: 100%;
    }
    #refine-left {
        padding: 2;
        border-right: solid $primary-darken-2;
        overflow-y: auto;
    }
    #refine-right {
        padding: 1;
    }
    #refine-left Label {
        margin-top: 1;
        color: $text-muted;
    }
    #refine-header {
        color: $text;
        margin-bottom: 1;
    }
    #current-resume {
        height: 14;
        margin-bottom: 1;
    }
    #feedback-area {
        height: 6;
        margin-bottom: 1;
    }
    #refine-error {
        color: $error;
    }
    #refine-result {
        color: $success;
    }
    """

    def compose(self) -> ComposeResult:
        yield NavSidebar(active="history")
        with Horizontal(id="refine-main"):
            with Vertical(id="refine-left"):
                yield Label("[bold]Refine resume[/bold]", markup=True)
                yield Label("", id="refine-header", markup=True)
                yield Label("Current resume (read-only):")
                yield TextArea("", id="current-resume", read_only=True)
                yield Label("Feedback (what should change?):")
                yield TextArea(id="feedback-area")
                yield Button("Generate revision", id="btn-refine", variant="success")
                yield Label("", id="refine-error")
                yield Label("", id="refine-result")
            with Vertical(id="refine-right"):
                yield StreamLog(id="refine-log", highlight=True, markup=True)
        yield Footer()

    def on_screen_resume(self) -> None:
        pending = getattr(self.app, "pending_refine", None)
        if not pending:
            self.query_one("#refine-error", Label).update(
                "No run selected. Open Applications and click Refine on a row."
            )
            self.query_one("#btn-refine", Button).disabled = True
            return

        run_dir = Path(pending["run_dir"])
        self._run_dir = run_dir
        company = pending.get("company", "")
        role = pending.get("role", "")
        version_n = self._count_versions(run_dir)
        self.query_one("#refine-header", Label).update(
            f"[dim]{company} · {role} · current is v{version_n}[/dim]"
        )

        resume_path = run_dir / "resume.md"
        if not resume_path.exists():
            self.query_one("#refine-error", Label).update(f"resume.md not found in {run_dir}")
            self.query_one("#btn-refine", Button).disabled = True
            return
        self.query_one("#current-resume", TextArea).load_text(
            resume_path.read_text(encoding="utf-8")
        )
        self.query_one("#feedback-area", TextArea).load_text("")
        self.query_one("#refine-error", Label).update("")
        self.query_one("#refine-result", Label).update("")
        self.query_one("#btn-refine", Button).disabled = False
        self.app.pending_refine = None

    @staticmethod
    def _count_versions(run_dir: Path) -> int:
        if not run_dir.exists():
            return 1
        highest = 1
        for p in run_dir.iterdir():
            name = p.name
            if name.startswith("resume.v") and name.endswith(".md"):
                try:
                    n = int(name[len("resume.v") : -len(".md")])
                    highest = max(highest, n)
                except ValueError:
                    continue
        return highest + 1 if (run_dir / "resume.md").exists() else highest

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "btn-refine":
            return
        feedback = self.query_one("#feedback-area", TextArea).text.strip()
        if not feedback:
            self.query_one("#refine-error", Label).update("Enter feedback first.")
            return
        if not getattr(self, "_run_dir", None):
            self.query_one("#refine-error", Label).update("No run selected.")
            return

        self.query_one("#refine-error", Label).update("")
        self.query_one("#refine-result", Label).update("")
        self.query_one("#btn-refine", Button).disabled = True
        self.query_one("#refine-log", StreamLog).clear()
        self.run_worker(self._do_refine(self._run_dir, feedback), exclusive=True)

    async def _do_refine(self, run_dir: Path, feedback: str) -> None:
        from blastjob.core.refine import run_refine

        log = self.query_one("#refine-log", StreamLog)

        def on_text(text: str) -> None:
            log.append_text(text)

        cfg = self.app.config  # type: ignore[attr-defined]
        tracker = self.app.cost_tracker  # type: ignore[attr-defined]
        try:
            new_path, fit_score = await run_refine(run_dir, feedback, cfg, tracker, on_text)
            log.flush()
            result = f"Saved: {new_path.name}"
            if fit_score is not None:
                result += (
                    f" · Fit: {fit_score.overall_score}/100"
                    f" · {fit_score.unsupported_count} unsupported"
                )
            self.query_one("#refine-result", Label).update(result)
            # Refresh the current-resume view so a follow-up refinement starts from the new text
            self.query_one("#current-resume", TextArea).load_text(
                new_path.read_text(encoding="utf-8")
            )
            version_n = self._count_versions(run_dir)
            self.query_one("#refine-header", Label).update(f"[dim]current is v{version_n}[/dim]")
            self.app.query_one("CostBar").update_cost(tracker.session_summary)
        except Exception as e:
            import blastjob.logging as applog

            applog.log_exception("refine", e)
            self.query_one("#refine-error", Label).update(f"Error: {e}  (see {applog.log_path()})")
        finally:
            self.query_one("#btn-refine", Button).disabled = False

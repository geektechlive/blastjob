from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Input, Label

from blastjob.tui.widgets.nav_sidebar import NavSidebar
from blastjob.tui.widgets.stream_log import StreamLog


class IngestScreen(Screen):
    DEFAULT_CSS = """
    #ingest-main {
        layout: grid;
        grid-size: 2;
        grid-columns: 1fr 2fr;
        height: 100%;
    }
    #ingest-left {
        padding: 2;
        border-right: solid $primary-darken-2;
    }
    #ingest-right {
        padding: 1;
    }
    #ingest-left Label {
        margin-bottom: 1;
        color: $text-muted;
    }
    #ingest-left Input {
        margin-bottom: 1;
    }
    #ingest-status {
        margin-top: 1;
    }
    #ingest-error {
        color: $error;
    }
    """

    def compose(self) -> ComposeResult:
        yield NavSidebar()
        with Horizontal(id="ingest-main"):
            with Vertical(id="ingest-left"):
                yield Label("[bold]Ingest Work History[/bold]", markup=True)
                yield Label("Path to file or folder:")
                yield Input(placeholder="/path/to/resumes/", id="ingest-path")
                yield Button("Ingest", id="btn-ingest", variant="primary")
                yield Label("", id="ingest-status")
                yield Label("", id="ingest-error")
            with Vertical(id="ingest-right"):
                yield StreamLog(id="ingest-log", highlight=True, markup=True)
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "btn-ingest":
            return
        path_str = self.query_one("#ingest-path", Input).value.strip()
        if not path_str:
            self.query_one("#ingest-error", Label).update("Enter a path first.")
            return
        path = Path(path_str).expanduser()
        if not path.exists():
            self.query_one("#ingest-error", Label).update(f"Path not found: {path}")
            return
        self.query_one("#ingest-error", Label).update("")
        self.query_one("#btn-ingest", Button).disabled = True
        self.query_one("#ingest-log", StreamLog).clear()
        self.run_worker(self._do_ingest(path), exclusive=True)

    async def _do_ingest(self, path: Path) -> None:
        from blastjob.core.ingest import run_ingestion

        log = self.query_one("#ingest-log", StreamLog)

        # Async worker runs on the event loop — direct widget calls, no call_from_thread
        def on_text(text: str) -> None:
            log.append_text(text)

        cfg = self.app.config  # type: ignore[attr-defined]
        tracker = self.app.cost_tracker  # type: ignore[attr-defined]
        status = self.query_one("#ingest-status", Label)
        try:
            await run_ingestion(path, cfg, tracker, on_text)
            log.flush()
            self.app.query_one("CostBar").update_cost(tracker.session_summary)
            status.update("[bold green]Done — work history ready.[/bold green]")
        except Exception as e:
            import blastjob.logging as applog

            applog.log_exception("ingest", e)
            self.query_one("#ingest-error", Label).update(f"Error: {e}  (see {applog.log_path()})")
            status.update("")
        finally:
            btn = self.query_one("#btn-ingest", Button)
            btn.disabled = False
            btn.label = "Ingest Again"

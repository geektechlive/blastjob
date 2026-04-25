import subprocess
from datetime import date

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Input, Label, Select, TextArea

from blastjob import config as cfg_mod
from blastjob.core.history import scan_history
from blastjob.core.tracking import load_tracking, save_tracking
from blastjob.models.tracking import STATUSES, TrackingRecord
from blastjob.tui.widgets.nav_sidebar import NavSidebar


class HistoryScreen(Screen):
    DEFAULT_CSS = """
    #history-content {
        padding: 2;
        height: 100%;
    }
    DataTable {
        height: 1fr;
        margin-bottom: 1;
    }
    #history-detail {
        color: $text-muted;
        height: 1;
    }
    #edit-panel {
        height: auto;
        border-top: solid $primary-darken-3;
        padding-top: 1;
        margin-top: 1;
    }
    #edit-panel Label {
        color: $text-muted;
    }
    #edit-row {
        layout: horizontal;
        height: auto;
        margin-bottom: 1;
    }
    #edit-row > Select, #edit-row > Input {
        width: 1fr;
        margin-right: 1;
    }
    #notes-area {
        height: 4;
        margin-bottom: 1;
    }
    #action-row {
        layout: horizontal;
        height: auto;
    }
    #action-row > Button {
        margin-right: 1;
    }
    #edit-status {
        color: $success;
        height: 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield NavSidebar(active="history")
        with Vertical(id="history-content"):
            yield Label("[bold]Applications[/bold]", markup=True)
            table = DataTable(id="history-table", cursor_type="row")
            table.add_columns("Status", "Date", "Company", "Role", "Applied", "Next Action", "Cost")
            yield table
            yield Label("", id="history-detail")
            with Vertical(id="edit-panel"):
                yield Label("[bold]Edit application[/bold]", markup=True)
                with Horizontal(id="edit-row"):
                    yield Select(
                        [(s, s) for s in STATUSES],
                        value="drafted",
                        id="sel-status",
                        allow_blank=False,
                    )
                    yield Input(placeholder="Next action", id="inp-next-action")
                    yield Input(placeholder="Due (YYYY-MM-DD)", id="inp-next-due")
                yield TextArea("", id="notes-area")
                with Horizontal(id="action-row"):
                    yield Button("Save", id="btn-save", variant="primary")
                    yield Button("Open Folder", id="btn-open", variant="default")
                    yield Button("Rebuild", id="btn-rebuild", variant="default")
                yield Label("", id="edit-status")
        yield Footer()

    def on_screen_resume(self) -> None:
        self._load_history()

    def on_mount(self) -> None:
        self._load_history()

    def _load_history(self) -> None:
        cfg = self.app.config  # type: ignore[attr-defined]
        out_root = cfg_mod.output_dir(cfg)
        self._entries = scan_history(out_root)
        table = self.query_one("#history-table", DataTable)
        table.clear()
        for entry in self._entries:
            table.add_row(
                entry.status,
                entry.date,
                entry.company[:20],
                entry.role[:25],
                entry.applied_at or "",
                entry.next_action[:30],
                f"${entry.cost_usd:.4f}",
            )
        self._clear_edit_panel()

    def _clear_edit_panel(self) -> None:
        self._selected_entry = None
        self._selected_path = None
        self.query_one("#sel-status", Select).value = "drafted"
        self.query_one("#inp-next-action", Input).value = ""
        self.query_one("#inp-next-due", Input).value = ""
        self.query_one("#notes-area", TextArea).load_text("")
        self.query_one("#edit-status", Label).update("")
        self.query_one("#history-detail", Label).update("")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if idx >= len(self._entries):
            return
        entry = self._entries[idx]
        self._selected_entry = entry
        self._selected_path = entry.path
        self.query_one("#history-detail", Label).update(f"[dim]{entry.path}[/dim]")
        self.query_one("#sel-status", Select).value = entry.status
        self.query_one("#inp-next-action", Input).value = entry.next_action
        self.query_one("#inp-next-due", Input).value = entry.next_action_due or ""
        self.query_one("#notes-area", TextArea).load_text(entry.notes)
        self.query_one("#edit-status", Label).update("")
        event.stop()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-open":
            if self._selected_path is None:
                self._set_status("[dim]Select a row first.[/dim]")
                return
            subprocess.run(["open", str(self._selected_path)], check=False)
        elif event.button.id == "btn-rebuild":
            self._rebuild_selected()
        elif event.button.id == "btn-save":
            self._save_selected()

    def _set_status(self, msg: str) -> None:
        self.query_one("#edit-status", Label).update(msg)

    def _save_selected(self) -> None:
        entry = self._selected_entry
        if entry is None:
            self._set_status("[dim]Select a row first.[/dim]")
            return
        status = str(self.query_one("#sel-status", Select).value)
        next_action = self.query_one("#inp-next-action", Input).value.strip()
        next_due = self.query_one("#inp-next-due", Input).value.strip() or None
        notes = self.query_one("#notes-area", TextArea).text

        existing = load_tracking(entry.path)
        applied_at = existing.applied_at
        if status == "applied" and not applied_at:
            applied_at = date.today().isoformat()

        record = TrackingRecord(
            status=status,
            applied_at=applied_at,
            next_action=next_action,
            next_action_due=next_due,
            notes=notes,
        )
        try:
            save_tracking(entry.path, record)
        except Exception as e:
            self._set_status(f"[red]Save failed: {e}[/red]")
            return

        self._set_status("[green]Saved.[/green]")
        self._load_history()

    def _rebuild_selected(self) -> None:
        entry = self._selected_entry
        if entry is None:
            self._set_status("[dim]Select a row first.[/dim]")
            return
        jd_file = entry.path / "job_description.md"
        if not jd_file.exists():
            self._set_status("[dim]Job description not found — cannot rebuild.[/dim]")
            return
        content = jd_file.read_text(encoding="utf-8")
        parts = content.split("\n\n", 2)
        jd = parts[2] if len(parts) >= 3 else content
        self.app.pending_build = {"company": entry.company, "role": entry.role, "jd": jd}
        self.app.switch_screen("build")

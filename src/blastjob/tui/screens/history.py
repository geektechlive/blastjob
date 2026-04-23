import subprocess

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Label

from blastjob import config as cfg_mod
from blastjob.core.history import scan_history
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
        height: 3;
    }
    #btn-open {
        width: 20;
    }
    """

    def compose(self) -> ComposeResult:
        yield NavSidebar()
        with Vertical(id="history-content"):
            yield Label("[bold]Resume History[/bold]", markup=True)
            table = DataTable(id="history-table")
            table.add_columns("Date", "Company", "Role", "Cost", "Tokens", "Cache%", "ATS")
            yield table
            yield Label("", id="history-detail")
            yield Button("Open Folder", id="btn-open", variant="default")
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
                entry.date,
                entry.company[:20],
                entry.role[:25],
                f"${entry.cost_usd:.4f}",
                f"{entry.total_tokens:,}",
                f"{entry.cache_hit_ratio:.0%}",
                "Y" if entry.ats_mode else "N",
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if idx < len(self._entries):
            entry = self._entries[idx]
            self.query_one("#history-detail", Label).update(f"[dim]{entry.path}[/dim]")
            self._selected_path = entry.path
        event.stop()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-open":
            path = getattr(self, "_selected_path", None)
            if path:
                subprocess.run(["open", str(path)], check=False)

from __future__ import annotations

import subprocess

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Label, TabbedContent, TabPane, TextArea

from blastjob import config as cfg_mod
from blastjob.tui.widgets.nav_sidebar import NavSidebar

_FILE_MAP = {
    "tab-wh": ("work_history.md", "#ta-work-history"),
    "tab-standard": ("templates/standard.md", "#ta-standard"),
    "tab-ats": ("templates/ats.md", "#ta-ats"),
}


class WorkHistoryScreen(Screen):
    DEFAULT_CSS = """
    WorkHistoryScreen #wh-content {
        width: 1fr;
        height: 100%;
        padding: 1 2;
    }
    WorkHistoryScreen #wh-actions {
        height: auto;
        padding: 1 0 0 0;
        layout: horizontal;
    }
    WorkHistoryScreen #wh-msg {
        padding: 0 2;
        color: $success;
    }
    WorkHistoryScreen TabbedContent {
        height: 1fr;
    }
    WorkHistoryScreen TabPane {
        height: 100%;
        padding: 0;
    }
    WorkHistoryScreen TextArea {
        height: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        yield NavSidebar(active="work-history")
        with Vertical(id="wh-content"):
            yield Label("[bold]Work History[/bold]", markup=True)
            with TabbedContent(id="wh-tabs"):
                with TabPane("Work History", id="tab-wh"):
                    yield TextArea("", id="ta-work-history", language="markdown")
                with TabPane("Standard Template", id="tab-standard"):
                    yield TextArea("", id="ta-standard", language="markdown")
                with TabPane("ATS Template", id="tab-ats"):
                    yield TextArea("", id="ta-ats", language="markdown")
            with Horizontal(id="wh-actions"):
                yield Button("Save", id="btn-wh-save", variant="primary")
                yield Button("Open Data Folder", id="btn-wh-open", variant="default")
                yield Label("", id="wh-msg")
        yield Footer()

    def on_mount(self) -> None:
        self._load_files()

    def on_screen_resume(self) -> None:
        self._load_files()

    def _load_files(self) -> None:
        cfg = self.app.config  # type: ignore[attr-defined]
        data_path = cfg_mod.data_dir(cfg)

        for _tab_id, (rel_path, widget_id) in _FILE_MAP.items():
            file_path = data_path / rel_path
            content = ""
            if file_path.exists():
                try:
                    content = file_path.read_text(encoding="utf-8")
                except Exception:
                    content = ""
            self.query_one(widget_id, TextArea).load_text(content)

    def _save_active_tab(self) -> None:
        cfg = self.app.config  # type: ignore[attr-defined]
        data_path = cfg_mod.data_dir(cfg)
        tabs = self.query_one("#wh-tabs", TabbedContent)
        active_tab = tabs.active

        if active_tab not in _FILE_MAP:
            return

        rel_path, widget_id = _FILE_MAP[active_tab]
        file_path = data_path / rel_path
        content = self.query_one(widget_id, TextArea).text

        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = file_path.with_suffix(file_path.suffix + ".tmp")
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(file_path)
            self.query_one("#wh-msg", Label).update("Saved.")
        except Exception as e:
            self.query_one("#wh-msg", Label).update(f"[red]Error: {e}[/red]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-wh-save":
            self._save_active_tab()
        elif event.button.id == "btn-wh-open":
            cfg = self.app.config  # type: ignore[attr-defined]
            data_path = cfg_mod.data_dir(cfg)
            subprocess.run(["open", str(data_path)], check=False)

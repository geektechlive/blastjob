import json

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Label

from blastjob import config as cfg_mod
from blastjob.tui.widgets.nav_sidebar import NavSidebar


class HomeScreen(Screen):
    DEFAULT_CSS = """
    #home-content {
        width: 1fr;
        align: center middle;
        padding: 4;
        height: 100%;
    }
    #home-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #home-status {
        margin-bottom: 1;
    }
    #home-provider {
        margin-bottom: 3;
    }
    HomeScreen Button {
        width: 30;
        margin-bottom: 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield NavSidebar(active="home")
        with Vertical(id="home-content"):
            yield Label("[bold]blastjob[/bold]", markup=True, id="home-title")
            yield Label(self._work_history_status(), id="home-status", markup=True)
            yield Label(self._provider_status(), id="home-provider", markup=True)
            yield Button("Ingest Work History", id="btn-ingest", variant="primary")
            yield Button("Build Resume", id="btn-build", variant="success")
            yield Button("Applications", id="btn-history", variant="default")
            yield Button("Settings", id="btn-settings", variant="default")
        yield Footer()

    def _work_history_status(self) -> str:
        cfg = self.app.config  # type: ignore[attr-defined]
        data_path = cfg_mod.data_dir(cfg)
        wh = data_path / "work_history.md"
        if not wh.exists():
            return "[dim]No work history yet — start with Ingest[/dim]"
        meta_path = data_path / "metadata.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                words = meta.get("word_count", "?")
                ingested = meta.get("last_ingested", "")[:10]
                return f"[dim]Work history: {words} words | Last ingested: {ingested}[/dim]"
            except Exception:
                pass
        return "[dim]Work history ready[/dim]"

    def _provider_status(self) -> str:
        from blastjob.llm.providers import ProviderNotConfiguredError, active_model

        cfg = self.app.config  # type: ignore[attr-defined]
        try:
            model_str = active_model(cfg)
            return f"[dim]Provider: {model_str}[/dim]"
        except ProviderNotConfiguredError:
            return (
                "[bold red]No AI provider found.[/bold red]\n"
                "Set ANTHROPIC_API_KEY or OPENAI_API_KEY in your environment,\n"
                "or install Claude Code (claude.ai/code) for automatic access.\n"
                "Go to [bold]Settings[/bold] to configure manually."
            )

    def on_screen_resume(self) -> None:
        self.query_one("#home-status", Label).update(self._work_history_status())
        self.query_one("#home-provider", Label).update(self._provider_status())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        routes = {
            "btn-ingest": "ingest",
            "btn-build": "build",
            "btn-history": "history",
            "btn-settings": "settings",
        }
        target = routes.get(event.button.id or "")
        if target:
            self.app.switch_screen(target)

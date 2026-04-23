from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Input, Label, Select

from blastjob import config as cfg_mod
from blastjob.tui.widgets.nav_sidebar import NavSidebar

_PROVIDER_OPTIONS = [
    ("Auto-detect (recommended)", "auto"),
    ("Anthropic (Claude)", "anthropic"),
    ("OpenAI (GPT)", "openai"),
    ("Claude Code CLI", "claude-cli"),
]

_PROVIDER_HELP = {
    "auto": "Checks ANTHROPIC_API_KEY → OPENAI_API_KEY → claude CLI in that order.",
    "anthropic": "Requires ANTHROPIC_API_KEY env var. Get a key at console.anthropic.com",
    "openai": "Requires OPENAI_API_KEY env var. Get a key at platform.openai.com",
    "claude-cli": (
        "Uses the `claude` CLI binary (Claude Code). "
        "No API key needed — uses your Claude Code session."
    ),
}


class SettingsScreen(Screen):
    DEFAULT_CSS = """
    #settings-content {
        padding: 2 4;
        height: 100%;
        overflow-y: auto;
    }
    #settings-content Label {
        margin-top: 1;
        color: $text-muted;
    }
    #settings-content Input {
        margin-bottom: 1;
    }
    #settings-save {
        margin-top: 2;
        width: 20;
    }
    #settings-msg {
        color: $success;
    }
    #provider-help {
        color: $warning;
        margin-bottom: 1;
    }
    """

    def compose(self) -> ComposeResult:
        cfg = self.app.config  # type: ignore[attr-defined]
        yield NavSidebar()
        with Vertical(id="settings-content"):
            yield Label("[bold]Settings[/bold]", markup=True)

            yield Label("[bold]AI Provider[/bold]", markup=True)
            yield Label(
                "blastjob works with Anthropic (Claude), OpenAI (GPT), or the Claude Code CLI.\n"
                "Set 'Auto-detect' to pick whichever key is in your environment.",
                markup=True,
            )
            yield Select(
                [(label, val) for label, val in _PROVIDER_OPTIONS],
                value=cfg.llm.provider,
                id="sel-provider",
            )
            yield Label("", id="provider-help")

            yield Label(
                "Anthropic model (used when provider = anthropic or auto with Anthropic key)"
            )
            yield Input(value=cfg.llm.anthropic_model, id="inp-anthropic-model")
            yield Label("ANTHROPIC_API_KEY env var name")
            yield Input(value=cfg.llm.anthropic_api_key_env, id="inp-anthropic-env")

            yield Label("OpenAI model (used when provider = openai or auto with OpenAI key)")
            yield Input(value=cfg.llm.openai_model, id="inp-openai-model")
            yield Label("OPENAI_API_KEY env var name")
            yield Input(value=cfg.llm.openai_api_key_env, id="inp-openai-env")

            yield Label("[bold]Paths[/bold]", markup=True)
            yield Label("Data directory (work_history.md and templates live here)")
            yield Input(value=cfg.paths.data_dir, id="inp-data-dir")
            yield Label("Output directory (generated resumes go here)")
            yield Input(value=cfg.paths.output_dir, id="inp-output-dir")

            yield Button("Save", id="settings-save", variant="primary")
            yield Label("", id="settings-msg")
        yield Footer()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "sel-provider":
            val = str(event.value)
            help_text = _PROVIDER_HELP.get(val, "")
            self.query_one("#provider-help", Label).update(help_text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "settings-save":
            return
        cfg = self.app.config  # type: ignore[attr-defined]
        sel = self.query_one("#sel-provider", Select)
        cfg.llm.provider = str(sel.value) if sel.value else "auto"
        cfg.llm.anthropic_model = self.query_one("#inp-anthropic-model", Input).value
        cfg.llm.anthropic_api_key_env = self.query_one("#inp-anthropic-env", Input).value
        cfg.llm.openai_model = self.query_one("#inp-openai-model", Input).value
        cfg.llm.openai_api_key_env = self.query_one("#inp-openai-env", Input).value
        cfg.paths.data_dir = self.query_one("#inp-data-dir", Input).value
        cfg.paths.output_dir = self.query_one("#inp-output-dir", Input).value
        cfg_mod.save_config(cfg)
        self.query_one("#settings-msg", Label).update("Saved.")
        # Refresh status bar
        from blastjob.llm.providers import active_model

        self.app.query_one("CostBar").set_provider(active_model(cfg))

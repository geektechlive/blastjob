from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Label


class CostBar(Widget):
    """Bottom status bar showing active provider and model."""

    DEFAULT_CSS = """
    CostBar {
        dock: bottom;
        height: 1;
        background: $surface;
        padding: 0 2;
    }
    CostBar Label {
        color: $text-muted;
    }
    """

    status: reactive[str] = reactive("initializing...")

    def compose(self) -> ComposeResult:
        yield Label("", id="status-label")

    def watch_status(self, value: str) -> None:
        try:
            self.query_one("#status-label", Label).update(value)
        except Exception:
            pass

    def set_provider(self, provider_str: str) -> None:
        self.status = f"[dim]provider: {provider_str}[/dim]"

    def set_message(self, msg: str) -> None:
        self.status = f"[dim]{msg}[/dim]"

    def update_cost(self, summary: str) -> None:
        # Called from screens after a build/ingest completes — show cost briefly
        try:
            cost = float(summary.split("$")[1].split()[0])
            if cost >= 5.0:
                formatted = f"[bold red]{summary}[/bold red]"
            elif cost >= 1.0:
                formatted = f"[yellow]{summary}[/yellow]"
            else:
                formatted = f"[dim]{summary}[/dim]"
        except Exception:
            formatted = f"[dim]{summary}[/dim]"
        self.status = formatted

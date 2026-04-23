from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Button, Label


class NavSidebar(Widget):
    DEFAULT_CSS = """
NavSidebar {
    width: 24;
    height: 100%;
    background: $panel;
    border-right: solid $primary-darken-3;
    padding: 1 0;
    layout: vertical;
}
NavSidebar > Label {
    text-style: bold;
    color: $accent;
    padding: 0 2 0 2;
    margin-bottom: 1;
    width: 100%;
}
NavSidebar > Button {
    width: 100%;
    margin: 0;
    background: transparent;
    border-left: solid $panel;
    text-align: left;
    padding: 0 2;
    color: $text-muted;
    height: 3;
}
NavSidebar > Button:hover {
    background: $boost;
    color: $text;
    border-left: solid $accent;
}
NavSidebar > Button:focus {
    background: $primary-darken-2;
    color: $text;
    border-left: solid $primary;
}
"""

    def compose(self) -> ComposeResult:
        yield Label("blastjob")
        yield Button("Home", id="nav-home", variant="default")
        yield Button("Ingest", id="nav-ingest", variant="default")
        yield Button("Build Resume", id="nav-build", variant="default")
        yield Button("History", id="nav-history", variant="default")
        yield Button("Settings", id="nav-settings", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        nav_map = {
            "nav-home": "home",
            "nav-ingest": "ingest",
            "nav-build": "build",
            "nav-history": "history",
            "nav-settings": "settings",
        }
        screen = nav_map.get(event.button.id or "")
        if screen:
            self.app.switch_screen(screen)

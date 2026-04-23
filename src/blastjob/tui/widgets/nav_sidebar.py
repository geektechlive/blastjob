from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Button, Label

_NAV = [
    ("nav-home", "Home", "home"),
    ("nav-ingest", "Ingest", "ingest"),
    ("nav-work-history", "Work History", "work-history"),
    ("nav-build", "Build Resume", "build"),
    ("nav-history", "History", "history"),
    ("nav-settings", "Settings", "settings"),
]


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
NavSidebar > Button.active {
    background: $primary-darken-2;
    color: $text;
    border-left: solid $primary;
}
"""

    def __init__(self, active: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self._active = active

    def compose(self) -> ComposeResult:
        yield Label("blastjob")
        for btn_id, label, screen_name in _NAV:
            classes = "active" if screen_name == self._active else ""
            yield Button(label, id=btn_id, variant="default", classes=classes)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        nav_map = {btn_id: screen_name for btn_id, _label, screen_name in _NAV}
        screen = nav_map.get(event.button.id or "")
        if screen:
            self.app.switch_screen(screen)

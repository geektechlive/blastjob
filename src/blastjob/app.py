from textual.app import App, ComposeResult

import blastjob.logging as applog
from blastjob.config import load_config
from blastjob.llm.cost import CostTracker
from blastjob.llm.providers import active_model
from blastjob.tui.screens.build import BuildResumeScreen
from blastjob.tui.screens.history import HistoryScreen
from blastjob.tui.screens.home import HomeScreen
from blastjob.tui.screens.ingest import IngestScreen
from blastjob.tui.screens.settings import SettingsScreen
from blastjob.tui.widgets.cost_bar import CostBar


class BlastJobApp(App):
    CSS_PATH = "tui/styles.tcss"
    TITLE = "blastjob"

    BINDINGS = [
        ("i", "switch_screen('ingest')", "Ingest"),
        ("b", "switch_screen('build')", "Build"),
        ("h", "switch_screen('history')", "History"),
        ("s", "switch_screen('settings')", "Settings"),
        ("q", "quit", "Quit"),
    ]

    SCREENS = {
        "home": HomeScreen,
        "ingest": IngestScreen,
        "build": BuildResumeScreen,
        "history": HistoryScreen,
        "settings": SettingsScreen,
    }

    def __init__(self) -> None:
        super().__init__()
        self.config = load_config()
        self.cost_tracker = CostTracker()

    def compose(self) -> ComposeResult:
        yield CostBar()

    def on_mount(self) -> None:
        self.push_screen("home")
        try:
            self.query_one(CostBar).set_provider(active_model(self.config))
        except Exception:
            self.query_one(CostBar).set_message("no provider — see Settings")


def main() -> None:
    applog.setup()
    BlastJobApp().run()

from textual.widgets import RichLog


class StreamLog(RichLog):
    DEFAULT_CSS = """
    StreamLog {
        height: 1fr;
        border: solid $primary-darken-2;
        overflow-y: scroll;
        padding: 1;
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._line_buffer = ""

    def append_text(self, text: str) -> None:
        """Buffer streaming text and flush complete lines to RichLog."""
        self._line_buffer += text
        while "\n" in self._line_buffer:
            line, self._line_buffer = self._line_buffer.split("\n", 1)
            self.write(line)

    def flush(self) -> None:
        """Write any remaining buffered text that has no trailing newline."""
        if self._line_buffer:
            self.write(self._line_buffer)
            self._line_buffer = ""

    def clear(self) -> None:
        self._line_buffer = ""
        super().clear()

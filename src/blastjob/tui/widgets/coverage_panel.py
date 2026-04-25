"""Renders a CoverageReport as a scrollable widget below the build form."""

from textual.widget import Widget
from textual.widgets import Static

from blastjob.models.coverage import CoverageReport


class CoveragePanel(Widget):
    DEFAULT_CSS = """
    CoveragePanel {
        height: auto;
        border: solid $primary-darken-3;
        padding: 1;
        margin-top: 1;
    }
    CoveragePanel > Static {
        height: auto;
    }
    """

    def render_report(self, report: CoverageReport) -> None:
        self.remove_children()
        score = report.coverage_score
        color = "green" if score >= 70 else "yellow" if score >= 50 else "red"
        header = (
            f"[b]Coverage:[/b] [{color}]{score}/100[/{color}]"
            f"  ·  Must-haves: {report.must_have_coverage_pct}%"
            f"  ·  Gaps: {report.gap_count}"
        )
        self.mount(Static(header, markup=True))
        if report.summary:
            self.mount(Static(f"[dim]{report.summary}[/dim]", markup=True))

        if not report.requirements:
            self.mount(Static("[dim]No requirements extracted.[/dim]", markup=True))
            return

        for req in report.requirements:
            mark = "[green]✓[/green]" if req.covered else "[red]✗[/red]"
            tag = "[b]MUST[/b]" if req.priority == "must" else "[dim]nice[/dim]"
            line = f"{mark} {tag}  {req.text}"
            self.mount(Static(line, markup=True))
            if req.covered and req.evidence_quote:
                quote = req.evidence_quote.strip().replace("\n", " ")
                if len(quote) > 120:
                    quote = quote[:117] + "…"
                self.mount(Static(f'    [dim]› "{quote}"[/dim]', markup=True))
            elif req.gap_note:
                self.mount(Static(f"    [yellow]› {req.gap_note}[/yellow]", markup=True))

    def clear_report(self) -> None:
        self.remove_children()

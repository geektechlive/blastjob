"""Headless TUI smoke — mounts the app, switches through every screen,
clicks the application edit Save button on a synthetic run, exits.

Catches crash-on-render bugs and basic event wiring without an API key.

Run: .venv/bin/python scripts/smoke_tui.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# ruff: noqa: E402
from blastjob.app import BlastJobApp


def _seed_data(root: Path) -> tuple[Path, Path]:
    data = root / "data"
    data.mkdir()
    (data / "work_history.md").write_text("## Acme — Engineer\n\n- Did things\n")
    (data / "templates").mkdir()
    (data / "templates" / "standard.md").write_text("# Resume")
    (data / "metadata.json").write_text(
        json.dumps({"word_count": 42, "last_ingested": "2026-04-20T10:00:00"})
    )

    out = root / "out"
    run = out / "2026-04-20" / "acme" / "engineer"
    run.mkdir(parents=True)
    (run / "metadata.json").write_text(
        json.dumps(
            {
                "timestamp": "2026-04-20T10:00:00",
                "company": "Acme",
                "role": "Engineer",
                "cost_usd": 0.05,
                "input_tokens": 1000,
                "output_tokens": 500,
                "cache_hit_ratio": 0.5,
                "formats": ["md"],
                "ats_mode": False,
            }
        )
    )
    (run / "job_description.md").write_text("# JD\n\n**Company:** Acme\n\nPaste\n")
    (run / "resume.md").write_text("# Old resume\n\n- bullet\n", encoding="utf-8")
    return data, out


async def _run() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        data, out = _seed_data(root)

        # Point config at our temp dirs via env, then load_config picks them up
        os.environ["BLASTJOB_CONFIG"] = str(root / "config.toml")
        (root / "config.toml").write_text(
            f'[paths]\ndata_dir = "{data}"\noutput_dir = "{out}"\n'
        )

        app = BlastJobApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Walk every screen, including the new refine screen
            screens = [
                "ingest",
                "work-history",
                "build",
                "history",
                "refine",
                "settings",
                "home",
            ]
            for screen_name in screens:
                app.switch_screen(screen_name)
                await pilot.pause()
                await pilot.pause()

            # On Applications screen — try selecting the row + saving tracking
            app.switch_screen("history")
            await pilot.pause()

            from blastjob.tui.screens.history import HistoryScreen

            hist = app.screen
            assert isinstance(hist, HistoryScreen), f"expected HistoryScreen, got {type(hist)}"
            # Force a row selection programmatically (no actual mouse click in pilot)
            if hist._entries:
                from textual.widgets import DataTable

                table = hist.query_one("#history-table", DataTable)
                table.move_cursor(row=0)
                # Synthesize the row-selected handler call
                hist._selected_entry = hist._entries[0]
                hist._selected_path = hist._entries[0].path
                from textual.widgets import Input, Select, TextArea

                hist.query_one("#sel-status", Select).value = "applied"
                hist.query_one("#inp-next-action", Input).value = "Follow up"
                hist.query_one("#notes-area", TextArea).load_text("Smoke test note")
                hist._save_selected()
                await pilot.pause()

                tracking_file = hist._entries[0].path / "tracking.json"
                if not tracking_file.exists():
                    print(f"FAIL: tracking.json not written at {tracking_file}")
                    return 1
                data = json.loads(tracking_file.read_text())
                if data.get("status") != "applied":
                    print(f"FAIL: status not applied, got {data.get('status')}")
                    return 1
                print(f"  saved tracking.json with status={data['status']}, "
                      f"applied_at={data.get('applied_at')}")

            # Build screen — confirm cover letter and coverage controls are present
            app.switch_screen("build")
            await pilot.pause()
            from textual.widgets import Button, Checkbox

            build_screen = app.screen
            cb = build_screen.query_one("#chk-cover-letter", Checkbox)
            coverage_btn = build_screen.query_one("#btn-coverage", Button)
            print(f"  cover-letter checkbox: default={cb.value}")
            print(f"  coverage button: present, disabled={coverage_btn.disabled}")
            if not coverage_btn.disabled:
                print("FAIL: coverage button should be disabled when JD is empty")
                return 1
            # Type into JD area, confirm coverage button enables
            jd_area = build_screen.query_one("#jd-area")
            jd_area.load_text("Need a senior engineer.")
            await pilot.pause()
            await pilot.pause()
            if coverage_btn.disabled:
                print("FAIL: coverage button still disabled after JD entered")
                return 1
            print("  coverage button enables when JD has content")

            # Refine screen — open via the Applications row + Refine button path
            run_dir = out / "2026-04-20" / "acme" / "engineer"
            app.pending_refine = {
                "run_dir": str(run_dir),
                "company": "Acme",
                "role": "Engineer",
            }
            app.switch_screen("refine")
            await pilot.pause()
            from blastjob.tui.screens.refine import RefineScreen
            from textual.widgets import TextArea

            refine_screen = app.screen
            assert isinstance(refine_screen, RefineScreen)
            current = refine_screen.query_one("#current-resume", TextArea)
            if "Old resume" not in current.text:
                print(f"FAIL: refine screen didn't load current resume, got: {current.text!r}")
                return 1
            print("  refine screen loaded current resume into read-only view")

        print("PASS — TUI mounted, all screens navigated, tracking saved, "
              "coverage gates on JD, refine loads selected run")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))

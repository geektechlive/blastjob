# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> See your global CLAUDE.md for cross-project conventions

# blastjob

AI-powered resume management and generation TUI.

## Stack

- Python 3.11-3.13 (not 3.14 — pymupdf/weasyprint wheel gaps)
- Textual for TUI
- Anthropic SDK (claude-sonnet-4-6) with prompt caching and web_search_20250305
- WeasyPrint for PDF output, python-docx for DOCX
- pymupdf for PDF ingestion, python-docx for DOCX ingestion
- Pydantic v2 for all Claude structured outputs
- Config: stdlib tomllib + tomli-w, platformdirs

## Key directories

- `src/blastjob/tui/` — Textual screens and widgets
- `src/blastjob/llm/` — Claude client, cost tracking, prompt caching utilities
- `src/blastjob/core/` — ingest, build, history, paths, scoring, refine, coverage, tracking pipelines
- `src/blastjob/importers/` — PDF, DOCX, text, folder ingestion
- `src/blastjob/exporters/` — MD, PDF, DOCX, ATS text output
- `src/blastjob/models/` — Pydantic models for config, history, fit_score, tracking, coverage

## Data store

Config: `~/.config/blastjob/config.toml`
Data: `~/.blastjob/` (configurable) — work_history.md, templates/standard.md, templates/ats.md
Output: `~/Documents/blastjob/` (configurable) — `{date}/{company}/{role}/` per resume run

## Commands

```bash
# Install
pip install -e ".[dev]"

# Run
python -m blastjob
# or: blastjob

# Test
pytest tests/

# Single test file or function
pytest tests/test_cost.py
pytest tests/test_paths.py::test_slugify_special_chars

# Lint
ruff check src/ tests/
ruff format src/ tests/

# Coverage (TUI excluded via pyproject.toml omit config — test non-TUI code only)
pytest --cov=src/blastjob --cov-report=term-missing tests/
```

Ruff enforces rules E, F, I (isort), UP (pyupgrade) at line length 100.

## Architecture

### App-level shared state

`BlastJobApp` (`app.py`) holds two instance attributes accessed by all screens:
- `app.config` — `BlastJobConfig` (mutable; SettingsScreen writes back via `save_config()`)
- `app.cost_tracker` — `CostTracker` (accumulates `CallCost` records across the session)

There is no message bus. Screens read these directly off `self.app`.

### Screen navigation

All screens are registered in the `SCREENS` dict on `BlastJobApp`. Navigation uses `app.switch_screen(name)`. Every screen composes a `NavSidebar()` as its first child — Textual renders it on the left because the screen layout is `horizontal`.

### Async work and UI updates

Long-running Claude calls run via `self.run_worker(coroutine, exclusive=True)`. Because Textual workers execute in a thread pool, any UI mutation from inside a worker must go through `self.app.call_from_thread(widget_method, args)`. The `on_text` streaming callback passed into `run_ingestion`/`run_build` wraps this pattern to push chunks to `StreamLog`.

### Ingest pipeline (`core/ingest.py`)

1. `labeled_chunks()` — reads source files, wraps each in `<file path="...">` XML tags
2. Streaming Claude call with `INGEST_SYSTEM` — Claude returns a JSON blob matching `MasterHistory`
3. `_parse_history()` — regex extracts JSON, Pydantic validates into `MasterHistory`
4. `_serialize_history()` — converts `MasterHistory` → canonical markdown (YAML frontmatter + `##` sections) → writes `work_history.md`
5. Second Claude call with `TEMPLATE_SYSTEM` — generates standard and ATS templates in one response, split on an `===ATS===` delimiter → writes `templates/standard.md` and `templates/ats.md`

### Build pipeline (`core/build.py`)

1. Reads `work_history.md` and selected template from data dir
2. `_research_company()` — non-streaming call using `web_search_20250305` tool, returns raw markdown
3. `build_resume_messages()` — assembles 3-breakpoint cached message list (see Prompt caching rules)
4. Streaming Claude call with `RESUME_SYSTEM` or `RESUME_ATS_SYSTEM` — streams resume markdown chunks
5. Exports to all selected formats via `exporters/`
6. Writes sidecars: `job_description.md`, `company_research.md`, `metadata.json`

Output folder: `{output_root}/{date-iso}/{company-slug}/{role-slug}/` with numeric suffix on collision.

### Prompt caching rules

Three cache breakpoints per resume generation call:
1. System prompt (constant)
2. work_history.md (changes only after ingestion)
3. Template (standard.md or ats.md)

NEVER interpolate per-call data into cached blocks — kills cache hit rate. Per-call data (job description, company research) goes in a trailing `plain_text()` block.

### LLM client

`get_client()` (`llm/client.py`) returns an `AsyncAnthropic` singleton via `lru_cache`, keyed on the env var name. Always async. Resume generation uses `claude.messages.stream()`; text chunks iterate via `async for text in stream.text_stream`. Company research uses `messages.create()` (non-streaming).

## Running

```bash
cd /path/to/blastjob
pip install -e ".[dev]"
export ANTHROPIC_API_KEY=...
python -m blastjob
# or: blastjob
```

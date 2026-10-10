# blastjob

A local, private job-search cockpit. It **finds** roles, **scores** them against
your real work history, lets you **review** them in a browser, and **tailors** a
resume, cover letter and interview prep pack for the ones you keep.

- **Draft-only.** It never applies, sends, posts or messages anyone on your
  behalf. You decide and you send.
- **Never invents facts.** Every resume claim must quote your own work history;
  unsupported claims are flagged before you see the packet. The check is
  model-assisted, so read every packet before you send it.
- **Local-only.** It runs on your machine, binds to `127.0.0.1` by default, and
  keeps your data out of this repo.

> **Start with [SETUP.md](SETUP.md).** The app ships empty: no candidate profile,
> no location, no target companies. SETUP.md is written so an AI assistant can run
> the setup interview with you and write your answers into your settings. Matching
> makes no LLM calls until your target titles are set.

---

## What it does

- **Ingest.** Upload your resumes (PDF, DOCX, text). They are merged into one
  deduplicated `work_history.md`, the source of truth for everything after. You
  review and correct it by hand.
- **Discover.** Polls public company job boards (Greenhouse, Lever, Ashby,
  Workday, Dayforce) and, optionally, the Adzuna aggregator, LinkedIn/Indeed
  job-alert emails over IMAP, retained search firms and Apple careers. Or add a
  posting by hand. No scraping, no browser automation.
- **Match.** An offline location filter (no maps API), a title prefilter, an LLM
  alignment score against your history, and a gate that surfaces strong fits and
  queues borderline ones. Unknown pay is shown, never silently dropped.
- **Pipeline.** Surfaced, Queue, Shortlist, Applied, Closed, with follow-ups,
  duplicate-posting clustering, and posting-closure checks that only move a job
  on proof.
- **Tailor.** Per-job resume (standard and ATS), cover letter, groundedness check,
  packet verification of the actual DOCX, and interview prep. Numbers that are not
  in your history are marked `[confirm]`.
- **Network.** Import your own LinkedIn data export to see who you know at each
  company. Message bodies are never stored.
- **Daily autopilot (optional).** A scheduled run that discovers, scores and sends
  a digest (Discord, stdout or off).

## Requirements

- macOS or Linux, Python 3.11 to 3.14, [uv](https://docs.astral.sh/uv/)
- One LLM: a logged-in [Claude Code](https://claude.ai/code) `claude` CLI (no key
  needed), **or** an Anthropic API key (adds prompt caching and live web search),
  **or** an OpenAI key
- Optional: WeasyPrint's system libraries (pango/cairo) for PDF export. Markdown
  and DOCX always work.

## Quick start

```bash
git clone <this repo> blastjob && cd blastjob
uv sync --extra dev
uv run blastjob-web          # http://127.0.0.1:8765
```

Then follow [SETUP.md](SETUP.md): the setup interview, resume ingest, and
`uv run blastjob setup doctor`. If an AI assistant is setting this up for you,
point it at SETUP.md.

## Commands

```bash
uv run blastjob-web [--no-browser] [--port 8765]
uv run blastjob setup doctor | get [key] | set <key> <value> | import <toml> | export
uv run blastjob-daily --dry-run          # print today's digest, send nothing
uv run blastjob-daily --status           # last recorded daily run
uv run python -m blastjob.discovery.resolve "Company"   # find a company's job board
sh scripts/install-launchd.sh            # macOS: always-on cockpit + 07:30 daily run

uv run --extra dev pytest -q             # offline; never calls a real LLM or the network
uv run --extra dev ruff check src tests
```

## Where your data lives

| What | Where |
|---|---|
| Data dir: database, settings, work history, templates, logs, backups | `BLASTJOB_DATA_DIR`, default `~/.blastjob` |
| Settings | the `settings` table in `<data_dir>/blastjob.db` (edit with `blastjob setup` or `/settings`) |
| Secrets (API keys, app passwords) | `<data_dir>/secrets.env` by default (`paths.secrets_file`); settings hold only the env-var names |
| Generated documents | `~/Documents/blastjob/` by default (`paths.output_dir`) |

**The privacy rule:** this repository holds code and generic docs only. Your work
history, settings, database, documents, contacts and secrets never go into it.
If you fork it, keep personal notes in `CLAUDE.local.md` and `local/` (both
gitignored), and run `uv run python scripts/check_no_personal_data.py` before you
push.

The cockpit has no login. It binds `127.0.0.1` unless you choose otherwise.

## More

- [SETUP.md](SETUP.md): installation and the setup interview
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): how a job flows through, trust
  boundaries, truth guarantees, and why each rule exists
- [CLAUDE.md](CLAUDE.md): working reference for a coding agent (conventions,
  commands, module map)
- [CONTRIBUTING.md](CONTRIBUTING.md): development setup, tests, the leak check

## License

MIT. See [LICENSE](LICENSE).

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

Everything blastjob knows about you lives in three places on your own machine.
None of them is inside this repository, so `git` cannot see them.

| What | Where |
|---|---|
| **Data dir** | `BLASTJOB_DATA_DIR`, default `~/.blastjob` |
| `blastjob.db` | your settings, every job and its score, applications and their history, people found for outreach, company profiles, pay data you recorded, and your LinkedIn export's connections (name, profile URL, email if shared, company, position, when you connected, message count and latest date; never message text) |
| `work_history.md`, `templates/` | your merged work history and the resume templates built from it |
| `backups/` | a database copy at the start of each daily run (14 kept) and a snapshot before each work-history overwrite |
| `mail-debug/` | up to 20 job-alert emails that could not be parsed, kept so the parser can be fixed |
| `daily.log`, `web.launchd.log` | logs from the background services, if you install them |
| **Secrets file** | `paths.secrets_file`, default `<data_dir>/secrets.env` (make it mode 600). API keys and app passwords; settings hold only the variable *names* |
| **Output folder** | `paths.output_dir`, default `~/Documents/blastjob` |
| `<company>-<date>/` at the top | resumes, cover letters, prep packs and notes still to send |
| `Submitted/` | packets for jobs you applied to |
| `Closed before applying/` | packets for postings that closed before you applied |
| `Archive/` | everything else: archived jobs, old or unlinked folders |

The app files output folders for you as a job changes stage; a folder is moved
whole and never copied. To put your documents somewhere else, run
`uv run blastjob setup set paths.output_dir <path>`. **On a Mac with iCloud
"Desktop & Documents" turned on, `~/Documents/blastjob` syncs to iCloud.** Pick
a path outside `~/Documents` if you don't want that.

**What leaves your machine:** your work history and each job description go to
the LLM provider you chose (Anthropic, OpenAI, or the `claude` CLI). Company
names and job titles go to Jina search if you add a Jina key. Discovery calls
public job boards and Adzuna with your search terms. The daily digest goes to
Discord only if you turn it on. Nothing else is sent anywhere, and nothing is
ever submitted on your behalf.

**To back up or remove everything,** copy or delete the data dir, the secrets
file (if you moved it) and the output folder.

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

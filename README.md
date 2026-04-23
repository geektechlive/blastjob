# blastjob

A terminal app for managing your work history and generating tailored resumes with AI.

You give it your resume files. It extracts a complete, deduplicated work history. Then, when you apply for a job, you paste in the job description and it generates a targeted resume in under a minute (formatted, length-controlled, and written to the actual job, not just filled in from a template).

---

## What it does

**Ingest.** Point blastjob at a folder of resumes (PDF, DOCX, or plain text). It reads all of them, merges duplicate roles, resolves conflicting metrics in favor of the most reliable source, and produces a single authoritative `work_history.md`. Every resume you generate pulls from this file.

**Build.** Paste a job description, enter the company name, and pick your format. blastjob researches the company (if you have an Anthropic API key), generates a resume targeted to that specific role, scores the fit against the job description, and exports to Markdown, PDF, DOCX, or ATS plain text. Every run gets its own dated folder so you always know who you applied to and when.

**History.** A built-in screen shows every resume you have generated, with cost, token count, and cache hit ratio.

---

## Requirements

- Python 3.11, 3.12, or 3.13 (not 3.14, wheel gaps in pymupdf and weasyprint)
- One of: Anthropic API key, OpenAI API key, or [Claude Code CLI](https://claude.ai/code) installed

An Anthropic API key gets you the most out of blastjob: prompt caching (cheaper repeated runs), live web search for company research, and the best extraction quality. OpenAI and Claude Code CLI work but web search is not available with those providers.

---

## Install

```bash
git clone https://github.com/geektechlive/blastjob.git
cd blastjob
pip install -e .
```

Copy `.env.example` to `.env` and add your API key:

```bash
cp .env.example .env
# Edit .env and set ANTHROPIC_API_KEY (or OPENAI_API_KEY)
```

---

## Run

```bash
./blastjob.sh
```

Or, if you prefer:

```bash
source .venv/bin/activate  # if using a venv
export ANTHROPIC_API_KEY=your_key_here
python -m blastjob
```

---

## How it works

### Work history

blastjob stores everything in `~/.blastjob/` by default:

```
~/.blastjob/
├── work_history.md        # your complete master history
├── templates/
│   ├── standard.md        # formatted resume template
│   └── ats.md             # ATS-optimized plain text template
└── metadata.json
```

You ingest once (or re-ingest when you have new material). After that, every resume build reads from `work_history.md`.

### Resume output

Each build creates a folder under `~/Documents/blastjob/`:

```
~/Documents/blastjob/
└── 2026-04-22/
    └── acme-corp/
        └── senior-engineer/
            ├── resume.md
            ├── resume.pdf
            ├── resume.docx
            ├── resume.ats.txt
            ├── job_description.md
            ├── company_research.md
            ├── fit_score.json
            └── metadata.json
```

### How resumes are generated

blastjob generates resumes the way a good career coach would tell you to write them: ruthlessly selective and targeted to the actual job, with real numbers where they exist. The prompt enforces length rules. The model doesn't get to decide. Roles older than 12 years get title, company, and dates only unless something is uniquely relevant. Every bullet has to earn its place.

---

## Configuration

Config lives at `~/.config/blastjob/config.toml`. blastjob creates it with defaults on first run. You can also edit it from the Settings screen inside the app.

```toml
[paths]
data_dir   = "~/.blastjob"
output_dir = "~/Documents/blastjob"

[llm]
provider = "auto"   # auto | anthropic | openai | claude-cli

[generation]
max_web_searches = 3
```

Provider auto-detection order: `ANTHROPIC_API_KEY` in environment, then `OPENAI_API_KEY`, then `claude` binary in PATH.

---

## Providers

| Provider | Ingest | Build | Web search | Cost tracking |
|---|---|---|---|---|
| Anthropic | yes | yes | yes | yes |
| OpenAI | yes | yes | no | yes |
| Claude Code CLI | yes | yes | no | no |

---

## Development

```bash
pip install -e ".[dev]"
pytest tests/
ruff check src/ tests/
```

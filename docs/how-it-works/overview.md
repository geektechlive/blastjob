# How blastjob works

**What this covers:** What the app is for, the path a job takes from a board to an interview, the design principles, the stack and the code map. Start here before any other how-it-works page. For contributors and curious users.

## How to read these docs

The pages under `docs/how-it-works/` describe what the code does today and why. Each page owns a topic, and a fact lives on exactly one page; everywhere else links to it. The code is the source of truth. If a page and the code disagree, the page is wrong, so fix the page.

Most rules here were learned from a real failure. The failure is described next to its rule so the rule does not get "simplified" away later. When you want to change a rule, read the failure first and check that your change would not bring it back.

For what the app promises about facts, read [truth guarantees](../truth-guarantees.md). For how to use it day to day, start at the [documentation index](../README.md).

## What the app is for

blastjob is AI-assisted resume tailoring plus a job-search cockpit. It discovers roles, matches them against a canonical work history, lets you review them in a local web app, and tailors a resume, cover letter and interview prep pack for the roles you keep.

Think of it as your personal recruiter, not a job board and not a resume tool. Its job is to get you interviews while staying factual. That framing settles priority arguments, because a recruiter who fails the first step is useless however polished the rest is. In order, the recruiter's priorities are:

1. Know the whole market, so no role you could take is missed.
2. Know the candidate, and advocate without inventing anything.
3. Work the network for warm introductions.
4. Prepare the candidate.
5. Chase.

Coverage bugs therefore outrank UI work. A role that is never surfaced cannot be applied to. The worst bug this app has had was a location parser that silently rejected most of the jobs it discovered, while the run log ("fetched N, scored M, new K") looked healthy. The point was never that the rejected jobs were all good. It was that you never got to decide.

## The pipeline

A job travels from a board to an interview along this path. The boxes name the code that does each step; the topic pages go deep on each one.

```
   boards / aggregators / alert email        your LinkedIn export
              |                                       |
      discovery/*  ---> RawPosting                core/network.py
              |            |                          |
       normalize.py --> upsert by (source, external_id) |
              |         source-owned columns only       |
              v                                         v
          jobs table  <----------- warm paths ----- network_contacts
              |
      matching/pipeline.py
        geo.py        three-way: listed town / other state / unknown
        prefilter.py  configured drop terms: job families, never title variants
        scorer.py     batched LLM alignment; reuses an unchanged JD's score
        gate.py       surfaced / queued / new
              |
        review in the cockpit  (Keep / Archive with a reason / Unkeep)
              |
      core/build.py  -> resume + cover letter + sidecars in one folder
              |          build_runs.artifact_dir records where
              v
      verify the packet (core/packet_gate.py) -> packet_report.json
      core/scoring.py -> fit_score.json  (groundedness + JD alignment)
              |
      web/fitscore.py -> the verdict, in the cockpit, before you send it
              |
        apply -> track -> follow up (core/followup.py) -> closure checks
```

Read the diagram top to bottom:

- **Discovery** fetches postings from public board feeds, optional aggregators and your job-alert email, and upserts them by `(source, external_id)`. See [discovery](discovery.md).
- **Matching** runs cheap checks before the expensive one: location, a deterministic title prefilter, then batched LLM scoring, then the gate that decides surfaced, queued or new. See [matching](matching.md).
- **Review** is you, in the cockpit, moving jobs through the [pipeline stages](web.md#pipeline-stages).
- **Tailoring** builds the packet, verifies it and scores it. See [tailoring](tailoring.md).
- **Applying and chasing** are tracked as [application events](events-and-filing.md#application-events); [closure](closure.md) notices when a posting is gone.
- **The network** feeds warm-path badges into the same job rows. See [network](network.md).

## Design principles

- **Shared `core/` code is held to the standard a web caller needs.** The cockpit is the only surface (see [truth guarantees](../truth-guarantees.md#web-is-the-only-surface)), so code in `core/` is written for a caller that is a web request, not for a terminal user.
- **No required API keys.** All LLM work can run through a logged-in `claude` CLI, the ATS boards are public JSON, and location matching is offline with no maps API. Every key (Anthropic, OpenAI, Adzuna, Jina, Discord, IMAP) unlocks an optional extra and nothing more. A fresh install is therefore useful with zero keys. See [the LLM layer](llm.md) for how the provider is chosen.
- **Settings and state live in one place you control.** Settings are rows in your own SQLite database, not a file you hand-edit. See [data model](data-model.md).
- **Anything untrusted is data, never instruction.** The full list of trust boundaries and the defence at each one is in [truth guarantees](../truth-guarantees.md#trust-boundaries).

## Stack

- Python, managed with `uv` (see [prerequisites](../getting-started/install.md#prerequisites)).
- The web cockpit is FastAPI with uvicorn, Jinja2 templates and HTMX. The static assets are one stylesheet and a vendored `web/static/htmx.min.js`. See [the web cockpit](web.md).
- SQLite (standard library, WAL mode) holds jobs, settings and pipeline state, alongside flat files in the data dir and the output folder. See [data model](data-model.md).
- httpx fetches from the discovery adapters. WeasyPrint and python-docx write output. pymupdf and python-docx read your existing documents during ingest.
- Pydantic v2 validates all structured model output and all settings.
- The LLM layer talks to the Anthropic API, the OpenAI API or the `claude` CLI. See [the LLM layer](llm.md).

## Code map

Everything lives under `src/blastjob/`.

- `src/blastjob/llm/`: the provider abstraction (Anthropic, OpenAI, `claude` CLI), cost tracking and prompt caching.
- `src/blastjob/core/`: the domain logic. Ingest, build, history, paths, scoring, refine, coverage, tracking, company profiles and comp signals, interview prep, closure, same-role clustering, events, artifacts and packet verification.
- `src/blastjob/importers/`: reads your existing PDF, DOCX, text and folder inputs.
- `src/blastjob/exporters/`: writes Markdown, PDF, DOCX and ATS plain text.
- `src/blastjob/models/`: Pydantic models for config and settings, history, fit score, tracking, coverage, job and contact.
- `src/blastjob/db/`: the SQLite schema, the migrations and the client. See [data model](data-model.md).
- `src/blastjob/discovery/`: the source adapters (Greenhouse, Ashby, Lever, Workday, Dayforce, Adzuna, LinkedIn and Indeed alert email over IMAP, retained search firms, Apple), plus normalize, pipeline and the board resolver.
- `src/blastjob/matching/`: the geo filter, the scope prefilter, the LLM alignment scorer, the gate and the pipeline that runs them.
- `src/blastjob/web/`: the FastAPI app, the HTMX templates, the background runtime, the tailor and outreach builds, prep, compare and settings. See [the web cockpit](web.md).
- `src/blastjob/daily.py` and `src/blastjob/notify/`: `blastjob-daily` (discover, score, close, digest). The launchd plist templates are in `launchd/`, the installer is `scripts/install-launchd.sh` and the runner is `scripts/run-daily.sh`. See [daily](daily.md).
- `src/blastjob/comp.py` parses compensation. `src/blastjob/secrets.py` autoloads the secrets file.
- `src/blastjob/config.py` and `src/blastjob/settings_store.py` load and save settings. `src/blastjob/setup_cli.py` and `src/blastjob/setup_doctor.py` are the `blastjob setup` command and its checks.
- `tests/` holds the suite and its guards. See [testing](testing.md).

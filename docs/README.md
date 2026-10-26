# blastjob documentation

Pick the line that matches what you want to do. Each page covers one topic, and other
pages link to it rather than repeating it.

## I want to use it

Start with the install guide, then the cockpit tour.

- [Install blastjob](getting-started/install.md): prerequisites, the secrets file, your
  profile and location, your work history, and one job tailored end to end.
- [Your first week](getting-started/first-week.md): tune the fit threshold, archive
  with reasons, and turn on closure once the lists look right.
- [A tour of the cockpit](using/cockpit-tour.md): what each page shows and how to move
  around.
- [Reviewing matches](using/reviewing-matches.md): work through Surfaced and Queue,
  read scores and badges, and handle the same role showing up twice.
- [Pipeline stages](using/stages.md): the five stages and the moves on each page.
- [Target companies](using/targets.md): keep a list of companies you want to work for
  and see dated news there, with who you know.
- [Interview debrief](using/interview-debrief.md): turn an interview transcript into your talk
  share, longest answers and numbers to double-check.
- [Interview stories](using/stories.md): keep your STAR stories in one place and see which
  numbers are not in your work history.
- [Tailoring a packet](using/tailoring.md): build a resume, cover letter and prep pack,
  then read the verification verdict.
- [Applying and following up](using/applying.md): mark a job applied, follow up, and
  see recruiter replies and the activity timeline.
- [The daily digest](using/digest.md): read the morning digest and act on it from a
  phone.
- [Truth guarantees](truth-guarantees.md): what blastjob promises about facts, and how
  far to trust a packet.

## I want to run it

- [Running the services](operations/services.md): keep the cockpit always on and run
  the daily job on a schedule (launchd, systemd or cron).
- [Using the cockpit from another device](operations/lan-access.md): LAN access, and
  what it exposes.
- [The setup doctor](operations/doctor.md): what `blastjob setup doctor` checks and how
  to fix what it reports.
- [Your data and backups](operations/data-and-backups.md): where your data lives, what
  leaves your machine, backups and upgrades.
- [Settings and secrets](operations/settings-and-secrets.md): change settings, keep
  secrets out of them, and see what each optional key unlocks.
- [SETUP.md](../SETUP.md): the setup interview, written for an AI assistant (or a
  person) setting blastjob up for someone.

## I want to understand it

Read the overview first; every other page assumes it.

- [How blastjob works](how-it-works/overview.md): what the app is for, the pipeline,
  the design principles and the code map.
- [Discovery](how-it-works/discovery.md): sources, identity and refresh, and each
  adapter's rules.
- [Matching](how-it-works/matching.md): geo, the title prefilter, alignment scoring,
  the gate and archive reasons.
- [Same role, different route](how-it-works/same-role.md): clusters, reposts,
  suggestions, one application and one apply link per role.
- [Posting closure](how-it-works/closure.md): how a posting is judged closed, and why
  only proof moves a job.
- [Tailoring](how-it-works/tailoring.md): ingest, the build pipeline, company research,
  refine and the prep pack.
- [The LLM layer](how-it-works/llm.md): providers, prompt caching, and the hardened
  claude CLI.
- [Employer data](how-it-works/employer-data.md): employer descriptions for aggregator
  jobs, company profiles, comp signals and people.
- [Interview debrief](how-it-works/debrief.md): the transcript formats, the in-memory hold
  and how each figure is computed.
- [Story bank](how-it-works/stories.md): saved interview stories, the version check and the
  number check.
- [Your network](how-it-works/network.md): the LinkedIn export import and warm paths.
- [The daily run](how-it-works/daily.md): what runs each morning and how the digest is
  delivered.
- [The web cockpit](how-it-works/web.md): app wiring, the security model, background
  runs and pipeline stages.
- [Events, artifacts and packet filing](how-it-works/events-and-filing.md): the
  application timeline, the artifact index and how the output folder files itself.
- [Data model](how-it-works/data-model.md): the data dir, the database and every
  settings group.

## I want to change it

- [AGENTS.md](../AGENTS.md): the rules every contributor and AI agent follows, the
  commands and the code map.
- [CONTRIBUTING.md](../CONTRIBUTING.md): development setup, offline tests, the leak
  check and pull request rules.
- [Testing](how-it-works/testing.md): the offline test guards in full, and why each
  one exists.
- [Truth guarantees](truth-guarantees.md): read before touching tailoring, scoring,
  closure or same-role code.
- [SECURITY.md](../SECURITY.md): what blastjob is built to resist and how to report a
  problem.

## Conventions for these docs

- **One fact, one home.** Link to the page that owns a fact instead of repeating it.
  The only repeats are the short rule summaries in AGENTS.md, and each links here.
- **Code comments cite a doc file, never a heading**, for example
  `# See docs/how-it-works/closure.md.`
- **The user guide (`using/`) and operations pages** speak to you, lead with the task,
  and use the labels the cockpit shows. **How-it-works pages** state invariants first
  and may name modules.
- **Examples use invented names** (Acme Corp, `example.com`, 555-01xx phone numbers).

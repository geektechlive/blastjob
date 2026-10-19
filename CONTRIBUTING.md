# Contributing to blastjob

Thanks for helping. blastjob is a local tool that handles people's job searches,
so the bar for privacy is high: **no personal data ever lands in this repo**, and
that includes yours.

## Development setup

```bash
uv sync --extra dev
uv run --extra dev pytest -q
uv run --extra dev ruff check src tests
uv run --extra dev ruff format src tests
uv run --extra dev mypy                 # whole tree (config in pyproject.toml)
sh scripts/verify.sh   # the whole gate in one command: run it before landing
```

- Python 3.11 to 3.14, managed with [uv](https://docs.astral.sh/uv/). Add
  dependencies by editing `pyproject.toml` and running `uv lock`; never
  `pip install` into the venv.
- Ruff rules E, F, I (isort), UP (pyupgrade), line length 100. Fix every finding.
- Type hints on every function you write or change; mypy stays at zero errors
  across the tree.
- External data (board JSON, mail, LLM output, uploaded files) is parsed into a
  Pydantic model at the boundary.
- Read `CLAUDE.md` ("Truth guarantees") and `docs/ARCHITECTURE.md` before touching
  matching, tailoring, scoring or closure. Most rules there exist because
  something went wrong without them.

## Tests run offline

The suite must pass with no network, no API keys and no real data dir.
`tests/conftest.py` enforces this with autouse guards:

- any socket connection to a non-loopback address raises, naming the test;
- every module's `make_stream` binding and `make_research_call` are patched to
  raise unless the test supplies a fake (`tests/test_no_real_llm_guard.py` fails if
  a new module binds `make_stream` without joining the list);
- real employer-page fetches and Jina searches raise;
- the secrets loader points at a throwaway file.

Inject fakes instead: `httpx.MockTransport`, `search=`/`fetch=` parameters, a
`tmp_path` data dir via `BLASTJOB_DATA_DIR`, `monkeypatch.setenv` for
credentials. Use Arrange-Act-Assert and descriptive test names.

## The leak check

Two layers, both required before a PR:

1. **`tests/test_no_personal_data.py`** runs with the suite. It scans every
   tracked file (`git ls-files`) and fails on email addresses outside
   `example.com` / `example.org` / `example.net`, US-style phone numbers outside
   555-0100 to 555-0199, macOS home-directory paths that name a user, and
   LinkedIn profile URLs. Fixture profile URLs use the reserved handle form
   `linkedin.com/in/example-<anything>`. A short built-in list of job
   platforms' no-reply sender addresses (exact addresses, never whole domains)
   is allowed, because alert-mail fixtures need the real From line. Minified
   vendor files (`*.min.js`) are not read.
2. **`scripts/check_no_personal_data.py`** checks the repo against YOUR OWN data:

   ```bash
   uv run python scripts/check_no_personal_data.py
   ```

   It reads your database and `work_history.md` (company names, contact names,
   emails, phones, your own name) and reports any of them found in tracked files.
   It has two tiers: **FAIL** for high-confidence hits (emails, phones, profile
   URLs, your name, contact names, distinctive company names) and **WARN** for
   lower-confidence ones (common company names, long word-for-word overlaps with
   your work history). It exits non-zero on any FAIL. False positives can be
   listed, one per line, in `local/leak_allowlist.txt` (gitignored).
   It also fails on the web domains of employers you kept or applied to, so
   fixtures must invent domains as well as names: use `example.com` or a
   `.example` host, never a real employer's careers site.

To run the second check on every `git push` and block the push when it fails:

```bash
sh scripts/install-git-hooks.sh
```

It installs a pre-push hook in your clone only. If you already have a pre-push
hook, it adds one line to the top of it rather than replacing it. Skip it for
one push with `git push --no-verify`, after you have read the findings.

These are pattern and list checks. They catch the common ways personal data
leaks; they are not a general secret scanner. Review your own diff too.

## Pull request rules

- **No personal data.** No real resumes, settings exports, databases, generated
  packets, mail, contacts, plans or notes. Your own notes belong in
  `CLAUDE.local.md` or `local/`, which are gitignored.
- **No real company or person names in fixtures.** Use invented ones ("Example
  Widgets Inc", "Pat Sample"), `example.com` / `example.org` addresses, 555-01xx
  phone numbers, and invented or public-gazetteer towns. A fixture modelled on a
  real posting must have every identifying detail replaced.
- **No secrets.** Settings reference secrets by env-var name; values live in the
  secrets file, which is never committed.
- **Keep the truth guarantees.** Draft-only; groundedness checks; only posted
  comp may reject; unplaceable locations go to review; only proof closes a
  posting. A change that weakens one needs an explicit argument in the PR.
- **Web is the only surface.** A new capability needs a cockpit route.
- **Tests with the change.** New behavior gets tests; the suite, ruff, mypy and
  the leak check all pass.
- One topic per PR, with a description of what changed and why, and how you
  tested it. Commit messages: `<type>: <description>` (feat, fix, refactor, docs,
  test, chore, perf, ci).

## License

By contributing you agree that your contributions are licensed under the MIT
License in [LICENSE](LICENSE).

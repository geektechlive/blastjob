# Testing

**What this covers:** Why the suite runs fully offline and the autouse guards that enforce it, one section per guard with the failure that motivated it, plus fixtures, fakes and coverage. For anyone writing or debugging a test.

## The offline suite

The suite must pass with no network, no API keys and no real data dir. Never point a test at the network, at a real LLM, or at your own data dir or output folder.

This is enforced, not requested. Many callers in this code base are fail-soft: they catch `Exception`, log it and carry on, which is right for a daily job and wrong for a test. A test that leaked a real call used to stay green while it spent money or read someone's files. So every rule below is an autouse fixture in `tests/conftest.py` that fails loudly and names what to fix. A test that wants a fake overrides the guard by patching or injecting its own.

The guards, and the section that explains each:

| Fixture | Keeps a test off | Section |
|---|---|---|
| `_no_real_network` | any non-loopback socket or DNS lookup | [Network guard](#network-guard) |
| `_no_real_llm_calls` | a real LLM provider | [LLM guard](#llm-guard) |
| `_no_real_page_fetches` | an employer page fetch or a Jina page search | [Page-fetch guard](#page-fetch-guard) |
| `_no_real_packet_research` | Jina-backed packet research | [Packet-research guard](#packet-research-guard) |
| `_no_real_target_watch` | Jina-backed target news scans | [Target-watch guard](#target-watch-guard) |
| `isolated_secrets` | the real secrets file | [Secrets guard](#secrets-guard) |
| `_no_real_config` | the real legacy config and data dir | [Config and data-dir guard](#config-and-data-dir-guard) |
| `_no_real_output_dir` | the real packet folder | [Output-folder guard](#output-folder-guard) |

The failure modes differ, and it matters which one you are looking at. The network guard records every refusal and fails the test at teardown, so a swallowed exception cannot hide it. The page-fetch and packet-research guards call `pytest.fail`, which is not an `Exception`, so a fail-soft `except Exception` cannot swallow it. The LLM guard raises a plain `AssertionError`: it stops the call, but a fail-soft caller can swallow the message, so a missing provider call can show up as a missing result rather than a red line naming the module.

The gate that runs the whole suite with lint, types and the personal-data check is described in [CONTRIBUTING](../../CONTRIBUTING.md).

## Network guard

No test may open a connection to anything but loopback. `_no_real_network` patches `socket.socket.connect`, `connect_ex`, `socket.create_connection` and `socket.getaddrinfo`. DNS is patched too, because asyncio and anyio resolve before they connect, so a lookup is itself a leak. A refusal happens before the real call, so a hostname is never handed to the resolver.

Allowed: `localhost`, any loopback address (including an IPv4-mapped one) and Unix sockets. FastAPI's `TestClient` is in-process and SQLite opens files, so neither is affected. A real listener on `127.0.0.1` still works.

Every refusal raises `NetworkBlockedError` and is also recorded; the test fails at teardown if anything was recorded. The error is deliberately not an `OSError`, because adapters have `except OSError` and transport-error handlers that would quietly turn a leak into an "offline" result. The fixture yields the list of violations so `tests/test_network_guard.py` can assert on a refusal and clear it. That test uses a reserved test address (192.0.2.1) and the `.invalid` top-level domain with a short timeout, so a broken guard can neither reach a real host nor hang the run.

To fake the network, pass an `httpx.MockTransport`, inject a fetch or deps object (`search=`, `fetch=`), or patch the adapter function in your test.

## LLM guard

No test may reach a real LLM provider. Each module that calls the model does `from blastjob.llm.providers import make_stream`, which copies the reference, so patching `providers.make_stream` covers nothing. `_no_real_llm_calls` therefore patches every binding: each module listed in `_STREAM_BINDINGS` in `tests/conftest.py`, plus `make_research_call` in `core/build.py`. An unmocked call raises an `AssertionError` that names the module to patch. A test that wants a fake patches its own module path (`"blastjob.core.scoring.make_stream"`), which overrides the guard.

The leak this prevents already happened and cost money. `run_build` makes seven provider calls through five modules. A cost test patched only `core/build.py` and `core/scoring.py`, so the research-distillation, coverage-gap and cover-letter-scoring calls went to the live provider on every run. All three callers are fail-soft, so the real call happened, the exception was swallowed and nothing was recorded. The suite was green, and one test run alone failed with an extra cost record carrying real cache tokens and a real dollar cost. It was invisible in the suite and reproducible only by running that one test.

`tests/test_no_real_llm_guard.py` keeps the list honest. It parses `src/` with `ast` and fails if a module binds `make_stream` without being in `_STREAM_BINDINGS`, and also if the list names a module that no longer binds it. It parses rather than greps so a comment that mentions `make_stream` does not count. The same file checks that a blocked call names its module.

## Page-fetch guard

No test may fetch an employer page or run a Jina page search. `_no_real_page_fetches` replaces `_safe_get` and `_jina_search` in `core/page_links.py` with functions that call `pytest.fail` and name the fix. The patch works because `core/page_links.py` looks both names up at call time, so it covers every path in: `find_page_link`, `check_page` and the defaults of `closure_links.LinkDeps`. It uses `pytest.fail` rather than `AssertionError` because closure's fail-soft `except Exception` handlers would swallow an `AssertionError` and hide the leak.

To fake it, inject `search=` and `fetch=` fakes, or patch those two names in your test. The real network functions, `page_fetch.safe_get` and `page_fetch.jina_search`, are tested separately against `httpx.MockTransport` with a fake DNS resolver.

## Packet-research guard

No test may run packet research against the network. Packet research (`core/packet_research.py`) searches and reads pages through Jina when a key is set. `_no_real_packet_research` does two things. It makes `_jina_key` return an empty string, so every test sees no key and `run_build` takes the knowledge-only fallback whatever the developer's shell exports. And it replaces `_search` and `_read` with functions that call `pytest.fail`, so a test that passes `api_key=` without injecting `search=` and `read=` fails loudly. It uses `pytest.fail` for the same reason as the page-fetch guard: the module's own fail-soft `except Exception` cannot hide it.

This guard came with the change that made packet research read fetched pages (#59). To test research, inject `search=` and `read=` fakes.

## Target-watch guard

No test may run the target news watch against the network. `core/target_watch.py` searches and reads pages through Jina when a key is set. `_no_real_target_watch` makes its `_jina_key` return an empty string and replaces `_search` and `_read` with functions that call `pytest.fail`, for the same reason as the packet-research guard. To test a scan, inject `search=` and `read=` fakes and patch the module's own `make_stream`, which `_STREAM_BINDINGS` also guards.

## Secrets guard

No test may read the real secrets file. Several code paths load it on purpose (`web/mail_settings.py`, `web/people.py`, `web/research.py`, `discovery/pipeline.py`, `daily.py`), so a test asserting "credentials missing" would otherwise pass or fail depending on whose machine it runs on.

`isolated_secrets` is autouse. It replaces `secrets.load_secrets_env` with a version that always reads a throwaway secrets file, whatever path the config installed, and yields that path so a test can write credentials into it. It also snapshots `os.environ` before the test and restores it after. The loader writes into `os.environ` directly, which `monkeypatch` would not undo, so without the restore the keys would leak into every later test file.

A test that needs a credential sets it with `monkeypatch.setenv`.

## Config and data-dir guard

No test may read or write your real config file or data dir. This is a different fixture from the secrets guard, `_no_real_config`. For each test it points the legacy config path at an empty temporary directory, points `BLASTJOB_DATA_DIR` at a fresh temporary directory, and resets the installed secrets path.

There is a second layer for import time. The first lines of `tests/conftest.py` set `BLASTJOB_DATA_DIR` to a temporary directory before any test module is imported, because a module-level default config object would otherwise default to your real data dir. `models/config.py` honors that variable in its default for this reason.

A test that needs a data dir sets `BLASTJOB_DATA_DIR` to its own `tmp_path`. A companion fixture, `_reset_runtime_settings`, clears the process-global settings registries (company aliases, agency names, keywords, alert words) before and after every test.

## Output-folder guard

No test may touch your real packet folder. The packet filer renames folders under `paths.output_dir`, and a default config object defaults that to `~/Documents/blastjob`, so a daily-run or route test on a default config could rename real packets.

`_no_real_output_dir` wraps `output_dir` (in `src/blastjob/config.py`) so that when the answer equals the default location it returns a throwaway folder instead. Every reader resolves the folder through `output_dir`, so the wrapper covers them. A test that needs packets sets `paths.output_dir` to a temporary directory.

## Personal-data test

`tests/test_no_personal_data.py` runs with the suite and scans every tracked file (the list from `git ls-files`) for real email addresses, phone numbers, home-directory paths and LinkedIn profile URLs. `ENFORCE` is on, so a hit fails the suite.

It uses only the generic detectors: emails outside `example.com`, `.org` and `.net` and the exact platform sender addresses the alert-mail fixtures need, US phone numbers outside the reserved 555-0100 to 555-0199 range, `/Users/<name>` paths, and `linkedin.com/in/<handle>` URLs other than the reserved `example` and `example-*` handles. Its own positive examples are assembled by concatenation at runtime so the file never matches its own scan.

The scan against *your* data, which looks for your name, your contacts, your companies and your employers' web domains, is `scripts/check_no_personal_data.py`. It reads your database and work history, so it is run by hand before publishing. The same test file covers its detectors using a synthetic temporary database and git repo. Fixtures in tracked files use invented people and companies, `example.com` and `example.org` addresses and 555-01xx numbers.

## Fixtures and fakes

Besides the guards, `tests/conftest.py` has four autouse fixtures that keep tests independent:

- `_clean_runtime` clears `runtime.RUNTIME` after every test. Background runs live on one process-wide object, and a finished run stays "active" in the status banner for minutes, so without this a run one test starts shows up in a later test's `/runs/active`.
- `_offline_closure_deps` replaces `closure_links.default_deps` with a network-free stand-in: no boards found, every probe inconclusive. The daily run includes the closure pass, whose default resolver probes live boards. Tests that build `LinkDeps` explicitly are unaffected.
- `_reset_runtime_settings` is described under the [config guard](#config-and-data-dir-guard).
- `_loopback_test_client` makes `TestClient` address `http://127.0.0.1` instead of `testserver`, because `security.LocalRequestGuard` refuses any non-local `Host` (the DNS-rebinding defense). A test of the refusal passes its own base URL or `Host` header (see `tests/test_host_guard.py`).

`create_app` takes injectable `load_config` and `save_config` callables, so a route test builds the app against a temporary data dir.

Shared fictional fixtures live in `tests/agency_fixture.py`, `tests/geo_fixture.py` and `tests/profile_fixture.py`. Geo, agency names and the candidate profile are all user configuration with empty defaults, so any test that needs "this is an agency", "this town is in range" or "scoring happens" must configure one of these invented ones. Do not rely on a default discovery or candidate config for such a test: the default means "not configured", and the behavior under test would silently not run.

To fake the model, patch `make_stream` in the module under test. To fake HTTP, use `httpx.MockTransport`. Async tests need no marker: `asyncio_mode` is `auto`.

Three scripts check behavior outside the unit suite:

- `scripts/golden_matching.py` records the geo, scope and gate verdict for every job in a database, with no LLM and no network, so a refactor of the matching path can be diffed before and after. Run it on a copy of a real database, with the code before and after the change. The diff must be empty, or every line explained. `--full` adds the discovery adapters built from the config, company-key aliases and former-employer matches.
- `scripts/regression_e2e.py` checks, offline, that a database copy configured only by its settings table still matches, prompts and discovers as a baseline did. It copies the database first, replaces the scorer's model call with a fake, and exits 0 when every check passes and 1 otherwise. `tests/test_regression_e2e.py` runs it on a synthetic database.
- `scripts/smoke_e2e.py` exercises build, application tracking, refine and coverage end to end with mocked model streams, so it needs no API key.

## Coverage

```bash
uv run --extra dev pytest --cov=src/blastjob --cov-report=term-missing tests/
```

There is no `omit` configuration: everything counts, including code that is hard to reach.

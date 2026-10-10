"""Suite-wide guards."""

from __future__ import annotations

import os
import tempfile

import pytest

from blastjob.models.config import DATA_DIR_ENV

# Before any test module is imported: a module-level `BlastJobConfig()` must
# never default to the owner's real data dir.
os.environ[DATA_DIR_ENV] = tempfile.mkdtemp(prefix="blastjob-test-data-")

from blastjob.core import closure_links  # noqa: E402
from blastjob.discovery.resolve import BoardHit  # noqa: E402


@pytest.fixture(autouse=True)
def _offline_closure_deps(monkeypatch):
    """The daily run now includes the closure pass, whose default resolver
    probes live ATS boards. Any test that reaches it without injecting its own
    deps gets a network-free stand-in: no boards found, every probe
    inconclusive. Tests that build `LinkDeps` explicitly are unaffected."""

    def _no_boards(*_a: object) -> list[BoardHit]:
        return []

    async def _no_postings(*_a: object) -> list:
        return []

    async def _inconclusive(*_a: object) -> None:
        return None

    def offline() -> closure_links.LinkDeps:
        return closure_links.LinkDeps(
            probe_company=_no_boards,
            probe_workday=_no_boards,
            probe_dayforce=_no_boards,
            fetch_board=_no_postings,
            check_link=_inconclusive,
            find_page=_inconclusive,
            check_page=_inconclusive,
        )

    monkeypatch.setattr(closure_links, "default_deps", offline)


@pytest.fixture(autouse=True)
def _no_real_page_fetches(monkeypatch):
    """No test may fetch an employer page or run a Jina search for real.
    `core/page_links` looks both up at call time, so this patch covers every
    path into them (find_page_link, check_page, LinkDeps defaults). A test
    that wants one injects `search=` / `fetch=`, or patches
    `blastjob.core.page_links._safe_get` / `._jina_search` itself; the real
    functions are tested against httpx.MockTransport via core/page_fetch."""
    from blastjob.core import page_links

    def _forbid(name: str):
        async def _raise(*_a: object, **_k: object):
            # pytest.fail, not AssertionError: closure's fail-soft `except
            # Exception` handlers would swallow an AssertionError and hide the leak.
            pytest.fail(
                f"blastjob.core.page_links.{name} tried to reach the network. Inject a "
                f"fake (search=/fetch=) or patch page_links.{name} in this test (see "
                "tests/conftest.py::_no_real_page_fetches)."
            )

        return _raise

    monkeypatch.setattr(page_links, "_safe_get", _forbid("_safe_get"))
    monkeypatch.setattr(page_links, "_jina_search", _forbid("_jina_search"))


@pytest.fixture(autouse=True)
def isolated_secrets(tmp_path_factory, monkeypatch):
    """Point a test's secrets load at a throwaway file instead of the owner's
    real `~/.claude/secrets.env`.

    Several code paths load it deliberately (`web/mail_settings`, `web/people`,
    `web/research`, `discovery/pipeline`, `daily`), so a test asserting
    "credentials missing" otherwise passes or fails depending on whose machine
    it runs on. Opt in per module with an autouse wrapper.

    NOT autouse suite-wide, though it arguably should be: making it global broke
    four unrelated tests and tripled suite runtime, because some tests today
    depend on real credentials being present in the environment. That is a
    latent problem worth fixing on its own, not as a side effect of adding a
    settings page.

    Patching `secrets.SECRETS_PATH` is not enough: `load_secrets_env` binds it
    as a default argument at definition time, so the bound default wins. The
    function is replaced instead. os.environ is snapshotted and restored
    because that function writes to it directly, which monkeypatch would not
    undo -- the keys would leak into every later test file.
    """
    import os

    from blastjob import secrets as secrets_mod

    path = tmp_path_factory.mktemp("secrets") / "secrets.env"
    real = secrets_mod.load_secrets_env
    monkeypatch.setattr(secrets_mod, "load_secrets_env", lambda *a, **k: real(path))
    before = dict(os.environ)
    yield path
    for key in set(os.environ) - set(before):
        del os.environ[key]
    os.environ.update(before)


@pytest.fixture(autouse=True)
def _no_real_config(tmp_path_factory, monkeypatch):
    """No test reads or writes the owner's real config file or data dir: the
    legacy config path points at an empty throwaway directory, and
    `BLASTJOB_DATA_DIR` at a fresh one (module import time is covered by the
    session-wide value set at the top of this file)."""
    from blastjob import config as cfg_mod
    from blastjob import secrets as secrets_mod

    cfg_dir = tmp_path_factory.mktemp("config")
    monkeypatch.setattr(cfg_mod, "_CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(cfg_mod, "_CONFIG_FILE", cfg_dir / "config.toml")
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path_factory.mktemp("data")))
    monkeypatch.setattr(secrets_mod, "_configured", None)


@pytest.fixture(autouse=True)
def _reset_runtime_settings():
    """The config-load registries (aliases, agencies, keywords, alert words)
    are process-global; every test starts and ends with them empty."""
    from blastjob import config as cfg_mod
    from blastjob.models.config import BlastJobConfig

    cfg_mod.apply_runtime_settings(BlastJobConfig())
    yield
    cfg_mod.apply_runtime_settings(BlastJobConfig())


# Every module that binds `make_stream` at import time. Patching
# `llm.providers.make_stream` does NOT cover these: `from ... import make_stream`
# copies the reference, so each module needs its own patch.
_STREAM_BINDINGS = (
    "blastjob.core.build",
    "blastjob.core.cover_letter_score",
    "blastjob.core.coverage",
    "blastjob.core.followup",
    "blastjob.core.ingest",
    "blastjob.core.network_enrich",
    "blastjob.core.prep",
    "blastjob.core.refine",
    "blastjob.core.research_facts",
    "blastjob.core.scoring",
    "blastjob.matching.scorer",
    "blastjob.web.build",
    "blastjob.web.people",
    "blastjob.web.research",
)


@pytest.fixture(autouse=True)
def _no_real_llm_calls(monkeypatch):
    """No test may reach a real LLM provider. A test that wants a fake patches
    its own module path, which overrides this guard.

    This exists because the leak it prevents already happened and cost money.
    `run_build` makes seven provider calls, through five different modules;
    `tests/test_cost_truth.py` patched only `core.build` and `core.scoring`, so
    the research-distillation, coverage-gap and cover-letter-scoring calls went
    to the live provider on every run. It stayed invisible because all three
    callers are fail-soft: the real call happened, the exception was swallowed,
    and nothing was recorded — so the suite was green while a single test run in
    isolation failed with an extra CallCost carrying real cache tokens and a
    real dollar cost (~$0.15).

    Failing loudly is the whole point: an unmocked call now names the module it
    came from instead of quietly spending money and skewing timings.
    """
    import importlib

    def _forbid(module: str):
        async def _raise(*_a: object, **_k: object):
            raise AssertionError(
                f"{module} tried to reach a real LLM provider. Patch "
                f'"{module}.make_stream" in this test (see '
                "tests/conftest.py::_no_real_llm_calls)."
            )

        return _raise

    for name in _STREAM_BINDINGS:
        mod = importlib.import_module(name)
        if hasattr(mod, "make_stream"):
            monkeypatch.setattr(mod, "make_stream", _forbid(name))
    build = importlib.import_module("blastjob.core.build")
    monkeypatch.setattr(build, "make_research_call", _forbid("blastjob.core.build"))


# --- Network guard -----------------------------------------------------------
# Hosts a test may connect to. Everything else (any other hostname, any
# non-loopback IP, the empty host, 0.0.0.0) is refused BEFORE the real call,
# so a hostname is never handed to the resolver.
_LOOPBACK_NAMES = frozenset({"localhost"})


class NetworkBlockedError(RuntimeError):
    """A test tried to reach a non-loopback address. Deliberately not an
    OSError: adapters' `except OSError` / transport-error fail-soft paths must
    not quietly turn a leak into an "offline" result."""


def _is_loopback_host(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    name = host.split("%", 1)[0].strip("[]").lower()
    if name in _LOOPBACK_NAMES:
        return True
    import ipaddress

    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    return bool(ip.is_loopback or (mapped is not None and mapped.is_loopback))


def _is_allowed_address(address: object) -> bool:
    # AF_UNIX takes a str/bytes path (or an abstract-namespace bytes name).
    if isinstance(address, str | bytes):
        return True
    if isinstance(address, tuple) and address:
        return _is_loopback_host(address[0])
    return False


@pytest.fixture(autouse=True)
def _no_real_network(request, monkeypatch):
    """No test may open a connection to anything but loopback.

    Patches `socket.socket.connect` / `connect_ex`, `socket.create_connection`
    and `socket.getaddrinfo` (asyncio/anyio resolve before they connect, so a
    DNS lookup is itself a leak). FastAPI's TestClient is in-process ASGI and
    sqlite opens files, so neither is affected; a real listener on 127.0.0.1
    or ::1, `localhost`, and AF_UNIX sockets still work.

    Each refusal raises AND is recorded, and the test fails at teardown if
    anything was recorded: several callers are fail-soft (`except Exception`)
    and would otherwise swallow the error and pass. The fixture yields that
    list so tests/test_network_guard.py can assert on it and clear it.
    """
    import socket

    violations: list[str] = []
    test_id = request.node.nodeid

    def _refuse(what: str, address: object) -> NetworkBlockedError:
        violations.append(f"{what}({address!r})")
        return NetworkBlockedError(
            f"{test_id} tried to reach the network: {what}({address!r}). Fake the call "
            "instead: pass an httpx.MockTransport or an injected fetch/deps, or "
            "monkeypatch the adapter function in this test "
            "(see tests/conftest.py::_no_real_network)."
        )

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create_connection = socket.create_connection
    real_getaddrinfo = socket.getaddrinfo

    def connect(self, address, *a, **k):
        if self.family != socket.AF_UNIX and not _is_allowed_address(address):
            raise _refuse("socket.connect", address)
        return real_connect(self, address, *a, **k)

    def connect_ex(self, address, *a, **k):
        if self.family != socket.AF_UNIX and not _is_allowed_address(address):
            raise _refuse("socket.connect_ex", address)
        return real_connect_ex(self, address, *a, **k)

    def create_connection(address, *a, **k):
        if not _is_allowed_address(address):
            raise _refuse("socket.create_connection", address)
        return real_create_connection(address, *a, **k)

    def getaddrinfo(host, *a, **k):
        if host is not None and not _is_loopback_host(host):
            raise _refuse("socket.getaddrinfo", host)
        return real_getaddrinfo(host, *a, **k)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield violations
    if violations:
        pytest.fail(
            f"{test_id} attempted {len(violations)} non-loopback network call(s): "
            f"{'; '.join(violations[:5])}. Fake them (see tests/conftest.py::_no_real_network).",
            pytrace=False,
        )

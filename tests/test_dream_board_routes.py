"""Finding a dream company's board from its page and adding it only on the owner's click (#90)."""

from __future__ import annotations

import threading
import time
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from blastjob.core import target_store as ts
from blastjob.core.company import company_key
from blastjob.db import client as dbc
from blastjob.discovery import board_finder as bf
from blastjob.discovery import resolve
from blastjob.discovery.resolve import BoardHit
from blastjob.discovery.resolve import probe_dayforce as REAL_PROBE_DAYFORCE
from blastjob.models.config import BlastJobConfig, TargetBoard
from blastjob.settings_store import StaleSettings
from blastjob.web import routes_boards
from blastjob.web import settings_form as sf
from blastjob.web.app import create_app
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN

COMPANY = "Northwind Traders"
KEY = company_key(COMPANY)
PATH = quote(KEY, safe="")
TOK = {CSRF_HEADER: LAUNCH_TOKEN}
SAMPLES = (bf.Sample("VP of Technology", "Phoenix, AZ"), bf.Sample("Controller", "Remote"))


class Env:
    def __init__(self, client, conn, state):
        self.client, self.conn, self.state = client, conn, state

    @property
    def boards(self) -> list[TargetBoard]:
        return self.state["cfg"].discovery.target_boards

    def wait(self, timeout: float = 5.0) -> str:
        """The boards fragment once the search has finished."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            page = self.client.get(f"/targets/{PATH}/boards").text
            if "hx-trigger" not in page:
                return page
            time.sleep(0.02)
        raise AssertionError("the search never finished")

    def find(self) -> str:
        r = self.client.post(f"/targets/{PATH}/boards/find", headers=TOK, follow_redirects=False)
        assert r.status_code == 303
        return self.wait()

    def use(self, ats: str, token: str, **extra: str):
        return self.client.post(
            f"/targets/{PATH}/boards/use",
            data={"ats": ats, "token": token, **extra},
            headers=TOK,
            follow_redirects=False,
        )


@pytest.fixture
def env(tmp_path, monkeypatch):
    routes_boards._FINDINGS.clear()
    monkeypatch.setenv("JINA_API_KEY", "test-key")
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path)
    conn = dbc.init_db(cfg)
    ts.add_targets(conn, [ts.TargetInput(name=COMPANY, domain="northwind.example.com")], "dream")
    state: dict = {"cfg": cfg, "saves": 0, "stale": False}

    def load() -> BlastJobConfig:
        return state["cfg"].model_copy(deep=True)

    def save(c: BlastJobConfig) -> None:
        if state["stale"]:
            raise StaleSettings("changed")
        state["cfg"] = c.model_copy(deep=True)
        state["saves"] += 1

    # What the finder's network leaves return; a test edits these.
    fake: dict = {"company": [], "workday": [], "samples": {}, "careers": ""}
    monkeypatch.setattr(resolve, "probe_company", lambda name: list(fake["company"]))
    monkeypatch.setattr(resolve, "probe_workday", lambda name, key: list(fake["workday"]))
    monkeypatch.setattr(resolve, "probe_dayforce", lambda name, key: [])
    monkeypatch.setattr(bf, "fetch_samples", lambda hit: fake["samples"].get(hit.ats, ((), "")))
    monkeypatch.setattr(bf, "read_careers_page", lambda domain, key: fake["careers"])
    state["fake"] = fake
    with TestClient(create_app(load_config=load, save_config=save)) as client:
        yield Env(client, conn, state)
    conn.close()


def _gh(env: Env, *, name: str = "", careers: str = "") -> None:
    env.state["fake"]["company"] = [BoardHit(COMPANY, "greenhouse", "northwind", 12)]
    env.state["fake"]["samples"]["greenhouse"] = (SAMPLES, name)
    env.state["fake"]["careers"] = careers


# ----------------------------------------------------------------------------- the page


def test_only_a_dream_target_offers_the_search(env):
    assert "Find job board" in env.client.get(f"/targets/{PATH}/boards").text
    ts.set_tier(env.conn, KEY, "core")
    assert env.client.get(f"/targets/{PATH}/boards").text == ""
    assert "board-search" not in env.client.get(f"/targets/{PATH}").text
    refused = env.client.post(f"/targets/{PATH}/boards/find", headers=TOK, follow_redirects=False)
    assert refused.status_code == 404


def test_a_dream_target_page_loads_the_section(env):
    page = env.client.get(f"/targets/{PATH}").text
    assert f'hx-get="/targets/{PATH}/boards"' in page


def test_the_search_needs_the_token_and_a_local_origin(env):
    url = f"/targets/{PATH}/boards/find"
    assert env.client.post(url).status_code == 403
    bad = env.client.post(url, headers={**TOK, "origin": "https://evil.example.com"})
    assert bad.status_code == 403
    use = f"/targets/{PATH}/boards/use"
    data = {"ats": "greenhouse", "token": "northwind", "owner_confirmed": "1"}
    assert env.client.post(use, data=data).status_code == 403
    bad = env.client.post(use, data=data, headers={**TOK, "origin": "https://evil.example.com"})
    assert bad.status_code == 403
    assert env.boards == []


# ----------------------------------------------------------------------------- evidence


def test_a_search_shows_the_evidence_and_adds_nothing(env):
    _gh(env, name="Northwind Traders, Inc.")
    page = env.find()
    assert "greenhouse" in page and "northwind" in page
    assert "12 postings" in page
    assert "VP of Technology" in page and "Phoenix, AZ" in page
    assert "the board reports the name Northwind Traders, Inc." in page
    assert env.state["saves"] == 0 and env.boards == []


def test_a_board_linked_from_the_careers_page_is_shown_as_verified(env):
    _gh(env, careers="Apply: https://boards.greenhouse.io/northwind")
    assert "verified: linked from the company" in env.find()


def test_a_board_without_evidence_asks_for_the_owners_tick(env):
    _gh(env, name="Northwind Logistics LLC")
    page = env.find()
    assert "not verified" in page
    assert "the board reports the name Northwind Logistics LLC" in page
    assert "This board belongs to Northwind Traders" in page


def test_nothing_found_is_a_coverage_gap_and_lists_what_was_not_checked(env, monkeypatch):
    monkeypatch.delenv("JINA_API_KEY")
    monkeypatch.setattr(routes_boards, "_jina_key", lambda: "")
    page = env.find()
    assert "No supported board found" in page
    assert "coverage gap" in page
    assert "Workday and Dayforce boards were not searched" in page


def test_hostile_text_from_a_board_is_escaped(env):
    env.state["fake"]["company"] = [BoardHit(COMPANY, "lever", "northwind", 1)]
    evil = (bf.Sample("<script>alert(1)</script>", "<img src=x>"),)
    env.state["fake"]["samples"]["lever"] = (evil, "<b>Northwind Traders</b>")
    page = env.find()
    assert "<script>alert(1)</script>" not in page
    assert "<img src=x>" not in page
    assert "&lt;script&gt;" in page


# ----------------------------------------------------------------------------- confirming


def test_using_a_verified_board_saves_it_with_its_basis(env):
    _gh(env, careers="https://boards.greenhouse.io/northwind")
    env.find()
    r = env.use("greenhouse", "northwind")
    assert r.status_code == 303 and r.headers["location"].endswith("notice=added")
    (board,) = env.boards
    assert (board.company, board.ats, board.token) == (COMPANY, "greenhouse", "northwind")
    assert board.identity_basis == "domain_link"


def test_an_ats_name_basis_is_stored(env):
    _gh(env, name="Northwind Traders")
    env.find()
    env.use("greenhouse", "northwind")
    assert env.boards[0].identity_basis == "ats_name"


def test_an_unverified_board_is_not_saved_without_the_tick(env):
    _gh(env)
    env.find()
    r = env.use("greenhouse", "northwind")
    assert r.headers["location"].endswith("notice=needs_confirm")
    assert env.boards == []
    assert (
        "tick that the board belongs"
        in env.client.get(f"/targets/{PATH}/boards?notice=needs_confirm").text
    )


def test_the_tick_saves_an_unverified_board_as_owner_confirmed(env):
    _gh(env)
    env.find()
    env.use("greenhouse", "northwind", owner_confirmed="1")
    assert env.boards[0].identity_basis == "owner_confirmed"


def test_a_basis_sent_by_the_browser_is_ignored(env):
    _gh(env)
    env.find()
    env.use("greenhouse", "northwind", identity_basis="domain_link", basis="ats_name")
    assert env.boards == []
    env.use("greenhouse", "northwind", owner_confirmed="1", identity_basis="domain_link")
    assert env.boards[0].identity_basis == "owner_confirmed"


def test_a_board_that_was_not_in_the_search_is_refused(env):
    _gh(env)
    env.find()
    assert env.use("lever", "somebody-else", owner_confirmed="1").status_code == 400
    assert env.boards == []


def test_using_a_board_before_any_search_is_refused_with_a_notice(env):
    r = env.use("greenhouse", "northwind", owner_confirmed="1")
    assert r.headers["location"].endswith("notice=no_findings")
    assert env.boards == []


def test_a_repeated_click_adds_the_board_once(env):
    _gh(env, careers="https://boards.greenhouse.io/northwind")
    env.find()
    assert env.use("greenhouse", "northwind").headers["location"].endswith("notice=added")
    assert env.use("greenhouse", "NorthWind").headers["location"].endswith("notice=exists")
    assert len(env.boards) == 1 and env.state["saves"] == 1


def test_stale_settings_say_so_and_save_nothing(env):
    _gh(env, careers="https://boards.greenhouse.io/northwind")
    env.find()
    env.state["stale"] = True
    r = env.use("greenhouse", "northwind")
    assert r.headers["location"].endswith("notice=stale")
    page = env.client.get(f"/targets/{PATH}/boards?notice=stale").text
    assert "settings changed while you were looking" in page and "Try again" in page
    assert env.boards == []
    env.state["stale"] = False
    assert env.use("greenhouse", "northwind").headers["location"].endswith("notice=added")


def test_two_confirms_both_land_because_each_reloads_the_latest_settings(env):
    env.state["fake"]["company"] = [
        BoardHit(COMPANY, "greenhouse", "northwind", 3),
        BoardHit(COMPANY, "lever", "northwind", 4),
    ]
    env.state["fake"]["samples"]["greenhouse"] = (SAMPLES, "Northwind Traders")
    env.state["fake"]["samples"]["lever"] = (SAMPLES, "")
    env.find()
    env.use("greenhouse", "northwind")
    env.use("lever", "northwind", owner_confirmed="1")
    assert [(b.ats, b.identity_basis) for b in env.boards] == [
        ("greenhouse", "ats_name"),
        ("lever", "owner_confirmed"),
    ]


def test_the_page_lists_the_boards_already_read(env):
    env.state["cfg"].discovery.target_boards = [
        TargetBoard(company=COMPANY, ats="ashby", token="nw", identity_basis="owner_confirmed"),
        TargetBoard(company="Fabrikam", ats="lever", token="fab"),
    ]
    page = env.client.get(f"/targets/{PATH}/boards").text
    assert "ashby" in page and "confirmed by you" in page
    assert "fab" not in page


# ----------------------------------------------------------------------------- threads


def test_dayforce_resolution_works_through_the_route(env, monkeypatch):
    """The real `probe_dayforce` calls `asyncio.run`, which raises inside a running loop. The
    route runs it in a worker thread, so it works."""

    class Resp:
        text = "https://jobs.dayforcehcm.com/nwcu/CANDIDATEPORTAL/jobs/9"

    async def count(ref):
        return 7

    monkeypatch.setattr(resolve, "probe_dayforce", REAL_PROBE_DAYFORCE)
    monkeypatch.setattr(resolve.httpx, "get", lambda *a, **k: Resp())
    monkeypatch.setattr(resolve, "count_postings", count)
    page = env.find()
    assert "dayforce" in page and "nwcu/CANDIDATEPORTAL" in page and "7 postings" in page


def test_the_cockpit_answers_other_requests_while_a_search_runs(env, monkeypatch):
    started, release = threading.Event(), threading.Event()
    safety = threading.Timer(10, release.set)  # never hang the suite if this test fails
    safety.start()

    def slow(name):
        started.set()
        release.wait()
        return [BoardHit(COMPANY, "lever", "northwind", 2)]

    monkeypatch.setattr(resolve, "probe_company", slow)
    env.state["fake"]["samples"]["lever"] = (SAMPLES, "")
    try:
        r = env.client.post(f"/targets/{PATH}/boards/find", headers=TOK, follow_redirects=False)
        assert r.status_code == 303
        assert started.wait(5)
        # The search is blocked in its thread; the event loop is free.
        assert env.client.get("/targets").status_code == 200
        busy = env.client.get(f"/targets/{PATH}/boards").text
        assert "hx-trigger" in busy and "Searching" in busy
        release.set()
        assert "lever" in env.wait()
    finally:
        release.set()
        safety.cancel()


def test_a_failed_search_is_reported_not_lost(env, monkeypatch):
    def boom(name):
        raise RuntimeError("down")

    monkeypatch.setattr(resolve, "probe_company", boom)
    page = env.find()
    assert "No supported board found" in page
    assert "failed" in page


# ----------------------------------------------------------------------------- settings form


def test_saving_the_settings_form_keeps_each_boards_identity_basis():
    cfg = BlastJobConfig()
    cfg.discovery.target_boards = [
        TargetBoard(
            company=COMPANY, ats="greenhouse", token="northwind", identity_basis="ats_name"
        ),
        TargetBoard(company="Fabrikam", ats="lever", token="fab"),
    ]
    form = {
        "_sections": "search",
        "target_boards": f"{COMPANY}, greenhouse, northwind\nFabrikam, lever, fab\n"
        "Contoso, ashby, contoso",
    }
    new = sf.apply_form(cfg, form)
    got = {(b.company, b.ats): b.identity_basis for b in new.discovery.target_boards}
    assert got == {
        (COMPANY, "greenhouse"): "ats_name",
        ("Fabrikam", "lever"): "",
        ("Contoso", "ashby"): "",
    }


def test_renaming_a_board_line_drops_its_basis():
    cfg = BlastJobConfig()
    cfg.discovery.target_boards = [
        TargetBoard(company=COMPANY, ats="greenhouse", token="northwind", identity_basis="ats_name")
    ]
    new = sf.apply_form(
        cfg, {"_sections": "search", "target_boards": "Fabrikam, greenhouse, northwind"}
    )
    assert new.discovery.target_boards[0].identity_basis == ""


# ----------------------------------------------------------------------------- review fixes


def test_findings_older_than_the_max_age_are_gone_at_confirm(env, monkeypatch):
    _gh(env, careers="https://boards.greenhouse.io/northwind")
    env.find()
    later = time.monotonic() + routes_boards.FINDINGS_MAX_AGE_S + 1
    monkeypatch.setattr(routes_boards, "_now", lambda: later)
    r = env.use("greenhouse", "northwind")
    assert r.headers["location"].endswith("notice=no_findings")
    assert env.boards == []
    assert "greenhouse" not in env.client.get(f"/targets/{PATH}/boards").text


def test_a_second_search_is_refused_while_one_is_running(env, monkeypatch):
    ts.add_targets(env.conn, [ts.TargetInput(name="Contoso")], "dream")
    other = quote(company_key("Contoso"), safe="")
    started, release = threading.Event(), threading.Event()
    safety = threading.Timer(10, release.set)
    safety.start()

    def slow(name):
        started.set()
        release.wait()
        return []

    monkeypatch.setattr(resolve, "probe_company", slow)
    try:
        env.client.post(f"/targets/{PATH}/boards/find", headers=TOK, follow_redirects=False)
        assert started.wait(5)
        r = env.client.post(f"/targets/{other}/boards/find", headers=TOK, follow_redirects=False)
        assert r.headers["location"].endswith("notice=busy")
        page = env.client.get(f"/targets/{other}/boards?notice=busy").text
        assert "search is already running" in page
        assert (
            routes_boards.RUNTIME.running_keyed(routes_boards.RUN_NAME, company_key("Contoso"))
            is None
        )
    finally:
        release.set()
        safety.cancel()
    env.wait()


def test_a_board_already_listed_under_another_company_names_it(env):
    _gh(env, careers="https://boards.greenhouse.io/northwind")
    env.state["cfg"].discovery.target_boards = [
        TargetBoard(company="Fabrikam", ats="greenhouse", token="northwind")
    ]
    env.find()
    r = env.use("greenhouse", "northwind")
    assert "notice=exists_other" in r.headers["location"]
    assert "Fabrikam" in r.headers["location"]
    page = env.client.get(
        r.headers["location"].replace(f"/targets/{PATH}", f"/targets/{PATH}/boards")
    ).text
    assert "already on your list under Fabrikam" in page
    assert len(env.boards) == 1


# ----------------------------------------------------------------------------- real store


def test_adding_a_board_round_trips_through_the_real_settings_store(tmp_path):
    """The real revision check and the identity_basis round trip, not a fake `save`."""
    from blastjob import settings_store as store

    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path)
    conn = dbc.init_db(cfg)
    store.save(conn, cfg)

    def load() -> BlastJobConfig:
        return store.load(conn, tmp_path).cfg

    def save(c: BlastJobConfig) -> None:
        store.save(conn, c)

    first = bf.Candidate(COMPANY, "greenhouse", "northwind", 3)
    second = bf.Candidate(COMPANY, "lever", "northwind", 4)
    assert routes_boards.add_board(load, save, COMPANY, first, "domain_link") == "added"
    assert [(b.ats, b.identity_basis) for b in load().discovery.target_boards] == [
        ("greenhouse", "domain_link")
    ]

    def racing_load() -> BlastJobConfig:
        loaded = load()
        store.set_value(conn, "discovery.comp_floor_usd", 123456)  # another edit lands
        return loaded

    assert routes_boards.add_board(racing_load, save, COMPANY, second, "owner_confirmed") == "stale"
    assert len(load().discovery.target_boards) == 1
    assert routes_boards.add_board(load, save, COMPANY, second, "owner_confirmed") == "added"
    assert [(b.ats, b.identity_basis) for b in load().discovery.target_boards] == [
        ("greenhouse", "domain_link"),
        ("lever", "owner_confirmed"),
    ]
    assert load().discovery.comp_floor_usd == 123456  # the other edit survived
    conn.close()

"""The Research button reads third-party pages and checks the parent (issue #107).

Invented company: Acme Castings (Racine, Wisconsin), owned by Example Holdings.
No network: search, read and the model are injected fakes.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile

import pytest

from blastjob.core import packet_research, research_match
from blastjob.core.packet_research import FetchedPage
from blastjob.core.research_match import JobContext
from blastjob.models.config import BlastJobConfig
from blastjob.web import research, research_parent

NAME = "Acme Castings"
DOMAIN = "acmecastings.example.com"
PARENT = "Example Holdings"
CTX = JobContext(company=NAME, title="VP IT", city="Racine", region="WI")

OWN_SPAN = "Acme Castings is an Example Holdings company based in Racine, Wisconsin."
OWN_TEXT = f"{OWN_SPAN} Founded in 1950."
PRESS_URL = "https://newswire.example.org/acme-castings-expands"
PRESS_TEXT = (
    "Acme Castings of Racine, Wisconsin announced a new plant. Visit acmecastings.example.com."
)
NEWS_URL = "https://newswire.example.org/example-holdings-recap"
PARENT_SPAN = "Jordan Vale was named CEO of Example Holdings in March 2026."
NEWS_TEXT = f"Example Holdings completed a recapitalization of its portfolio. {PARENT_SPAN}"
PARENT_PREFIX = "Parent ("


def _anchors() -> research_match.Anchors:
    cfg = BlastJobConfig()
    cfg.discovery.employer_domains = {NAME: [DOMAIN]}
    return research_match.build_anchors(CTX, cfg.discovery)


def _r(url: str, title: str = "", desc: str = "") -> dict:
    return {"title": title, "url": url, "description": desc}


def _parent_events(profile) -> list[dict]:
    return [e for e in profile.recent_events if e["event"].startswith(PARENT_PREFIX)]


# ---- the page pick -------------------------------------------------------------


def test_pick_takes_two_own_pages_then_third_party_news_first_then_fills_with_own():
    results = {f"https://{DOMAIN}/page{i}": _r(f"https://{DOMAIN}/page{i}") for i in range(6)}
    blog = "https://blog.example.net/acme-castings-profile"
    news_a = "https://newswire.example.org/acme-castings-a"
    news_b = "https://newswire.example.org/acme-castings-b"
    for u in (blog, news_a, news_b):
        results[u] = _r(u, "Acme Castings", "Acme Castings did a thing")

    picked = [p["url"] for p in research.pick_research_pages(results, _anchors(), NAME)]

    assert len(picked) == research.TOTAL_PAGES == 6
    assert picked[:2] == [f"https://{DOMAIN}/page0", f"https://{DOMAIN}/page1"]
    assert picked[2:5] == [news_a, news_b, blog]
    assert picked[5].startswith(f"https://{DOMAIN}/")


def test_pick_excludes_review_hosts_linkedin_and_unnamed_third_party():
    results = {
        u: _r(u, "Acme Castings reviews", "Acme Castings")
        for u in (
            "https://www.glassdoor.com/Reviews/acme-castings",
            "https://www.indeed.com/cmp/acme-castings",
            "https://www.comparably.com/companies/acme-castings",
            "https://www.linkedin.com/company/acme-castings",
        )
    }
    results["https://other.example.net/p"] = _r("https://other.example.net/p", "Other", "Nope")

    picked = [p["url"] for p in research.pick_research_pages(results, _anchors(), NAME)]

    assert not any(
        h in u for u in picked for h in ("glassdoor", "indeed", "comparably", "linkedin")
    )


def test_pick_dedupes_by_host_and_path():
    a = f"https://{DOMAIN}/about"
    results = {a: _r(a), a + "?utm=1": _r(a + "?utm=1")}

    assert len(research.pick_research_pages(results, _anchors(), NAME)) == 1


def test_packet_research_pick_is_unchanged():
    results = {f"https://{DOMAIN}/p{i}": _r(f"https://{DOMAIN}/p{i}") for i in range(6)}
    results[PRESS_URL] = _r(PRESS_URL, "Acme Castings", "Acme Castings")

    picked = packet_research.pick_own_first(results, _anchors())

    assert len(picked) == research.MAX_PAGES
    assert all(DOMAIN in p["url"] for p in picked)


# ---- the domain identity path -------------------------------------------------


def test_domain_token_admits_a_page_with_no_location():
    text = "Acme Castings opened a plant. See www.acmecastings.example.com for details."
    a = _anchors()

    assert not research_match.page_matches("https://n.example.net/x", text, NAME, a)
    assert research_match.page_matches("https://n.example.net/x", text, NAME, a, domain_token=True)


@pytest.mark.parametrize(
    "text",
    [
        "Acme Castings opened a plant in Dayton.",
        "Acme Castings opened a plant. See notacmecastings.example.com.",
        "Acme Castings opened a plant. See acmecastings.example.com.attacker.example/x",
        "A plant opened. See acmecastings.example.com for details.",
    ],
)
def test_domain_token_path_rejects_adversaries(text):
    assert not research_match.page_matches(
        "https://n.example.net/x", text, NAME, _anchors(), domain_token=True
    )


# ---- the whole run -----------------------------------------------------------


class _Stream:
    def __init__(self, text: str) -> None:
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None

    @property
    def text_stream(self):
        async def _iter():
            yield self._text

        return _iter()

    async def get_final_message(self):
        return type("Final", (), {"usage": None})()


class _World:
    def __init__(self, monkeypatch, *, pages, parent_name=PARENT, parent_span=OWN_SPAN, **fail):
        self.pages = pages
        self.fail = fail
        self.read_urls: list[str] = []
        self.parent_queries: list[str] = []
        self.parent_calls = 0
        self.parent_reply = self.reply(PARENT_SPAN)
        self.profile = json.dumps(
            {
                "company": NAME,
                "hq": "Racine, Wisconsin",
                "founded": 1950,
                "summary": "A Wisconsin foundry.",
                "parent_name": parent_name,
                "parent_span": parent_span,
                "recent_events": [{"date": "2026-01", "event": "New plant", "source": PRESS_URL}],
                "sources": [{"label": "own", "url": f"https://{DOMAIN}/about"}],
                "value_sources": {"hq": f"https://{DOMAIN}/about"},
            }
        )

        async def search(query, api_key):
            if "recapitalization" in query:
                self.parent_queries.append(query)
                if self.fail.get("search"):
                    raise RuntimeError("search down")
                return [
                    _r(NEWS_URL, "Recap news", f"{PARENT} recap"),
                    _r("https://x.example.net/none", "Unrelated", "Nothing"),
                ]
            return [_r(u, f"{NAME} page", f"{NAME} snippet") for u in self.pages if u != NEWS_URL]

        async def read(_client, url, api_key):
            self.read_urls.append(url)
            if url == NEWS_URL and self.fail.get("read"):
                raise RuntimeError("reader down")
            return self.pages.get(url, "")

        async def stream(_cfg, system, messages, max_tokens=4096):
            blob = json.dumps(system, default=str)
            if "OWNERSHIP NEWS" in blob:
                self.parent_calls += 1
                if self.fail.get("model"):
                    raise RuntimeError("model down")
                return _Stream("not json" if self.fail.get("parse") else self.parent_reply)
            if "SAME" in blob:
                return _Stream("[]")
            return _Stream(self.profile)

        monkeypatch.setenv("JINA_API_KEY", "test-key")
        monkeypatch.setattr(research, "jina_search", search)
        monkeypatch.setattr(research, "read_page", read)
        monkeypatch.setattr(research, "make_stream", stream)
        monkeypatch.setattr("blastjob.core.packet_research.make_stream", stream)

    @staticmethod
    def reply(span: str, polarity: str = "affirmed", date: str = "March 2026") -> str:
        return json.dumps(
            [{"url": NEWS_URL, "source_span": span, "polarity": polarity, "date": date}]
        )


def _pages(**over) -> dict[str, str]:
    pages = {f"https://{DOMAIN}/about": OWN_TEXT, PRESS_URL: PRESS_TEXT, NEWS_URL: NEWS_TEXT}
    pages.update(over)
    return pages


@pytest.fixture
def cfg():
    c = BlastJobConfig()
    c.paths.data_dir = tempfile.mkdtemp()
    c.discovery.employer_domains = {NAME: [DOMAIN]}
    return c


async def _run(cfg, emit=None):
    return await research.research_company(
        cfg, NAME, contexts=[CTX], emit=emit or (lambda *_a: None)
    )


async def test_press_releases_are_read_and_an_event_citing_one_survives(monkeypatch, cfg):
    pages = {f"https://{DOMAIN}/p{i}": OWN_TEXT for i in range(6)}
    pages[PRESS_URL] = PRESS_TEXT
    world = _World(monkeypatch, pages=pages, parent_name="", parent_span="")

    profile = await _run(cfg)

    assert PRESS_URL in world.read_urls
    assert profile is not None
    assert any(e["source"] == PRESS_URL for e in profile.recent_events)


async def test_a_no_location_article_with_the_employer_domain_is_admitted(monkeypatch, cfg):
    good = "https://newswire.example.org/acme-castings-no-place"
    adversary = "https://newswire.example.org/acme-castings-other"
    pages = {
        good: "Acme Castings opened a plant. See acmecastings.example.com.",
        adversary: "Acme Castings opened a plant in Ohio.",
    }
    seen: list[str] = []
    _World(monkeypatch, pages=pages, parent_name="", parent_span="")
    inner = research.make_stream

    async def spy(cfg_, system, messages, max_tokens=4096):
        seen.append(json.dumps(messages, default=str))
        return await inner(cfg_, system, messages, max_tokens=max_tokens)

    monkeypatch.setattr(research, "make_stream", spy)

    await _run(cfg)

    sent = "\n".join(seen)
    assert good in sent
    assert "in Ohio" not in sent


async def test_the_parent_pass_runs_when_the_span_verifies(monkeypatch, cfg):
    world = _World(monkeypatch, pages=_pages())

    profile = await _run(cfg)

    assert world.parent_calls == 1
    assert profile is not None
    events = _parent_events(profile)
    assert len(events) == 1
    assert events[0]["event"] == f"Parent ({PARENT}): {PARENT_SPAN}"
    assert events[0]["source"] == NEWS_URL
    assert events[0]["date"] == "March 2026"
    assert NEWS_URL in {s["url"] for s in profile.sources}
    assert "recapitalization" in world.parent_queries[0]
    assert len(world.parent_queries) == 1


async def test_no_pass_when_the_parent_is_on_the_page_without_ownership_language(monkeypatch, cfg):
    span = "Acme Castings thanks Example Holdings for a fine lunch in Racine, Wisconsin."
    world = _World(monkeypatch, pages=_pages(**{f"https://{DOMAIN}/about": span}), parent_span=span)

    await _run(cfg)

    assert world.parent_calls == 0
    assert world.parent_queries == []


async def test_no_pass_when_the_parent_name_is_not_in_the_span(monkeypatch, cfg):
    world = _World(monkeypatch, pages=_pages(), parent_name="Other Holdings")

    await _run(cfg)

    assert world.parent_calls == 0
    assert world.parent_queries == []


async def test_no_pass_when_the_span_is_not_on_an_own_domain_page(monkeypatch, cfg):
    span = "Acme Castings is a wholly invented Example Holdings company in Racine."
    world = _World(monkeypatch, pages=_pages(), parent_span=span)

    await _run(cfg)

    assert world.parent_calls == 0


async def test_no_pass_when_the_span_sits_only_on_a_third_party_page(monkeypatch, cfg):
    span = "Acme Castings is an Example Holdings company, the release said."
    pages = _pages(**{PRESS_URL: PRESS_TEXT + " " + span})
    world = _World(monkeypatch, pages=pages, parent_span=span)

    await _run(cfg)

    assert world.parent_calls == 0


async def test_no_pass_when_the_parent_is_the_company(monkeypatch, cfg):
    span = "Acme Castings is an Acme Castings company based in Racine, Wisconsin."
    world = _World(
        monkeypatch,
        pages=_pages(**{f"https://{DOMAIN}/about": span}),
        parent_name=NAME,
        parent_span=span,
    )

    await _run(cfg)

    assert world.parent_calls == 0


async def test_an_unverifiable_event_span_is_dropped(monkeypatch, cfg):
    world = _World(monkeypatch, pages=_pages())
    world.parent_reply = world.reply("Example Holdings was acquired by a giant for a billion.")

    profile = await _run(cfg)

    assert profile is not None
    assert _parent_events(profile) == []


@pytest.mark.parametrize("polarity", ["considered", "abandoned", "proposed", "unknown", ""])
async def test_a_non_affirmed_event_is_dropped(monkeypatch, cfg, polarity):
    world = _World(monkeypatch, pages=_pages())
    world.parent_reply = world.reply(PARENT_SPAN, polarity)

    profile = await _run(cfg)

    assert profile is not None
    assert _parent_events(profile) == []


async def test_a_date_is_blank_unless_it_appears_verbatim(monkeypatch, cfg):
    world = _World(monkeypatch, pages=_pages())
    world.parent_reply = world.reply(PARENT_SPAN, date="2019-05-05")

    profile = await _run(cfg)

    assert profile is not None
    events = _parent_events(profile)
    assert len(events) == 1
    assert events[0]["date"] == ""


@pytest.mark.parametrize("failure", ["search", "read", "model", "parse"])
async def test_a_parent_pass_failure_still_returns_the_profile(monkeypatch, cfg, failure):
    _World(monkeypatch, pages=_pages(), **{failure: True})
    logs: list[str] = []

    profile = await _run(cfg, emit=lambda kind, data: logs.append(str(data)))

    assert profile is not None
    assert profile.hq == "Racine, Wisconsin"
    assert _parent_events(profile) == []
    assert any(m.startswith("Parent check skipped") for m in logs)


# ---- review hardening (issue #107) ---------------------------------------------


def _own(text: str) -> FetchedPage:
    return FetchedPage(f"https://{DOMAIN}/about", "About", text, text)


def test_the_research_prompt_asks_for_the_parent_fields():
    assert '"parent_name"' in research.RESEARCH_SYSTEM
    assert '"parent_span"' in research.RESEARCH_SYSTEM
    assert "ONE sentence" in research.RESEARCH_SYSTEM


@pytest.mark.parametrize(
    "span",
    [
        "Acme Castings is proud to be part of the Example Holdings Chamber of Commerce.",
        "Our customer Northwind Supply, a subsidiary of Example Holdings, buys steel.",
        "Acme Castings is an Example Holdings company that may be sold, reportedly.",
        "Acme Castings is an Example Holdings company " + "x" * 300 + ".",
    ],
)
def test_a_weak_ownership_span_is_not_a_parent(span):
    got = research_parent.verified_parent(NAME, PARENT, span, [_own(span)])

    assert got is None


def test_a_good_ownership_span_is_a_parent():
    assert research_parent.verified_parent(NAME, PARENT, OWN_SPAN, [_own(OWN_SPAN)]) is not None


@pytest.mark.parametrize("name", ["Holdings", "Example", "  ", "---"])
def test_a_one_word_parent_name_is_too_generic(name):
    span = f"Acme Castings is a {name} company in Racine, Wisconsin."

    assert research_parent.verified_parent(NAME, name, span, [_own(span)]) is None


async def test_a_generic_parent_logs_and_skips(monkeypatch, cfg):
    span = "Acme Castings is a Holdings company in Racine, Wisconsin."
    world = _World(
        monkeypatch,
        pages=_pages(**{f"https://{DOMAIN}/about": span}),
        parent_name="Holdings",
        parent_span=span,
    )
    logs: list[str] = []

    await _run(cfg, emit=lambda kind, data: logs.append(str(data)))

    assert world.parent_calls == 0
    assert "Parent check skipped: parent name too generic to search." in logs


def test_the_parent_query_carries_only_alphanumeric_words():
    q = research_parent.parent_query(
        'Example "Holdings" OR site:evil.example', dt.date(2026, 10, 1)
    )

    assert q.startswith('"Example Holdings OR site evil example" ')
    assert q.count('"') == 2


def _event(span: str, date: str, page_text: str) -> tuple:
    page = FetchedPage(NEWS_URL, "n", page_text, page_text)
    reply = json.dumps(
        [{"url": NEWS_URL, "source_span": span, "polarity": "affirmed", "date": date}]
    )
    events, _ = research_parent.parse_events(reply, PARENT, [page])
    return events


def test_a_copyright_year_far_from_the_span_is_not_attached():
    text = PARENT_SPAN + (" filler" * 100) + " Copyright 2019 Example Holdings."

    assert _event(PARENT_SPAN, "2019", text)[0]["date"] == ""


def test_a_date_near_the_span_is_attached():
    text = "Published 2026-03-04. " + PARENT_SPAN + " More."

    assert _event(PARENT_SPAN, "2026-03-04", text)[0]["date"] == "2026-03-04"


@pytest.mark.parametrize("date", ["soon", "Mar", "26"])
def test_a_date_without_a_digit_or_too_short_is_blank(date):
    text = f"{date} " + PARENT_SPAN

    assert _event(PARENT_SPAN, date, text)[0]["date"] == ""


@pytest.mark.parametrize(
    "span",
    [
        "Example Holdings is reportedly exploring a sale of its portfolio.",
        "Example Holdings could name a new CEO soon, the release said.",
        "Example Holdings is in talks to be acquired by a rival group.",
        "Example Holdings plans to appoint a new CEO in March 2026.",
        "Example Holdings " + "very " * 80 + "named a new CEO.",
    ],
)
def test_a_hedged_or_overlong_event_span_is_rejected(span):
    assert _event(span, "", span) == ()


def test_may_hedges_only_as_a_whole_word():
    clean = "Example Holdings named Mayfield Dunn CEO on 4 March 2026, the release said."

    assert _event(clean, "", clean) != ()


def test_may_the_month_is_not_a_hedge_but_may_the_verb_is():
    month = "Example Holdings acquired Acme Castings in May 2025, the release said."
    verb = "Example Holdings may sell Acme Castings next year, the release said."

    assert _event(month, "", month) != ()
    assert _event(verb, "", verb) == ()


def test_the_domain_far_from_the_name_is_not_admitted():
    far = "Acme Castings opened a plant." + (" filler" * 100) + " See acmecastings.example.com."

    assert not research_match.page_matches(
        "https://n.example.net/x", far, NAME, _anchors(), domain_token=True
    )


def test_the_domain_next_to_the_name_is_admitted():
    near = "Acme Castings (acmecastings.example.com) opened a plant." + (" filler" * 100)

    assert research_match.page_matches(
        "https://n.example.net/x", near, NAME, _anchors(), domain_token=True
    )


def test_names_domain_is_fast_on_hostile_input():
    text = ("a." * 50000) + "x"

    assert not research_match.names_domain(text, frozenset({DOMAIN}))

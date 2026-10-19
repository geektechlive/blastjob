"""Company research never reads or cites LinkedIn pages (issue #66).

LinkedIn is read only where a feature needs it (alert guest pages, people search).
Packet research and the Research button pick pages from Jina results and must drop
LinkedIn hosts (and subdomains), while a host that merely contains the word stays.
No network: search, read and the model are injected fakes.
"""

from __future__ import annotations

import json

import pytest

from blastjob.core import jina
from blastjob.core import packet_research as pr
from blastjob.core.research_match import JobContext
from blastjob.llm.cost import CostTracker
from blastjob.models.config import BlastJobConfig
from blastjob.web import research

ABOUT = "https://northwind.example.com/about"
LOOKALIKE = "https://notlinkedin.example/northwind"
LINKEDIN_URLS = [
    "https://www.linkedin.com/company/northwind-fabrication",
    "https://LinkedIn.com/company/northwind-fabrication/about",
    "https://uk.linkedin.com/company/northwind-fabrication",
    "https://lnkd.in/abc123",
]
PAGE_TEXT = "Northwind Fabrication was founded in 1951 and is headquartered in Fairhaven, Ohio."


def _result(url: str) -> dict:
    return {"title": "t", "url": url, "description": "d"}


class _Usage:
    input_tokens = 10
    output_tokens = 5
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0


class _Final:
    usage = _Usage()


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
        return _Final()


def test_pick_pages_drops_linkedin_hosts_and_keeps_lookalikes_and_order():
    results = [_result(u) for u in LINKEDIN_URLS] + [
        _result("https://random.blog/post"),
        _result(LOOKALIKE),
        _result("https://acme.com/about-us/"),
    ]

    picked = [p["url"] for p in jina.pick_pages(results)]

    assert picked == ["https://acme.com/about-us/", "https://random.blog/post", LOOKALIKE]


async def test_packet_research_never_reads_or_cites_a_linkedin_page(monkeypatch):
    reads: list[str] = []
    searched = [_result(u) for u in LINKEDIN_URLS] + [_result(ABOUT), _result(LOOKALIKE)]

    async def search(query, api_key):
        return list(searched)

    async def read(url, api_key):
        reads.append(url)
        return PAGE_TEXT

    cited = LINKEDIN_URLS[0]
    reply = json.dumps(
        {
            "facts": [],
            "values": {"founded": {"value": 1951, "source_url": cited}},
        }
    )

    async def fake_stream(*_a, **_kw):
        return _Stream(reply)

    monkeypatch.setattr(pr, "make_stream", fake_stream)

    attempt = await pr.research_from_pages(
        BlastJobConfig(),
        JobContext(company="Northwind Fabrication", title="VP IT", city="", region=""),
        CostTracker(),
        api_key="test-key",
        search=search,
        read=read,
    )

    assert sorted(reads) == sorted([ABOUT, LOOKALIKE])
    assert attempt.cited is None or cited not in json.dumps(
        attempt.cited, default=lambda o: getattr(o, "__dict__", str(o))
    )


async def test_research_button_never_reads_or_cites_a_linkedin_page(monkeypatch):
    reads: list[str] = []
    prompts: list[str] = []
    searched = [_result(u) for u in LINKEDIN_URLS] + [_result(ABOUT), _result(LOOKALIKE)]

    async def fake_search(query, api_key):
        return list(searched)

    async def fake_read(_client, url, api_key):
        reads.append(url)
        return PAGE_TEXT

    reply = json.dumps(
        {
            "company": "Northwind Fabrication",
            "sources": [
                {"label": "LinkedIn", "url": LINKEDIN_URLS[0]},
                {"label": "A", "url": ABOUT},
            ],
        }
    )

    async def fake_stream(_cfg, _system, messages, max_tokens=4096):
        prompts.append(json.dumps(messages, default=str))
        return _Stream(reply)

    monkeypatch.setenv("JINA_API_KEY", "test-key")
    monkeypatch.setattr(research, "jina_search", fake_search)
    monkeypatch.setattr(research, "read_page", fake_read)
    monkeypatch.setattr(research, "make_stream", fake_stream)

    profile = await research.research_company(
        BlastJobConfig(),
        "Northwind Fabrication",
        contexts=[JobContext("Northwind Fabrication", city="Fairhaven", region="OH")],
    )

    assert sorted(reads) == sorted([ABOUT, LOOKALIKE])
    assert all("linkedin.com" not in p and "lnkd.in" not in p for p in prompts)
    assert profile is not None
    assert [s["url"] for s in profile.sources] == [ABOUT]


@pytest.mark.parametrize("url", LINKEDIN_URLS)
def test_is_linkedin_url_matches_hosts_and_subdomains(url):
    assert jina.is_linkedin_url(url)


@pytest.mark.parametrize("url", [LOOKALIKE, "https://example.com/?u=linkedin.com", ABOUT])
def test_is_linkedin_url_ignores_substrings(url):
    assert not jina.is_linkedin_url(url)

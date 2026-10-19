"""Owner decisions on #59 (2026-10-19): the letter gets the SOURCE SENTENCE, not
the model's statement, and every value and fact carries its source host and the
page's date (or "undated", never guessed). No network."""

from __future__ import annotations

import json

from blastjob.core import cover_letter_score as cls
from blastjob.core import packet_research as pr
from blastjob.core import page_dates
from blastjob.core import research_facts as rf
from blastjob.core.research_facts import CompanyFact, Polarity, Provenance
from blastjob.core.research_match import JobContext
from blastjob.llm.cost import CostTracker
from blastjob.models.config import BlastJobConfig
from tests.test_packet_research import _Stream

OFFICES = "https://www2.northwind.example.com/leadership-team"
OFFICES_TEXT = (
    "Northwind Fabrication offices. Lakeview, WI: 2 Wisconsin Circle, Suite 1180. "
    "The company employs 200 people."
)
NEWS = "https://news.example.org/2013/12/19/northwind-acquires-widgetco"
NEWS_TEXT = (
    "Northwind Fabrication, based in Lakeview, WI, acquired WidgetCo, adding 1,500 customers."
)
CTX = JobContext(company="Northwind Fabrication", title="VP IT", city="Lakeview", region="WI")


# --- dates ------------------------------------------------------------------------


def test_date_from_a_url_path():
    assert page_dates.page_date("https://x.example.org/2013/12/19/story", "") == "2013-12-19"
    assert page_dates.page_date("https://x.example.org/news/2024-03-05-story", "") == "2024-03-05"


def test_date_from_a_meta_tag_or_schema_in_the_page():
    meta = '<meta property="article:published_time" content="2021-06-02T09:00:00Z">'
    assert page_dates.page_date("https://x.example.com/a", meta) == "2021-06-02"
    schema = '{"@type": "NewsArticle", "datePublished": "2019-11-30T08:00:00-05:00"}'
    assert page_dates.page_date("https://x.example.com/a", schema) == "2019-11-30"


def test_date_from_the_reader_metadata_and_published_time():
    assert (
        page_dates.page_date("https://x.example.com/a", "", published="2013-12-19T15:01:16-0500")
        == "2013-12-19"
    )
    meta = {"article:published_time": "2022-01-07T10:00:00Z"}
    assert page_dates.page_date("https://x.example.com/a", "", metadata=meta) == "2022-01-07"
    assert (
        page_dates.page_date("https://x.example.com/a", "Published Time: 2020-02-02T00:00:00Z\n")
        == "2020-02-02"
    )


def test_date_from_a_dateline_at_the_top():
    text = "LAKEVIEW, Wis., March 4, 2025 /PRNewswire/ -- Northwind Fabrication today announced"
    assert page_dates.page_date("https://x.example.com/a", text) == "2025-03-04"


def test_no_date_is_undated_never_guessed():
    text = "Northwind Fabrication employs 200 people. Call 555-0100. Suite 1180."
    assert page_dates.page_date("https://x.example.com/about", text) == ""
    assert page_dates.page_date("https://x.example.com/2013/13/45/x", "") == ""  # not a date


# --- the letter gets spans, never statements --------------------------------------


def _fact(statement: str, span: str, **kw) -> CompanyFact:
    return CompanyFact(
        statement=statement,
        entity=kw.pop("entity", "offices"),
        polarity=Polarity.AFFIRMED,
        source_span=span,
        confidence=0.9,
        **kw,
    )


def test_letter_block_renders_the_span_and_host_never_the_statement():
    fact = _fact(
        "Northwind lists an office in Chevy Chase, MD.",
        "2 Wisconsin Circle, Suite 1180",
        entity="Chevy Chase office",
        source_url=OFFICES,
        source_date="",
        provenance=Provenance.WEB_SEARCH,
    )

    block = rf.facts_block([fact])

    assert "Chevy Chase" not in block
    assert "2 Wisconsin Circle, Suite 1180" in block
    assert "www2.northwind.example.com, undated" in block


def test_letter_block_carries_the_dated_figure_instruction_and_values():
    fact = _fact(
        "Acquired WidgetCo.",
        "Northwind Fabrication acquired WidgetCo",
        source_url=NEWS,
        source_date="2013-12-19",
        provenance=Provenance.WEB_SEARCH,
    )

    block = rf.facts_block([fact], values=["Employees: 200 (www2.northwind.example.com, undated)"])

    assert "news.example.org, 2013-12-19" in block
    assert "Employees: 200 (www2.northwind.example.com, undated)" in block
    assert '"as of <date>"' in block
    assert '"over 2 years old"' in block


def test_the_model_cannot_set_a_fact_date():
    sidecar = "Northwind Fabrication acquired WidgetCo in a deal announced Tuesday."
    reply = json.dumps(
        [
            {
                "statement": "Acquired WidgetCo.",
                "source_span": "Northwind Fabrication acquired WidgetCo",
                "source_date": "2026-01-01",
                "polarity": "affirmed",
            }
        ]
    )
    facts = rf.parse_facts(reply, sanitized=sidecar, original=sidecar)
    assert [f.source_date for f in facts] == [""]


def test_scorer_support_compares_against_the_span_not_the_statement():
    fact = _fact("Northwind has an office in Chevy Chase, MD.", "2 Wisconsin Circle, Suite 1180")
    assert rf.supports(fact, "Northwind has an office in Chevy Chase") is False
    span_fact = _fact("unrelated words", "Northwind serves aerospace and defense customers")
    assert rf.supports(span_fact, "Northwind serves aerospace customers") is True


# --- end to end through packet research -------------------------------------------


class _Web:
    def __init__(self, pages: dict[str, object]) -> None:
        self.pages = pages

    async def search(self, query: str, api_key: str) -> list[dict]:
        return [{"title": f"p{i}", "url": u, "description": ""} for i, u in enumerate(self.pages)]

    async def read(self, url: str, api_key: str):
        return self.pages.get(url, "")


async def test_values_and_facts_carry_host_and_date_into_sidecar_and_letter(monkeypatch):
    reply = json.dumps(
        {
            "facts": [
                {
                    "statement": "Northwind lists an office in Chevy Chase, MD.",
                    "source_url": OFFICES,
                    "source_span": "2 Wisconsin Circle, Suite 1180",
                    "polarity": "affirmed",
                    "confidence": 0.9,
                },
                {
                    "statement": "Northwind acquired WidgetCo.",
                    "source_url": NEWS,
                    "source_span": "acquired WidgetCo, adding 1,500 customers",
                    "polarity": "affirmed",
                    "confidence": 0.9,
                },
            ],
            "values": {"employees": {"value": 200, "source_url": OFFICES}},
        }
    )

    async def fake_stream(*_a, **_kw):
        return _Stream(reply)

    monkeypatch.setattr(pr, "make_stream", fake_stream)
    web = _Web({OFFICES: OFFICES_TEXT, NEWS: pr.PageRead(NEWS_TEXT, published="")})

    got = await pr.research_from_pages(
        BlastJobConfig(), CTX, CostTracker(), api_key="k", search=web.search, read=web.read
    )

    assert got.cited is not None
    cited = got.cited
    assert [f.source_date for f in cited.facts] == ["", "2013-12-19"]
    lines = pr.value_lines(cited)
    assert lines == ["Employees: 200 (www2.northwind.example.com, undated)"]
    assert "Employees: 200 (www2.northwind.example.com, undated)" in cited.research_md
    assert "2013-12-19" in cited.research_md
    assert "Chevy Chase" not in cited.research_md
    letter = rf.facts_block(list(cited.facts), values=lines)
    assert "Chevy Chase" not in letter
    assert "2 Wisconsin Circle, Suite 1180" in letter


async def test_downgrade_still_sees_the_statement(monkeypatch):
    """Downgrading is the conservative direction: an abandoned fact still catches a
    claim on its statement's topic even when its span is terse."""
    fact = CompanyFact(
        statement="Northwind considered aerospace and defense but abandoned it",
        polarity=Polarity.ABANDONED,
        source_span="the aerospace initiative was wound down in 2019",
        confidence=0.9,
    )
    assert cls._on_topic_any_polarity("Northwind serves aerospace and defense.", fact)

"""The company news query takes its year from the current date, not a literal."""

from __future__ import annotations

from datetime import date

from blastjob.core.jina import company_search_queries


def test_news_query_uses_the_injected_year() -> None:
    queries = company_search_queries("Acme", today=date(2027, 3, 1))

    assert '"Acme" news 2027' in queries
    assert not any("2026" in q for q in queries)


def test_news_query_defaults_to_the_current_year() -> None:
    queries = company_search_queries("Acme")

    assert f'"Acme" news {date.today().year}' in queries

"""Round 2, R9: currency comes from the marker beside the amount, not from anywhere
in the text, and the dollar-sign currencies other than USD are not USD."""

from __future__ import annotations

import pytest

from blastjob.comp import detect_currency, parse_salary_range


@pytest.mark.parametrize(
    ("text", "currency"),
    [
        ("NZ$150,000 - NZ$190,000", "NZD"),
        ("S$150,000", "SGD"),
        ("HK$1,200,000", "HKD"),
        ("R$300,000", "BRL"),
        ("US$150,000", "USD"),
        ("1,200,000 kr", "KR"),
        ("kr 1,200,000", "KR"),
        ("150,000 - 190,000 SEK", "SEK"),
        ("CAD 150,000 - 190,000", "CAD"),
        ("150,000 - 190,000 CAD", "CAD"),
        ("£90,000 - $110,000", "GBP"),
    ],
)
def test_the_marker_beside_the_amount_names_the_currency(text, currency):
    assert parse_salary_range(text)[2] == currency


@pytest.mark.parametrize(
    "text",
    [
        "Our CAD office is in Toronto; this role pays $150,000 - $190,000",
        "Relocation from the EUR zone is not offered. Salary $150,000",
        "Salary $150,000 (the GBP figure is on the UK page)",
    ],
)
def test_a_currency_word_elsewhere_in_the_text_does_not_relabel_a_dollar_amount(text):
    assert parse_salary_range(text)[2] == "USD"


def test_detect_currency_defaults_to_usd_and_follows_the_amount():
    assert detect_currency("competitive") == "USD"
    assert detect_currency("A$150,000") == "AUD"

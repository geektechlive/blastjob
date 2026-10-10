"""Small text helpers shared by adapters."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_WS = re.compile(r"[ \t]+")
_BLANKS = re.compile(r"\n{3,}")

_BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "tr", "table"}


class _Stripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(raw: str) -> str:
    """Convert (possibly entity-escaped) HTML to readable plain text.

    Greenhouse returns JD `content` as HTML-escaped markup; unescape once, strip
    tags, and collapse whitespace so it reads cleanly and scores consistently.
    """
    if not raw:
        return ""
    unescaped = html.unescape(raw)
    p = _Stripper()
    p.feed(unescaped)
    text = "".join(p.parts)
    text = _WS.sub(" ", text)
    text = _BLANKS.sub("\n\n", text)
    return text.strip()


# Full state names -> USPS codes. Workday (and some career sites) spell the
# state out ("Springfield, Ohio, United States"); the geo filter keys on the
# two-letter code.
US_STATE_CODES: dict[str, str] = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
    "district of columbia": "DC",
}


# Territories geo also places (matching/geo.py); not in US_STATE_CODES, which
# region parsing uses.
US_TERRITORY_CODES: dict[str, str] = {"puerto rico": "PR"}
US_STATE_ABBRS: frozenset[str] = frozenset(US_STATE_CODES.values()) | frozenset(
    US_TERRITORY_CODES.values()
)


def us_state_code(text: str) -> str:
    """'Ohio' / 'ohio' / 'oh' / ' OH ' -> 'OH'; '' for anything that is not a
    US state, DC or Puerto Rico."""
    folded = " ".join(text.split()).lower()
    names = {**US_STATE_CODES, **US_TERRITORY_CODES}
    if folded in names:
        return names[folded]
    return folded.upper() if folded.upper() in US_STATE_ABBRS else ""


def normalize_region(region: str) -> str:
    """'Ohio' -> 'OH'; already-coded or unknown values pass through."""
    return US_STATE_CODES.get(region.strip().lower(), region.strip())


def split_location(location_raw: str) -> tuple[str, str]:
    """Best-effort (city, region) from a free-form location string like
    'Springfield, OH' or 'Remote - US'. Returns ('', '') when unclear."""
    if not location_raw:
        return "", ""
    cleaned = location_raw.replace("Remote", "").strip(" -–,")
    parts = [p.strip() for p in cleaned.split(",") if p.strip()]
    if len(parts) >= 2:
        return parts[0], normalize_region(parts[1])
    if len(parts) == 1:
        return parts[0], ""
    return "", ""

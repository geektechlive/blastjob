"""A fetched page's date, found deterministically or not at all (issue #59).

A figure from a 2013 press release ("200 employees") read today as current is
a wrong statement in a cover letter. So every value and fact from a fetched page
carries the page's date when one can be found WITHOUT guessing, else it is
"undated". In order:

1. the reader's `publishedTime` (Jina takes it from the page's own metadata);
2. a published-time meta key in the reader metadata, a
   `<meta property="article:published_time">` or a schema.org
   `datePublished` in the page text, or a "Published Time:" header line;
3. a date in the URL path (`/2013/12/19/`, `/2013-12-19-`);
4. a dateline in the first few hundred characters ("March 4, 2025 /PRNewswire/").

Every candidate must parse as a real calendar date; anything else is no date.
Only PUBLISHED dates count: a modified / last-updated stamp (`dateModified`,
`article:modified_time`) is often today's build date on an old page, which
would make a 2013 figure look current. An ambiguous bare `date` key is skipped
for the same reason.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from typing import Any

DATELINE_CHARS = 300
_META_KEYS = (
    "article:published_time",
    "datePublished",
    "og:published_time",
    "publishdate",
)
_ISO = re.compile(r"(?<!\d)((?:19|20)\d\d)-(\d\d)-(\d\d)(?!\d)")
_IN_TEXT = (
    re.compile(
        r"<meta[^>]+(?:property|name|itemprop)=[\"'](?:article:published_time|datePublished|"
        r"og:published_time)[\"'][^>]*content=[\"']([^\"']+)",
        re.IGNORECASE,
    ),
    re.compile(r"\"datePublished\"\s*:\s*\"([^\"]+)\""),
    re.compile(r"^Published Time:\s*(\S+)", re.MULTILINE),
)
_URL_DATE = re.compile(r"/((?:19|20)\d\d)[/-](\d\d)[/-](\d\d)(?=[/-]|$|\.)")
_MONTHS = {
    m: i
    for i, names in enumerate(
        (
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ),
        1,
    )
    for m in names
}
_DATELINE = re.compile(
    r"\b("
    + "|".join(sorted(_MONTHS, key=len, reverse=True))
    + r")\.?\s+(\d{1,2}),\s+((?:19|20)\d\d)\b",
    re.IGNORECASE,
)


def _valid(year: str, month: str | int, day: str) -> str:
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return ""


def _iso_prefix(value: str) -> str:
    m = _ISO.search(value.strip()[:40])
    return _valid(*m.groups()) if m else ""


def _from_metadata(metadata: Mapping[str, Any] | None) -> str:
    for key in _META_KEYS:
        value = (metadata or {}).get(key)
        if isinstance(value, str) and (found := _iso_prefix(value)):
            return found
    return ""


def _from_text(text: str) -> str:
    for pattern in _IN_TEXT:
        m = pattern.search(text)
        if m and (found := _iso_prefix(m.group(1))):
            return found
    return ""


def _from_url(url: str) -> str:
    m = _URL_DATE.search(url.split("?", 1)[0])
    return _valid(*m.groups()) if m else ""


def _from_dateline(text: str) -> str:
    head = text[:DATELINE_CHARS]
    m = _DATELINE.search(head)
    if m:
        return _valid(m.group(3), _MONTHS[m.group(1).lower()], m.group(2))
    return ""


def page_date(
    url: str, text: str, *, published: str = "", metadata: Mapping[str, Any] | None = None
) -> str:
    """ISO "YYYY-MM-DD", or "" when the page states no date we can read."""
    return (
        _iso_prefix(published)
        or _from_metadata(metadata)
        or _from_text(text)
        or _from_url(url)
        or _from_dateline(text)
    )

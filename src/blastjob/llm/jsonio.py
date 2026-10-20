"""Shared JSON extraction for model responses that should contain one JSON value.

Consolidated from two byte-identical copies (`matching/scorer._extract_json` and
`core/research_facts._extract_json_array`) found in a 2026-09-17 duplication review.
Do not re-fork this — callers keep their own error type/message via `error_message`;
the parsing algorithm itself belongs here and only here.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"```(?:json)?")
_DECODER = json.JSONDecoder()


def find_json_value(text: str, opener: str, expected: type) -> Any | None:
    """First JSON value of type `expected` that starts at an `opener` character.

    Tries `raw_decode` from each `opener` in turn, so bracketed prose ("see [below]")
    or a trailing "[1]" cannot hide or poison the real payload the way a greedy
    first-to-last-bracket match did. Returns None when nothing parses to the shape.
    """
    pos = text.find(opener)
    while pos != -1:
        try:
            value, _ = _DECODER.raw_decode(text, pos)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, expected):
            return value
        pos = text.find(opener, pos + 1)
    return None


def extract_json_list(text: str, *, error_message: str) -> list:
    """Pull a JSON list of objects out of a model response.

    Tolerates: code fences, prose around the JSON, a bare single object (some
    providers return one object instead of a 1-element array), and a top-level
    array. Always returns a list. Raises `ValueError(error_message)` when no
    JSON array or object can be found at all.
    """
    cleaned = _FENCE_RE.sub("", text).strip()

    # 1) Whole thing is valid JSON (array or object).
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        pass

    # 2) An array embedded in prose.
    array = find_json_value(cleaned, "[", list)
    if array is not None:
        return array

    # 3) A single object embedded in prose.
    obj = find_json_value(cleaned, "{", dict)
    if obj is not None:
        return [obj]

    raise ValueError(error_message)

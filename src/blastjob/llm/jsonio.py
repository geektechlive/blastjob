"""Shared JSON extraction for model responses that should contain one JSON value.

Consolidated from two byte-identical copies (`matching/scorer._extract_json` and
`core/research_facts._extract_json_array`) found in a 2026-09-17 duplication review.
Do not re-fork this — callers keep their own error type/message via `error_message`;
the parsing algorithm itself belongs here and only here.
"""

from __future__ import annotations

import json
import re

_FENCE_RE = re.compile(r"```(?:json)?")
_ARRAY_RE = re.compile(r"\[[\s\S]*\]")
_OBJECT_RE = re.compile(r"\{[\s\S]*\}")


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
    m = _ARRAY_RE.search(cleaned)
    if m:
        return json.loads(m.group())

    # 3) A single object embedded in prose.
    m = _OBJECT_RE.search(cleaned)
    if m:
        return [json.loads(m.group())]

    raise ValueError(error_message)

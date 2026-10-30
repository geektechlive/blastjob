"""Validation for the company page's "Edit notes" form (#109): the owner's curated
fields. Pure functions; the route is in `routes_network.py`."""

from __future__ import annotations

import math
from typing import Any

STALE_NOTES_MESSAGE = "The profile changed since you opened it. Reload and try again."
MAX_RATING = 5.0
# Field name -> longest accepted text, in characters.
TEXT_LIMITS = {"culture_notes": 8000, "risks": 8000, "questions": 4000}


def _clean(text: str) -> str:
    return text.replace("\x00", "")


def typed_notes(
    culture_rating: str, culture_notes: str, risks: str, questions: str
) -> dict[str, str]:
    """The submitted values as shown back in a rejected form (NULs removed)."""
    return {
        "culture_rating": _clean(culture_rating).strip(),
        "culture_notes": _clean(culture_notes),
        "risks": _clean(risks),
        "questions": _clean(questions),
    }


def parse_curated_form(
    culture_rating: str, culture_notes: str, risks: str, questions: str
) -> tuple[dict[str, Any], str]:
    """Validate the form. Returns (fields to save, error message); the error is
    "" when valid. A blank rating is None; a rating must be a number in 0..5."""
    typed = typed_notes(culture_rating, culture_notes, risks, questions)
    rating: float | None = None
    if typed["culture_rating"]:
        try:
            rating = float(typed["culture_rating"])
        except ValueError:
            return {}, "The culture rating must be a number from 0 to 5."
        if not math.isfinite(rating) or not 0.0 <= rating <= MAX_RATING:
            return {}, "The culture rating must be a number from 0 to 5."
    for field, limit in TEXT_LIMITS.items():
        if len(typed[field]) > limit:
            return {}, f"{field.replace('_', ' ').capitalize()} is over {limit} characters."
    fields: dict[str, Any] = {
        "culture_rating": rating,
        "culture_notes": typed["culture_notes"],
        "risks": typed["risks"],
        "questions": typed["questions"],
    }
    return fields, ""

"""A named person at a target company, found for a kept job.

A human path in often beats the resume alone. Every contact carries the
URL it was found at so the user verifies the person before writing to them; the
LLM extracts, it never invents (the prompt forbids it and validation drops
entries without a source URL).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class ContactRole(StrEnum):
    HIRING_MANAGER = "hiring_manager"  # the role's likely boss
    RECRUITER = "recruiter"  # talent acquisition / recruiting
    EXECUTIVE = "executive"  # other leadership who could route the note
    PEER = "peer"  # would-be peer / team member with insight


class Contact(BaseModel):
    """What the extractor returns for one person; also the row shape."""

    model_config = {"extra": "ignore"}

    name: str = Field(min_length=2, max_length=120)
    title: str = Field(default="", max_length=200)
    role: ContactRole = ContactRole.EXECUTIVE
    url: str = Field(min_length=8, max_length=500)
    why: str = Field(default="", max_length=400)

    @field_validator("url")
    @classmethod
    def _http_url(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError("contact url must be an http(s) URL from the search results")
        return v

    @field_validator("name", "title", "why")
    @classmethod
    def _strip(cls, v: str) -> str:
        return " ".join(v.split())

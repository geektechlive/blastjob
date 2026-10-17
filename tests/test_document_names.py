"""Packet document names stay under upload limits (issue #19: Indeed refuses a
name of 90+ characters). `paths.document_token` is the one rule."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blastjob.core.artifacts import role_files
from blastjob.core.paths import (
    DOCUMENT_TOKEN_MAX,
    document_token,
    document_tokens,
    name_token,
    person_slug,
)
from blastjob.core.quarantine import held_names
from blastjob.core.refine import locate

PERSON = person_slug("Alexandra Q. Riverson")  # 18 characters
MAX_UPLOAD_NAME = 80

LONG_PAIRS = [
    ("Meridian Home Services LLC", "Senior Director Enterprise Technology"),
    ("The Fabrikam Corporation", "Vice President Information Technology"),
    ("Fabrikam, Inc", "Senior Director, IT (Seattle Preferred)"),
    ("Northwind Specialty Chemicals Holdings Group", "Chief Technology Officer / VP of Technology"),
    ("Contoso Regional Healthcare Partners", "VP of Information Technology (Remote)"),
    ("A" * 60, "B" * 60),
    ("Contoso", "Supercalifragilisticexpialidociousadministratorship"),
    (
        "Woodgrove Financial Advisory Services",
        "Director of Infrastructure Operations and Engineering Management",
    ),
]


def _uploadable(token: str) -> list[str]:
    return [
        f"{PERSON}_Resume_-_{token}.docx",
        f"{PERSON}_Resume_-_{token}.pdf",
        f"{PERSON}_Resume_-_{token}_ATS.ats.txt",
        f"{PERSON}_Cover_Letter_-_{token}.docx",
        f"{PERSON}_Cover_Letter_-_{token}.pdf",
    ]


def test_person_fixture_is_the_documented_worst_case_length():
    assert len(PERSON) == 18


def test_a_token_that_already_fits_is_unchanged():
    assert document_token("Acme Corp", "VP, IT Platforms") == "Acme_Corp_VP_IT_Platforms"
    assert document_token("Acme", "Director of Technology") == "Acme_Director_of_Technology"


@pytest.mark.parametrize(("company", "role"), LONG_PAIRS)
def test_every_uploadable_name_is_at_most_80_characters(company, role):
    token = document_token(company, role)
    assert len(token) <= DOCUMENT_TOKEN_MAX
    for name in _uploadable(token):
        assert len(name) <= MAX_UPLOAD_NAME, name


@pytest.mark.parametrize(("company", "role"), LONG_PAIRS)
def test_the_token_is_deterministic_and_filename_safe(company, role):
    token = document_token(company, role)
    assert token == document_token(company, role)
    assert all(c.isalnum() or c == "_" for c in token)
    assert "__" not in token and not token.startswith("_") and not token.endswith("_")


def test_suffixes_parentheticals_and_title_words_are_shortened_before_anything_is_cut():
    assert document_token("Fabrikam, Inc", "Senior Director, IT (Seattle Preferred)") == (
        "Fabrikam_Sr_Director_IT"
    )
    assert document_token("The Fabrikam Corporation", "Vice President Information Technology") == (
        "Fabrikam_VP_IT"
    )


def test_trailing_company_words_go_before_the_role_is_cut():
    token = document_token("Meridian Home Services LLC", "Senior Director Enterprise Technology")
    assert token.startswith("Meridian")
    assert token.endswith("Sr_Dir_Enterprise_Tech")


def test_two_long_roles_that_differ_only_after_the_cut_get_different_tokens():
    base = "Principal Enterprise Architect Supply Chain Planning Systems"
    a = document_token("Contoso", f"{base} Northern Region")
    b = document_token("Contoso", f"{base} Southern Region")
    assert a != b
    assert len(a) <= DOCUMENT_TOKEN_MAX and len(b) <= DOCUMENT_TOKEN_MAX


def test_document_tokens_lists_the_capped_token_then_the_legacy_one():
    company, role = "Meridian Home Services LLC", "Senior Director Enterprise Technology"
    legacy = f"{name_token(company)}_{name_token(role)}"
    assert document_tokens(company, role) == (document_token(company, role), legacy)
    assert document_tokens("Acme", "VP IT") == ("Acme_VP_IT",)


# --- every file-matcher finds capped AND legacy names ---------------------------

LONG_CO, LONG_ROLE = "Meridian Home Services LLC", "Senior Director Enterprise Technology"
SLUG = "senior-director-enterprise-technology"


def _packet(folder: Path, token: str) -> dict[str, str]:
    folder.mkdir(parents=True, exist_ok=True)
    names = {
        "resume": f"{PERSON}_Resume_-_{token}.md",
        "resume_docx": f"{PERSON}_Resume_-_{token}.docx",
        "ats": f"{PERSON}_Resume_-_{token}_ATS.ats.txt",
        "cover": f"{PERSON}_Cover_Letter_-_{token}.md",
    }
    for name in names.values():
        (folder / name).write_text("x", encoding="utf-8")
    (folder / f"{SLUG}.metadata.json").write_text(
        json.dumps({"company": LONG_CO, "role": LONG_ROLE}), encoding="utf-8"
    )
    return names


@pytest.mark.parametrize("which", ["capped", "legacy"])
def test_role_files_finds_capped_and_legacy_document_names(tmp_path, which):
    capped, legacy = document_tokens(LONG_CO, LONG_ROLE)
    names = _packet(tmp_path, capped if which == "capped" else legacy)
    found = {p.name for p in role_files(tmp_path, company=LONG_CO, role=LONG_ROLE, job_id=1)}
    assert set(names.values()) <= found


@pytest.mark.parametrize("which", ["capped", "legacy"])
def test_refine_locates_a_capped_or_legacy_resume(tmp_path, which):
    capped, legacy = document_tokens(LONG_CO, LONG_ROLE)
    names = _packet(tmp_path, capped if which == "capped" else legacy)
    assert locate(tmp_path, LONG_CO, LONG_ROLE).resume_path.name == names["resume"]


def test_quarantine_derives_capped_names_for_an_old_style_report(tmp_path):
    names = _packet(tmp_path, document_token(LONG_CO, LONG_ROLE))
    (tmp_path / f"{SLUG}.packet_report.json").write_text(
        json.dumps({"state": "quarantined", "quarantined_artifacts": ["resume"]}),
        encoding="utf-8",
    )
    held = held_names(tmp_path)
    assert {names["resume"], names["resume_docx"], names["ats"]} <= held
    assert names["cover"] not in held

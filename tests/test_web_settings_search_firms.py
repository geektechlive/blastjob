"""/settings edits `discovery.search_firms`: one `Name | domain | slug` line per firm (#114)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from blastjob import config as cfg_mod
from blastjob.models.config import SearchFirm
from blastjob.web import settings_form as sf
from blastjob.web.app import create_app
from tests.test_web_settings_db import _form


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("BLASTJOB_DATA_DIR", str(tmp_path / "data"))
    return TestClient(create_app())


def _firms() -> list[SearchFirm]:
    return cfg_mod.load_config().discovery.search_firms


def _save(client: TestClient, text: str):
    return client.post("/settings", data=_form(client, search_firms=text), follow_redirects=False)


def test_a_firm_line_adds_a_firm_with_a_slug_from_the_domain(client):
    r = _save(client, "Acme Executive Search | acmesearch.example")

    assert r.status_code == 303
    assert _firms() == [
        SearchFirm(name="Acme Executive Search", slug="acmesearch", domain="acmesearch.example")
    ]
    assert "Acme Executive Search | acmesearch.example | acmesearch" in client.get("/settings").text


def test_an_explicit_slug_is_kept(client):
    _save(client, "Acme Executive Search | acmesearch.example | acme-retained")

    assert [f.slug for f in _firms()] == ["acme-retained"]


@pytest.mark.parametrize(
    "domain",
    [
        "https://acmesearch.example",
        "acmesearch.example/jobs",
        "acmesearch.example:8080",
        "localhost",
        "10.0.0.5",
        "acme search.example",
        "acmesearch",
    ],
)
def test_a_domain_that_is_not_a_bare_host_is_refused_and_nothing_is_saved(client, domain):
    r = _save(client, f"Acme | {domain}")

    assert r.status_code == 400
    assert "search firms line 1" in r.text
    assert _firms() == []


def test_a_duplicate_slug_is_refused(client):
    r = _save(client, "Acme One | acme.example\nAcme Two | other.example | acme")

    assert r.status_code == 400
    assert "search firms line 2" in r.text and "acme" in r.text
    assert _firms() == []


def test_a_slug_outside_lowercase_letters_digits_and_hyphen_is_refused(client):
    for slug in ("Acme", "acme_x", "acme:x", "a b"):
        r = _save(client, f"Acme | acme.example | {slug}")
        assert r.status_code == 400, slug
    assert _firms() == []


def test_every_bad_line_is_reported(client):
    r = _save(client, "Acme | https://a.example\nGood | good.example\n | nothing.example")

    assert r.status_code == 400
    assert "search firms line 1" in r.text and "search firms line 3" in r.text
    assert _firms() == []


def test_a_line_without_a_slug_keeps_the_stored_slug_for_its_domain(client):
    _save(client, "Acme Executive Search | acmesearch.example | acme-retained")

    _save(client, "Acme Executive Search (renamed) | acmesearch.example")

    (firm,) = _firms()
    assert (firm.slug, firm.name) == ("acme-retained", "Acme Executive Search (renamed)")


def test_editing_the_domain_on_the_rendered_line_keeps_the_slug(client):
    _save(client, "Acme | acmesearch.example | acme-retained")
    assert "Acme | acmesearch.example | acme-retained" in client.get("/settings").text

    _save(client, "Acme | acme-new.example | acme-retained")

    (firm,) = _firms()
    assert (firm.domain, firm.slug) == ("acme-new.example", "acme-retained")


def test_removing_a_line_removes_the_firm_from_future_sweeps(client):
    _save(client, "Acme | acme.example\nGlobex | globex.example")

    _save(client, "Globex | globex.example")

    assert [f.slug for f in _firms()] == ["globex"]


def test_a_form_without_the_field_leaves_the_firms_alone(client):
    _save(client, "Acme | acme.example")
    data = _form(client)
    data.pop("search_firms", None)

    client.post("/settings", data=data, follow_redirects=False)

    assert [f.slug for f in _firms()] == ["acme"]


def test_a_www_label_is_not_the_slug():
    (firm,) = sf.parse_search_firms("Acme | www.acmesearch.example", [])
    assert firm.slug == "acmesearch"

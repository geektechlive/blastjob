"""Settings that pure helpers read at call time (aliases, agencies, network
keywords, alert subject words) come from the loaded config, not from code."""

from __future__ import annotations

import pytest

from blastjob import config as cfg_mod
from blastjob.core import agencies, company, network_views
from blastjob.core import role_clusters as rc
from blastjob.discovery import mailalerts
from blastjob.models.config import BlastJobConfig
from tests.geo_fixture import region_cfg


def _cfg(**discovery) -> BlastJobConfig:
    cfg = BlastJobConfig()
    for key, value in discovery.items():
        setattr(cfg.discovery, key, value)
    return cfg


def test_company_aliases_come_from_settings():
    assert (
        company.company_key("Contoso Mutual Life Insurance Company")
        == "contoso mutual life insurance"
    )

    cfg_mod.apply_runtime_settings(
        _cfg(company_aliases={"Contoso Mutual Life Insurance Company": "Contoso Mutual"})
    )

    assert company.company_key("Contoso Mutual Life Insurance Company") == "contoso mutual"
    assert company.company_key("CONTOSO MUTUAL LIFE INSURANCE CO.") == "contoso mutual"


def test_blank_alias_entries_are_ignored():
    company.set_company_aliases({"": "Contoso", "Fabrikam": " "})
    assert company.company_key("Fabrikam") == "fabrikam"


def test_agency_names_come_from_settings():
    assert not agencies.is_agency("Adatum Talent Partners")
    cfg_mod.apply_runtime_settings(_cfg(agency_companies=["Adatum Talent Partners"]))
    assert agencies.is_agency("Adatum Talent Partners Government Solutions")


def test_network_agency_keywords_come_from_settings():
    assert not network_views._is_agency_company("Adatum Executive Partners")
    cfg_mod.apply_runtime_settings(_cfg(network_agency_keywords=["Adatum"]))
    assert network_views._is_agency_company("Adatum Executive Partners")


@pytest.mark.parametrize(
    ("subject", "configured"),
    [("New director jobs near you", True), ("New VP roles", True), ("New role at Contoso", False)],
)
def test_alert_subject_words_come_from_settings(subject, configured):
    sender = "notifications@example.com"
    assert not mailalerts.looks_like_alert(sender, subject)
    cfg_mod.apply_runtime_settings(_cfg(mail_alert_subject_terms=["director", "VP"]))
    assert mailalerts.looks_like_alert(sender, subject) is configured


def test_generic_alert_subjects_need_no_settings():
    assert mailalerts.looks_like_alert("x@example.com", "Your job alert for operations")
    assert not mailalerts.looks_like_alert("x@example.com", "Congratulate Pat on their new role")


def test_load_config_installs_and_a_fresh_load_clears():
    cfg_mod.apply_runtime_settings(_cfg(agency_companies=["Adatum Talent Partners"]))
    cfg_mod.load_config()  # no config file in tests: defaults, empty lists
    assert not agencies.is_agency("Adatum Talent Partners")


def test_other_state_location_borrows_no_home_town_centroid():
    """Clustering places a posting the way the geo gate does: 'Jackson, MS'
    is not the configured home town named jackson."""
    geo = region_cfg()
    home = {"geo_city": "Jackson", "geo_region": "OH", "geo_location_raw": "Jackson, OH"}
    away = {"geo_city": "Jackson", "geo_region": "MS", "geo_location_raw": "Jackson, MS"}

    assert rc.place_of({**home, "geo_remote": 0}, geo).centroid is not None
    assert rc.place_of({**away, "geo_remote": 0}, geo).centroid is None

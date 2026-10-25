"""Finding a dream company's job board, and the evidence that it is the right company (#90)."""

from __future__ import annotations

import pytest

from blastjob.discovery import board_finder as bf
from blastjob.discovery import resolve
from blastjob.discovery.resolve import BoardHit

COMPANY = "Northwind Traders"


def _hit(ats: str, token: str, n: int = 12) -> BoardHit:
    return BoardHit(COMPANY, ats, token, n)


@pytest.fixture
def fakes(monkeypatch):
    """Offline stand-ins for the five network leaves, set per test."""
    state = {
        "company": [],
        "workday": [],
        "dayforce": [],
        "samples": {},
        "careers": "",
        "calls": [],
    }

    def probe_company(name):
        state["calls"].append(("company", name))
        return list(state["company"])

    def probe_workday(name, key):
        state["calls"].append(("workday", name))
        return list(state["workday"])

    def probe_dayforce(name, key):
        state["calls"].append(("dayforce", name))
        return list(state["dayforce"])

    def fetch_samples(hit):
        return state["samples"].get((hit.ats, hit.token), ((), ""))

    def read_careers_page(domain, key):
        state["calls"].append(("careers", domain))
        return state["careers"]

    monkeypatch.setattr(resolve, "probe_company", probe_company)
    monkeypatch.setattr(resolve, "probe_workday", probe_workday)
    monkeypatch.setattr(resolve, "probe_dayforce", probe_dayforce)
    monkeypatch.setattr(bf, "fetch_samples", fetch_samples)
    monkeypatch.setattr(bf, "read_careers_page", read_careers_page)
    return state


SAMPLES = (bf.Sample("VP of Technology", "Phoenix, AZ"), bf.Sample("Controller", "Remote"))


# ----------------------------------------------------------------------------- links


@pytest.mark.parametrize(
    ("ats", "token", "text"),
    [
        ("greenhouse", "northwind", "Apply at https://boards.greenhouse.io/northwind today"),
        ("greenhouse", "northwind", "https://job-boards.greenhouse.io/northwind/jobs/1"),
        ("lever", "northwind", "see https://jobs.lever.co/northwind"),
        ("ashby", "northwind", "[Jobs](https://jobs.ashbyhq.com/northwind)"),
        (
            "workday",
            "nw/wd5/Careers",
            "https://nw.wd5.myworkdayjobs.com/en-US/Careers/job/Phoenix/VP_R1",
        ),
        ("dayforce", "nwcu/CANDIDATEPORTAL", "https://jobs.dayforcehcm.com/nwcu/CANDIDATEPORTAL"),
    ],
)
def test_a_careers_page_that_links_to_the_board_proves_it(ats, token, text):
    assert bf.links_to_board(text, ats, token) is True


@pytest.mark.parametrize(
    ("ats", "token", "text"),
    [
        ("greenhouse", "northwind", "https://boards.greenhouse.io/northwindlogistics"),
        ("greenhouse", "northwind", "We love northwind. No links here."),
        ("lever", "northwind", "https://jobs.lever.co/other"),
        ("workday", "nw/wd5/Careers", "https://nw.wd5.myworkdayjobs.com/Other"),
        ("dayforce", "nwcu/CANDIDATEPORTAL", "https://jobs.dayforcehcm.com/other/CANDIDATEPORTAL"),
    ],
)
def test_a_page_without_that_board_link_proves_nothing(ats, token, text):
    assert bf.links_to_board(text, ats, token) is False


# ----------------------------------------------------------------------------- basis


def test_the_domain_link_wins_over_the_ats_name(fakes):
    fakes["company"] = [_hit("greenhouse", "northwind")]
    fakes["samples"][("greenhouse", "northwind")] = (SAMPLES, "Northwind Traders Inc")
    fakes["careers"] = "https://boards.greenhouse.io/northwind"
    found = bf.find_boards(COMPANY, "northwind.example.com", "key")
    (cand,) = found.candidates
    assert cand.identity_basis == "domain_link"
    assert cand.samples == SAMPLES
    assert cand.job_count == 12


def test_an_ats_name_that_names_the_company_is_evidence(fakes):
    fakes["company"] = [_hit("greenhouse", "northwind")]
    fakes["samples"][("greenhouse", "northwind")] = (SAMPLES, "Northwind Traders, Inc.")
    (cand,) = bf.find_boards(COMPANY, "", "").candidates
    assert cand.identity_basis == "ats_name"
    assert cand.ats_name == "Northwind Traders, Inc."


def test_an_ats_name_of_a_different_company_is_not_evidence(fakes):
    fakes["company"] = [_hit("greenhouse", "northwind")]
    fakes["samples"][("greenhouse", "northwind")] = (SAMPLES, "Northwind Logistics LLC")
    (cand,) = bf.find_boards(COMPANY, "", "").candidates
    assert cand.identity_basis == ""
    assert cand.ats_name == "Northwind Logistics LLC"  # still shown to the owner


def test_an_ats_name_that_only_contains_the_company_name_is_not_evidence(fakes):
    fakes["company"] = [BoardHit("Acme", "greenhouse", "acme", 5)]
    fakes["samples"][("greenhouse", "acme")] = (SAMPLES, "Acme Staffing")
    (cand,) = bf.find_boards("Acme", "", "").candidates
    assert cand.identity_basis == ""
    assert cand.ats_name == "Acme Staffing"  # shown to the owner, who must tick


def test_an_ats_name_with_only_a_legal_suffix_difference_is_evidence(fakes):
    fakes["company"] = [BoardHit("Acme", "greenhouse", "acme", 5)]
    fakes["samples"][("greenhouse", "acme")] = (SAMPLES, "ACME, Inc.")
    (cand,) = bf.find_boards("Acme", "", "").candidates
    assert cand.identity_basis == "ats_name"


def test_a_board_with_no_evidence_has_no_basis(fakes):
    fakes["company"] = [_hit("lever", "northwind")]
    fakes["samples"][("lever", "northwind")] = (SAMPLES, "")
    (cand,) = bf.find_boards(COMPANY, "northwind.example.com", "key").candidates
    assert cand.identity_basis == ""


def test_samples_alone_are_never_evidence(fakes):
    fakes["company"] = [_hit("ashby", "northwind")]
    fakes["samples"][("ashby", "northwind")] = (SAMPLES, "")
    (cand,) = bf.find_boards(COMPANY, "", "").candidates
    assert cand.samples and cand.identity_basis == ""


# ----------------------------------------------------------------------------- coverage


def test_without_a_jina_key_workday_and_dayforce_are_not_probed_and_it_says_so(fakes):
    found = bf.find_boards(COMPANY, "", "")
    kinds = [c[0] for c in fakes["calls"]]
    assert "workday" not in kinds and "dayforce" not in kinds
    assert found.candidates == ()
    assert any("Workday and Dayforce" in n for n in found.notes)


def test_without_a_domain_the_careers_page_is_not_read_and_it_says_so(fakes):
    found = bf.find_boards(COMPANY, "", "key")
    assert ("careers", "") not in fakes["calls"]
    assert not [c for c in fakes["calls"] if c[0] == "careers"]
    assert any("website" in n for n in found.notes)


def test_one_failing_probe_does_not_lose_the_others_and_is_reported(fakes, monkeypatch):
    fakes["company"] = [_hit("lever", "northwind")]
    fakes["samples"][("lever", "northwind")] = (SAMPLES, "")

    def boom(name, key):
        raise RuntimeError("jina down")

    monkeypatch.setattr(resolve, "probe_workday", boom)
    found = bf.find_boards(COMPANY, "", "key")
    assert [c.ats for c in found.candidates] == ["lever"]
    assert any("Workday" in n and "failed" in n for n in found.notes)


def test_duplicate_boards_are_listed_once(fakes):
    fakes["company"] = [_hit("lever", "northwind"), _hit("lever", "Northwind")]
    fakes["samples"][("lever", "northwind")] = (SAMPLES, "")
    assert len(bf.find_boards(COMPANY, "", "").candidates) == 1


# ----------------------------------------------------------------------------- guard


def test_the_network_leaves_are_blocked_in_tests():
    """The autouse guard (tests/conftest.py::_no_real_board_finder) fails loudly."""
    with pytest.raises(BaseException, match="tried to reach the network"):
        bf.fetch_samples(_hit("lever", "northwind"))
    with pytest.raises(BaseException, match="tried to reach the network"):
        bf.read_careers_page("northwind.example.com", "key")
    with pytest.raises(BaseException, match="tried to reach the network"):
        resolve.probe_company(COMPANY)


@pytest.mark.parametrize(
    ("ats", "token", "tail"),
    [
        ("greenhouse", "northwind", "boards.greenhouse.io/northwind"),
        ("greenhouse", "northwind", "greenhouse.io/northwind"),
        ("lever", "northwind", "jobs.lever.co/northwind"),
        ("ashby", "northwind", "jobs.ashbyhq.com/northwind"),
    ],
)
def test_a_board_url_inside_another_sites_query_string_is_not_a_link(ats, token, tail):
    assert bf.links_to_board(f"https://evil.example.com/p?next={tail}", ats, token) is False
    assert bf.links_to_board(f"see {tail} for jobs", ats, token) is False

"""No personal data in tracked files, plus tests for the detectors behind it.

The repo scan is enforced (`ENFORCE = True`): a hit in any tracked file fails
the suite. Setting `ENFORCE` back to False turns it into a non-strict xfail.

The repo scan uses only the generic detectors (emails outside
example.com/.org/.net and the script's exact platform-sender list, US phones
outside 555-01xx, /Users/<name> paths, linkedin.com/in/<handle> URLs other
than the reserved `example` / `example-*` fixture handles). The per-user check
against YOUR OWN database and work history is
`scripts/check_no_personal_data.py`, run by hand before publishing; its tests
below use a synthetic temp DB and temp git repo only.

Synthetic positives are assembled at runtime by concatenation so this file
never matches its own scan.
"""

from __future__ import annotations

import importlib.util
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from blastjob.db.client import migrate, upsert_job

ENFORCE = True  # the tracked tree is clean; keep it that way

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check_no_personal_data.py"

AT = "@"
ALICE_EMAIL = "alice.w" + AT + "corp-mail.io"
PHONE = "(312) " + "867-5309"
HOME_PATH = "/Us" + "ers/alicew/projects"
PROFILE = "linkedin.com/" + "in/alice-w-1234"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_no_personal_data", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


leak = _load()


def kinds(line: str) -> list[str]:
    return [k for k, _ in leak.find_generic(line)]


# --- the repo scan (xfail until enforced) ------------------------------------


@pytest.mark.xfail(
    not ENFORCE, strict=False, raises=AssertionError, reason="repo scan not enforced"
)
def test_tracked_files_hold_no_personal_data():
    if shutil.which("git") is None or not (REPO / ".git").exists():
        pytest.skip("not a git checkout")
    empty = leak.Personal(terms={}, literals=(), shingles=frozenset())
    hits = [
        f"{f.source}:{f.line} {f.kind}"
        for label, text in leak.tracked_files(REPO)
        for f in leak.scan_text(label, text, empty, leak.VENDOR_ALLOWLIST)
    ]
    assert hits == [], f"{len(hits)} personal-data hits, e.g. {hits[:10]}"


# --- generic detectors -------------------------------------------------------


def test_real_looking_email_is_flagged():
    assert kinds(f"contact {ALICE_EMAIL} today") == ["email"]


@pytest.mark.parametrize(
    "line",
    [
        "jane" + AT + "example.com",
        "ops" + AT + "mail.example.org",
        "x" + AT + "example.net",
        "noreply" + AT + "anthropic.com",
        "123+bot" + AT + "users.noreply.github.com",
        "git" + AT + "github.com:org/repo.git",
        "someone" + AT + "host.test",
        "icon" + AT + "2x.png",
        "pkg" + AT + "1.2.3",
        "@pytest.fixture",
    ],
)
def test_reserved_and_non_email_strings_pass(line):
    assert "email" not in kinds(line)


@pytest.mark.parametrize(
    "line", [PHONE, "312-" + "867-5309", "+1 312." + "867.5309", "1-312-" + "867-5309"]
)
def test_us_phone_numbers_are_flagged(line):
    assert kinds(line) == ["phone"]


@pytest.mark.parametrize(
    "line",
    [
        "(312) 555-" + "0142",
        "312-555-" + "0199",
        "2026-10-10",
        "3128675309",
        "version 1.312.867.5309.2",
        "sha 312-867-5309abc",
    ],
)
def test_fictional_phones_and_non_phones_pass(line):
    assert "phone" not in kinds(line)


def test_fictional_range_ends_at_0199():
    assert kinds("312-555-" + "0200") == ["phone"]


def test_home_paths_are_flagged_but_placeholders_pass():
    assert kinds(HOME_PATH) == ["home_path"]
    assert kinds("/Us" + "ers/<name>/x") == []
    assert kinds("/Us" + "ers/you/x") == []


def test_linkedin_profiles_are_flagged_but_company_pages_pass():
    assert kinds("https://www." + PROFILE) == ["linkedin"]
    assert kinds("https://linkedin.com/company/acme") == []
    assert kinds("linkedin.com/" + "in/your-handle") == []


@pytest.mark.parametrize("handle", ["example", "example-jane-doe", "Example-P7"])
def test_reserved_example_linkedin_handles_pass(handle):
    assert kinds("https://linkedin.com/" + "in/" + handle) == []


@pytest.mark.parametrize("handle", ["examplecorp", "exampleuser", "my-example-jane"])
def test_example_prefix_needs_the_hyphen_and_must_lead(handle):
    assert kinds("https://linkedin.com/" + "in/" + handle) == ["linkedin"]


@pytest.mark.parametrize(
    "address",
    [
        "alert" + AT + "indeed.com",
        "jobs-noreply" + AT + "linkedin.com",
        "Jobalerts-Noreply" + AT + "LinkedIn.com",
        "no-reply" + AT + "greenhouse.io",
        "donotreply" + AT + "myworkday.com",
        "person" + AT + "yahoo.com",
    ],
)
def test_exact_platform_senders_and_the_webmail_fixture_pass(address):
    assert kinds(f"From: {address}") == []


@pytest.mark.parametrize(
    "address",
    [
        "recruiter" + AT + "linkedin.com",
        "jane.doe" + AT + "indeed.com",
        "noreply" + AT + "greenhouse.io.evil.example2",
        "alert" + AT + "mail.indeed.com",
        "another.person" + AT + "yahoo.com",
    ],
)
def test_other_addresses_at_platform_domains_still_fail(address):
    assert kinds(f"From: {address}") == ["email"]


def test_contact_initials_never_become_a_term():
    names = ["Z. W.", "Q R", "J. Doe", "Al B."]
    assert leak.person_terms(names, "contact", frozenset(), leak.MIN_CONTACT_NAME_WORDS) == []


def test_contact_with_two_real_name_words_keeps_its_initial_variant():
    terms = {
        t.words
        for t in leak.person_terms(
            ["Dana Q. Okafor", "Bo Li"], "contact", frozenset(), leak.MIN_CONTACT_NAME_WORDS
        )
    }
    assert terms == {("dana", "q", "okafor"), ("dana", "okafor"), ("bo", "li")}


def _contact_personal(*names: str) -> object:
    terms = leak.person_terms(names, "contact", frozenset(), leak.MIN_CONTACT_NAME_WORDS)
    index: dict[str, list] = {}
    for t in terms:
        index.setdefault(t.words[0], []).append(t)
    return leak.Personal(terms=index, literals=(), shingles=frozenset())


@pytest.mark.parametrize(
    ("line", "hit"),
    [
        ("met Dana Okafor today", True),
        ("dana_okafor = 1", True),
        ("xdana okafor", False),
        ("danaokafor", False),
        ("dana okafory", False),
        (r"re.compile(r'[A-Z]\w+ Z W. x')", False),
    ],
)
def test_contact_matching_is_whole_words_only(line, hit):
    personal = _contact_personal("Dana Okafor", "Z. W.")
    found = leak.scan_text("f", line, personal, frozenset())
    assert bool(found) is hit, found


def test_minified_vendor_files_are_recognized():
    assert leak.is_minified_vendor("src/web/static/htmx.min.js")
    assert leak.is_minified_vendor("A.MIN.JS")
    assert not leak.is_minified_vendor("src/web/static/app.js")
    assert not leak.is_minified_vendor("notes.min.md")


def test_the_script_and_this_file_do_not_match_themselves():
    empty = leak.Personal(terms={}, literals=(), shingles=frozenset())
    for path in (SCRIPT, Path(__file__)):
        text = path.read_text(encoding="utf-8")
        assert leak.scan_text(path.name, text, empty, leak.VENDOR_ALLOWLIST) == []


# --- normalization and term rules --------------------------------------------


def test_normalize_folds_case_punctuation_apostrophes_and_space():
    assert leak.normalize("  Brandt\u2019s,   ORVIK & Sallow\n") == "brandts orvik sallow"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Quill Harbor Group", True),
        ("Zentrafyx", True),
        ("Global Staffing Solutions", False),
        ("Target", False),
        ("Acme", False),
        ("Confidential", False),
    ],
)
def test_distinctive_company(name, expected):
    words = leak._strip_legal(leak.tokens(name))
    assert leak.is_distinctive_company(words) is expected


def test_company_tiers_by_rarity_and_distinctiveness():
    counts = leak.Counter(
        {"Zentrafyx, Inc.": 2, "Brightloom Analytics LLC": 9, "Target": 1, "Quorvo": 1}
    )
    terms = {" ".join(t.words): t.tier for t in leak.company_terms(counts, frozenset())}
    assert terms == {
        "zentrafyx": "FAIL",  # distinctive + rare
        "brightloom analytics": "WARN",  # distinctive but common in the corpus
        # "Target": a common word, skipped; "quorvo": 6 chars, distinctive, rare
        "quorvo": "FAIL",
    }


def test_allowlist_and_vendor_names_are_never_terms():
    counts = leak.Counter({"Zentrafyx": 1, "Workday": 1})
    allow = leak.VENDOR_ALLOWLIST | {"zentrafyx"}
    assert leak.company_terms(counts, allow) == []


def test_person_terms_need_two_words_and_add_first_last_variant():
    terms = {
        t.words for t in leak.person_terms(["Dana Q. Okafor, PhD", "Cher"], "contact", frozenset())
    }
    assert terms == {("dana", "q", "okafor"), ("dana", "okafor")}


def test_owner_name_from_frontmatter_else_first_heading():
    assert leak.owner_name("---\nname: 'Rowan Thistlewood'\nemail: x\n---\n# Other\n") == (
        "Rowan Thistlewood"
    )
    assert leak.owner_name("intro\n# Rowan Thistlewood\n") == "Rowan Thistlewood"
    assert leak.owner_name("no name here") == ""


def test_owner_surname_alone_fails_only_when_distinctive():
    kinds_ = {t.kind for t in leak.owner_terms("Rowan Thistlewood", frozenset())}
    assert kinds_ == {"own_name", "own_surname"}
    assert {t.kind for t in leak.owner_terms("Rowan Smith", frozenset())} == {"own_name"}


# --- the script on synthetic user data ---------------------------------------

WORK_HISTORY = (
    "---\nname: Rowan Thistlewood\n---\n\n## Zentrafyx\n"
    "Rebuilt the quarterly forecasting pipeline so finance closed the books four days sooner.\n"
)


def _make_data_dir(root: Path) -> Path:
    data = root / "data"
    data.mkdir()
    (data / "work_history.md").write_text(WORK_HISTORY, encoding="utf-8")
    conn = sqlite3.connect(data / "blastjob.db")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    jobs = [("zentrafyx", "Zentrafyx Inc"), ("target", "Target")]
    jobs += [(f"bl{i}", "Brightloom Analytics") for i in range(5)]
    jobs += [(f"cv{i}", "Corvantis Health - Midwest Region") for i in range(5)]
    for ext, company in jobs:
        upsert_job(conn, source="t", external_id=ext, company=company, title="VP")
    # Common in the corpus, but applied to: that alone makes it personal.
    conn.execute(
        "UPDATE jobs SET review_state = 'kept', application_state = 'applied'"
        " WHERE external_id = 'cv0'"
    )
    conn.execute(
        "INSERT INTO contacts (job_id, name, url, created_at) VALUES (1, 'Dana Okafor', 'u', 'x')"
    )
    conn.execute("INSERT INTO network_contacts (full_name) VALUES ('Imogen Vasquez-Tran')")
    conn.commit()
    conn.close()
    return data


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@example.com",
            "-c",
            "user.name=T",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(repo),
            *args,
        ],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def setup(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    data = _make_data_dir(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("A generic tool. Uses Workday and Target boards.\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return data, repo


def _run(capsys, *argv: str) -> tuple[int, str]:
    code = leak.main(list(argv))
    return code, capsys.readouterr().out


def test_clean_repo_passes(setup, capsys):
    data, repo = setup
    code, out = _run(capsys, "--repo", str(repo), "--data-dir", str(data))
    assert code == 0, out
    assert out.startswith("FAIL: 0  WARN: 0")


def test_each_personal_kind_fails_with_file_and_line(setup, capsys):
    data, repo = setup
    (repo / "notes.md").write_text(
        "line one\n"
        "Thanks, Rowan Thistlewood\n"
        "met dana okafor and IMOGEN VASQUEZ TRAN\n"
        "worked at zentrafyx, inc.\n"
        "brightloom analytics again\n"
        f"mail {ALICE_EMAIL}\n"
        "rebuilt the quarterly forecasting pipeline so finance closed the books\n"
        "now at Corvantis Health\n"
    )
    _git(repo, "add", ".")
    code, out = _run(capsys, "--repo", str(repo), "--data-dir", str(data))
    assert code == 1
    assert "notes.md  (FAIL 7, WARN 2)" in out  # own name + surname on L2
    for expected in (
        "L2     FAIL own_name",
        "L3     FAIL contact: Dana Okafor",
        "L3     FAIL contact: Imogen Vasquez-Tran",
        "L4     FAIL company: Zentrafyx Inc",
        "L5     WARN company: Brightloom Analytics",
        "L6     FAIL email",
        "L7     WARN work_history_overlap",
        "L8     FAIL applied_company: Corvantis Health - Midwest Region",
    ):
        assert expected in out, out


def test_summary_prints_counts_and_files_but_no_values(setup, capsys):
    data, repo = setup
    (repo / "notes.md").write_text("Rowan Thistlewood\n")
    _git(repo, "add", ".")
    code, out = _run(capsys, "--repo", str(repo), "--data-dir", str(data), "--summary")
    assert code == 1
    assert "notes.md  (FAIL 2, WARN 0)" in out
    assert "thistlewood" not in out.lower()


def test_allowlist_file_suppresses_a_term(setup, capsys):
    data, repo = setup
    (repo / "notes.md").write_text("zentrafyx\n")
    _git(repo, "add", ".")
    allow = repo / "local" / "leak_allowlist.txt"
    allow.parent.mkdir()
    allow.write_text("# ok to publish\nZentrafyx\n")
    code, out = _run(capsys, "--repo", str(repo), "--data-dir", str(data))
    assert code == 0, out


def test_untracked_files_are_ignored_by_default_scan(setup, capsys):
    data, repo = setup
    (repo / "scratch.md").write_text("Rowan Thistlewood\n")
    code, _ = _run(capsys, "--repo", str(repo), "--data-dir", str(data))
    assert code == 0


def test_tracked_path_names_are_scanned(setup, capsys):
    data, repo = setup
    (repo / "zentrafyx-notes.txt").write_text("nothing\n")
    _git(repo, "add", ".")
    code, out = _run(capsys, "--repo", str(repo), "--data-dir", str(data))
    assert code == 1
    assert "zentrafyx-notes.txt (path)" in out


def test_minified_js_content_is_skipped_but_its_path_is_scanned(setup, capsys):
    data, repo = setup
    (repo / "vendor.min.js").write_text(f"var a='{ALICE_EMAIL}';\n")
    (repo / "app.js").write_text(f"var a='{ALICE_EMAIL}';\n")
    (repo / "zentrafyx.min.js").write_text("var a=1;\n")
    _git(repo, "add", ".")
    code, out = _run(capsys, "--repo", str(repo), "--data-dir", str(data))
    assert code == 1
    assert "vendor.min.js" not in out
    assert "app.js  (FAIL 1, WARN 0)" in out
    assert "zentrafyx.min.js (path)" in out


def test_git_objects_finds_a_leak_deleted_from_the_tree(setup, capsys):
    data, repo = setup
    (repo / "old.md").write_text(f"call {PHONE}\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "add notes")
    _git(repo, "rm", "-q", "old.md")
    _git(repo, "commit", "-q", "-m", "Remove notes for Rowan Thistlewood")

    tree_code, _ = _run(capsys, "--repo", str(repo), "--data-dir", str(data))
    code, out = _run(
        capsys, "--repo", str(repo), "--git-objects", str(repo), "--data-dir", str(data)
    )

    assert tree_code == 0
    assert code == 1
    assert "(old.md)" in out and "FAIL phone" in out
    assert "commit " in out and "FAIL own_name" in out


def test_paths_mode_scans_files_and_dirs(setup, capsys, tmp_path):
    data, repo = setup
    loose = tmp_path / "loose"
    (loose / "sub").mkdir(parents=True)
    (loose / "sub" / "a.txt").write_text(HOME_PATH + "\n")
    (loose / "b.bin").write_bytes(b"\0\x01Rowan Thistlewood")
    code, out = _run(capsys, "--repo", str(repo), "--paths", str(loose), "--data-dir", str(data))
    assert code == 1
    assert "a.txt" in out and "b.bin" not in out


def test_missing_data_dir_runs_generic_detectors_only(setup, capsys, tmp_path):
    _, repo = setup
    (repo / "notes.md").write_text("Rowan Thistlewood\n" + PROFILE + "\n")
    _git(repo, "add", ".")
    code, out = _run(capsys, "--repo", str(repo), "--data-dir", str(tmp_path / "nope"))
    assert code == 1
    assert "(FAIL 1, WARN 0)" in out and "linkedin" in out


def test_data_dir_comes_from_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("BLASTJOB_DATA_DIR", str(tmp_path))
    assert leak.resolve_data_dir(None) == tmp_path
    assert leak.resolve_data_dir(str(tmp_path / "x")) == tmp_path / "x"


def test_database_is_opened_read_only(setup):
    data, _ = setup
    db = data / "blastjob.db"
    db.chmod(0o444)
    try:
        user = leak.read_db(db)
    finally:
        db.chmod(0o644)
    assert user.companies["Brightloom Analytics"] == 5
    assert user.applied == {"Corvantis Health - Midwest Region"}
    assert set(user.people) == {"Dana Okafor", "Imogen Vasquez-Tran"}


def test_company_variants_drop_parentheticals_and_division():
    assert leak.company_variants("Korn Ferry (retained search)") == {("korn", "ferry")}
    assert leak.company_variants("Acme Widgets, Inc. - Global Division") == {
        ("acme", "widgets", "inc", "global", "division"),
        ("acme", "widgets"),
    }


def test_git_objects_uses_the_working_checkouts_allowlist(setup, capsys, tmp_path):
    """A fresh publication repo has no local/ dir; the allowlist must come
    from --repo (the working checkout), not from the scanned target."""
    data, repo = setup
    allow = repo / "local" / "leak_allowlist.txt"
    allow.parent.mkdir()
    allow.write_text("Zentrafyx\n")
    target = tmp_path / "public"
    target.mkdir()
    _git(target, "init", "-q")
    (target / "a.md").write_text("built at zentrafyx\n")
    _git(target, "add", ".")
    _git(target, "commit", "-q", "-m", "init")

    code, out = _run(
        capsys, "--repo", str(repo), "--git-objects", str(target), "--data-dir", str(data)
    )

    assert code == 0, out
    assert f"allowlist: {allow} (1 terms)" in out

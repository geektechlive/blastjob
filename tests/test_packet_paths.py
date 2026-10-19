"""`paths.locate` / `paths.canonical` and the rebuild naming of issue #20.

Stored packet paths stay in ROOT form (`<output_dir>/<entry>/...`) forever;
filing moves an entry into a bucket folder and every reader finds it again
with `locate`. These tests pin that contract.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from blastjob.core import paths
from blastjob.core.paths import (
    ARCHIVE,
    BUCKETS,
    CLOSED_BEFORE_APPLYING,
    SUBMITTED,
    application_dir,
    canonical,
    document_token,
    document_tokens,
    locate,
)


def test_bucket_names_are_the_three_folders() -> None:
    assert BUCKETS == (SUBMITTED, CLOSED_BEFORE_APPLYING, ARCHIVE)
    assert SUBMITTED == "Submitted"
    assert CLOSED_BEFORE_APPLYING == "Closed before applying"
    assert ARCHIVE == "Archive"


def test_canonical_strips_a_bucket_segment_directly_under_the_root(tmp_path: Path) -> None:
    filed = tmp_path / "Submitted" / "acme-2026-10-01" / "x.md"
    assert canonical(filed, tmp_path) == tmp_path / "acme-2026-10-01" / "x.md"


def test_canonical_leaves_root_form_and_outside_paths_alone(tmp_path: Path) -> None:
    root_form = tmp_path / "acme-2026-10-01" / "x.md"
    outside = tmp_path.parent / "elsewhere" / "Archive" / "x.md"
    assert canonical(root_form, tmp_path) == root_form
    assert canonical(outside, tmp_path) == outside


def test_canonical_keeps_a_bucket_named_folder_deeper_in_an_entry(tmp_path: Path) -> None:
    deep = tmp_path / "acme-2026-10-01" / "Archive" / "x.md"
    assert canonical(deep, tmp_path) == deep


def test_canonical_expands_the_home_tilde(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    out = tmp_path / "out"
    assert canonical("~/out/Archive/acme-2026-10-01", out) == out / "acme-2026-10-01"


def test_locate_finds_an_entry_wherever_it_was_filed(tmp_path: Path) -> None:
    stored = tmp_path / "acme-2026-10-01" / "a.md"
    for bucket in BUCKETS:
        moved = tmp_path / bucket / "acme-2026-10-01"
        moved.mkdir(parents=True)
        (moved / "a.md").write_text("x")
        assert locate(stored, tmp_path) == moved / "a.md"
        (moved / "a.md").unlink()
        moved.rmdir()


def test_locate_prefers_root_and_accepts_a_bucket_form_input(tmp_path: Path) -> None:
    (tmp_path / "acme-2026-10-01").mkdir()
    (tmp_path / "Archive" / "acme-2026-10-01").mkdir(parents=True)
    filed_form = tmp_path / "Archive" / "acme-2026-10-01"
    assert locate(filed_form, tmp_path) == tmp_path / "acme-2026-10-01"


def test_locate_returns_root_form_when_the_entry_is_nowhere(tmp_path: Path) -> None:
    stored = tmp_path / "Submitted" / "gone-2026-10-01" / "a.md"
    assert locate(stored, tmp_path) == tmp_path / "gone-2026-10-01" / "a.md"


def test_locate_never_leaves_the_output_dir(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    sneaky = out / ".." / "secret"
    (tmp_path / "Submitted" / "secret").mkdir(parents=True)
    # Outside after normalization: returned normalized, never re-rooted.
    assert locate(sneaky, out) == tmp_path / "secret"


def test_locate_matches_a_resolved_stored_path_too(tmp_path: Path) -> None:
    # artifacts.register stores resolved paths; finish_build stores the root as
    # configured. Both forms must locate.
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    (real / "Archive" / "acme-2026-10-01").mkdir(parents=True)
    stored = real.resolve() / "acme-2026-10-01" / "a.md"
    assert locate(stored, link) == real.resolve() / "Archive" / "acme-2026-10-01" / "a.md"


def test_application_dir_reuses_todays_root_folder(tmp_path: Path) -> None:
    first = application_dir(tmp_path, "Acme Corp")
    assert application_dir(tmp_path, "Acme Corp") == first
    assert first.name == f"acme-corp-{date.today().isoformat()}"


def test_application_dir_skips_a_name_already_filed_in_a_bucket(tmp_path: Path) -> None:
    today = date.today().isoformat()
    (tmp_path / "Submitted" / f"acme-corp-{today}").mkdir(parents=True)
    (tmp_path / "Archive" / f"acme-corp-{today}-2").mkdir(parents=True)
    got = application_dir(tmp_path, "Acme Corp")
    assert got == tmp_path / f"acme-corp-{today}-3"
    assert got.is_dir()


def test_manufacturing_abbreviates_in_the_tight_tier() -> None:
    assert ("manufacturing", "Mfg") in paths._ROLE_ABBREVIATIONS_TIGHT


def test_document_tokens_keep_the_pre_mfg_hashed_form() -> None:
    company = "Fabrikam Industrial Holdings"
    role = "Director of Manufacturing Systems and Information Technology Strategy"
    tokens = document_tokens(company, role)
    assert tokens[0] == document_token(company, role)
    prior = paths._document_token(company, role, paths._PRIOR_TIGHT_TABLES[0])
    assert prior in tokens
    assert prior != tokens[0]


def test_locate_prefers_the_place_where_the_full_path_exists(tmp_path: Path) -> None:
    # A stray empty root folder must not shadow the real filed entry.
    (tmp_path / "acme-2026-10-01").mkdir()
    filed = tmp_path / "Submitted" / "acme-2026-10-01"
    filed.mkdir(parents=True)
    (filed / "a.md").write_text("x")
    assert locate(tmp_path / "acme-2026-10-01" / "a.md", tmp_path) == filed / "a.md"
    # With the file nowhere, the first existing entry folder wins (root first).
    assert locate(tmp_path / "acme-2026-10-01" / "b.md", tmp_path) == (
        tmp_path / "acme-2026-10-01" / "b.md"
    )


async def test_shared_lock_waits_without_blocking_the_event_loop(tmp_path: Path) -> None:
    import asyncio

    from blastjob.core import filing_lock

    got = asyncio.Event()
    ticks: list[int] = []

    async def taker() -> None:
        async with filing_lock.shared_async(tmp_path):
            got.set()

    with filing_lock.exclusive(tmp_path) as ok:
        assert ok
        task = asyncio.create_task(taker())
        for i in range(3):
            await asyncio.sleep(0.01)
            ticks.append(i)
        assert not got.is_set()
    await asyncio.wait_for(task, 2)
    assert got.is_set() and ticks == [0, 1, 2]

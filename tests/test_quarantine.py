from datetime import datetime
from pathlib import Path

import pytest

from blastjob.core.quarantine import (
    UnsafeStemError,
    is_quarantined,
    quarantine,
    quarantined_files,
    release,
)

STEM = "Alex_Rivera_Resume_-_Acme_VP_IT"
ATS_STEM = f"{STEM}_ATS"


def _make_packet(packet_dir: Path, *, stems: list[str] = (STEM, ATS_STEM)) -> None:
    packet_dir.mkdir(parents=True, exist_ok=True)
    if STEM in stems:
        (packet_dir / f"{STEM}.md").write_text("# Resume\n", encoding="utf-8")
        (packet_dir / f"{STEM}.docx").write_bytes(b"fake docx bytes")
    if ATS_STEM in stems:
        (packet_dir / f"{ATS_STEM}.ats.txt").write_text("Resume ATS text", encoding="utf-8")
    # Sidecars — must never move.
    (packet_dir / "vp-ops.job_description.md").write_text("JD", encoding="utf-8")
    (packet_dir / "vp-ops.company_research.md").write_text("Research", encoding="utf-8")
    (packet_dir / "vp-ops.fit_score.json").write_text("{}", encoding="utf-8")
    (packet_dir / "vp-ops.packet_report.json").write_text("{}", encoding="utf-8")


# ---------------------------------------------------------------------------
# quarantine() — documents move, sidecars stay
# ---------------------------------------------------------------------------


def test_quarantine_moves_generated_documents(tmp_path):
    _make_packet(tmp_path)

    qdir = quarantine(tmp_path, stems=[STEM, ATS_STEM], reasons=["banned characters"])

    assert qdir == tmp_path / "quarantine"
    assert not (tmp_path / f"{STEM}.md").exists()
    assert not (tmp_path / f"{STEM}.docx").exists()
    assert not (tmp_path / f"{ATS_STEM}.ats.txt").exists()
    assert (qdir / f"{STEM}.md").exists()
    assert (qdir / f"{STEM}.docx").exists()
    assert (qdir / f"{ATS_STEM}.ats.txt").exists()


def test_quarantine_leaves_sidecars_in_packet_dir(tmp_path):
    _make_packet(tmp_path)

    quarantine(tmp_path, stems=[STEM, ATS_STEM], reasons=["banned characters"])

    assert (tmp_path / "vp-ops.job_description.md").exists()
    assert (tmp_path / "vp-ops.company_research.md").exists()
    assert (tmp_path / "vp-ops.fit_score.json").exists()
    assert (tmp_path / "vp-ops.packet_report.json").exists()


def test_quarantine_writes_why_txt_with_reasons_and_timestamp(tmp_path):
    _make_packet(tmp_path)
    stamp = datetime(2026, 9, 16, 12, 30, 0)

    qdir = quarantine(
        tmp_path,
        stems=[STEM, ATS_STEM],
        reasons=["banned characters", "unverified_total: '$590K' not in work history"],
        now=stamp,
    )

    why = (qdir / "WHY.txt").read_text(encoding="utf-8")
    assert "2026-09-16T12:30:00" in why
    assert "banned characters" in why
    assert "unverified_total: '$590K' not in work history" in why


def test_quarantine_why_txt_handles_no_reasons(tmp_path):
    _make_packet(tmp_path)

    qdir = quarantine(tmp_path, stems=[STEM], reasons=[])

    why = (qdir / "WHY.txt").read_text(encoding="utf-8")
    assert "no reasons given" in why


# ---------------------------------------------------------------------------
# quarantine() — safety
# ---------------------------------------------------------------------------


def test_quarantine_skips_missing_file_without_raising(tmp_path):
    _make_packet(tmp_path, stems=[STEM])  # no ATS file generated this build

    qdir = quarantine(tmp_path, stems=[STEM, ATS_STEM], reasons=["x"])

    assert (qdir / f"{STEM}.md").exists()
    assert not (qdir / f"{ATS_STEM}.ats.txt").exists()


def test_quarantine_does_not_clobber_existing_destination(tmp_path):
    _make_packet(tmp_path)
    qdir = tmp_path / "quarantine"
    qdir.mkdir()
    (qdir / f"{STEM}.md").write_text("PRE-EXISTING", encoding="utf-8")

    quarantine(tmp_path, stems=[STEM], reasons=["x"])

    # The pre-existing quarantined file must survive untouched...
    assert (qdir / f"{STEM}.md").read_text(encoding="utf-8") == "PRE-EXISTING"
    # ...and the source that could not be moved must still exist (not deleted).
    assert (tmp_path / f"{STEM}.md").exists()


@pytest.mark.parametrize(
    "bad_stem",
    [
        "../escape",
        "/etc/passwd",
        "sub/dir",
        "",
        "  ",
    ],
)
def test_quarantine_refuses_unsafe_stem(tmp_path, bad_stem):
    _make_packet(tmp_path)

    with pytest.raises(UnsafeStemError):
        quarantine(tmp_path, stems=[bad_stem], reasons=["x"])

    # Nothing on disk moved when the call is refused.
    assert (tmp_path / f"{STEM}.md").exists()
    assert not (tmp_path / "quarantine").exists()


def test_quarantine_refuses_unsafe_stem_before_moving_earlier_valid_stems(tmp_path):
    _make_packet(tmp_path)

    with pytest.raises(UnsafeStemError):
        quarantine(tmp_path, stems=[STEM, "../escape"], reasons=["x"])

    # The whole call is refused up front — even the safe stem's files stay put.
    assert (tmp_path / f"{STEM}.md").exists()
    assert not (tmp_path / "quarantine").exists()


# ---------------------------------------------------------------------------
# double-quarantine is a no-op
# ---------------------------------------------------------------------------


def test_double_quarantine_is_a_noop(tmp_path):
    _make_packet(tmp_path)
    stamp = datetime(2026, 9, 16, 9, 0, 0)

    first = quarantine(tmp_path, stems=[STEM, ATS_STEM], reasons=["first reason"], now=stamp)
    why_after_first = (first / "WHY.txt").read_text(encoding="utf-8")

    second = quarantine(
        tmp_path,
        stems=[STEM, ATS_STEM],
        reasons=["a completely different reason"],
        now=datetime(2026, 9, 17, 9, 0, 0),
    )
    why_after_second = (second / "WHY.txt").read_text(encoding="utf-8")

    assert second == first
    assert why_after_second == why_after_first  # untouched, not rewritten
    assert (first / f"{STEM}.md").exists()
    assert (first / f"{ATS_STEM}.ats.txt").exists()


# ---------------------------------------------------------------------------
# is_quarantined / quarantined_files
# ---------------------------------------------------------------------------


def test_is_quarantined_false_before_and_true_after(tmp_path):
    _make_packet(tmp_path)
    assert is_quarantined(tmp_path) is False

    quarantine(tmp_path, stems=[STEM, ATS_STEM], reasons=["x"])
    assert is_quarantined(tmp_path) is True


def test_quarantined_files_lists_documents_not_why_txt(tmp_path):
    _make_packet(tmp_path)
    quarantine(tmp_path, stems=[STEM, ATS_STEM], reasons=["x"])

    names = {p.name for p in quarantined_files(tmp_path)}
    assert names == {f"{STEM}.md", f"{STEM}.docx", f"{ATS_STEM}.ats.txt"}


def test_is_quarantined_false_for_untouched_packet(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    assert is_quarantined(tmp_path) is False
    assert quarantined_files(tmp_path) == []


# ---------------------------------------------------------------------------
# release() — round trip + audit trail
# ---------------------------------------------------------------------------


def test_release_moves_documents_back(tmp_path):
    _make_packet(tmp_path)
    quarantine(tmp_path, stems=[STEM, ATS_STEM], reasons=["x"])

    moved = release(tmp_path, reason="Alex confirmed the figure by hand")

    assert (tmp_path / f"{STEM}.md").exists()
    assert (tmp_path / f"{STEM}.docx").exists()
    assert (tmp_path / f"{ATS_STEM}.ats.txt").exists()
    assert set(moved) == {
        tmp_path / f"{STEM}.md",
        tmp_path / f"{STEM}.docx",
        tmp_path / f"{ATS_STEM}.ats.txt",
    }
    assert is_quarantined(tmp_path) is False


def test_release_appends_audit_line_with_reason_and_timestamp(tmp_path):
    _make_packet(tmp_path)
    quarantine(tmp_path, stems=[STEM, ATS_STEM], reasons=["x"])
    stamp = datetime(2026, 9, 16, 15, 45, 0)

    release(tmp_path, reason="Alex confirmed the figure by hand", now=stamp)

    audit = (tmp_path / "RELEASED.txt").read_text(encoding="utf-8")
    assert "2026-09-16T15:45:00" in audit
    assert "Alex confirmed the figure by hand" in audit


def test_release_audit_trail_accumulates_across_cycles(tmp_path):
    _make_packet(tmp_path)
    quarantine(tmp_path, stems=[STEM, ATS_STEM], reasons=["x"])
    release(tmp_path, reason="first override", now=datetime(2026, 9, 16, 10, 0, 0))

    # Re-quarantine (e.g. a rebuild finds a fresh fatal) and release again.
    quarantine(tmp_path, stems=[STEM, ATS_STEM], reasons=["y"], now=datetime(2026, 9, 16, 11, 0, 0))
    release(tmp_path, reason="second override", now=datetime(2026, 9, 16, 12, 0, 0))

    audit = (tmp_path / "RELEASED.txt").read_text(encoding="utf-8")
    assert "first override" in audit
    assert "second override" in audit
    assert audit.count("released:") == 2


def test_release_on_unquarantined_packet_is_a_noop(tmp_path):
    _make_packet(tmp_path)

    moved = release(tmp_path, reason="nothing to release")

    assert moved == []
    assert not (tmp_path / "RELEASED.txt").exists()


def test_release_does_not_clobber_existing_destination(tmp_path):
    _make_packet(tmp_path)
    quarantine(tmp_path, stems=[STEM], reasons=["x"])
    # Something else now occupies the spot the document would return to.
    (tmp_path / f"{STEM}.md").write_text("SOMETHING ELSE WROTE THIS", encoding="utf-8")

    release(tmp_path, reason="x")

    assert (tmp_path / f"{STEM}.md").read_text(encoding="utf-8") == "SOMETHING ELSE WROTE THIS"
    # The quarantined copy is preserved rather than lost.
    assert (tmp_path / "quarantine" / f"{STEM}.md").exists()

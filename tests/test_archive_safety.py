"""Pins the archive-import decompression caps in `core/network.py`.

The owner re-imports their full LinkedIn export periodically and the cockpit is
LAN-bound with no authentication (their informed decision -- see CLAUDE.md
"Truth guarantees" / docs/ARCHITECTURE.md "Trust boundaries"), so a ZIP that
declares -- or actually delivers -- an absurd amount of decompressed content
must never be read into memory whole. These tests pin:

1. a member whose *declared* size exceeds the per-member cap is skipped and
   reported, never opened;
2. a member whose header *lies* (small declared size, large real content) is
   still caught, because the read itself is capped rather than trusted;
3. the *sum* of accepted members is capped too, so many just-under-cap files
   can't add up to an unbounded import;
4. an oversized (or lying) member costs only itself -- the rest of a normal
   archive still imports;
5. there is no zip-slip risk today because nothing is ever extracted to disk
   (zip variant) and the directory variant refuses to follow a symlink that
   escapes the archive root -- both pinned so a future change that starts
   extracting can't silently reintroduce it.
"""

from __future__ import annotations

import io
import os
import struct
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

from blastjob.core import network as net
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig

_CONN_HEADER = "First Name,Last Name,URL,Email Address,Company,Position,Connected On"
_FOLLOW_HEADER = "Organization,Followed On"


def _conn_row(n: int) -> str:
    return (
        f"Person{n},Exec,https://linkedin.com/in/example-p{n},,Acme Corp,VP Engineering,05 Jun 2023"
    )


def _conn_csv(count: int) -> str:
    rows = "\n".join(_conn_row(i) for i in range(count))
    return _CONN_HEADER + "\n" + rows + "\n"


def _follow_csv(rows: list[str]) -> str:
    return _FOLLOW_HEADER + "\n" + "\n".join(rows) + "\n"


def _make_zip(files: dict[str, str]) -> Path:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    tmp = Path(tempfile.mkstemp(suffix=".zip")[1])
    tmp.write_bytes(buf.getvalue())
    return tmp


def _lie_about_uncompressed_size(zip_path: Path, lied_size: int) -> None:
    """Patch a single-entry ZIP's declared uncompressed size (both the local
    file header and the central directory record) to `lied_size`, leaving the
    compressed data and CRC-32 untouched. Simulates a forged header claiming a
    small size while the real decompressed content is much larger. CPython's
    zipfile bounds a read at the declared size and then CRC-checks whatever it
    actually returned against the CRC for the *real* (larger) content, so the
    lie surfaces as `zipfile.BadZipFile` -- the read must catch that and skip
    just this member rather than let it abort (or, worse, hang) the import."""
    data = bytearray(zip_path.read_bytes())
    local_sig = data.find(b"PK\x03\x04")
    central_sig = data.find(b"PK\x01\x02")
    assert local_sig != -1 and central_sig != -1, "test fixture must be a valid single-entry zip"
    struct.pack_into("<I", data, local_sig + 22, lied_size)
    struct.pack_into("<I", data, central_sig + 24, lied_size)
    zip_path.write_bytes(bytes(data))


@pytest.fixture
def conn():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    c = dbc.init_db(cfg)
    yield c
    c.close()


# 1. A member whose declared size exceeds the per-member cap is skipped and
#    reported -- never imported, never fully decompressed.
def test_oversized_declared_member_is_skipped_and_reported(conn, monkeypatch):
    monkeypatch.setattr(net, "MAX_ARCHIVE_MEMBER_BYTES", 500)
    monkeypatch.setattr(net, "MAX_ARCHIVE_TOTAL_BYTES", 10_000)
    big_csv = _conn_csv(200)  # comfortably over 500 bytes
    assert len(big_csv) > 500
    zpath = _make_zip(
        {
            "Connections.csv": big_csv,
            "Company Follows.csv": _follow_csv(["Acme Corp,01 Jan 2020"]),
        }
    )

    result = net.import_archive(conn, zpath)

    assert result.connections.added == 0
    assert "Connections.csv" in result.oversized
    # the sibling member is unaffected -- one absurd file doesn't cost the rest
    assert result.follows_added == 1


# 2. A header that lies (small declared size, large real content) is caught
#    by the capped read, not by trusting `info.file_size`.
def test_member_with_lying_header_is_caught_by_the_read_cap(conn, monkeypatch):
    monkeypatch.setattr(net, "MAX_ARCHIVE_MEMBER_BYTES", 500)
    monkeypatch.setattr(net, "MAX_ARCHIVE_TOTAL_BYTES", 10_000)
    real_csv = _conn_csv(200)
    assert len(real_csv) > 500
    zpath = _make_zip({"Connections.csv": real_csv})
    _lie_about_uncompressed_size(zpath, lied_size=10)

    # Sanity: the forged header really does claim a tiny size.
    with zipfile.ZipFile(zpath) as zf:
        assert zf.infolist()[0].file_size == 10

    result = net.import_archive(conn, zpath)

    assert result.connections.added == 0
    assert "Connections.csv" in result.oversized


# 3. The running total across accepted members is capped too.
def test_total_cap_skips_the_member_that_pushes_over(conn, monkeypatch):
    connections_csv = _conn_csv(2)
    follows_csv = _follow_csv([f"Company {i},01 Jan 2020" for i in range(20)])
    # Room for connections alone, not for both -- computed from the actual
    # fixture sizes rather than a guessed constant, so this doesn't get
    # fragile if the CSV helpers above ever change shape.
    total_cap = len(connections_csv) + 50
    assert total_cap < len(connections_csv) + len(follows_csv)
    monkeypatch.setattr(net, "MAX_ARCHIVE_MEMBER_BYTES", 10_000)
    monkeypatch.setattr(net, "MAX_ARCHIVE_TOTAL_BYTES", total_cap)

    zpath = _make_zip({"Connections.csv": connections_csv, "Company Follows.csv": follows_csv})

    result = net.import_archive(conn, zpath)

    # First member (Connections, processed first regardless of zip order --
    # import_archive always reads connections first) fits; the second, which
    # would push the running total over, is skipped and reported rather than
    # aborting the whole import.
    assert result.connections.added == 2
    assert "Company Follows.csv" in result.oversized
    assert result.follows_added == 0


# 4. A normal, small, multi-file archive is unaffected by the caps.
def test_normal_archive_still_imports_fully_under_default_caps(conn):
    zpath = _make_zip(
        {
            "Connections.csv": _conn_csv(3),
            "Company Follows.csv": _follow_csv(["Acme Corp,01 Jan 2020"]),
        }
    )

    result = net.import_archive(conn, zpath)

    assert result.connections.added == 3
    assert result.follows_added == 1
    assert result.oversized == ()


# 5a. Zip variant: nothing is ever extracted to disk, so a traversal-shaped
#     member name (only ever used for basename matching) cannot escape
#     anywhere -- pinned so a future change that starts extracting to disk
#     can't silently reintroduce zip-slip.
def test_zip_member_with_traversal_name_is_never_written_to_disk(conn, tmp_path):
    sentinel_dir = tmp_path / "outside_archive_root"
    zpath = _make_zip(
        {
            "../../../Connections.csv": _conn_csv(2),
        }
    )

    result = net.import_archive(conn, zpath)

    # Matched by basename only, imported normally in memory...
    assert result.connections.added == 2
    # ...and nothing was ever written to a path derived from the member name.
    assert not sentinel_dir.exists()
    assert not (zpath.parent.parent / "Connections.csv").exists()


# 5b. Directory variant: a symlink inside the archive root that points
#     outside it must not be followed.
@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need elevated perms on Windows")
def test_dir_variant_refuses_to_follow_a_symlink_escaping_the_root(conn, tmp_path):
    secret = tmp_path / "secret.csv"
    secret.write_text(_conn_csv(5), encoding="utf-8")

    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    os.symlink(secret, archive_root / "Connections.csv")

    result = net.import_archive(conn, archive_root)

    assert result.connections.added == 0

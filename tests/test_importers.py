from blastjob.importers.folder import collect, labeled_chunks
from blastjob.importers.text import extract


def test_text_extract(tmp_path):
    f = tmp_path / "resume.txt"
    f.write_text("Some resume content")
    result = extract(f)
    assert result == "Some resume content"


def test_folder_collect_txt(tmp_path):
    (tmp_path / "a.txt").write_text("Hello")
    (tmp_path / "b.md").write_text("World")
    pairs = collect(tmp_path)
    assert len(pairs) == 2


def test_folder_collect_single_file(tmp_path):
    f = tmp_path / "resume.txt"
    f.write_text("Single file resume")
    pairs = collect(f)
    assert len(pairs) == 1
    assert pairs[0][1] == "Single file resume"


def test_folder_skips_unsupported(tmp_path):
    (tmp_path / "file.json").write_text("{}")
    (tmp_path / "file.txt").write_text("content")
    pairs = collect(tmp_path)
    assert len(pairs) == 1


def test_labeled_chunks(tmp_path):
    (tmp_path / "resume.txt").write_text("My resume")
    chunks = labeled_chunks(tmp_path)
    assert len(chunks) == 1
    assert '<file path="' in chunks[0]
    assert "My resume" in chunks[0]


def test_labeled_chunks_on_skip_called_for_failed_file(tmp_path):
    good = tmp_path / "good.txt"
    good.write_text("Good resume content")

    # A .pdf with invalid content will fail pymupdf parsing
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"this is not a valid PDF")

    skipped: list[tuple] = []

    def on_skip(path, reason):
        skipped.append((path.name, reason))

    chunks = labeled_chunks(tmp_path, on_skip=on_skip)
    assert len(chunks) == 1  # Only the good txt file
    assert len(skipped) == 1
    assert skipped[0][0] == "bad.pdf"


def test_labeled_chunks_on_file_callback(tmp_path):
    (tmp_path / "a.txt").write_text("content a")
    (tmp_path / "b.txt").write_text("content b")

    calls: list[tuple] = []

    def on_file(i, total, name):
        calls.append((i, total, name))

    labeled_chunks(tmp_path, on_file=on_file)
    assert len(calls) == 2
    assert calls[0][0] == 1
    assert calls[1][0] == 2
    assert calls[0][1] == 2

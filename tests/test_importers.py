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

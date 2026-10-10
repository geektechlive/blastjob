from blastjob.exporters.ats import _strip_markdown, _wrap
from blastjob.exporters.ats import write as ats_write
from blastjob.exporters.markdown import write as md_write


def test_strip_headings():
    result = _strip_markdown("## Experience")
    assert result == "EXPERIENCE:"


def test_strip_bold():
    result = _strip_markdown("This is **bold** text")
    assert result == "This is bold text"


def test_strip_italic():
    result = _strip_markdown("This is *italic* text")
    assert result == "This is italic text"


def test_strip_frontmatter():
    md = "---\nname: John\n---\n\n## Skills"
    result = _strip_markdown(md)
    assert "name:" not in result


def test_wrap_long_line():
    line = "x" * 100
    result = _wrap(line)
    for line in result.splitlines():
        assert len(line) <= 80


def test_wrap_short_line_preserved():
    short = "This is a short line."
    result = _wrap(short)
    assert result == short


def test_markdown_write(tmp_path):
    content = "# Hello\n\nWorld"
    out = md_write(content, tmp_path)
    assert out.exists()
    assert out.read_text() == content


def test_ats_write_creates_file(tmp_path):
    content = "## Experience\n\nBuilt **things** at *Acme*."
    out = ats_write(content, tmp_path)
    assert out.exists()
    assert out.name == "resume.ats.txt"
    text = out.read_text()
    assert "EXPERIENCE:" in text
    assert "**" not in text
    assert "*" not in text


def test_ats_write_returns_path(tmp_path):
    out = ats_write("Plain text resume.", tmp_path)
    assert out == tmp_path / "resume.ats.txt"

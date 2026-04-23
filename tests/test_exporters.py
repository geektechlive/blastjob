from blastjob.exporters.ats import _strip_markdown, _wrap
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


def test_markdown_write(tmp_path):
    content = "# Hello\n\nWorld"
    out = md_write(content, tmp_path)
    assert out.exists()
    assert out.read_text() == content

"""Tests for full-JD extraction from JSON-LD JobPosting."""

from __future__ import annotations

from blastjob.discovery.enrich import extract_jobposting_jd

PAGE = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"WebPage"}
</script>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"JobPosting",
 "title":"Operations Director",
 "description":"&lt;p&gt;Lead Ops.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;AWS&lt;/li&gt;&lt;/ul&gt;"}
</script>
</head><body>noise</body></html>
"""


def test_extract_full_jd_from_jobposting():
    jd = extract_jobposting_jd(PAGE)
    assert jd is not None
    assert "Lead Ops." in jd
    assert "AWS" in jd
    assert "<" not in jd  # HTML stripped


def test_returns_none_without_jobposting():
    html = '<script type="application/ld+json">{"@type":"Organization"}</script>'
    assert extract_jobposting_jd(html) is None


def test_returns_none_on_garbage():
    assert extract_jobposting_jd("<html>no structured data</html>") is None


def test_handles_array_ld_json():
    html = """<script type="application/ld+json">
    [{"@type":"BreadcrumbList"},{"@type":"JobPosting","description":"Own IT strategy."}]
    </script>"""
    assert extract_jobposting_jd(html) == "Own IT strategy."

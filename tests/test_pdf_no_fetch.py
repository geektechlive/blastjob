"""Rendering a packet PDF must never fetch a resource.

The resume markdown is model output shaped by untrusted job and research text,
and markdown passes raw HTML through: an <img src="file:///..."> or an http URL
would make WeasyPrint read a local file into a PDF that goes to an employer, or
call out to an attacker's host. The exporter hands WeasyPrint a fetcher that
allows no protocol at all.

WeasyPrint >= 70 requires a `URLFetcher` instance (its fetch wrapper reads
`url_fetcher._fail_on_errors`, so a plain callable crashes the render), and
`allowed_protocols=None` means "all", so the empty tuple is load-bearing.
The native libs are absent in CI, so the module is faked here.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

from blastjob.exporters import pdf


def test_write_pdf_allows_no_fetch_protocol(monkeypatch, tmp_path: Path):
    seen: dict = {}

    class FakeURLFetcher:
        def __init__(self, **kwargs):
            seen["fetcher_kwargs"] = kwargs

    class FakeHTML:
        def __init__(self, *, string, url_fetcher=None, **kwargs):
            seen["fetcher"] = url_fetcher

        def write_pdf(self, target, stylesheets=None):
            Path(target).write_bytes(b"%PDF-fake")

    urls = types.SimpleNamespace(URLFetcher=FakeURLFetcher)
    fake = types.SimpleNamespace(HTML=FakeHTML, CSS=lambda string: string, urls=urls)
    monkeypatch.setitem(sys.modules, "weasyprint", fake)
    monkeypatch.setitem(sys.modules, "weasyprint.urls", urls)

    pdf.write('<img src="file:///etc/hosts">\n\n# Name', tmp_path)

    assert isinstance(seen["fetcher"], FakeURLFetcher), "HTML() built without our fetcher"
    allowed = seen["fetcher_kwargs"].get("allowed_protocols")
    assert allowed is not None and len(allowed) == 0

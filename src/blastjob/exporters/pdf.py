from pathlib import Path

RESUME_CSS = """
@page {
    size: letter;
    margin: 0.75in;
}
body {
    font-family: Georgia, 'Times New Roman', serif;
    font-size: 11pt;
    line-height: 1.4;
    color: #111;
}
h1 { font-size: 20pt; margin-bottom: 2pt; }
h2 { font-size: 13pt; border-bottom: 1px solid #555; margin-top: 12pt; margin-bottom: 4pt; }
h3 { font-size: 11pt; margin-top: 8pt; margin-bottom: 2pt; }
ul { margin: 4pt 0 4pt 16pt; padding: 0; }
li { margin-bottom: 2pt; }
p { margin: 4pt 0; }
em { color: #555; }
"""

HTML_WRAPPER = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><style>{css}</style></head>
<body>{body}</body>
</html>"""


def write(content_md: str, out_dir: Path, stem: str = "resume") -> Path:
    import markdown as md_lib
    from weasyprint import CSS, HTML
    from weasyprint.urls import URLFetcher

    html_body = md_lib.markdown(content_md, extensions=["tables", "nl2br", "fenced_code"])
    full_html = HTML_WRAPPER.format(css=RESUME_CSS, body=html_body)
    dest = out_dir / f"{stem}.pdf"
    # Fetch nothing. The markdown is model output shaped by untrusted job and
    # research text, and markdown passes raw HTML through, so an <img> pointing
    # at file:// or a remote host would otherwise be read into a PDF that is sent
    # to an employer. An empty tuple refuses every scheme (None would allow all).
    no_fetch = URLFetcher(allowed_protocols=(), allow_redirects=False)
    HTML(string=full_html, url_fetcher=no_fetch).write_pdf(
        str(dest), stylesheets=[CSS(string=RESUME_CSS)]
    )
    return dest

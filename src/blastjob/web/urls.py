"""Scheme allowlist for any URL that reaches an `href=` in a cockpit page.

Jinja autoescaping handles quotes and angle brackets, but it does not sanitize
URL schemes: `javascript:alert(1)` survives escaping intact and runs on click.
That matters more here than in most apps, because `base.html` carries the
per-launch CSRF token on `<body hx-headers=...>` -- script running in the
cockpit origin can read that attribute and drive every state-changing route
with a valid token. The cockpit has no auth and may be bound beyond this computer
(`web.host`, an opt-in), which is a reason to close this properly rather than
treat it as theoretical.

Two sources of untrusted URLs reach hrefs: LLM output distilled from pages read
through r.jina.ai (`profile.website`, `it_leadership[].url`, `sources[].url`)
and `job.url` from the discovery feeds.

Deliberately conservative: only absolute http(s) passes. Relative and
protocol-relative URLs return "" because every internal link in the templates
is a literal, so anything arriving through this filter is external by
definition.
"""

from __future__ import annotations

# Browsers skip leading control characters and ignore TAB/LF/CR *inside* a
# scheme, so "java\tscript:" and "\x01javascript:" both execute. The check runs
# against a copy with those characters removed; the value returned is the
# original, untouched apart from surrounding whitespace.
_IGNORED = {c: None for c in range(0x21)}
_IGNORED[0x7F] = None

_SAFE_SCHEMES = ("http://", "https://")


def safe_external_url(value: object) -> str:
    """`value` if it is an absolute http(s) URL, else "" (renders no href)."""
    if not isinstance(value, str):
        return ""
    url = value.strip()
    if not url:
        return ""
    probe = url.translate(_IGNORED).lower()
    if not probe.startswith(_SAFE_SCHEMES):
        return ""
    return url

"""Round 2 D4: every validated address is tried in turn; IPv6 answers pin; the
SNI name loses a trailing dot."""

from __future__ import annotations

import httpx

from blastjob.core import page_fetch as pf


def _transport(fail_hosts: set[str]):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host in fail_hosts:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, text="<html></html>")

    return httpx.MockTransport(handler), seen


def _answers(*addrs: str):
    async def resolve(_host: str) -> list[str]:
        return list(addrs)

    return resolve


async def test_a_dead_first_address_falls_through_to_the_next():
    transport, seen = _transport({"93.184.216.34"})
    page = await pf.safe_get("https://careers.example.org/x", transport=transport,
                             resolve=_answers("93.184.216.34", "93.184.216.35"))  # fmt: skip
    assert page is not None and page.status == 200
    assert [r.url.host for r in seen] == ["93.184.216.34", "93.184.216.35"]
    assert seen[1].headers["host"] == "careers.example.org"


async def test_every_address_must_be_public_or_the_hop_is_refused():
    transport, seen = _transport(set())
    page = await pf.safe_get("https://careers.example.org/x", transport=transport,
                             resolve=_answers("93.184.216.34", "10.0.0.5"))  # fmt: skip
    assert page is None and seen == []


async def test_an_ipv6_answer_is_pinned_with_the_name_as_host_and_sni():
    transport, seen = _transport(set())
    page = await pf.safe_get("https://careers.example.org/x", transport=transport,
                             resolve=_answers("2606:2800:220:1:248:1893:25c8:1946"))  # fmt: skip
    assert page is not None
    assert seen[0].url.host == "2606:2800:220:1:248:1893:25c8:1946"
    assert seen[0].headers["host"] == "careers.example.org"
    assert seen[0].extensions["sni_hostname"] == "careers.example.org"


async def test_trailing_dot_is_stripped_from_the_sni_name():
    transport, seen = _transport(set())
    await pf.safe_get("https://careers.example.org./x", transport=transport,
                      resolve=_answers("93.184.216.34"))  # fmt: skip
    assert seen[0].extensions["sni_hostname"] == "careers.example.org"

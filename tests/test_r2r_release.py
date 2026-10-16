"""Round 2, R7: a release whose override cannot be recorded must not leave the files
public under a report that still says quarantined."""

from __future__ import annotations

import json

from blastjob.core import quarantine
from tests.test_packet_release import TOKEN, _client, _job_with_packet, cfg, conn  # noqa: F401


def test_a_release_that_cannot_record_the_override_is_rolled_back(cfg, conn, monkeypatch):  # noqa: F811
    jid, out = _job_with_packet(conn, cfg)
    monkeypatch.setattr("blastjob.web.packet_view.record_override", lambda *_a, **_kw: False)

    r = _client(cfg).post(
        f"/jobs/{jid}/packet/release", data={"reason": "checked it by hand"}, headers=TOKEN
    )

    assert r.status_code == 500
    assert "HX-Refresh" not in r.headers
    # The folder and the sidecar agree again: still held, nothing public.
    assert quarantine.is_quarantined(out)
    assert not (out / "Alex_Rivera_Resume_-_Acme_VP_IT.docx").exists()
    report = json.loads((out / "vp-it.packet_report.json").read_text())
    assert report["state"] == "quarantined"
    assert "rolled back" in (out / "RELEASED.txt").read_text()


def test_a_retry_after_a_rolled_back_release_can_still_succeed(cfg, conn, monkeypatch):  # noqa: F811
    jid, out = _job_with_packet(conn, cfg)
    real = __import__("blastjob.web.packet_view", fromlist=["x"]).record_override
    monkeypatch.setattr("blastjob.web.packet_view.record_override", lambda *_a, **_kw: False)
    client = _client(cfg)
    client.post(f"/jobs/{jid}/packet/release", data={"reason": "first"}, headers=TOKEN)
    monkeypatch.setattr("blastjob.web.packet_view.record_override", real)

    r = client.post(f"/jobs/{jid}/packet/release", data={"reason": "second"}, headers=TOKEN)

    assert r.status_code == 200
    assert not quarantine.is_quarantined(out)
    report = json.loads((out / "vp-it.packet_report.json").read_text())
    assert report["state"] == "acknowledged_override"

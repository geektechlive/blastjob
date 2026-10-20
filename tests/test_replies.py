"""Human replies about applications already made: matching against app_notes /
company domain, ATS-acknowledgment and job-alert exclusion, ledger dedupe
across runs, and IMAP-failure isolation -- no real network."""

from __future__ import annotations

import tempfile

import pytest

from blastjob.db import client as dbc
from blastjob.discovery import mailalerts as ma
from blastjob.discovery import replies as rp
from blastjob.models.config import BlastJobConfig

SAM_REPLY = (
    "Full transparency - I have no feedback for anyone. Things are moving slow "
    "as Taylor has been swamped with other priorities, but I will let you know "
    "as soon as I hear anything."
)

ATS_ACK = (
    "Thank you for applying to the Generic VP Role position. Our team will "
    "review your application and reach out if there is a match."
)


@pytest.fixture
def db():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    cfg.discovery.mail_alerts_enabled = True
    conn = dbc.init_db(cfg)
    job_id = dbc.upsert_job(
        conn,
        source="dayforce:lakeshore",
        external_id="667",
        company="Lakeshore Credit Union",
        title="Generic VP Role",
        url="https://cockpit/lakeshore",
        review_state="kept",
        application_state="screen",
        app_notes="Emailed Sam Lee <sam.lee@example.org> on 2026-09-15.",
    )
    conn.commit()
    yield cfg, conn, job_id
    conn.close()


def _adapter_env(monkeypatch):
    monkeypatch.setenv("ICLOUD_IMAP_USER", "alex@example.com")
    monkeypatch.setenv("ICLOUD_IMAP_APP_PASSWORD", "app-pw")


def _fetch_returning(messages):
    def fake_fetch(**kw):
        assert kw["mailbox"] == "INBOX"
        return [m for m in messages if m.message_id not in kw["skip_message_ids"]]

    return fake_fetch


def _no_dates(**kw):
    return {}


# ---------- header-level candidate gate ----------

LINKEDIN = "linkedin.com"
INDEED = "indeed.com"


def test_linkedin_alert_sender_is_not_a_reply_candidate():
    assert not rp._is_reply_candidate("LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>", "x")


def test_indeed_alert_sender_is_not_a_reply_candidate():
    assert not rp._is_reply_candidate("Indeed <alert@indeed.com>", "x")


def test_alert_domain_in_a_display_name_or_local_part_is_still_a_reply_candidate():
    assert rp._is_reply_candidate('"Jane (ex-linkedin.com)" <jane@example.com>', "RE: role")
    assert rp._is_reply_candidate("Jane <jane.indeed.com@example.org>", "RE: role")
    assert rp._is_reply_candidate(f"Jane <jane@not{LINKEDIN}>", "RE: role")


def test_alert_sender_subdomain_is_not_a_reply_candidate():
    assert not rp._is_reply_candidate(f"Indeed <hello@jobalert.{INDEED}>", "x")


def test_alert_sender_behind_the_apple_relay_is_not_a_reply_candidate():
    relay = "privaterelay.appleid.com"
    sender = f"LinkedIn <updates_at_linkedin_com_a1b2c3d4e5_f6a7b8c@{relay}>"
    assert not rp._is_reply_candidate(sender, "x")


def test_ats_no_reply_sender_is_not_a_reply_candidate():
    assert not rp._is_reply_candidate("Greenhouse <no-reply@greenhouse.io>", "Application received")
    assert not rp._is_reply_candidate("ATS <donotreply@myworkday.com>", "x")


def test_a_human_sender_is_a_reply_candidate():
    assert rp._is_reply_candidate("Sam Lee <sam.lee@example.org>", "RE: VP Operations role")


# ---------- matching ----------


def test_app_notes_match_finds_the_job(db):
    _, conn, job_id = db
    jobs = rp._reply_candidate_jobs(conn)
    job, matched_by = rp.match_job("Sam Lee <sam.lee@example.org>", jobs)
    assert job is not None and int(job["id"]) == job_id and matched_by == "app_notes"


def test_domain_match_requires_exact_squashed_equality(db):
    _, conn, _job_id = db
    dbc.upsert_job(
        conn,
        source="t",
        external_id="99",
        company="Acme Industries",
        title="Chief Officer",
        url="https://x",
        review_state="kept",
        application_state="applied",
    )
    conn.commit()
    jobs = rp._reply_candidate_jobs(conn)
    job, matched_by = rp.match_job("Jane Doe <jane@acmeindustries.example>", jobs)
    assert job is not None and job["company"] == "Acme Industries" and matched_by == "domain"
    # a domain that merely resembles a company name is not accepted
    job2, matched_by2 = rp.match_job("Jane Doe <jane@acme-industries-careers.example>", jobs)
    assert job2 is None and matched_by2 == ""


def test_webmail_domain_never_matched_by_domain(db):
    _, conn, _job_id = db
    dbc.upsert_job(
        conn,
        source="t",
        external_id="100",
        company="Yahoo",
        title="Whatever",
        url="https://x",
        review_state="kept",
        application_state="applied",
    )
    conn.commit()
    jobs = rp._reply_candidate_jobs(conn)
    job, matched_by = rp.match_job("Someone <person@yahoo.com>", jobs)
    assert job is None and matched_by == ""


def test_reply_about_a_job_never_applied_to_does_not_match(db):
    _, conn, _job_id = db
    dbc.upsert_job(
        conn,
        source="t",
        external_id="55",
        company="Other Co",
        title="Chief Officer",
        url="https://x",
        review_state="kept",
        application_state="",  # never applied
        app_notes="recruiter@example.net",
    )
    conn.commit()
    jobs = rp._reply_candidate_jobs(conn)
    assert len(jobs) == 1  # only the fixture's 'screen' job is a candidate
    job, matched_by = rp.match_job("recruiter@example.net", jobs)
    assert job is None and matched_by == ""


# ---------- end-to-end scan_replies ----------


@pytest.mark.asyncio
async def test_genuine_reply_in_app_notes_is_surfaced(db, monkeypatch):
    cfg, conn, job_id = db
    _adapter_env(monkeypatch)
    msgs = [
        ma.MailMessage(
            "<reply1@lakeshorecu>",
            "10",
            "Sam Lee <sam.lee@example.org>",
            "RE: Generic VP Role",
            SAM_REPLY,
        )
    ]
    matches = await rp.scan_replies(
        cfg, conn, fetch_messages=_fetch_returning(msgs), fetch_dates=_no_dates
    )
    assert len(matches) == 1
    m = matches[0]
    assert m.job_id == job_id
    assert m.matched_by == "app_notes"
    assert m.sender == "Sam Lee <sam.lee@example.org>"
    assert "I have no feedback for anyone" in m.snippet
    # no draft / suggested reply text anywhere in the surfaced object
    assert "draft" not in m.snippet.lower()


@pytest.mark.asyncio
async def test_ats_acknowledgment_is_not_surfaced(db, monkeypatch):
    cfg, conn, _job_id = db
    _adapter_env(monkeypatch)
    msgs = [
        ma.MailMessage(
            "<ack1@dayforcehcm_lakeshore>",
            "11",
            "Lakeshore Credit Union Careers <no-reply@dayforcehcm.com>",
            "Thank you for applying",
            ATS_ACK,
        )
    ]
    matches = await rp.scan_replies(
        cfg, conn, fetch_messages=_fetch_returning(msgs), fetch_dates=_no_dates
    )
    assert matches == []


@pytest.mark.asyncio
async def test_linkedin_job_alert_is_not_surfaced(db, monkeypatch):
    cfg, conn, _job_id = db
    _adapter_env(monkeypatch)
    msgs = [
        ma.MailMessage(
            "<alert1@li>",
            "12",
            "LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>",
            "3 new jobs for you",
            "<p>Some job alert content that even mentions sam.lee@example.org</p>",
        )
    ]
    matches = await rp.scan_replies(
        cfg, conn, fetch_messages=_fetch_returning(msgs), fetch_dates=_no_dates
    )
    assert matches == []


@pytest.mark.asyncio
async def test_reply_about_unapplied_job_is_not_surfaced(db, monkeypatch):
    cfg, conn, _job_id = db
    _adapter_env(monkeypatch)
    dbc.upsert_job(
        conn,
        source="t",
        external_id="55",
        company="Other Co",
        title="Chief Officer",
        url="https://x",
        review_state="kept",
        application_state="",
        app_notes="recruiter@example.net",
    )
    conn.commit()
    msgs = [
        ma.MailMessage(
            "<other1@othercorp>",
            "13",
            "Some Recruiter <recruiter@example.net>",
            "Following up",
            "Just checking in on your application.",
        )
    ]
    matches = await rp.scan_replies(
        cfg, conn, fetch_messages=_fetch_returning(msgs), fetch_dates=_no_dates
    )
    assert matches == []


@pytest.mark.asyncio
async def test_same_message_is_not_surfaced_twice_across_runs(db, monkeypatch):
    cfg, conn, job_id = db
    _adapter_env(monkeypatch)
    msgs = [
        ma.MailMessage(
            "<reply1@lakeshorecu>",
            "10",
            "Sam Lee <sam.lee@example.org>",
            "RE: Generic VP Role",
            SAM_REPLY,
        )
    ]
    first = await rp.scan_replies(
        cfg, conn, fetch_messages=_fetch_returning(msgs), fetch_dates=_no_dates
    )
    assert len(first) == 1
    assert rp.processed_reply_ids(conn) == {"<reply1@lakeshorecu>"}

    # second run: the fake fetch honours skip_message_ids just like the real
    # adapter's IMAP SEARCH would (the message never gets re-fetched at all)
    second = await rp.scan_replies(
        cfg, conn, fetch_messages=_fetch_returning(msgs), fetch_dates=_no_dates
    )
    assert second == []


@pytest.mark.asyncio
async def test_scan_replies_is_a_noop_with_no_imap_credentials(db, monkeypatch):
    cfg, conn, _job_id = db
    monkeypatch.delenv("ICLOUD_IMAP_USER", raising=False)
    monkeypatch.delenv("ICLOUD_IMAP_APP_PASSWORD", raising=False)

    def should_not_fetch(**kw):
        raise AssertionError("must not fetch without credentials")

    matches = await rp.scan_replies(cfg, conn, fetch_messages=should_not_fetch)
    assert matches == []


@pytest.mark.asyncio
async def test_scan_replies_raises_on_imap_failure_for_caller_to_isolate(db, monkeypatch):
    """`scan_replies` itself propagates an IMAP failure -- isolation is the
    daily-runner's job (see test_daily.py), matching `_closure_pass`'s split
    of responsibilities."""
    cfg, conn, _job_id = db
    _adapter_env(monkeypatch)

    def boom(**kw):
        raise OSError("IMAP connection refused")

    with pytest.raises(OSError):
        await rp.scan_replies(cfg, conn, fetch_messages=boom)


# ---------- which mailbox is scanned ----------
#
# The defect these cover shipped green: matching worked, 1672 tests passed, and
# the scanner could not see a recruiter reply because it was pointed at the
# alert mailbox (iCloud) while every recruiter writes to the personal one
# (a webmail account). Nothing asserted which account `scan_replies` opens, so nothing
# failed. These assert exactly that.


def _capturing_fetch(captured, messages=()):
    def fake_fetch(**kw):
        captured.update(kw)
        return list(messages)

    return fake_fetch


def test_personal_account_falls_back_to_the_alert_account_when_unset():
    cfg = BlastJobConfig()
    acct = rp.personal_account(cfg.discovery)
    assert acct.host == cfg.discovery.mail_imap_host
    assert acct.user_env == cfg.discovery.mail_user_env
    assert acct.password_env == cfg.discovery.mail_password_env
    assert acct.mailbox == cfg.discovery.mail_mailbox


def test_personal_account_overrides_each_field_independently():
    cfg = BlastJobConfig()
    cfg.discovery.personal_mail_imap_host = "imap.mail.example.com"
    cfg.discovery.personal_mail_user_env = "PERSONAL_IMAP_USER"
    acct = rp.personal_account(cfg.discovery)
    assert acct.host == "imap.mail.example.com"
    assert acct.user_env == "PERSONAL_IMAP_USER"
    # untouched fields still fall back
    assert acct.password_env == cfg.discovery.mail_password_env
    assert acct.mailbox == cfg.discovery.mail_mailbox


@pytest.mark.asyncio
async def test_scan_opens_the_personal_mailbox_not_the_alert_one(db, monkeypatch):
    cfg, conn, _job_id = db
    _adapter_env(monkeypatch)  # the iCloud alert account, which must NOT be used
    monkeypatch.setenv("PERSONAL_IMAP_USER", "alex.rivera@example.com")
    monkeypatch.setenv("PERSONAL_IMAP_APP_PASSWORD", "personal-pw")
    cfg.discovery.personal_mail_imap_host = "imap.mail.example.com"
    cfg.discovery.personal_mail_user_env = "PERSONAL_IMAP_USER"
    cfg.discovery.personal_mail_password_env = "PERSONAL_IMAP_APP_PASSWORD"
    cfg.discovery.personal_mail_mailbox = "INBOX"

    captured: dict = {}
    await rp.scan_replies(cfg, conn, fetch_messages=_capturing_fetch(captured))

    assert captured["host"] == "imap.mail.example.com"
    assert captured["user"] == "alex.rivera@example.com"
    assert captured["password"] == "personal-pw"


@pytest.mark.asyncio
async def test_date_fetch_uses_the_same_account_as_the_message_fetch(db, monkeypatch):
    """Two IMAP calls, two chances to wire the wrong account."""
    cfg, conn, _job_id = db
    _adapter_env(monkeypatch)
    monkeypatch.setenv("PERSONAL_IMAP_USER", "alex.rivera@example.com")
    monkeypatch.setenv("PERSONAL_IMAP_APP_PASSWORD", "personal-pw")
    cfg.discovery.personal_mail_imap_host = "imap.mail.example.com"
    cfg.discovery.personal_mail_user_env = "PERSONAL_IMAP_USER"
    cfg.discovery.personal_mail_password_env = "PERSONAL_IMAP_APP_PASSWORD"

    msgs = [
        ma.MailMessage(
            "<reply1@lakeshorecu>",
            "10",
            "Sam Lee <sam.lee@example.org>",
            "RE: Generic VP Role",
            SAM_REPLY,
        )
    ]
    captured: dict = {}

    def capturing_dates(**kw):
        captured.update(kw)
        return {}

    matches = await rp.scan_replies(
        cfg, conn, fetch_messages=_fetch_returning(msgs), fetch_dates=capturing_dates
    )
    assert len(matches) == 1
    assert captured["host"] == "imap.mail.example.com"
    assert captured["user"] == "alex.rivera@example.com"
    assert captured["password"] == "personal-pw"


def test_replies_are_configured_independently_of_the_alert_flag(monkeypatch):
    """Reply scanning must not hang off `mail_alerts_enabled` -- that is a flag
    about a different account's different purpose. Credentials decide."""
    cfg = BlastJobConfig()
    cfg.discovery.mail_alerts_enabled = False
    monkeypatch.setenv(cfg.discovery.mail_user_env, "alex@example.com")
    monkeypatch.setenv(cfg.discovery.mail_password_env, "app-pw")
    assert rp.replies_configured(cfg) is True


def test_replies_are_not_configured_without_credentials(monkeypatch):
    cfg = BlastJobConfig()
    cfg.discovery.mail_alerts_enabled = True
    monkeypatch.delenv(cfg.discovery.mail_user_env, raising=False)
    monkeypatch.delenv(cfg.discovery.mail_password_env, raising=False)
    assert rp.replies_configured(cfg) is False


# ---------- the header gate keeps unrelated mail unread ----------


def test_header_gate_body_fetches_only_senders_that_match_a_job(db):
    """Matching needs only the sender, so it belongs in the header gate: the
    live mailbox had 58 messages pass the old gate (bank, medical, personal),
    every one of them body-fetched into a process whose log we just scrubbed."""
    _cfg, conn, _job_id = db
    jobs = rp._reply_candidate_jobs(conn)
    gate = rp.reply_gate(jobs)
    assert gate("Sam Lee <sam.lee@example.org>", "RE: Generic VP Role") is True
    assert gate("Parcel Service <notices@example.com>", "Your package was delivered") is False
    assert gate("Dr. Office <scheduling@example.net>", "Your test results") is False


# ---------- a dry run must not consume a reply ----------


@pytest.mark.asyncio
async def test_dry_run_does_not_stamp_the_ledger(db, monkeypatch):
    """`--dry-run` is documented as "print the digest, send/stamp nothing".
    Stamping here is worse than a cosmetic contract break: the reply is marked
    seen, and the real 07:30 run then never surfaces it."""
    cfg, conn, _job_id = db
    _adapter_env(monkeypatch)
    msgs = [
        ma.MailMessage(
            "<reply1@lakeshorecu>",
            "10",
            "Sam Lee <sam.lee@example.org>",
            "RE: Generic VP Role",
            SAM_REPLY,
        )
    ]
    matches = await rp.scan_replies(
        cfg, conn, fetch_messages=_fetch_returning(msgs), fetch_dates=_no_dates, record=False
    )
    assert len(matches) == 1  # still surfaced
    assert rp.processed_reply_ids(conn) == set()  # but not consumed

    # and the next real run still sees it
    again = await rp.scan_replies(
        cfg, conn, fetch_messages=_fetch_returning(msgs), fetch_dates=_no_dates
    )
    assert len(again) == 1
    assert rp.processed_reply_ids(conn) == {"<reply1@lakeshorecu>"}


# ---------- the snippet is what the user reads on their phone ----------


def test_snippet_drops_style_block_contents():
    """Live output opened with 'P {margin-top:0;margin-bottom:0;}' -- Outlook
    ships a <style> block and stripping tags alone leaves the CSS behind."""
    html = (
        "<style>P {margin-top:0;margin-bottom:0;}</style>"
        "<p>Full transparency - no feedback yet.</p>"
    )
    assert rp._snippet(html).startswith("Full transparency")
    assert "margin-top" not in rp._snippet(html)


def test_snippet_decodes_html_entities():
    html = "<p>Slow&nbsp;&mdash; Alex &lt;alex.rivera@example.com&gt; will hear soon.</p>"
    out = rp._snippet(html)
    assert "&nbsp;" not in out and "&lt;" not in out
    assert "<alex.rivera@example.com>" in out


def test_snippet_drops_script_contents_too():
    assert "alert" not in rp._snippet("<script>alert('x')</script><p>Real text.</p>")


def test_snippet_stops_at_the_quoted_original():
    """The live digest line read: "I appreciate the feedback, timing is
    everything! -Sam From: Alex Rivera <alex.rivera@example.com> Sent: Tuesday..."
    -- eight words of Sam, then Alex's own message quoted back at them. On a
    phone that is the whole snippet."""
    body = (
        "I appreciate the feedback, timing is everything! -Sam "
        "From: Alex Rivera <alex.rivera@example.com> Sent: Tuesday, September 15, 2026 9:35 AM "
        "To: Lee, Sam <sam.lee@example.org> Subject: [EXTERNAL] Re: Following up"
    )
    out = rp._snippet(body)
    assert out.startswith("I appreciate the feedback")
    assert "From:" not in out and "Subject:" not in out


def test_snippet_stops_at_an_on_date_wrote_quote():
    body = "Thanks, I will check. On Tue, Sep 15, 2026 at 9:35 AM Alex Rivera wrote: > my text"
    out = rp._snippet(body)
    assert out.startswith("Thanks, I will check")
    assert "wrote:" not in out


def test_snippet_stops_at_an_original_message_divider():
    body = "Sounds good. -----Original Message----- From: Alex Rivera"
    assert rp._snippet(body) == "Sounds good."


def test_snippet_keeps_the_whole_body_when_nothing_is_quoted():
    assert rp._snippet("<p>Short and unquoted.</p>") == "Short and unquoted."


# ---------- exact-address matching and deterministic order (#46) ----------


def _applied_job(conn, ext_id, company, notes="", applied_at=""):
    job_id = dbc.upsert_job(
        conn,
        source="t",
        external_id=ext_id,
        company=company,
        title="Chief Officer",
        url="https://x",
        review_state="kept",
        application_state="applied",
        app_notes=notes,
    )
    conn.execute("UPDATE jobs SET applied_at=? WHERE id=?", (applied_at, job_id))
    conn.commit()
    return job_id


def test_notes_address_is_not_matched_as_a_substring_of_a_longer_local_part(db):
    _, conn, _ = db
    _applied_job(conn, "e1", "Zed Co", notes="Wrote to jimbob@example.com")
    job, matched_by = rp.match_job("bob@example.com", rp._reply_candidate_jobs(conn))
    assert job is None and matched_by == ""


def test_notes_address_is_not_matched_as_a_substring_of_a_longer_domain(db):
    _, conn, _ = db
    _applied_job(conn, "e2", "Zed Co", notes="Wrote to jane@example.org.example")
    job, matched_by = rp.match_job("jane@example.org", rp._reply_candidate_jobs(conn))
    assert job is None and matched_by == ""


def test_notes_address_match_is_case_insensitive_and_exact(db):
    _, conn, _ = db
    job_id = _applied_job(conn, "e3", "Zed Co", notes="Met Bob <Bob@Example.com>.")
    job, matched_by = rp.match_job("Bob <bob@example.com>", rp._reply_candidate_jobs(conn))
    assert job is not None and int(job["id"]) == job_id and matched_by == "app_notes"


@pytest.mark.parametrize("order", [("old", "new"), ("new", "old")])
def test_domain_match_prefers_the_most_recently_applied_job(db, order):
    _, conn, _ = db
    ids = {}
    for key in order:
        stamp = "2026-01-01" if key == "old" else "2026-09-01"
        ids[key] = _applied_job(conn, f"d-{key}", "Acme Industries", applied_at=stamp)
    job, matched_by = rp.match_job(
        "Jane <jane@acmeindustries.example>", rp._reply_candidate_jobs(conn)
    )
    assert job is not None and int(job["id"]) == ids["new"] and matched_by == "domain"


def test_job_with_empty_applied_at_sorts_after_dated_ones(db):
    _, conn, _ = db
    undated = _applied_job(conn, "u1", "Acme Industries", applied_at="")
    dated = _applied_job(conn, "u2", "Acme Industries", applied_at="2026-05-05")
    ids = [
        int(j["id"]) for j in rp._reply_candidate_jobs(conn) if j["company"] == "Acme Industries"
    ]
    assert ids == [dated, undated]

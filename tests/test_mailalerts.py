"""Alert-email parsers (LinkedIn / Indeed shapes), sender routing, the ledger
contract, and pipeline integration — no IMAP, no network."""

from __future__ import annotations

import email
import email.message
import email.policy
import logging
import tempfile
from urllib.parse import quote

import pytest

from blastjob.db import client as dbc
from blastjob.discovery import mailalerts as ma
from blastjob.discovery.pipeline import processed_message_ids, run_discovery
from blastjob.models.config import BlastJobConfig

LINKEDIN_HTML = """
<html><body>
<style>.x{color:red}</style>
<p>Your job alert for Chief Operations Officer in Fairhaven</p>
<table><tr><td>
  <a href="https://www.linkedin.com/comm/jobs/view/4123456789/?trackingId=abc&refId=x">
    Chief Operations Officer</a>
  <p>Acme Health · Fairhaven, OH</p>
  <p>Actively recruiting · 2 days ago</p>
</td></tr><tr><td>
  <a href="https://www.linkedin.com/comm/jobs/view/ops-at-beta-4987654321?trk=eml">
    VP, Business Operations</a>
  <p>Beta Financial · Fairview, OH (Hybrid)</p>
</td></tr><tr><td>
  <a href="https://www.linkedin.com/comm/jobs/view/4123456789/?trackingId=dup">See job</a>
  <a href="https://www.linkedin.com/comm/jobs/search/?keywords=ops">See all jobs</a>
</td></tr></table>
<a href="https://www.linkedin.com/e/v2?e=unsubscribe">Unsubscribe</a>
</body></html>
"""

INDEED_HTML = """
<html><body>
<a href="https://www.indeed.com/rc/clk/dl?jk=0a1b2c3d4e5f6789&from=ja&tk=1">
  Director of Business Operations</a>
<div>Gamma Manufacturing</div><div>Oakbrook, OH</div>
<div>$180,000 - $220,000 a year</div>
<a href="https://www.indeed.com/viewjob?jk=ffee00112233aabb&from=ja">Ops Director - Remote</a>
<div>Delta Corp</div><div>Remote in Ohio</div>
<a href="https://www.indeed.com/jobs?q=director&l=OH">More jobs</a>
</body></html>
"""

_CTS_NESTED_URL = quote(
    "https://www.indeed.com/viewjob?jk=aaaa11112222bbbb&from=ja&tk=xyz", safe=""
)

# Real-world shapes: a cts.indeed.com click-tracking wrapper (jk buried in the
# nested, percent-encoded viewjob URL), a /pagead/clk link, a /rc/clk/dl link,
# assorted salary-line formats, template chrome mixed into the "after" text,
# and a non-job "More jobs" anchor that must be ignored.
INDEED_REAL_HTML = f"""
<html><body>
<a href="https://cts.indeed.com/v1/rpt/dl?ctk=abc123&url={_CTS_NESTED_URL}">
  Chief Operations Officer</a>
<div>Northwind Health</div><div>Rivermont, OH</div>
<div>Easily apply</div>
<div>$180,000 - $220,000 a year</div>
<div>Posted 3 days ago</div>

<a href="https://www.indeed.com/pagead/clk?ad=1234&jk=cccc33334444dddd&from=ja">
  Director of Operations</a>
<div>Lakeside Manufacturing</div><div>Hybrid remote in Oakbrook, OH</div>
<div>Urgently hiring</div>

<a href="https://www.indeed.com/rc/clk/dl?jk=eeee55556666ffff&from=ja&tk=1">
  VP, Business Operations - Remote</a>
<div>Prairie Systems</div><div>Remote</div>
<div>From $150,000 a year</div>
<div>Responsive employer</div>

<a href="https://www.indeed.com/jobs?q=ops&l=Rivermont%2C+OH">More jobs</a>
<a href="https://www.indeed.com/alertsmanage">Manage alerts</a>
</body></html>
"""


def test_parse_linkedin_alert_extracts_jobs_and_dedupes():
    jobs = ma.parse_linkedin_alert(LINKEDIN_HTML)
    assert [j.external_id for j in jobs] == ["4123456789", "4987654321"]
    a, b = jobs
    assert a.source == "linkedin"
    assert a.title == "Chief Operations Officer"
    assert a.company == "Acme Health"
    assert (a.city, a.region) == ("Fairhaven", "OH")
    assert a.url == "https://www.linkedin.com/jobs/view/4123456789/"
    assert "Actively recruiting" in a.description
    assert b.company == "Beta Financial"
    assert b.location_raw.startswith("Fairview")
    assert a.remote is None and b.remote is None
    assert "color:red" not in a.description  # style stripped


def test_country_only_location_means_remote():
    html = (
        '<a href="https://www.linkedin.com/comm/jobs/view/4383892749/">Division VP Ops</a>'
        "<p>Fabrikam Instruments · United States</p><p>Actively recruiting</p>"
    )
    (p,) = ma.parse_linkedin_alert(html)
    assert p.remote is True and p.company == "Fabrikam Instruments"
    assert p.location_raw == "United States"


def test_parse_indeed_alert_extracts_jk_ids():
    jobs = ma.parse_indeed_alert(INDEED_HTML)
    assert [j.external_id for j in jobs] == ["0a1b2c3d4e5f6789", "ffee00112233aabb"]
    a, b = jobs
    assert a.company == "Gamma Manufacturing"
    assert (a.city, a.region) == ("Oakbrook", "OH")
    assert "$180,000" in a.description
    assert a.url == "https://www.indeed.com/viewjob?jk=0a1b2c3d4e5f6789"
    assert b.remote is True


def test_parse_indeed_alert_extracts_jk_from_tracking_wrappers():
    """cts.indeed.com click wrapper (jk nested in an encoded viewjob URL),
    /pagead/clk, and /rc/clk/dl all yield a posting; the search-link
    'More jobs' anchor and the 'Manage alerts' anchor do not."""
    jobs = ma.parse_indeed_alert(INDEED_REAL_HTML)
    ids = {j.external_id for j in jobs}
    assert ids == {"aaaa11112222bbbb", "cccc33334444dddd", "eeee55556666ffff"}
    assert len(jobs) == 3


def test_indeed_alert_strips_chrome_lines_and_parses_salary():
    jobs = {j.external_id: j for j in ma.parse_indeed_alert(INDEED_REAL_HTML)}
    job_a = jobs["aaaa11112222bbbb"]
    assert job_a.company == "Northwind Health"
    assert (job_a.city, job_a.region) == ("Rivermont", "OH")
    assert (job_a.comp_min, job_a.comp_max) == (180000, 220000)
    assert job_a.comp_summary == "$180,000 - $220,000 a year"
    assert "Easily apply" not in job_a.description
    assert "Posted 3 days ago" not in job_a.description


def test_indeed_alert_recognizes_hybrid_remote_and_bare_remote_lines():
    jobs = {j.external_id: j for j in ma.parse_indeed_alert(INDEED_REAL_HTML)}
    director = jobs["cccc33334444dddd"]
    assert director.remote is True
    assert (director.city, director.region) == ("Oakbrook", "OH")
    assert "Urgently hiring" not in director.description

    vp = jobs["eeee55556666ffff"]
    assert vp.remote is True and vp.location_raw == "Remote"
    assert (vp.comp_min, vp.comp_max) == (150000, 0)  # "From": a minimum, max unknown
    assert "Responsive employer" not in vp.description


@pytest.mark.parametrize(
    "salary_line,expected",
    [
        ("$95 an hour", (0, 0, "$95 an hour")),
        ("From $150,000 a year", (150000, 0, "From $150,000 a year")),
        ("Up to $200K a year", (200000, 200000, "Up to $200K a year")),
        ("$150K - $200K", (150000, 200000, "$150K - $200K")),
    ],
)
def test_indeed_salary_line_formats(salary_line, expected):
    html = f"""
    <a href="https://www.indeed.com/viewjob?jk=1122334455667788">Some Title</a>
    <div>Some Co</div><div>Fairview, OH</div>
    <div>{salary_line}</div>
    """
    (job,) = ma.parse_indeed_alert(html)
    assert (job.comp_min, job.comp_max, job.comp_summary) == expected


def test_indeed_plaintext_fallback_scans_text_for_jk_urls():
    """No anchors at all (text/plain alert): find jk via viewjob/rc-clk-dl
    URLs, title from the nearest preceding non-URL line."""
    text = (
        "Your daily Indeed alert\n\n"
        "Chief Operations Officer\n"
        "Northwind Health - Rivermont, OH\n"
        "https://www.indeed.com/viewjob?jk=1122334455667788\n\n"
        "https://www.indeed.com/rc/clk/dl?jk=99aa88bb77cc66dd\n\n"
        "Manage your alerts at indeed.com/alerts\n"
    )
    jobs = {j.external_id: j for j in ma.parse_indeed_alert(text)}
    assert set(jobs) == {"1122334455667788", "99aa88bb77cc66dd"}
    first = jobs["1122334455667788"]
    assert first.title == "Northwind Health - Rivermont, OH"
    assert first.url == "https://www.indeed.com/viewjob?jk=1122334455667788"
    second = jobs["99aa88bb77cc66dd"]
    assert second.title == "Indeed posting"  # no title line directly precedes it


def test_html_body_falls_back_to_plaintext_when_no_html_part():
    msg = email.message.EmailMessage(policy=email.policy.default)
    msg["Subject"] = "plain alert"
    msg.set_content("plain text body with a jk=12345678 link")
    body = ma._html_body(msg)
    assert "plain text body" in body


def test_parse_alert_routes_by_sender_then_by_body():
    li = "LinkedIn Job Alerts <jobs-noreply@linkedin.com>"
    assert ma.parse_alert(li, LINKEDIN_HTML)[0] == "linkedin"
    assert ma.parse_alert("Indeed <alert@indeed.com>", INDEED_HTML)[0] == "indeed"
    # relay rewrote the sender beyond recognition: the body still identifies the source
    assert ma.parse_alert("relay_abc123@relay.example.com", INDEED_HTML)[0] == "indeed"
    assert ma.parse_alert("someone@example.com", "<a href='x'>y</a>") == ("", [])


def test_normalize_sender_undoes_hide_my_email_rewrite():
    relay = "Indeed <donotreply_at_jobalert_indeed_com_a1b2c3d4e5_f6a7b8c@relay.example.com>"
    assert "jobalert.indeed.com" in ma.normalize_sender(relay)
    assert ma.normalize_sender("Plain <a@example.com>") == "plain <a@example.com>"
    # '_at_' somewhere that is not a relay fold must not crash or rewrite
    assert (
        ma.normalize_sender("Look_at_this <news@example.com>") == "look_at_this <news@example.com>"
    )
    assert ma.normalize_sender("x@relay_at_home.com") == "x@relay_at_home.com"
    assert ma.looks_like_alert(relay, "whatever")
    assert ma.parse_alert(relay, INDEED_HTML)[0] == "indeed"


def test_relay_display_name_and_mixed_case_headers_route_to_indeed():
    """The real inbox form: display name kept, relay local-part rewritten.
    Also confirm a mixed-case header (relay or otherwise) still routes."""
    relay = '"Indeed" <donotreply_at_jobalert_indeed_com_a1b2c3d4e5_f6a7b8c@relay.example.com>'
    assert ma.looks_like_alert(relay, "3 new jobs match your search")
    assert ma.parse_alert(relay, INDEED_HTML)[0] == "indeed"

    mixed_case = '"INDEED" <DoNotReply_At_JobAlert_Indeed_Com_x@relay.example.com>'
    assert ma.looks_like_alert(mixed_case, "whatever")
    assert ma.parse_alert(mixed_case, INDEED_HTML)[0] == "indeed"


def test_linkedin_notifications_are_not_alerts():
    assert not ma.looks_like_alert(
        "LinkedIn <notifications-noreply@linkedin.com>", "your posts got 703 impressions"
    )
    assert not ma.looks_like_alert("LinkedIn <messages-noreply@linkedin.com>", "X commented")
    assert ma.looks_like_alert(
        "LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>", "Your weekly digest: your job alert"
    )


def test_looks_like_alert_by_domain_or_subject():
    assert ma.looks_like_alert("Indeed <alert@indeed.com>", "anything")
    assert ma.looks_like_alert("relay@relay.example.com", "New jobs for you: Ops in Fairhaven")
    assert ma.looks_like_alert("relay@relay.example.com", "12 new jobs match your Job Alert")
    assert not ma.looks_like_alert("mom@example.com", "Dinner Sunday?")


def test_fetch_alert_messages_filters_by_header_and_ledger():
    """Drive the IMAP client with a fake: SEARCH by date, header fetch for all,
    body fetch only for alert-looking, unprocessed messages."""
    import datetime as dt

    hdrs = {
        b"1": b"Message-ID: <a@x>\r\nFrom: Indeed <alert@indeed.com>\r\nSubject: 3 new\r\n\r\n",
        b"2": b"Message-ID: <b@x>\r\nFrom: mom@example.com\r\nSubject: Dinner\r\n\r\n",
        b"3": b"Message-ID: <c@x>\r\nFrom: relay@example.com\r\nSubject: New jobs for you\r\n\r\n",
        b"4": b"Message-ID: <d@x>\r\nFrom: alert@indeed.com\r\nSubject: seen\r\n\r\n",
        b"5": (
            b"Message-ID: <e@x>\r\nFrom: notifications-noreply@linkedin.com\r\n"
            b"Subject: impressions\r\n\r\n"
        ),
    }
    body = b"Message-ID: <z@x>\r\nContent-Type: text/html\r\n\r\n" + INDEED_HTML.encode()
    fetched: list[str] = []

    class FakeIMAP:
        def __init__(self, host):
            pass

        def login(self, u, p):
            return "OK", []

        def select(self, mailbox, readonly=False):
            return "OK", []

        def logout(self):
            return "BYE", []

        def uid(self, cmd, *args):
            if cmd == "SEARCH":
                return "OK", [b"1 2 3 4 5"]
            uid, spec = args
            fetched.append(f"{uid}:{'hdr' if 'HEADER' in spec else 'body'}")
            if "HEADER" in spec:
                return "OK", [(b"x", hdrs[uid.encode()])]
            return "OK", [(b"x", body)]

    msgs = ma.fetch_alert_messages(
        host="h",
        user="u",
        password="p",
        since=dt.date(2026, 9, 1),
        skip_message_ids={"<d@x>"},
        connect=FakeIMAP,
    )
    assert [m.message_id for m in msgs] == ["<a@x>", "<c@x>"]
    assert not any(f in fetched for f in ("2:body", "4:body", "5:body"))
    assert "indeed.com/viewjob" in msgs[0].html or "jk=" in msgs[0].html


def _adapter(messages, skip=()):
    def fake_fetch(**kw):
        assert kw["skip_message_ids"] == set(skip) and kw["mailbox"] == "INBOX"
        return [m for m in messages if m.message_id not in kw["skip_message_ids"]]

    return ma.MailAlertAdapter(
        host="h",
        user="u",
        password="p",
        skip_message_ids=set(skip),
        fetch_messages=fake_fetch,
        enrich=False,
    )


@pytest.mark.asyncio
async def test_adapter_builds_ledger_and_dedupes_across_messages():
    msgs = [
        ma.MailMessage("<m1@li>", "10", "jobs-noreply@linkedin.com", "alert", LINKEDIN_HTML),
        ma.MailMessage("<m2@li>", "11", "jobs-noreply@linkedin.com", "alert", LINKEDIN_HTML),
        ma.MailMessage("<m3@in>", "12", "alert@indeed.com", "alert", INDEED_HTML),
        ma.MailMessage("<m4@x>", "13", "news@example.com", "spam", "<p>hi</p>"),
    ]
    a = _adapter(msgs)
    out = await a.fetch()
    assert len(out) == 4  # 2 linkedin (deduped across m1/m2) + 2 indeed
    statuses = {e.message_id: e.status for e in a.ledger}
    assert statuses == {"<m1@li>": "ok", "<m2@li>": "ok", "<m3@in>": "ok", "<m4@x>": "ignored"}
    assert a.snapshot is False


@pytest.mark.asyncio
async def test_adapter_dumps_debug_html_on_zero_postings(tmp_path, caplog):
    """A recognized-source alert that parses to 0 postings (e.g. Indeed's
    'your alert is now active' confirmation) stays ledger status 'ok' but
    logs a warning and dumps the body once for template capture."""
    confirmation = "Your job alert is now active.\nYou'll get an email when new jobs match."
    msgs = [ma.MailMessage("<empty@in>", "20", "alert@indeed.com", "Alert active", confirmation)]
    debug_dir = tmp_path / "mail-debug"
    a = ma.MailAlertAdapter(
        host="h",
        user="u",
        password="p",
        skip_message_ids=set(),
        fetch_messages=lambda **kw: msgs,
        enrich=False,
        debug_dir=debug_dir,
    )
    with caplog.at_level(logging.WARNING):
        out = await a.fetch()
    assert out == []
    (entry,) = a.ledger
    # "empty" not "ok": a recognized alert that parsed to nothing is retryable.
    # Recording it as ok meant the pipeline's status filter skipped that
    # Message-ID permanently, so a template change burned every alert in the
    # window with no way back.
    assert entry.status == "empty" and entry.postings == 0 and entry.source == "indeed"
    assert "alert parsed to 0 postings" in caplog.text
    dumped = list(debug_dir.glob("*.html"))
    assert len(dumped) == 1
    assert dumped[0].read_text() == confirmation


@pytest.mark.asyncio
async def test_adapter_debug_dump_never_raises_without_debug_dir():
    """debug_dir is optional; a zero-posting message is still fine (no crash,
    no directory created)."""
    msgs = [ma.MailMessage("<empty@in>", "20", "alert@indeed.com", "Alert active", "no jobs here")]
    a = _adapter(msgs)
    out = await a.fetch()
    assert out == []


@pytest.mark.asyncio
async def test_adapter_debug_dump_respects_20_file_cap(tmp_path):
    debug_dir = tmp_path / "mail-debug"
    debug_dir.mkdir()
    for i in range(20):
        (debug_dir / f"existing_{i}.html").write_text("x", encoding="utf-8")
    msgs = [ma.MailMessage("<empty2@in>", "21", "alert@indeed.com", "alert active", "still none")]
    a = ma.MailAlertAdapter(
        host="h",
        user="u",
        password="p",
        skip_message_ids=set(),
        fetch_messages=lambda **kw: msgs,
        enrich=False,
        debug_dir=debug_dir,
    )
    await a.fetch()
    assert len(list(debug_dir.glob("*.html"))) == 20  # cap respected — nothing new written


@pytest.mark.asyncio
async def test_pipeline_records_ledger_and_skips_processed_next_run():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    conn = dbc.init_db(cfg)
    msgs = [ma.MailMessage("<m1@li>", "10", "jobs-noreply@linkedin.com", "s", LINKEDIN_HTML)]
    res = await run_discovery(cfg, conn, adapters=[_adapter(msgs)])
    assert res.inserted_or_updated == 2
    assert processed_message_ids(conn) == {"<m1@li>"}
    # stored under the posting's real origin, not the adapter's umbrella name
    srcs = {r["source"] for r in conn.execute("SELECT source FROM jobs").fetchall()}
    assert srcs == {"linkedin"}
    # second run: the pipeline hands the ledger back as skip ids; nothing re-parsed
    res2 = await run_discovery(cfg, conn, adapters=[_adapter(msgs, skip=["<m1@li>"])])
    assert res2.fetched == 0 and res2.stale_marked == 0
    rows = conn.execute("SELECT source_state FROM jobs").fetchall()
    assert all(r["source_state"] == "active" for r in rows)
    conn.close()


def test_pipeline_reports_misconfigured_mail_alerts(monkeypatch):
    """Enabled but no creds -> a visible per-source error, never a crash."""
    import asyncio

    from blastjob.discovery.pipeline import build_adapters

    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    cfg.discovery.mail_alerts_enabled = True
    cfg.discovery.enable_aggregators = False
    cfg.discovery.search_firms_enabled = False  # keep this assertion weekday-independent
    cfg.discovery.apple_enabled = False  # isolate this assertion to mail-alerts
    monkeypatch.delenv("ICLOUD_IMAP_USER", raising=False)
    monkeypatch.delenv("ICLOUD_IMAP_APP_PASSWORD", raising=False)
    conn = dbc.init_db(cfg)
    adapters = build_adapters(cfg, conn)
    assert [a.source for a in adapters] == ["mail-alerts"]
    res = asyncio.run(run_discovery(cfg, conn, adapters=adapters))
    assert "mail-alerts" in res.errors and "ICLOUD_IMAP_USER" in res.errors["mail-alerts"]
    conn.close()


LI_GUEST_PAGE = (
    """Skip to main content
LinkedIn
Jobs
Sign in
Join now
VP of Business Operations (Remote)
Northgate Holdings  United States
3 days ago   Over 200 applicants

See who Northgate Holdings has hired for this role

Apply
Save
Northgate Holdings provided pay range

Base pay range
$190,000.00/yr - $225,000.00/yr

Department: Professional Services

Description

Our Story
Join a group that ships real projects together. """
    + ("Lead ops strategy and a team of 40. " * 40)
    + """

Seniority level
Executive
Similar jobs
Other job
"""
)


def test_parse_linkedin_guest_page_extracts_jd_and_pay():
    jd, pay, span = ma.parse_linkedin_guest_page(LI_GUEST_PAGE)
    assert jd.startswith("Our Story")
    assert "Lead ops strategy" in jd and "Seniority level" not in jd and "Similar jobs" not in jd
    assert pay == "$190,000.00-$225,000.00"
    assert span == ""  # structured widget path never sets a source span
    assert ma.parse_linkedin_guest_page("Sign in\nJoin now\nauthwall") == ("", "", "")


# ---------- W7: employer-authored pay range written straight into the JD
# body, no LinkedIn "Base pay range" widget at all. Real text shapes below are
# taken verbatim (including the LRM marks) from live DB rows so regex
# assumptions get tested against what Jina actually returns, not a tidied-up
# fixture. ----------

NORTHWIND_PAY_BLOCK = (
    "\u200e\n\nNorthwind Pay Range\n\n$165,000.00 - $190,000.00* + benefits.\n\n\u200e \u200e\n\n"
    "Pay varies by where a role is based, reflecting local market conditions."
)

NORTHWIND_BODY_PAGE = (
    """Skip to main content
LinkedIn
Jobs
Sign in
Join now
Head of Business Platforms
Northwind Dynamics  United States
3 days ago

See who Northwind Dynamics has hired for this role

Apply
Save

Description

"""
    + ("Lead global infrastructure and platform services. " * 30)
    + "\n\nMinimum Qualifications\n\n"
    + ("Bachelor's degree in a technical field, or equivalent experience. " * 20)
    + "\n\n"
    + NORTHWIND_PAY_BLOCK
    + """

Seniority level
Executive
Similar jobs
Other job
"""
)

# Same "Base pay range" widget + "provided by <aggregator>" disclaimer shape
# as live DB rows 847/912/970 (Quickhire), minus the widget itself, so the body
# scan is exercised on its own rather than short-circuited by _LI_PAY.
QUICKHIRE_BODY_TEXT = (
    "Apply \nSave\nQuickhire provided pay range\n\n"
    "This range is provided by Quickhire. Final pay depends on each candidate's "
    "background — your recruiter can walk you through the details.\n\n"
    "About the Role\n\n"
    + ("The organization is seeking an experienced technology leader. " * 20)
    + "\n\nCompensation and Benefits\n\n"
    "The pay band for Region A (National Average) runs $275,000 to $315,000 USD per year.\n\n"
    "Final offers reflect each candidate's background, the role's scope, and where it is based."
)


def test_employer_body_pay_extracts_vantage_range():
    pay, span = ma._employer_body_pay(NORTHWIND_PAY_BLOCK, "Northwind Dynamics")
    assert pay == "$165,000-$190,000"
    assert "Northwind Pay Range" in span


def test_parse_linkedin_guest_page_falls_back_to_body_pay_when_no_widget():
    jd, pay, span = ma.parse_linkedin_guest_page(NORTHWIND_BODY_PAGE, "Northwind Dynamics")
    assert "Lead global infrastructure" in jd
    assert pay == "$165,000-$190,000"
    assert "Northwind Pay Range" in span


def test_parse_linkedin_guest_page_without_company_finds_no_body_pay():
    """Regression guard for the pre-fix behavior: without a `company` to
    anchor the label match against, no comp signal is produced -- proving
    the fallback, not the structured-widget path, is what's under test."""
    jd, pay, span = ma.parse_linkedin_guest_page(NORTHWIND_BODY_PAGE)
    assert "Lead global infrastructure" in jd
    assert (pay, span) == ("", "")


def test_employer_body_pay_rejects_aggregator_attributed_range():
    """Same real shape as DB rows 847/912/970 (Quickhire): a 'provided by
    <aggregator>' disclaimer and a prose salary-range sentence, but no
    heading naming the employer. Must yield no signal, not guess."""
    pay, span = ma._employer_body_pay(QUICKHIRE_BODY_TEXT, "Quickhire")
    assert (pay, span) == ("", "")


def test_employer_body_pay_rejects_aggregator_disclaimer_before_a_heading():
    """A 'provided by <aggregator>' disclaimer immediately ahead of a generic
    'Salary Range' heading, where the aggregator's own name also happens to
    equal `company` (as it does for every 'Quickhire' row) -- must not be
    accepted just because the label lookback window matches the company."""
    body = (
        "This range is provided by Quickhire. Final pay depends on each candidate's "
        "background.\n\nSalary Range\n\n$275,000 - $315,000\n\n"
    )
    pay, span = ma._employer_body_pay(body, "Quickhire")
    assert (pay, span) == ("", "")


def test_employer_body_pay_ignores_prose_range_with_no_employer_label():
    """The 'pay band for Region A... $275,000 to $315,000' sentence has no
    'Pay Range'/'Salary Range'/'Base Salary' heading at all — must not be
    read as an employer-posted figure."""
    pay, span = ma._employer_body_pay(QUICKHIRE_BODY_TEXT, "Northwind Dynamics")
    assert (pay, span) == ("", "")


def test_employer_body_pay_carries_a_non_usd_currency():
    """The pay text is re-parsed downstream; rebuilding it as '$lo-$hi' would
    relabel a Canadian-dollar range as USD."""
    body = "\n\nAcme Pay Range\n\nC$165,000 - C$190,000 per year.\n\n"
    pay, _span = ma._employer_body_pay(body, "Acme Corp")
    assert ma.parse_salary_range(pay) == (165000, 190000, "CAD")


def test_employer_body_pay_usd_text_is_unchanged():
    pay, _span = ma._employer_body_pay(NORTHWIND_PAY_BLOCK, "Northwind Dynamics")
    assert ma.parse_salary_range(pay) == (165000, 190000, "USD")


def test_indeed_comp_carries_currency_into_the_posting():
    p = ma._indeed_posting(
        "abc123",
        ma._Anchor(
            href="https://www.indeed.com/viewjob?jk=abc123",
            text="VP IT",
            after=["Acme", "Toronto, ON", "C$180,000 - C$220,000 a year"],
        ),
    )
    assert (p.comp_min, p.comp_max, p.comp_currency) == (180000, 220000, "CAD")


def test_indeed_comp_min_only_keeps_zero_max():
    p = ma._indeed_posting(
        "abc124",
        ma._Anchor(
            href="https://www.indeed.com/viewjob?jk=abc124",
            text="VP IT",
            after=["Acme", "Chicago, IL", "From $150,000 a year"],
        ),
    )
    assert (p.comp_min, p.comp_max, p.comp_currency) == (150000, 0, "USD")


def test_employer_body_pay_rejects_ambiguous_magnitude():
    body = "\n\nAcme Pay Range\n\n$165 - $190 per year, DOE.\n\n"
    pay, span = ma._employer_body_pay(body, "Acme Corp")
    assert (pay, span) == ("", "")


def test_employer_body_pay_rejects_hourly_range():
    body = "\n\nAcme Pay Range\n\n$45.00/hr - $60.00/hr\n\n"
    pay, span = ma._employer_body_pay(body, "Acme Corp")
    assert (pay, span) == ("", "")


def test_employer_body_pay_rejects_generic_heading_naming_a_different_company():
    """A bare 'Salary Range' heading only counts when the posting's own
    employer name is nearby -- not any company mentioned in the JD."""
    body = "\n\nWorking with our partner Acme Corp.\n\nSalary Range\n\n$150,000 - $180,000\n\n"
    pay, span = ma._employer_body_pay(body, "Beta Industries")
    assert (pay, span) == ("", "")


def test_employer_body_pay_accepts_generic_heading_near_employer_name():
    body = "\n\nBeta Industries is hiring.\n\nSalary Range\n\n$150,000 - $180,000\n\n"
    pay, span = ma._employer_body_pay(body, "Beta Industries")
    assert pay == "$150,000-$180,000"


def test_employer_body_pay_rejects_multiple_distinct_ranges():
    """Two locations, two ranges -- picking either would be a guess."""
    body = (
        "\n\nAcme Pay Range\n\n$205,000 - $235,000 (California)\n\n"
        "\n\nAcme Pay Range\n\n$185,000 - $215,000 (Texas)\n\n"
    )
    pay, span = ma._employer_body_pay(body, "Acme Corp")
    assert (pay, span) == ("", "")


def test_employer_body_pay_yields_nothing_with_no_pay_at_all():
    body = "\n\nWe are hiring an infrastructure leader.\n\nNo compensation details here.\n\n"
    pay, span = ma._employer_body_pay(body, "Acme Corp")
    assert (pay, span) == ("", "")


@pytest.mark.asyncio
async def test_enrich_linkedin_via_jina_sets_jd_and_comp(monkeypatch):
    import httpx as _httpx

    calls: list[str] = []

    def handler(req: _httpx.Request) -> _httpx.Response:
        calls.append(str(req.url))
        assert req.headers["authorization"] == "Bearer k"
        return _httpx.Response(200, text=LI_GUEST_PAGE)

    real = _httpx.AsyncClient

    def fake_client(**kw):
        return real(transport=_httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(_httpx, "AsyncClient", fake_client)
    li = ma.parse_linkedin_alert(LINKEDIN_HTML)[0]
    indeed = ma.parse_indeed_alert(INDEED_HTML)[0]
    n = await ma.enrich_linkedin_via_jina([li, indeed], "k")
    assert (
        n == 1 and len(calls) == 1 and "r.jina.ai/https://www.linkedin.com/jobs/view/" in calls[0]
    )
    assert "Lead ops strategy" in li.description
    assert (li.comp_min, li.comp_max) == (190000, 225000)
    assert indeed.description.startswith("Gamma")  # untouched


# --- whole-card LinkedIn layout ---------------------------------------------
# A second LinkedIn template wraps the WHOLE job card in one <a>: title, company,
# location, pay and badges, with nothing after it. Shapes from the 15 live rows.

GRANITE_CARD = (
    "Vice President Information Technology Granite Ridge Hotels & Resorts · Fairhaven, OH (On-site)"
)
BRIGHTLAKE_CARD = "Head of Infrastructure (Remote) Brightlake Corporation · Fairhaven, OH"
COPPERFIELD_CARD = (
    "Director of IT Infrastructure Copperfield · Fairhaven, OH $240K-$250K / year Easy Apply"
)
AMBIGUOUS_CARD = "Director of IT Acme Technologies · Fairhaven, OH"


def _card_html(*cards: tuple[str, str]) -> str:
    rows = "".join(
        f'<tr><td><a href="https://www.linkedin.com/comm/jobs/view/{jid}/?trk=eml">'
        f"<table><tr><td><p>{text.replace('&', '&amp;')}</p></td></tr></table></a></td></tr>"
        for jid, text in cards
    )
    return f"<html><body><table>{rows}</table></body></html>"


def test_split_card_text_vocab_split_granite():
    assert ma.split_card_text(GRANITE_CARD) == (
        "Vice President Information Technology",
        "Granite Ridge Hotels & Resorts",
        "Fairhaven, OH",
        False,
    )


def test_split_card_text_moves_a_remote_parenthetical_into_the_hint():
    assert ma.split_card_text(BRIGHTLAKE_CARD) == (
        "Head of Infrastructure",
        "Brightlake Corporation",
        "Fairhaven, OH",
        True,
    )


def test_split_card_text_never_puts_pay_or_badges_in_title_or_location():
    assert ma.split_card_text(COPPERFIELD_CARD) == (
        "Director of IT Infrastructure",
        "Copperfield",
        "Fairhaven, OH",
        False,
    )


@pytest.mark.parametrize("card", [AMBIGUOUS_CARD, "Acme Widgets · Fairhaven, OH"])
def test_split_card_text_without_a_clean_split_guesses_nothing(card):
    head = card.split(" · ")[0]
    assert ma.split_card_text(card) == (head, "", "Fairhaven, OH", False)


def test_split_card_prefers_a_known_company_suffix():
    card = ma.split_card(AMBIGUOUS_CARD, known_companies={"Acme Technologies", "IT", "Acme"})
    assert (card.title, card.company, card.basis) == (
        "Director of IT",
        "Acme Technologies",
        ma.SPLIT_KNOWN,
    )


def test_split_card_ignores_a_known_company_that_would_leave_no_title():
    card = ma.split_card("Copperfield · Fairhaven, OH", known_companies={"Copperfield"})
    assert (card.company, card.basis) == ("", "")


def test_split_card_vocab_split_is_marked_as_needing_confirmation():
    card = ma.split_card(GRANITE_CARD)
    assert card.basis == ma.SPLIT_VOCAB
    assert card.head == "Vice President Information Technology Granite Ridge Hotels & Resorts"


def test_whole_card_vocab_only_is_never_auto_applied_at_ingest():
    (p,) = ma.parse_linkedin_alert(_card_html(("4300000001", GRANITE_CARD)))
    assert p.company == ""
    assert p.title == "Vice President Information Technology Granite Ridge Hotels & Resorts"
    assert (p.location_raw, p.city, p.region) == ("Fairhaven, OH", "Fairhaven", "OH")


def test_whole_card_with_known_company_is_split_at_ingest():
    html = _card_html(("4300000002", COPPERFIELD_CARD), ("4300000003", BRIGHTLAKE_CARD))
    a, b = ma.parse_linkedin_alert(html, known_companies={"Copperfield", "Brightlake Corporation"})
    assert (a.title, a.company, a.location_raw) == (
        "Director of IT Infrastructure",
        "Copperfield",
        "Fairhaven, OH",
    )
    assert "$" not in a.title and "$" not in a.location_raw
    assert (b.title, b.company, b.remote) == (
        "Head of Infrastructure",
        "Brightlake Corporation",
        True,
    )


def test_title_only_link_beats_a_whole_card_link_for_the_same_job():
    html = (
        _card_html(("4300000004", GRANITE_CARD))
        + '<a href="https://www.linkedin.com/comm/jobs/view/4300000004/">'
        "Vice President Information Technology</a>"
        "<p>Granite Ridge Hotels &amp; Resorts · Fairhaven, OH</p>"
    )
    (p,) = ma.parse_linkedin_alert(html)
    assert (p.title, p.company) == (
        "Vice President Information Technology",
        "Granite Ridge Hotels & Resorts",
    )


def test_a_see_job_anchor_never_replaces_a_whole_card():
    html = _card_html(("4300000005", GRANITE_CARD)) + (
        '<a href="https://www.linkedin.com/comm/jobs/view/4300000005/?x=1">See job</a>'
    )
    (p,) = ma.parse_linkedin_alert(html)
    assert p.title.startswith("Vice President Information Technology")


def test_guest_page_header_confirms_the_company():
    page = (
        "Sign in\nJoin now\nVice President Information Technology\n"
        "Granite Ridge Hotels & Resorts  Fairhaven, OH\n3 days ago\n"
    )
    head = "Vice President Information Technology Granite Ridge Hotels & Resorts"
    assert ma.guest_page_company(page, head) == "Granite Ridge Hotels & Resorts"
    # The header must account for the whole head, or it confirms nothing.
    assert ma.guest_page_company(page, head + " Group") == ""
    assert ma.guest_page_company("Sign in\nJoin now\n", head) == ""


@pytest.mark.asyncio
async def test_enrichment_splits_a_whole_card_from_the_guest_page(monkeypatch):
    import httpx as _httpx

    page = (
        "Skip to main content\nLinkedIn\nJobs\nSign in\nJoin now\n"
        "Head of Infrastructure (Remote)\nBrightlake Corporation  Fairhaven, OH\n"
        "3 days ago   Over 200 applicants\n\nDescription\n\n" + ("Run the platform. " * 40)
    )

    def handler(req: _httpx.Request) -> _httpx.Response:
        return _httpx.Response(200, text=page)

    real = _httpx.AsyncClient
    monkeypatch.setattr(
        _httpx, "AsyncClient", lambda **kw: real(transport=_httpx.MockTransport(handler), **kw)
    )
    (p,) = ma.parse_linkedin_alert(_card_html(("4300000006", BRIGHTLAKE_CARD)))
    assert p.company == ""

    await ma.enrich_linkedin_via_jina([p], "k")

    assert (p.title, p.company, p.remote) == (
        "Head of Infrastructure",
        "Brightlake Corporation",
        True,
    )


@pytest.mark.asyncio
async def test_adapter_passes_known_companies_to_the_parser():
    html = _card_html(("4300000007", AMBIGUOUS_CARD))
    msgs = [ma.MailMessage("<card@li>", "20", "jobs-noreply@linkedin.com", "alert", html)]
    a = _adapter(msgs)
    a.known_companies = frozenset({"Acme Technologies"})
    (p,) = await a.fetch()
    assert (p.title, p.company) == ("Director of IT", "Acme Technologies")


def test_known_company_names_reads_distinct_db_companies():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    conn = dbc.init_db(cfg)
    for i, company in enumerate(["Acme Technologies", "Acme Technologies", "", "Copperfield"]):
        dbc.upsert_job(conn, source="adzuna", external_id=f"k{i}", company=company, title="CIO")
    assert ma.known_company_names(conn) == frozenset({"Acme Technologies", "Copperfield"})
    conn.close()


@pytest.mark.asyncio
async def test_rediscovered_card_keeps_a_repaired_company(monkeypatch):
    """A repaired row's company is in the DB, so a later alert re-sending the same
    whole card splits on it instead of writing company='' over the repair."""
    from blastjob.discovery.pipeline import _mail_adapter

    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    conn = dbc.init_db(cfg)
    dbc.upsert_job(
        conn,
        source="linkedin",
        external_id="4300000008",
        company="Granite Ridge Hotels & Resorts",
        title="Vice President Information Technology",
    )
    monkeypatch.setenv(cfg.discovery.mail_user_env, "u")
    monkeypatch.setenv(cfg.discovery.mail_password_env, "p")
    adapter = _mail_adapter(cfg, conn)
    html = _card_html(("4300000008", GRANITE_CARD))
    msg = ma.MailMessage("<again@li>", "30", "jobs-noreply@linkedin.com", "alert", html)
    adapter._fetch_messages = lambda **kw: [msg]  # type: ignore[attr-defined]
    adapter.enrich = False  # type: ignore[attr-defined]

    await run_discovery(cfg, conn, adapters=[adapter])

    row = conn.execute("SELECT company, title FROM jobs WHERE external_id='4300000008'").fetchone()
    assert (row["company"], row["title"]) == (
        "Granite Ridge Hotels & Resorts",
        "Vice President Information Technology",
    )
    conn.close()


def test_sender_domain_is_the_address_not_the_display_name():
    """A display name that mentions linkedin.com or indeed.com must not route the
    message to that parser or skip the subject requirement."""
    spoof_li = '"linkedin.com jobalerts" <evil@example.com>'
    assert not ma.looks_like_alert(spoof_li, "Dinner Sunday?")
    assert ma.parse_alert(spoof_li, "<a href='x'>y</a>") == ("", [])
    spoof_in = '"indeed.com alerts" <evil@example.com>'
    assert not ma.looks_like_alert(spoof_in, "Dinner Sunday?")
    # a look-alike registered domain is not a subdomain
    for host in ("notindeed.com", "indeed.com.example.com", "indeed.com.evil"):
        assert not ma.looks_like_alert(f"Indeed <alert@{host}>", "Dinner Sunday?")
    # a fold of the original address in the display name is not trusted either
    folded = '"donotreply_at_jobalert_indeed_com_x@" <evil@example.com>'
    assert not ma.looks_like_alert(folded, "Dinner Sunday?")


def test_sender_domain_matches_exactly_or_as_subdomain():
    assert ma.looks_like_alert("Indeed <alert@indeed.com>", "anything")
    for host in ("jobalert.indeed.com", "mail.indeed.com"):
        assert ma.looks_like_alert(f"Indeed <alert@{host}>", "anything")
        assert ma.parse_alert(f"Indeed <alert@{host}>", INDEED_HTML)[0] == "indeed"
    assert ma.looks_like_alert("INDEED <ALERT@INDEED.COM>", "anything")
    assert ma.looks_like_alert("alert@indeed.com", "anything")


def test_relay_fold_must_end_at_the_domain_plus_relay_id():
    ok = "Indeed <donotreply_at_indeed_com_a1b2c3d4e5_f6a7b8c@relay.example.com>"
    assert ma.looks_like_alert(ok, "Dinner Sunday?")
    for tail in ("evil", "au", "evil_net"):
        spoof = f"Indeed <donotreply_at_indeed_com_{tail}_a1b2c3@relay.example.com>"
        assert not ma.looks_like_alert(spoof, "Dinner Sunday?")
    assert not ma.looks_like_alert("", "Dinner Sunday?")
    assert not ma.looks_like_alert("not an address", "Dinner Sunday?")

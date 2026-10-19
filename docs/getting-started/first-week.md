# Your first week

**What this covers:** Tuning blastjob once it has run for a few days: the fit threshold, archive reasons, agencies, company aliases and turning on closure. For anyone whose lists look too long, too short or a little wrong. It assumes you have finished [install](install.md).

Nothing here is required. Every default is safe, and each change below is easy to undo.

## Tune the threshold

`discovery.alignment_threshold` decides how high a job must score to appear in **Surfaced**. The default is 78, an uncalibrated starting guess (see [the gate](../how-it-works/matching.md#gate)).

To calibrate it, pick about twenty postings you know you would want. Look at where they scored, then move the threshold so those land above it:

```bash
uv run blastjob setup set discovery.alignment_threshold 75
```

You can also change it on the **Settings** page. Jobs scoring up to 5 points below the threshold are not dropped; they go to the review queue. See [reviewing matches](../using/reviewing-matches.md) for what to do with them, and [matching](../how-it-works/matching.md) for how the score and the gate work.

## Archive with a reason

When you archive a job, choose a reason. The reasons teach the scorer and the filters:

- **wrong level / scope** and **wrong domain** are fed back to the scorer as examples, so it learns what you do not want.
- **never this company** hides that employer's future postings before any scoring.
- **location / commute** and **comp too low** are already handled by fixed rules, so they do not change the scorer.
- **other** is recorded but not used.

A reason takes a few seconds to give and is the cheapest way to make the lists better.

## Staffing agencies

Job-board platforms such as Indeed and LinkedIn are built in as "not the employer", and names ending in words like Staffing or Recruiters are caught by a simple rule. Agencies in your own feed that those rules miss can be added to the `discovery.agency_companies` setting, so their postings are badged as not the employer:

```bash
uv run blastjob setup set discovery.agency_companies '["Northwind Staffing Group", "Example Talent Partners"]'
```

## Company aliases

When one employer shows up under two names, tell blastjob they are the same with `discovery.company_aliases`. It maps a variant to the name you want to use:

```bash
uv run blastjob setup set discovery.company_aliases '{"Acme Corp Holdings Inc": "Acme Corp"}'
```

Both names are then treated as one company. Stored jobs are not renamed automatically.

## Turn on closure

blastjob checks whether postings are still open. By default (`discovery.closure_auto` is `false`) it only records what it finds and shows verdicts; it never moves a job. This is called shadow mode.

Run the shadow report for a few weeks:

```bash
uv run blastjob-daily --closure-report
```

It moves nothing and sends nothing. When its verdicts keep matching what you see on the real postings, turn automatic moves on:

```bash
uv run blastjob setup set discovery.closure_auto true
```

[Closure](../how-it-works/closure.md) explains what counts as proof that a posting closed, and which of your jobs can be moved.

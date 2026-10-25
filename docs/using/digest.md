# The daily digest

**What this covers:** What the daily run does each morning, what the digest message shows, how to read it, and how it relates to the buttons in the cockpit. It is for anyone who gets the digest, usually on a phone. To install and schedule the daily run, see [running the services](../operations/services.md). For how the run works inside, see [the daily run](../how-it-works/daily.md).

## When it arrives

If you installed the daily service, it runs at 07:30 each day on the computer that holds your data. It fetches new postings, scores the new ones, checks whether postings you kept have closed, looks for recruiter replies, and then sends one digest.

Where the digest goes is set in **Settings**, under **Daily digest and cockpit**, in **Daily digest goes to**:

- `discord` posts it to a private channel through a bot you set up. The message begins with a header line so the channel can be shared with other tools.
- `stdout` prints it to the run's log. This is the default.
- `off` sends nothing.

If there is nothing to report, no message is sent. A run is also skipped entirely while a file named `PAUSE` sits in your data folder.

## What the digest shows

New roles. The title line reads "N new roles". Each role takes two lines:

```
• 82 Acme Corp - VP of Technology
   Springfield, OH · $240K posted · <apply link> · cockpit <link>
```

The first number is the fit score, and the company and title follow it after a dash. The pay tag says where the figure came from: "posted" for a figure the employer published, "inferred (from the posting)" for one read out of the text, "est." for a modelled estimate, or "comp n/a" when there is none. A role from the Queue is marked "(review)", because its score is just under your bar or its location needs a look. A role at a company where you know someone is marked "you know someone here" and listed ahead of higher-scoring cold roles.

The first link is the best place to apply, so you can act from your phone. The cockpit link goes to the job's page.

The digest shows at most 25 roles. If there are more, the last line says how many are left and links to **All**. Roles that were not shown are not marked as announced, so they lead tomorrow's digest. Each role is announced once.

A role is not announced if the same job is already kept or archived under another posting, so a repost of something you applied to stays quiet.

Closed postings. A line like "2 kept postings closed: Acme Corp VP of Technology, Northwind ..." names kept roles whose posting closed this run.

Replies. Under "Replies", each reply from a company you applied to, with the sender, the date and a short snippet. It is shown for you to read. No response is ever drafted.

Target news. Under "Target news", dated news at companies on your [target list](targets.md): the company, what kind of news, "published" and the date, whether it came from the employer's own site or a news report, the host, the sentence the page printed, how many people you know there and a link to that company in the cockpit. It is a lead to read, not a fact to quote. If a weekly scan failed, one line says how many, so a quiet week and a broken scan look different.

Dream list. Once a week, on the search-firm weekday (`discovery.search_firm_weekday`, Monday unless you changed it), a "Dream list" section lists roles at your [dream companies](targets.md#dream-companies) that are on-site far from home and scored at or above your threshold, best fit first, in the same two-line form as new roles. A role that is remote or near comes in the daily list. Each role is announced once, at most 15 a week; the rest lead next week's section. A day with only a Dream list still sends.

Follow-ups due. Under "Follow-ups due", the applied roles that need a nudge, with the reason. See [applying](applying.md#follow-ups).

Source errors and notes. Under "Source errors", a source that failed this run. Under "Notes", things worth knowing, such as a board that returned suspiciously few jobs and so was not used to mark postings stale.

Monday heartbeat. On Mondays an italic line confirms the run is alive and gives seven-day totals.

Footer. An italic line at the end gives how long the run took, how many postings it fetched from which sources, how many it scored and what scoring and re-scoring cost (`cost unknown` when the model provider did not report usage).

## Cockpit links

By default the cockpit links in the digest point at `http://127.0.0.1:<web.port>` (8765 unless you changed it). That address only works on the computer running the cockpit. From a phone it will not load. Use the apply link on the phone, or set `web.public_url` to the computer's network address so the cockpit links open on the phone too (see [Digest links on a phone](../operations/lan-access.md#digest-links-on-a-phone)). [Reaching the cockpit from other devices](../operations/lan-access.md) covers using the cockpit from a phone or tablet.

## Keeping scores current

The daily run scores only jobs that have never been scored. It does not redo old scores. If you change your target titles, threshold, profile or work history, the existing scores were made under the old settings.

When that happens, a note appears beside the buttons at the top of every page: "N scores predate your last profile or work-history change". It counts what **Re-score changed** would redo. Click **Re-score changed** to score them again. It can take a while for a large list, and the run banner shows its progress. **Re-score all** goes further and redoes every job, so it asks you to confirm the job count first.

**Find + Score** is the manual version of the morning run's first half: it fetches new postings and scores the new ones. It does not redo old scores either.

## Check on the last run

On the machine that runs it, `blastjob-daily --status` prints the last recorded run without sending anything, and `blastjob-daily --dry-run` prints the digest that would go out without sending or recording it.

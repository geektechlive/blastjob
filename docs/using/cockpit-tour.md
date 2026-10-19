# A tour of the cockpit

**What this covers:** What each page of the cockpit shows and how to move between them, using the labels you see on screen. It is for anyone using blastjob day to day; to install it, start with [getting started](../getting-started/install.md).

## The pages

You run everything from the browser, normally at `http://127.0.0.1:8765`. Every job sits on at most one of five stage pages, in the order you work them:

| Page | What is on it |
|---|---|
| **Surfaced** | New roles that cleared your fit bar. Start here. |
| **Queue** | Borderline fits, or roles where the location or pay needs a human look. |
| **Shortlist** | Roles you kept and are weighing or tailoring for. |
| **Applied** | Roles you applied to that are still in a hiring process. |
| **Closed** | Finished applications, and kept roles whose posting is gone. |

[Pipeline stages](stages.md) explains how a job gets into each one and how it moves.

## The navigation bar

The five stage names run along the top, each with a count. The count is the length of that page, so it tells you what is waiting.

Everything else is under **More**:

- **Browse**: **Matches** (every undecided role scored 65 or higher, ranked), **All** (every active job and why it is where it is), and **Compare** (a side-by-side table).
- **Track**: **Pipeline** (a board of your kept jobs by lane), **Funnel** (every job counted by the stage it sits in, plus sources that never produce a match), and **Packets** (every resume and cover letter bundle built).
- **Research**: **Companies** (company workups, plus the companies you have excluded) and **Network** (your own LinkedIn export, matched to employers).
- **Setup**: **Work history**, **Ingest** (upload old resumes to build the work history) and **Settings**.

On the right of the bar are four buttons:

- **+ Add job** opens a form for a posting no source picked up. See [Add a job by hand](reviewing-matches.md#add-a-job-by-hand).
- **Find + Score** fetches new postings from your sources, then scores the new ones.
- **Re-score changed** scores again the jobs whose score predates your last profile, work-history or rubric change, plus any job never scored. Every other job keeps its score, and only your threshold and location rules are applied to it again, with no model call. Use it after you change your threshold, profile or work history.
- **Re-score all** sends every active job to the model again and ignores saved scores. It does not start on the first click: it asks "Re-score all N jobs?" with the exact count, and only **Start** begins the run (**Cancel** backs out). Jobs your location or title rules drop are still skipped before the model. Use it when you want a fresh opinion on everything, knowing it can take a long time on a big list.

Next to them, a small note may read "N scores predate your last profile or work-history change". That count is what **Re-score changed** would redo. The daily run does not redo those scores. See [the digest](digest.md).

## The run banner

When **Find + Score**, **Re-score changed** or **Re-score all** is running, a banner under the bar shows its name, a progress bar and the latest message. It follows you from page to page, so you can leave and come back. When it finishes it reads "done" with a **refresh →** link that reloads the page with the new results.

**Find + Score**, **Re-score changed** and **Re-score all** share one slot. If you click one while another is running, you see the run already in progress instead of a second one starting. The run log's first line says which one ran.

## The first-run banner

Until your search profile has target titles, the top of every page says "blastjob is not set up yet." With no target titles there is nothing to score against, so nothing gets scored. Click **Set up your profile** to go to the profile section of **Settings**. [Your first week](../getting-started/first-week.md) walks through what to set.

## A job's page

Click any job title to open it. From top to bottom you will see:

- The title, company and source, an "apply on ..." link to the best place to apply, and "open posting" for the page the job came from.
- Badges for fit, pay, location and state. [Reviewing matches](reviewing-matches.md) explains them.
- A same-role section if the role may be listed more than once, and a field for the **Employer posting URL**.
- "Why this scored" with the reasons, then **Strengths** and **Gaps to probe**.
- Action buttons. On an undecided role: **Keep & tailor** and **Archive**. On a kept role: tailoring, outreach, prep and follow-up buttons ([tailoring](tailoring.md), [applying](applying.md)).
- The packet, once you have tailored one.
- **Posting** status, **Activity** and **Files** for kept roles, then **Application status**, **Company**, **Compensation signals** and **People at** the company.
- The job description at the bottom.

A **← back** link returns you to the list you came from.

## Settings

**Settings** is one page with sections: **Who the search is for**, **Fit and pay**, **Location**, **Where to look**, **Daily digest and cockpit**, **Active model** and **Mail accounts**. Change a value, click **Save**, and the next run uses it. Settings are read fresh on each request, so you do not restart anything, with one exception: the address and port the cockpit listens on only change when the cockpit restarts.

For what each setting does, and for where passwords and keys go, see [settings and secrets](../operations/settings-and-secrets.md).

## If a click seems ignored

If you leave a tab open while the cockpit restarts, your next click is refused. The page reloads and shows "The cockpit restarted since this page was opened, so your last click was not applied." Click again.

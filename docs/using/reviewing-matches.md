# Reviewing matches

**What this covers:** How to work through **Surfaced** and **Queue**: what the scores and badges mean, when to keep or archive, and what to do when the same role shows up twice. It is for anyone triaging new jobs. For why a score is what it is, see [matching](../how-it-works/matching.md).

## What you see on a row

Each row is one role, best fit first. The row shows:

- The title (click it to open the job) and the company.
- Badges, described below.
- A one-line reason for the score, when there is one.
- **Keep** and **Archive** buttons.

Every list is sorted best fit first. On **Surfaced** and **Matches** a line under the title says "Sorted by fit" with a link to **sort by pay** instead. Sorting never hides anything.

**Surfaced** holds roles at or above your fit threshold. **Queue** holds roles that need a look: a score just under the threshold (a band five points wide), inferred pay that is under your floor, or a location the cockpit could not place or that is inside your extended radius but past your normal one. Open a Queue job to see which reason applies.

## Badges

| Badge | Meaning |
|---|---|
| "78% fit" | The alignment score. Filled in when it is at or above your threshold, outlined when it is under. |
| "$200k–$250k" | A pay range the posting itself published. |
| "$200k–$250k inferred" | A range read out of the posting text, not published as pay. Treat it as a hint. |
| "comp unverified" | No pay figure at all. These roles still surface; only a published figure under your floor rejects one. |
| "remote", or a city and miles | Where the job is, and how far from home. |
| The source name | Where the posting was found. |
| "+2 sources" | The same role was found on several sources. They are shown as one row. |
| "staffing agency" | The poster is an agency, not the employer. |
| "employer hidden" | The posting does not name the employer. |
| "3 in your network" | You have that many connections at the company, from your network import. |
| "you worked here" | The company is a former employer you listed in settings. |
| "posting closed", "likely closed", "open · checked 2026-10-03" | The posting check. See [pipeline stages](stages.md#when-a-posting-closes). |

Open the job and you will also see "Why this scored", the **Strengths** the match found and the **Gaps to probe**.

## Keep or archive

Click **Keep** to move the role to the **Shortlist**. On the job's page the same action is labelled **Keep & tailor**. It only keeps the role. Tailoring starts when you click **Tailor resume + cover letter**.

Click **Archive** to dismiss a role. Pick a reason first from the **why?** list. On the job's page you can also add a note. The reasons do different things:

| Reason | What it does |
|---|---|
| wrong level / scope | The role is remembered as an example, and the scorer is told about your most recent ones when it scores new jobs. |
| wrong domain | Same as above. |
| location / commute | Recorded with the job. Location is already checked on its own, so scoring does not change. |
| comp too low | Recorded with the job. Pay is already checked on its own. |
| never this company | Adds the company to your exclusion list. Every future posting from it is archived on arrival, with no scoring. |
| other | Recorded with the job only. |

A reason is optional, but the first two reasons are how the scorer learns from you.

To undo an archive, open the job and click **Unarchive**. To undo a company exclusion, open **Companies** and click **Lift** next to the company. Lifting stops future auto-archiving. Roles already archived stay archived until you unarchive them one by one.

On the **Shortlist**, **Remove** sends a role back to **Surfaced** or **Queue** by its score. See [pipeline stages](stages.md#moving-a-job).

## The same role twice

The same job often appears under different names or on different sites. blastjob never links two postings for you when it is unsure. It shows a suggestion and waits for your answer.

On a job's page you may see one of these notes:

- "Possibly the same role as ..." followed by a company, title and the evidence it used. This appears on an agency posting that looks like a role you already have.
- "An agency may be advertising this role too" on an employer posting, with the same evidence.
- "This may be a repost of ..." when you already applied to what looks like the same role.

Each comes with two buttons:

- **Same role** joins the postings so the role shows once, in its furthest stage.
- **Not the same** separates them, and blastjob never suggests that pair again.

If you say **Same role** and both postings were already applications, the one furthest along survives and the other is archived as a duplicate, with a note added to the survivor.

Once postings are joined, the job page lists them under "Also listed as", each with a **Not the same role** button to undo it.

A row can also carry a chip that says the role may match one you have. Open the job to decide. When you click **Mark applied** on such a role, the confirmation names the other posting, so you do not apply twice. Applying through both an agency and the employer can cost you the placement.

If you know the employer's own posting, type its address into **Employer posting URL** and click **Save**. It becomes the preferred "apply on" link. blastjob never opens it or checks it, so a typo cannot close a role by mistake.

For the rules behind all of this, see [same role, different route](../how-it-works/same-role.md).

## Compare side by side

Open **More**, then **Compare**. The table lists roles with fit, pay, location, company facts, strengths, gaps, red flags (such as an agency posting) and a link to the posting. Use the **Source** box and the state list (surfaced, queued or kept) and click **Filter** to narrow it. Company facts appear only for companies you have researched.

## Add a job by hand

For a posting no source found, such as a screenshot or a forwarded link, click **+ Add job**. Fill in **Company**, **Title** and the **Job description**, then click **Add & score**. Paste the whole posting, not a summary: scoring and interview prep both read it. The link, location and remote box are optional.

The role is scored like any other and lands where its score puts it. A link that is not a valid web address is dropped, and the page tells you so.

## When a description is only a snippet

Some sources give only a short snippet. The job page says "only a snippet; scoring and tailoring can't see the rest". You can:

- Click **Find the employer's posting** to look for the employer's own copy (this only appears when the lookup is allowed for that job).
- Paste the text into **Paste the full description** and click **Save description**. An undecided role is scored again on the new text.

Tailoring and interview prep refuse to run on a snippet.

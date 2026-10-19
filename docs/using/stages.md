# Pipeline stages

**What this covers:** The five stages a job moves through, what puts a job in each one, and the moves available on each page. It is for anyone tracking applications. For how stage membership is decided in code, see [the web layer](../how-it-works/web.md).

## The five stages

Every job is on at most one stage page at a time, and the count beside each name in the top bar is the length of that page. A role posted on several sites appears once, in the furthest stage any copy has reached.

| Stage | What is in it | Order |
|---|---|---|
| **Surfaced** | Undecided roles that cleared your fit threshold. | Best fit, then newest. |
| **Queue** | Undecided roles that need a look: borderline score, inferred pay under your floor, or a location that needs checking. | Best fit. |
| **Shortlist** | Roles you kept and have not applied to. | Best fit; closed postings sink to the bottom. |
| **Applied** | Roles you applied to that are still going: applied, screen, interview or offer. | Furthest stage first, then follow-ups that are due, most overdue first. |
| **Closed** | Rejected, withdrawn, ghosted, posting closed or filled. | Most recently closed first. |

Archived roles are in no stage. Find them on **All**, which lists every active job and gives the reason it is where it is.

Surfaced and Queue are for deciding, so they have the **Keep** and **Archive** buttons covered in [reviewing matches](reviewing-matches.md). The rest of this page is about kept roles.

### Filters on a page

Once a page has more than one job, a filter bar appears. On **Shortlist** it offers **All**, **Not tailored**, **Tailored** and "Likely closed". On **Applied** it offers **Applied**, **Screen**, **Interview**, **Offer** and "Follow-up due". A chip shows only when it has jobs. A filter only hides rows. It never reorders the list.

### The board

**Pipeline**, under **More**, shows every kept job as a card in a lane: Not tailored, Tailoring, Tailored and Tailor failed under Shortlist; Applied, Screen, Interview and Offer under Applied; and Closed. Click a lane's stage name to open that page.

## Moving a job

Each move checks the job's current stage first. If another tab already moved it, your click is refused and the page reloads rather than putting the job somewhere wrong.

| From | Button | What happens |
|---|---|---|
| Surfaced or Queue | **Keep** | Moves to **Shortlist**. |
| Surfaced or Queue | **Archive** | Archived, with the reason you chose. |
| Shortlist | **Mark applied** | Moves to **Applied** and stamps today as the date you applied. |
| Shortlist | **Remove** | Back to **Surfaced** or **Queue** by its stored score. If the score is low or the posting is gone, it leaves your lists. Your notes are kept. |
| Applied | A button named for the next step ("To screen", "To interview", "To offer") | Advances one step. The job stays on **Applied**. |
| Applied | **Withdraw** | Moves to **Closed** as withdrawn, after you confirm. |
| Closed | **Reopen** | Back to **Applied** if you had applied, otherwise back to **Shortlist**. |

On a job's page you can also set the status directly with the buttons under **Application status**: Applied, Screen, Interview, Offer, Rejected and Withdrawn. Rejected and Withdrawn move the job to **Closed**. Choosing the status a job already has changes nothing and records nothing.

Marking a job applied also records which resume and cover letter you sent. See [applying](applying.md).

## When a posting closes

Many jobs vanish from a board before you get to them. blastjob checks, but it only treats a posting as closed when something that can prove it says so.

The **Posting** section of a job's page shows the verdict:

| Badge | Meaning |
|---|---|
| "open · checked 2026-10-03" | The employer's own board or page still lists it. |
| "posting closed" | The employer's board or page no longer lists it. |
| "likely closed" | It aged out of an aggregator's search. That is not proof, so nothing moves. |
| "can't verify" | Nothing that can prove the posting open or closed is available: an agency posting, no employer board found, a careers page that could not be fully confirmed, or a link that stopped proving the posting. See [closure](../how-it-works/closure.md). |
| "not checked yet" | No check has run. |

What happens next depends on the stage:

- Undecided or archived roles only get the badge. Nothing is moved or archived.
- Shortlist roles move to **Closed** as "posting closed" only if automatic closure is on. It is off by default, and in that mode blastjob records verdicts and moves nothing. To turn it on, run `blastjob setup set discovery.closure_auto true`. If the posting turns out to be open again, the job returns to where it was, unless you moved it yourself in the meantime.
- Applied roles get a badge and a follow-up nudge. If the posting stays closed for 21 days, the job moves to **Closed** as "filled", but only when automatic closure is on (the same setting as for Shortlist). Screen, interview and offer never move on their own.

You can always override the verdict. Click **Posting closed** or **Still open** under **Posting**. Your choice is marked "set by you" and automation never overturns it.

To see why a role moved, open its **Activity** list. Every automatic move writes an entry. See [applying](applying.md#the-activity-list). For the checks themselves, see [closure](../how-it-works/closure.md).

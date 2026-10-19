# Tailoring a packet

**What this covers:** How to build a tailored resume and cover letter for a role you kept, read the checks blastjob runs on them, fix what it flags, and get the files. It is for anyone about to apply. blastjob only drafts: it never sends, submits or posts anything. For how a packet is built, see [tailoring internals](../how-it-works/tailoring.md), and for the rules it holds to, [truth guarantees](../truth-guarantees.md).

## Start a build

1. Keep the role. It is on your **Shortlist**.
2. Open it (the **Tailor →** link on its row, or click the title).
3. Click **Tailor resume + cover letter**.

A panel opens under the buttons and streams the work as it happens. The line above the text shows the current step, such as "Tailoring for ... @ ...". It ends with "Saved bundle to ..." and the **Packet** section below refreshes by itself. If you refresh the page mid-build, the stream picks up where it was.

A build can fail or refuse, and the status line says why:

- "Not tailored: we only have a snippet of this job description." Paste the full description first. See [reviewing matches](reviewing-matches.md#when-a-description-is-only-a-snippet).
- "A tailoring build or a revision is already in progress for this job." Wait for it to finish.
- "Error: ..." Something went wrong. Click **Tailor resume + cover letter** again.

Before it writes, blastjob researches the company so the cover letter can say true things about the employer. If a Jina key is set, the research comes from web pages it actually read, and every fact cites the page and quotes it. Pages must be about this employer, matched by its website or by its name plus the job's location. Without a key, or when no page can be tied to the employer, it falls back to what the model already knows, and the packet verification says so (see below). A confidential posting skips research.

## The packet

The **Packet** section lists what was built, with a badge:

- **current** means nothing it was built from has changed since.
- "needs re-tailoring" means your work history or a template changed after it was built, or you pasted or fetched a fuller job description. The reason is printed under the heading. Click **Re-tailor** to build again.

Each file is a link. Click one to open it in your browser, which is also how you reach it from a phone or tablet. The set includes the resume and cover letter in a few formats, the job description, the company research notes, a verification report, and a small page with the best link to apply. The Files list under **Activity** shows the same files for the job. **Packets**, under **More**, lists every bundle ever built, newest first, with the cost of each.

On the machine running the cockpit, **Reveal in Finder** opens the folder. It does not appear when you use the cockpit from another device.

Finished packets are filed into folders for you. See [events and filing](../how-it-works/events-and-filing.md) if you want the folder names.

## Read the verification

Every build is checked, and the result is in the **Packet verification** section. The badge at the top is one of:

- **ready**: nothing blocking was found.
- **quarantined**: something blocking was found, and the documents are held back. See below.
- **not verified**: the checks themselves did not run. A packet whose checks did not run is not a clean packet. The page lists which checks crashed.

Findings come in three sizes:

- **Fatal** blocks the packet. Examples are characters that job boards reject, damaged text, a number or total that is not in your work history, or a cover letter claim the research contradicts.
- **Major** is worth fixing but does not block.
- **Minor / advisory** never blocks. It comes from a published resume-parser rulebook or from blastjob's own writing conventions (em dashes, stale cliches, repeating the posting's own phrases), so treat it as a second opinion worth a glance.

Each finding shows the exact text, the file and line, and what it was checked against.

### Groundedness

The **Groundedness** section scores how many resume claims are backed by a quote from your work history. When the score is under the floor (85), it shows a **needs review** badge and a table of "Claim in the packet". Each row is a sentence the review could not tie to your work history.

Two things to know. The score is uncalibrated: the floor has not been tested against a human-reviewed set of claims, so a low score is a prompt to read, not a verdict. And nothing is ever blocked on it alone.

For each row, do one of two things: fix the sentence, or add the true fact to your work history if it was missing. Never send a sentence you cannot stand behind.

The verification table also shows **JD phrasing**, how closely the resume repeats the posting's own words. It is not a fit score. A low number can describe a strong, honestly worded resume. **Cover letter groundedness** is a new check that flags more than it should, so read what it flags before believing it. **Coverage gaps** lists true material from your work history that the resume did not use. Treat the list as a prompt to check, not a to-do list.

### Company facts and sources

If the company research had no cited web page behind it, the verification lists a reason: "Company facts have no cited source". Check anything the cover letter says about the employer before you send it. If pages were found but none could be confirmed as this employer, a reason says that too, and the pages were not used. If the model had to judge which pages describe the employer, a reason tells you to check that the cited pages are about the right company.

## Quarantine and release

If a build finds a Fatal problem, the documents it makes unsendable are moved into a `quarantine` folder inside the packet, and the packet is marked "not sendable". Holding is per document. A Fatal in the resume holds the resume and its ATS version. A Fatal in the cover letter holds only the letter. A Fatal that is not tied to one document holds everything. A document that is not held was still checked; it just had no Fatal. The job description, research notes and verification report stay put so you can decide. It is also per role, so other roles tailored for the same company that day are not affected. Downloads of held files are refused. The rules behind this are in [packet verification](../truth-guarantees.md#packet-verification).

The page lists the reasons. You have three choices:

- Fix the cause and click **Re-tailor**.
- Click **Revise** to fix a specific problem, described below.
- If the flag is wrong, use **Release & send**. Type why in the **Send anyway** box and confirm. The reason is written to a record in the packet folder, and the documents come back. Do this only when you know something the check cannot, for example that a flagged character is part of the company's own name.

If you click **Mark applied** while the packet is held, blastjob cannot record what you sent and says so on the page. Release the packet if you did send it.

## Fix one thing without rebuilding

Type what to change in the **Revise** box under the packet, for example "drop the NIST claim, it is not in my history", and click **Revise**. blastjob revises the resume and checks it again. It keeps the old file as an earlier version. If the revised resume does not pass, the panel says "The revision was NOT accepted; do not send this packet."

Use **Re-tailor** instead when your work history or the job description changed, because that rebuilds everything from the new material.

## After you apply

Marking the role applied records which documents you sent, so a later rebuild cannot change that record. The next step is [applying](applying.md).

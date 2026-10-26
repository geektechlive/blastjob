# Applying and following up

**What this covers:** What to do once a role is ready: where to apply, how to record that you applied, how follow-ups work, how to find people and draft a note, and how to prepare for interviews. It is for anyone working through applications. blastjob only drafts and records. You send everything yourself, from your own email and the employer's site.

## Apply

Each role shows an "apply on ..." link on its row and at the top of its page. It points to the best place to apply. The employer's own posting is preferred over an aggregator, and an agency is last, so you do not apply through a middleman when the employer is known. If you know the employer's own page, paste it into **Employer posting URL** on the job's page. See [reviewing matches](reviewing-matches.md#the-same-role-twice).

Apply on the employer's site, using the documents from your [packet](tailoring.md).

## Mark it applied

If a recruiter has put you forward for a role, log it first: open the job and use **Log a submission** under **Recruiter submissions**. Applying directly to the same role afterwards asks you to confirm, because the employer would see two submissions. See [same role](../how-it-works/same-role.md#recruiter-submissions).

Back in the cockpit, click **Mark applied** on the role's row in the **Shortlist**, or click **Applied** under **Application status** on the job's page. Two things happen:

- The role moves to **Applied**, with today as the date you applied.
- blastjob records which resume and cover letter were in the packet at that moment. A later rebuild cannot change that record. If there was no packet to record, the page says so in one sentence.

The row also has a button per way it started: Cold application, Referral, Recruiter and Inbound. On the job's page the matching buttons read "Applied: cold application" and so on. One click marks it applied and records how it started. The plain button skips the question, which leaves it as unknown. You can set or change it later under **How it started** on the job's page, with an optional note such as a recruiter's firm. blastjob never guesses it: older applications show as unknown.

If the role looks like one you already applied to, a confirmation names the other posting before it goes through.

## Move it along

As things happen, record them. On the **Applied** page, the button on each row is named for the next step, such as "To screen". Click it to advance one step: applied, then screen, then interview, then offer. **Withdraw** ends the application and moves the role to **Closed**.

On the job's page you can set any status directly: Applied, Screen, Interview, Offer, Rejected or Withdrawn. This is also how you record a rejection. A rejected or withdrawn role moves to **Closed**. Use **Reopen** on **Closed** if you got it wrong. See [pipeline stages](stages.md).

## Follow-ups

Under **Application status**, pick a date in **Follow up on** and click **Set reminder**. When the date arrives, the role shows a flag on its row, and the **Applied** page says "N follow-ups due" with a **show** link that filters to them.

You also get a nudge without setting anything. An applied role with no update and no reminder gets flagged after 7 days ("applied 9 days ago, no update logged"). If the posting closes while you are in the process, the flag asks you to confirm where things stand. A role at screen or interview with no next step set and no update for 21 days (the Quiet application days setting) is flagged too, and so is an applied role that quiet; the flag suggests following up or marking it ghosted. You decide: nothing is marked ghosted for you. Roles in offer or closed stages are not nudged.

To write the message, click **Draft follow-up** on the job's page. The draft streams into a panel, is saved to a file (the path is shown), and shows an **Open in mail** link that opens your mail app with the text filled in. Read it, edit it and send it yourself.

Use **Save notes** to keep anything worth remembering: a recruiter's name, a referral, the next step. If the notes changed after the page loaded (linking duplicate postings adds a line, or you saved from another tab), the save is refused rather than overwriting them. The page then shows the saved notes above the box and keeps your unsaved text in it, with a message saying so. Combine the two and save again.

## The Activity list

Every kept role has an **Activity** list: when you applied, each stage change, replies, and automatic changes like a posting closing. Add your own entries (a thank-you note, a call) with the kind list, a date, an optional note and **Add**. You can **delete** entries you added. Automatic ones stay as a record.

The **Files** list under it shows the documents tied to the role.

## Recruiter replies

If you have set up a mailbox for replies, blastjob looks at your recent mail (the last 14 days) for messages from people at companies you applied to. It matches a sender to a role by an address you saved in that role's notes, or by the company's domain. The scan runs as part of the daily run, so a reply shows up after the next one. A match is shown in two places: under "Replies" in the daily digest ([the digest](digest.md)) and as a "Recruiter reply" entry in that role's **Activity** list.

It only shows you that a reply arrived. It never writes or sends a response. Mail from anyone else is ruled out from its header and never read. To set up the mailbox, see **Mail accounts** in **Settings** and [settings and secrets](../operations/settings-and-secrets.md).

## Find people and draft a note

On a kept role, **People at** the company has a **Find people** button. It runs a web search for the company's leaders in your field and its recruiters and takes about 30 seconds. It needs a Jina key in your secrets file. Every person it lists links to the page where they were found. A person with no real source page is dropped, so nobody is invented. After you have results the button reads **Search again**.

Open each link and check the person before you write to them. Then:

- **Draft note to** a person writes a short note addressed to them and shaped for their role. A recruiter gets a crisp fit summary. A hiring manager gets a peer-level note.
- **Mark contacted** records that you reached out. **Undo** takes it back.
- **Draft outreach email** (near the top of the job's page) writes a note with no recipient, opening "Dear Hiring Manager,".

Drafts appear in the panel. Copy what you want. They are not saved anywhere and nothing is sent.

**People you know here** lists your own connections at the company from your network import, with the people who recommended you first. Verify them on LinkedIn before you write.

## Prepare for interviews

Click **Interview prep** on a kept role. It builds a pack in the panel: the role at a glance, the job description matched to your own stories, likely questions and answers, a 30/60/90 plan, questions to ask, and gaps to be ready for. Stories come only from your work history. Any money or percentage figure it cannot find in your work history or the saved pay and company records is marked "[confirm]", so check it before you say it. The pack is saved in the role's packet folder. It will not run on a job description that is only a snippet.

## Research the company

The **Company** section on a kept job has **Research company** (or **Refresh research** once there is a workup). It reads a few pages about the employer and fills a profile with facts such as size, revenue and leadership, each with its source. Companies share names, so it uses only pages tied to this employer by its website or by its name beside the job's state. If a company has no website or location on file to check against, the model judges the pages and the profile says "Identity judged by the model, not confirmed"; check its sources. If no page can be tied to the employer, the button says it could not confirm which company and saves nothing: add the company's website under `discovery.employer_domains` and try again. **Companies** under **More** lists all profiles.

Under **Compensation signals**, record every pay figure you learn: a recruiter's range, a number from a site, what you asked for, what they answered. Choose a kind, fill in low and high, a source and a date, and click **Add signal**. The highest minimum you stated that drew no pushback shows as the "negotiated floor".

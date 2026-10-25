# Interview debrief

**What this covers:** How the interview debrief is built: the accepted transcript formats, why the transcript is held only in memory, how each figure is computed and where it is honest about uncertainty, what is stored, and the route and test map. It is for anyone changing `core/transcript.py`, `core/debrief.py`, `core/debrief_store.py`, `core/number_tokens.py` or `web/routes_debrief.py`. For the owner's view, see [Interview debrief](../using/interview-debrief.md).

## Shape

The debrief reads one pasted transcript and shows the owner what to fix. It is deterministic and local: no model call, no network. The transcript is never stored. Only computed figures and the owner's own short excerpts are.

1. `POST /jobs/{id}/debrief/upload` parses the pasted text (`transcript.parse_transcript`) and keeps the turns in memory (`debrief_store.UploadStore`). It redirects to the speaker page.
2. `GET /jobs/{id}/debrief/{upload_id}` asks which speaker the owner is.
3. `POST /jobs/{id}/debrief/{upload_id}/compute` claims the entry, computes (`debrief.compute_debrief`) and stores a row in `interview_debriefs` (migration 61).
4. `GET /jobs/{id}` shows the stored debriefs, newest first. `POST /jobs/{id}/debrief/{debrief_id}/delete` removes one, scoped to the job in the path.

`GET /jobs/{id}/debrief` is the paste page. Every POST goes through `require_csrf`, which also checks the Origin, and the job is always the path id.

## No file upload

The transcript arrives as an urlencoded form field, never as a file. A browser-side reader (`FileReader`, a small inline script with the page's nonce) can fill the textarea from a chosen file, so the file itself is never sent. This matters because Starlette spools a multipart body to a temporary file before any route runs (see [the web security model](web.md#security-model)), which would put the transcript on disk and break the promise to drop it.

`upload_guard.PathBodyCapMiddleware` applies to `/jobs/{id}/debrief...` paths only. It rejects a body over `MAX_DEBRIEF_BODY_BYTES` (2 MB) before anything buffers it, and answers 415 to a POST with any multipart content-type header. It matches by path prefix, because Starlette's route patterns also accept a trailing newline in the path. The parser also refuses text over `transcript.MAX_TRANSCRIPT_CHARS`.

## The in-memory store

`UploadStore` holds each parsed transcript with the job id it was pasted for and the speaker labels it contains. It is bounded and self-cleaning:

- an entry lives `TTL_S` (30 minutes) on a monotonic clock, and every `put`, `peek` and `claim` sweeps expired entries;
- at most `MAX_ENTRIES` (5) entries and `MAX_BYTES` (4 MB) in total; the oldest entry is evicted first;
- the app clears the store when it shuts down.

`claim` pops the entry under a lock, only when the job id matches and the speaker is one of the entry's labels. So two concurrent submits give one winner, and an upload id from another job behaves like an unknown id (410, "upload again"). The pop happens before computing. If saving then fails, the transcript is already gone and the owner pastes again; the unique `ingest_id` column also stops a duplicate row. OS swap of process memory is outside the app's control.

## Formats and the timeline

`parse_transcript` accepts three layouts and names them in its refusal:

- header lines `HH:MM:SS Speaker N` (or `MM:SS`) with the text below. A header line's label must look like a name (at most four words, no comma), so a sentence starting with a clock time is not a speaker change;
- WebVTT, with `<v Name>` tags or a `Name:` prefix; the only layout with cue end times;
- inline `[HH:MM:SS] Name: text`.

The parser treats the pasted text as hostile. Regexes see only the first `MAX_MATCH_CHARS` (500) of a line, a header line over 80 characters is text, and the patterns are written so they cannot backtrack; a turn's text is still taken from the full line, so long answers keep all their words. Speaker names are cut to `MAX_NAME_CHARS` (40), a transcript is refused above `MAX_TURNS` (5,000) turns or `MAX_SPEAKERS` (12) speakers, and the route parses in a worker thread (`run_in_threadpool`) so the event loop stays free.

The timeline is validated: timestamps must not decrease and must take at least two distinct values, and at least two speakers must appear. Overlapping WebVTT cues are clipped to the next cue's start. The end point is the last cue end for WebVTT and the last timestamp for the others.

## The figures

- **Word share** is the owner's words over all words, last turn included. It is the primary talk-share figure.
- **Time share** is an estimate. With cue times, a turn is credited its cue length, clipped to the next start. Without them, a turn is credited the time to the next turn's start, capped at `SILENCE_CAP_S` (90 seconds), so a long pause is not credited to a speaker. The last turn has no known length and is left out of time. `time_basis` is `cue` or `estimate`.
- **Longest answers.** Consecutive owner turns are merged. The top three by words (ties by start) are kept with the start time, word count, estimated seconds and the first 160 characters of the owner's own words.
- **Question-like lines near the close.** In the last 15% of start-to-end point, the owner's sentences that end in a question mark or start with an interrogative word. The count and up to three excerpts are stored. A contraction such as "can't" does not count. Punctuation in a transcript varies, and the page says so.
- **Numbers.** The owner's lines only. The tokens are the shared figure tokens in `core/number_tokens.py` (the same definition and comparison keys the prep pack uses; see [truth guarantees](../truth-guarantees.md)), compared as whole tokens with `work_history.md`, so `$182` is not cleared by `$182,000`. The transcript policy on top: bare one- to three-digit integers are not tokens, and years 1990 to 2039 are skipped. Duplicates are dropped and at most ten prompts are kept, each with its timestamp. A missing, unreadable or empty work history gives `numbers_status = unavailable` and no prompts. A prompt is a prompt: the wording is "You said X: not in your work history", never "wrong".

## What is stored

The table `interview_debriefs` holds `job_id`, the unique `ingest_id`, `created_at`, an optional owner label (60 characters), the owner's speaker label, duration, `time_basis`, word counts, seconds, the shares, JSON for the longest answers, the closing excerpts and the number prompts, and `numbers_status`. It has no transcript text, no interviewer words and no file name. The speaker page shows a short preview of each speaker's first line, but only in that response.

Every stored value is rendered through Jinja autoescaping; nothing is marked safe.

## Tests

- `tests/test_debrief_parse.py`: each format, refusals, timeline rules.
- `tests/test_debrief_compute.py`: the figures and the number policy.
- `tests/test_debrief_store.py`: expiry, bounds, claiming, concurrency.
- `tests/test_debrief_web.py`: the flow, CSRF on every POST, job scoping, the size cap and multipart refusal, failure cleanup, and a search of the database for interviewer text.
- `tests/test_debrief_migration.py`: migration 61.
- `tests/debrief_fixtures.py`: invented transcripts in the three formats.

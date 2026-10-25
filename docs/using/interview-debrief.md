# Interview debrief

**What this covers:** How to turn a transcript of one of your interviews into a short list of things to fix: how much you talked, your longest answers, whether you asked questions near the end, and numbers you said that are not in your work history. It is for anyone using the cockpit. For how it works inside, see [the debrief internals](../how-it-works/debrief.md).

## What you get

Everything is worked out on your computer by fixed rules. There is no model call and nothing leaves the machine. You get:

- Your share of the talking, by words. This is the main figure, because a transcript's timestamps only say when each turn began.
- Your share by time, shown as an estimate. A pause counts for at most 90 seconds, and the last turn has no known length. A WebVTT transcript has cue end times, and then the time figure uses them.
- Your three longest answers, with the start time, the word count and the first 160 characters of what you said.
- How many question-like lines you spoke in the last 15% of the interview, with up to three of them. A line counts if it ends in a question mark or starts with a word such as "what" or "how". Transcription tools punctuate differently, so this is a guide.
- Numbers you said that are not in your work history. Each one is a prompt to check, never a verdict: you may have rounded, or the figure may be right and simply missing from your work history.

## Make a debrief

1. Open a job, find the **Interview debrief** section, and choose **Add a debrief from a transcript**.
2. Paste the transcript into the box. Or use **Choose a transcript file**: your browser reads the file into the box, and the file itself is not uploaded. Pasting also works from an iPad.
3. Optionally add a label such as "second round" (up to 60 characters). The file name is never kept.
4. Press **Read transcript**.
5. Pick which speaker you are, then press **Make the debrief**.

The debrief appears on the job page, newest first. **Delete this debrief** removes one.

## Formats that work

- Header lines with the text below: `00:01:05 Speaker 1`, then the words on the next lines. Minutes and seconds only (`01:05`) also works.
- WebVTT, a file that starts with `WEBVTT`, with `<v Name>` tags or `Name: text` lines.
- One line per turn: `[00:01:05] Name: text`.

A transcript with no timestamps, with a single speaker, or whose timestamps run backwards is refused, and the page says why. Keep a transcript under about 1.5 MB.

## What is kept

Only the figures and your own short excerpts are saved. The interviewer's words, the transcript and the file name are not. The pasted transcript waits in the cockpit's memory, for up to 30 minutes, until you pick a speaker; it is dropped at that moment, and also when you restart the cockpit. If the cockpit cannot save the result, it says so and you paste the transcript again. Your operating system may write memory to its swap file, and blastjob cannot control that.

## Numbers that look wrong

The check reads only money, percentages, figures with a scale such as "3.5 million", and numbers with a thousands separator or four digits or more. Plain small numbers such as "12 people" and years are not checked, and spoken numbers such as "ninety percent" are not read. If your work history file cannot be read, the page says the numbers check is unavailable instead of listing everything.

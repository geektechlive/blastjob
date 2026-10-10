"""Scoring-quality defects found on the live DB (2026-09-12).

Four separate ways the alignment scorer produced a wrong or unstable answer:

1. **Comp magnitude was never sanity-checked.** The model reads "$180K - $240K"
   and returns `comp_min=180, comp_max=240`; the old `_clamp(v, 0, 10_000_000)`
   accepted those verbatim. Nine live rows carry 3-digit "salaries" — a
   fictional company's COO, another's Director of Cybersecurity, a third's
   Managing Director — all of which the gate then read as $240 against a
   comp floor far above that. The parser is not at fault:
   `comp.parse_salary_range("$180K - $240K")` returns (180000, 240000) correctly.
2. **Only the first 2,000 characters of a JD reached the model.** Live Workday
   postings run 5,500-9,900 chars and open with company boilerplate, so the
   scorer was judging fit from the part of the posting that says the least.
3. **Run-to-run variance.** Nine groups of byte-identical `raw_jd` scored
   differently on different days (90/89/88/84; 78/80/84; 76/72), flipping jobs
   across the surface threshold.
4. **A failed batch lost all 15 of its jobs.** 21 such warnings in the log.

Every test here is offline: `make_stream` is always replaced by a local fake.
"""

from __future__ import annotations

import pytest

from blastjob.llm.cost import CostTracker
from blastjob.matching import scorer
from blastjob.matching.scorer import parse_scores, sanitize_comp, score_candidates
from blastjob.models.config import BlastJobConfig

# --------------------------------------------------------------------------
# Offline provider fake
# --------------------------------------------------------------------------


class _Usage:
    input_tokens = 100
    output_tokens = 50
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0


class _Final:
    usage = _Usage()


class _FakeStream:
    def __init__(self, text: str):
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @property
    def text_stream(self):
        return self._iter()

    async def _iter(self):
        yield self._text

    async def get_final_message(self):
        return _Final()


class Recorder:
    """Stands in for `make_stream`, recording every call and replying on script."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls: list[dict] = []

    async def __call__(self, cfg, system, messages, max_tokens=4096, **kwargs):
        prompt = "\n".join(b["text"] for b in messages[0]["content"])
        self.calls.append(
            {
                "prompt": prompt,
                "system": "\n".join(b["text"] for b in system),
                "kwargs": kwargs,
            }
        )
        reply = self._replies.pop(0) if self._replies else "[]"
        if isinstance(reply, Exception):
            raise reply
        return _FakeStream(reply)

    @property
    def n(self) -> int:
        return len(self.calls)


def scored(*job_ids: int, alignment: int = 80) -> str:
    items = [
        f'{{"job_id": {j}, "alignment": {alignment}, "rationale": "ok", '
        f'"strengths": ["a"], "gaps": [], "comp_min": 0, "comp_max": 0}}'
        for j in job_ids
    ]
    return "[" + ", ".join(items) + "]"


def candidate(job_id: int, jd: str | None = None) -> dict:
    # Distinct text per job: an identical default JD would make every candidate
    # a duplicate of the first and quietly collapse the fallback tests.
    jd = f"Role {job_id}. Lead the Ops organisation. " * 20 if jd is None else jd
    return {"job_id": job_id, "title": "VP Ops", "company": f"Co{job_id}", "jd": jd}


# --------------------------------------------------------------------------
# Defect 1 — comp magnitude sanity check
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected, why",
    [
        ((180, 240), (180_000, 240_000), "the live Acme/Fabrikam bug: $180K-$240K as 180/240"),
        ((260, 360), (260_000, 360_000), "Associated Electric Managing Director"),
        ((0, 240), (0, 240_000), "a lone thousands-shorthand bound still scales"),
        ((180_000, 240_000), (180_000, 240_000), "already in dollars — untouched"),
        ((45, 65), (0, 0), "plainly an hourly rate — no signal, never a guess"),
        ((85, 110), (0, 0), "mixed magnitudes (hourly vs thousands) — ambiguous, no signal"),
        ((5_000, 8_000), (0, 0), "4-digit: monthly? truncated annual? ambiguous, no signal"),
        ((9_000_000, 9_500_000), (0, 0), "implausible for an annual salary"),
        ((0, 0), (0, 0), "no comp stated stays no comp"),
        ((240, 180), (0, 0), "incoherent pair — not silently transposed"),
    ],
)
def test_sanitize_comp_rule(raw, expected, why):
    assert sanitize_comp(*raw) == expected, why


def test_parse_scores_applies_the_magnitude_rule():
    """The end-to-end path that wrote the nine bad rows."""
    text = (
        '[{"job_id": 3, "alignment": 74, "rationale": "COO", "strengths": [], '
        '"gaps": [], "comp_min": 180, "comp_max": 240}]'
    )
    out = parse_scores(text, {3})
    assert (out[3]["comp_min"], out[3]["comp_max"]) == (180_000, 240_000)


def test_parse_scores_drops_an_hourly_figure_rather_than_guessing():
    text = (
        '[{"job_id": 7, "alignment": 80, "rationale": "x", "strengths": [], '
        '"gaps": [], "comp_min": 52, "comp_max": 78}]'
    )
    out = parse_scores(text, {7})
    # 0 means "no signal": the gate treats a 0 comp_max as unknown and surfaces
    # the role badged, rather than burying it.
    assert (out[7]["comp_min"], out[7]["comp_max"]) == (0, 0)


# --------------------------------------------------------------------------
# Defect 2 — the scorer must see the requirements, not the boilerplate
# --------------------------------------------------------------------------

SENTINEL = "FIFTEEN_YEARS_OF_ENTERPRISE_IT_LEADERSHIP"


def workday_sized_jd() -> str:
    boilerplate = "Acme is a great place to work and we value our people. " * 150  # ~8.2k
    requirements = (
        "\n\nMinimum Qualifications\n"
        f"- {SENTINEL}\n"
        "- Experience owning a multi-site technology budget\n"
        "- Track record of building and leading platform teams\n"
    ) * 6
    jd = boilerplate + requirements
    assert len(jd) > 9_000, len(jd)
    return jd


def test_default_cap_is_sized_for_real_workday_postings():
    assert scorer.DEFAULT_JD_CHAR_CAP >= 6_000


async def test_requirements_section_reaches_the_model(monkeypatch):
    jd = workday_sized_jd()
    assert SENTINEL not in jd[: scorer.DEFAULT_JD_CHAR_CAP], "control: plain truncation loses it"

    rec = Recorder([scored(1)])
    monkeypatch.setattr(scorer, "make_stream", rec)
    await score_candidates(BlastJobConfig(), "history", [candidate(1, jd)], CostTracker())

    assert SENTINEL in rec.calls[0]["prompt"], "the requirements section must survive selection"


async def test_short_jd_is_sent_whole(monkeypatch):
    jd = "Short posting. Requirements: lead ops."
    rec = Recorder([scored(1)])
    monkeypatch.setattr(scorer, "make_stream", rec)
    await score_candidates(BlastJobConfig(), "history", [candidate(1, jd)], CostTracker())
    assert jd in rec.calls[0]["prompt"]


async def test_excerpt_never_exceeds_the_cap(monkeypatch):
    rec = Recorder([scored(1)])
    monkeypatch.setattr(scorer, "make_stream", rec)
    await score_candidates(
        BlastJobConfig(),
        "history",
        [candidate(1, workday_sized_jd())],
        CostTracker(),
        jd_char_cap=3_000,
    )
    prompt = rec.calls[0]["prompt"]
    body = prompt.split("Description:\n", 1)[1].split("\n</JOB>", 1)[0]
    assert len(body) <= 3_000


# --------------------------------------------------------------------------
# Defect 3 — variance
# --------------------------------------------------------------------------


async def test_determinism_rules_reach_the_model(monkeypatch):
    rec = Recorder([scored(1)])
    monkeypatch.setattr(scorer, "make_stream", rec)
    await score_candidates(BlastJobConfig(), "history", [candidate(1)], CostTracker())
    assert scorer.SCORING_DETERMINISM_RULES.strip() in rec.calls[0]["system"]


async def test_temperature_is_passed_when_the_provider_accepts_it(monkeypatch):
    rec = Recorder([scored(1)])
    monkeypatch.setattr(scorer, "make_stream", rec)
    await score_candidates(BlastJobConfig(), "history", [candidate(1)], CostTracker())
    assert rec.calls[0]["kwargs"].get("temperature") == scorer.SCORING_TEMPERATURE


async def test_temperature_is_omitted_when_the_provider_does_not_accept_it(monkeypatch):
    """The shipped `make_stream` has no `temperature` parameter yet (see the
    INTEGRATION PATCH). Passing one unconditionally would TypeError every call."""
    seen: list[dict] = []

    async def narrow(cfg, system, messages, max_tokens=4096):
        seen.append({})
        return _FakeStream(scored(1))

    monkeypatch.setattr(scorer, "make_stream", narrow)
    res = await score_candidates(BlastJobConfig(), "history", [candidate(1)], CostTracker())
    assert res.scores and seen, "the call must succeed against a temperature-less provider"


# --------------------------------------------------------------------------
# Defect 4 — identical raw_jd is scored once, not re-scored per repost
# --------------------------------------------------------------------------


async def test_identical_jd_is_scored_once_and_the_score_shared(monkeypatch):
    jd = "Identical repost. " * 40
    rec = Recorder([scored(1, alignment=83)])
    monkeypatch.setattr(scorer, "make_stream", rec)

    # A genuine repost: SAME employer and title, byte-identical text. Reuse keys on
    # company+title+text, not text alone, so two different employers sharing
    # boilerplate stay two jobs (see scorer.reuse_key).
    repost = [{"job_id": i, "title": "VP Ops", "company": "Acme", "jd": jd} for i in (1, 2, 3)]
    res = await score_candidates(BlastJobConfig(), "history", repost, CostTracker())
    assert rec.n == 1
    assert rec.calls[0]["prompt"].count("<JOB id=") == 1, "only the representative is sent"
    assert res.scores[1]["alignment"] == res.scores[2]["alignment"] == res.scores[3]["alignment"]
    assert res.reused == 2


async def test_prior_score_for_an_unchanged_jd_is_reused_without_a_call(monkeypatch):
    jd = "Yesterday's posting, reposted byte for byte. " * 30
    rec = Recorder([])
    monkeypatch.setattr(scorer, "make_stream", rec)

    prior = {
        scorer.reuse_key("Acme", "VP Ops", jd): {
            "alignment": 84,
            "rationale": "scored yesterday",
            "strengths": ["a"],
            "gaps": [],
            "comp_min": 0,
            "comp_max": 0,
        }
    }
    res = await score_candidates(
        BlastJobConfig(),
        "history",
        [{"job_id": 9, "title": "VP Ops", "company": "Acme", "jd": jd}],
        CostTracker(),
        prior_scores=prior,
    )
    assert rec.n == 0, "an unchanged JD must not be re-scored"
    assert res.scores[9]["alignment"] == 84
    assert res.reused == 1


async def test_presence_only_postings_are_never_collapsed(monkeypatch):
    """An empty description means 'presence only'. All of them hash alike, so
    fingerprint reuse must not treat them as the same job."""
    rec = Recorder([scored(1, 2)])
    monkeypatch.setattr(scorer, "make_stream", rec)
    res = await score_candidates(
        BlastJobConfig(),
        "history",
        [candidate(1, ""), candidate(2, "")],
        CostTracker(),
    )
    assert rec.calls[0]["prompt"].count("<JOB id=") == 2
    assert set(res.scores) == {1, 2}
    assert res.reused == 0


def test_fingerprint_is_none_for_a_jd_too_short_to_identify():
    assert scorer.jd_fingerprint("") is None
    assert scorer.jd_fingerprint("Apply here.") is None
    assert scorer.jd_fingerprint("x" * 400) is not None


def test_fingerprint_does_not_fuzzy_match():
    a = "Lead the platform team. " * 20
    assert scorer.jd_fingerprint(a) != scorer.jd_fingerprint(a + " One extra requirement.")


# --------------------------------------------------------------------------
# Defect 5 — a failed batch must not lose all 15 jobs
# --------------------------------------------------------------------------


async def test_failed_batch_falls_back_to_per_job_retries(monkeypatch):
    batch = [candidate(i) for i in range(1, 4)]
    # Two batch attempts return junk; the three per-job calls then succeed.
    rec = Recorder(["no json here", "still no json", scored(1), scored(2), scored(3)])
    monkeypatch.setattr(scorer, "make_stream", rec)

    res = await score_candidates(BlastJobConfig(), "history", batch, CostTracker(), batch_size=3)
    assert set(res.scores) == {1, 2, 3}, "the batch failure must not lose the jobs"
    assert res.failed_batches == 0, "nothing was actually lost"
    assert res.recovered_jobs == 3


async def test_partial_batch_response_recovers_the_missing_jobs(monkeypatch):
    batch = [candidate(i) for i in range(1, 4)]
    rec = Recorder([scored(1, 2), scored(3)])  # job 3 missing from the batch reply
    monkeypatch.setattr(scorer, "make_stream", rec)

    res = await score_candidates(BlastJobConfig(), "history", batch, CostTracker(), batch_size=3)
    assert set(res.scores) == {1, 2, 3}
    assert res.recovered_jobs == 1


async def test_fallback_stops_after_a_batch_recovers_nothing(monkeypatch):
    """21 batch failures in one log means a systemic provider fault. Retrying
    every job of every batch individually would burn hundreds of calls and still
    recover nothing, so the fallback trips a breaker after one total failure."""
    cands = [candidate(i) for i in range(1, 13)]
    rec = Recorder([RuntimeError("provider down")] * 200)
    monkeypatch.setattr(scorer, "make_stream", rec)

    res = await score_candidates(BlastJobConfig(), "history", cands, CostTracker(), batch_size=4)
    assert res.scores == {}
    assert res.failed_batches == 3
    # batch 1: 2 attempts + 4 per-job probes; batches 2 and 3: 2 attempts each.
    assert rec.n == 10, f"breaker should cap the fallback, got {rec.n} calls"


async def test_failed_batches_still_counts_only_total_losses(monkeypatch):
    """`failed_batches` feeds MatchResult.scoring_failures in the web layer; it
    must keep meaning 'this batch ended with nothing', not 'something retried'."""
    rec = Recorder(["garbage", "garbage", scored(1), "garbage"])
    monkeypatch.setattr(scorer, "make_stream", rec)
    res = await score_candidates(
        BlastJobConfig(),
        "history",
        [candidate(1), candidate(2)],
        CostTracker(),
        batch_size=2,
    )
    assert set(res.scores) == {1}
    assert res.failed_batches == 0  # the batch did yield a score
    assert res.unrecovered_jobs == 1  # but job 2 is still unscored

"""Batched LLM requirements-alignment scoring.

One make_stream call scores a whole batch of pre-filtered JDs. On the Claude CLI
provider there is no prompt caching (the prompt is flattened to a single string),
so the cost strategy is "few big calls," not "many small cached calls": we send N
JDs + the work history once per batch, not once per job.

Output is validated defensively: only known job_ids are accepted, scores are
clamped, and prompt-injection attempts inside a JD can't smuggle fields through
because we read only the whitelisted keys.

Four quality defects found on the live DB (2026-09-12) are handled here:

* **Comp magnitude** (`sanitize_comp`). The model reads "$180K - $240K" and
  returns 180/240; the old clamp accepted anything from 0 to 10,000,000. Nine
  live rows carried 3-digit "salaries" that the gate then compared against
  the configured comp floor. The rule is deliberately conservative — see
  `sanitize_comp`.
* **JD selection** (`select_jd_excerpt`). Live Workday postings run 5,500-9,900
  chars and open with company boilerplate, so a 2,000-char head truncation
  showed the model the least informative third. We now keep a short head for
  framing and then splice in the requirements/qualifications section.
* **Run-to-run variance** (`SCORING_DETERMINISM_RULES`, `SCORING_TEMPERATURE`,
  fingerprint reuse). Nine groups of byte-identical `raw_jd` scored differently
  on different days and flipped across the surface threshold. Reuse makes that
  spread exactly 0 for an unchanged posting.
* **Batch loss** (per-job fallback). A batch whose response failed to parse lost
  all 15 of its jobs; 21 such warnings sit in the log.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from blastjob.llm import caching
from blastjob.llm.cost import CostTracker, cost_from_usage
from blastjob.llm.jsonio import extract_json_list
from blastjob.llm.providers import make_stream
from blastjob.matching.prompts import build_user_content, system_for
from blastjob.models.config import BlastJobConfig

DEFAULT_BATCH_SIZE = 15

# Raised from 2,000. Live Workday JDs average 5,500-9,900 chars (one tenant's
# JD ran 9,883; another's 9,583; another's 8,988). The cap is a backstop on the *selected*
# excerpt, not on the raw posting — selection is what keeps the prompt affordable.
DEFAULT_JD_CHAR_CAP = 6000

# Chars of the posting kept verbatim from the top, for role framing, before the
# requirements section is spliced in.
JD_HEAD_CHARS = 1500
# Labelled, because the model sees it inside the <JOB> fence and must not read
# it as posting content.
_JD_SPLICE = "\n[... middle of posting omitted; requirements section follows ...]\n"

# Below this, a JD carries too little to identify a posting by content. Notably
# an empty description means "presence only" (see CLAUDE.md) and every such
# posting hashes alike — collapsing them would share one score across unrelated
# jobs. Those are always scored individually.
MIN_JD_CHARS_FOR_REUSE = 200

# Deterministic scoring where the provider supports it. Threaded through
# make_stream only if that function accepts it (see `_temperature_kwargs`).
SCORING_TEMPERATURE = 0.0

# Per-run ceiling on single-job fallback calls after a batch fails to parse.
FALLBACK_JOB_BUDGET = 30

# Plausible US annual compensation, in dollars. The lower bound matches
# `comp.parse_salary_range`, which also refuses to read a salary below $10k.
MIN_PLAUSIBLE_ANNUAL_USD = 10_000
MAX_PLAUSIBLE_ANNUAL_USD = 5_000_000
# A 3-digit figure in this range is thousands-shorthand ("180" for $180K). This
# assumes a salaried US role, not an hourly contractor rate -- a posting that
# quotes "$100-$999/hour" would be misread as a $100K-$999K salary. Flag for
# review if a profile's target postings routinely quote hourly rates in that
# range.
MIN_THOUSANDS_SHORTHAND = 100
MAX_THOUSANDS_SHORTHAND = 999

log = logging.getLogger(__name__)


# Appended to the scorer's system prompt. Deliberately carries NO numbers: a new
# weighted rubric would re-baseline every score while the recorded 78 threshold
# stayed fixed, which is re-tuning the threshold by the back door. This only
# tells the model to apply the existing SCALE bands reproducibly.
SCORING_DETERMINISM_RULES = """

DETERMINISM (run-to-run stability):
Identical inputs must produce an identical score. Work in this fixed order:
1. Choose the SCALE band from the evidence in the fenced job description and the \
work history alone — never from how much space the posting devotes to a topic, \
its formatting, its posting date, or how many times it has been reposted.
2. Pick the value inside that band. Stay inside it: evidence that moves the fit \
only slightly must not carry the score across a band boundary.
3. Do not re-weigh or round afterwards to make the number "feel" considered.
A bracketed "[... middle of posting omitted ...]" line inside a fence marks text \
that was cut for length; it is not part of the posting and carries no meaning.
Output the JSON array only — no commentary, no alternatives, no hedging, no \
confidence statements.
"""

# Neutral/unconfigured rendering, kept importable by name for callers and tests
# that want the prompt text without a config (e.g. to assert on wording that
# never varies with the candidate profile). The real scoring call below builds
# its own system prompt per-call from `cfg.candidate` via `system_for`.
_SYSTEM_PROMPT = system_for(None) + SCORING_DETERMINISM_RULES


@dataclass
class ScoringResult:
    """Scores plus how the run went.

    `failed_batches` keeps its original meaning — a batch that ended with zero
    scores even after the per-job fallback — because the pipeline surfaces it as
    `MatchResult.scoring_failures`. The other counters are additive detail:

    * `recovered_jobs` — jobs the per-job fallback rescued from a batch that
      failed or answered only partially.
    * `unrecovered_jobs` — jobs still unscored after the fallback (retried next
      run), including those inside a fully failed batch.
    * `reused` — jobs given a prior/sibling score for a byte-identical JD
      instead of being re-scored.
    """

    scores: dict[int, dict] = field(default_factory=dict)
    failed_batches: int = 0
    recovered_jobs: int = 0
    unrecovered_jobs: int = 0
    reused: int = 0


def _clamp(v: object, lo: int, hi: int) -> int:
    if not isinstance(v, (int, float, str)):
        return 0
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 0
    return max(lo, min(hi, n))


def _as_int(v: object) -> int:
    """Non-negative int, or 0 for anything that isn't a number."""
    if not isinstance(v, (int, float, str)):
        return 0
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def sanitize_comp(comp_min: object, comp_max: object) -> tuple[int, int]:
    """Sanity-check the magnitude of a model-reported salary pair.

    The rule, applied to the pair as a whole so the two bounds can never be read
    on different scales:

    * every non-zero bound in $10,000-$5,000,000 -> a plain annual figure, kept;
    * every non-zero bound in 100-999            -> thousands-shorthand ("180"
      for $180K), scaled by 1,000;
    * anything else                              -> (0, 0), no signal.

    That last line covers a plainly hourly figure (1-99), the genuinely
    ambiguous 1,000-9,999 band (monthly? weekly? a truncated annual?), an
    implausible magnitude, and any pair whose bounds land on different scales
    (e.g. 85 and 110 — "$85-110/hr" and "$85K-$110K" are indistinguishable).
    An out-of-order pair is dropped rather than silently transposed.

    Returning 0 is safe and deliberate: the pipeline only records model comp
    when `comp_max > 0`, and the gate reads a 0 as "comp unknown", which
    SURFACES the role badged. A wrong number would instead bury it — or be
    quoted in a negotiation.
    """
    lo, hi = _as_int(comp_min), _as_int(comp_max)
    values = [v for v in (lo, hi) if v]
    if not values:
        return 0, 0

    if all(MIN_PLAUSIBLE_ANNUAL_USD <= v <= MAX_PLAUSIBLE_ANNUAL_USD for v in values):
        scale = 1
    elif all(MIN_THOUSANDS_SHORTHAND <= v <= MAX_THOUSANDS_SHORTHAND for v in values):
        scale = 1_000
    else:
        log.debug("comp magnitude not resolvable, dropping: min=%r max=%r", comp_min, comp_max)
        return 0, 0

    lo, hi = lo * scale, hi * scale
    if lo and hi and lo > hi:
        log.debug("comp bounds out of order, dropping: min=%d max=%d", lo, hi)
        return 0, 0
    return lo, hi


# Headings that introduce the part of a posting worth scoring. Matched at the
# start of a line, tolerating a little markdown/bullet/numbering chrome.
_REQ_MARKER = re.compile(
    r"^[ \t]{0,8}[#*\->\d.)\s]{0,6}("
    r"(?:minimum|basic|preferred|required|key|essential|desired)\s+"
    r"(?:qualifications|requirements|experience|skills)"
    r"|qualifications?\b"
    r"|requirements\b"
    r"|what you(?:'|’)?ll (?:do|bring|need)"
    r"|what we(?:'|’)?re looking for"
    r"|who you are"
    r"|essential (?:duties|functions|job functions)"
    r"|(?:key |primary |core )?responsibilities"
    r"|skills (?:and|&) (?:experience|qualifications)"
    r"|experience (?:and|&) (?:skills|qualifications)"
    r")",
    re.IGNORECASE | re.MULTILINE,
)


def select_jd_excerpt(jd: str, cap: int) -> str:
    """Return at most `cap` chars of `jd`, preferring the requirements section.

    Short postings are returned whole. For a long one we keep a head for framing
    (who the employer is, what the role is called) and then splice in from the
    first requirements/qualifications heading that falls past the head — which
    is where the signal actually lives on a 9,000-char Workday posting. With no
    recognisable heading we fall back to plain head truncation.

    Both pieces are verbatim slices of the JD, so nothing new is synthesised
    into the `<JOB>` fence.
    """
    jd = jd or ""
    if cap <= 0:
        return ""
    if len(jd) <= cap:
        return jd

    head_chars = min(JD_HEAD_CHARS, cap // 4)
    marker = _REQ_MARKER.search(jd, head_chars)
    if marker is None:
        return jd[:cap]

    head = jd[:head_chars].rstrip()
    budget = cap - len(head) - len(_JD_SPLICE)
    if budget <= 0:
        return jd[:cap]
    return head + _JD_SPLICE + jd[marker.start() : marker.start() + budget]


def jd_fingerprint(jd: str) -> str | None:
    """Stable identity for a posting's text, or None when it can't have one.

    Byte-exact apart from whitespace normalisation — deliberately not fuzzy. The
    live evidence is *byte-identical* reposts; a near-match rule would copy a
    stale score onto a JD that actually changed, and nothing downstream could
    detect it.
    """
    normalized = " ".join((jd or "").split())
    if len(normalized) < MIN_JD_CHARS_FOR_REUSE:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def reuse_key(company: str, title: str, jd: str) -> str | None:
    """Identity of a posting for score-reuse purposes, or None if it has none.

    Employer and title are folded in alongside the JD hash so reuse can never
    cross postings: a short JD that two employers happen to share word-for-word
    (boilerplate "apply on our site" text is real) stays two separate jobs.
    `MIN_JD_CHARS_FOR_REUSE` then only governs whether a posting is
    content-identifiable at all, not whether two jobs are the same job.
    """
    fp = jd_fingerprint(jd)
    if fp is None:
        return None
    who = " ".join(f"{company or ''}|{title or ''}".split()).lower()
    return hashlib.sha256(f"{who}|{fp}".encode()).hexdigest()


def _extract_json(text: str) -> list:
    """Pull a JSON list of score objects out of a model response.

    Tolerates: code fences, prose around the JSON, a bare single object (the CLI
    sometimes returns one object instead of a 1-element array), and a top-level
    array. Always returns a list of dicts.

    Thin wrapper over the shared `llm.jsonio.extract_json_list` (consolidated
    2026-09-17 — this used to be a byte-for-byte fork of
    `core.research_facts._extract_json_array`); kept as a module-level name
    because `web/people.py` and `web/research.py` import it directly.
    """
    return extract_json_list(
        text, error_message="No JSON array or object found in scoring response."
    )


def normalize_score(item: dict) -> dict:
    """Whitelist + clamp one score record. Used for model output and for a prior
    score replayed from the DB, so both go through the same validation."""
    comp_min, comp_max = sanitize_comp(item.get("comp_min"), item.get("comp_max"))
    return {
        "alignment": _clamp(item.get("alignment"), 0, 100),
        "rationale": str(item.get("rationale", ""))[:400],
        "strengths": _str_list(item.get("strengths")),
        "gaps": _str_list(item.get("gaps")),
        "comp_min": comp_min,
        "comp_max": comp_max,
    }


def parse_scores(text: str, valid_ids: set[int]) -> dict[int, dict]:
    """Extract scores and return {job_id: {alignment, rationale, ...}}.

    Only whitelisted keys and known job_ids survive — a JD that tries to inject
    extra fields or a bogus id is ignored, not trusted.
    """
    data = _extract_json(text)
    out: dict[int, dict] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        raw_id = item.get("job_id")
        if not isinstance(raw_id, (int, str)):
            continue
        try:
            jid = int(raw_id)
        except (TypeError, ValueError):
            continue
        if jid not in valid_ids:
            continue
        out[jid] = normalize_score(item)
    return out


def _str_list(value: object, limit: int = 6) -> list[str]:
    """Coerce a model field into a clean list of short strings (injection-safe)."""
    if not isinstance(value, list):
        return []
    out = [
        str(v).strip()[:120] for v in value if isinstance(v, (str, int, float)) and str(v).strip()
    ]
    return out[:limit]


def _batches(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _temperature_kwargs(stream_fn: Callable) -> dict:
    """`temperature` only if the provider layer accepts it.

    The shipped `make_stream` has no such parameter yet (an INTEGRATION PATCH
    threads it to `messages.stream(...)` / `chat.completions.stream(...)`), and
    passing it unconditionally would TypeError every call. Note this buys
    nothing on the claude-cli provider, which is the default when no API key is
    set and exposes no temperature knob.
    """
    try:
        sig = inspect.signature(stream_fn)
    except (TypeError, ValueError):
        return {}
    accepts = "temperature" in sig.parameters or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    return {"temperature": SCORING_TEMPERATURE} if accepts else {}


@dataclass
class _ReusePlan:
    """How a candidate list splits into jobs to score and jobs to copy."""

    to_score: list[dict] = field(default_factory=list)
    reused: dict[int, dict] = field(default_factory=dict)  # job_id -> score (prior run)
    followers: dict[int, list[int]] = field(default_factory=dict)  # rep job_id -> siblings


def _plan_reuse(candidates: list[dict], prior_scores: dict[str, dict]) -> _ReusePlan:
    """Collapse byte-identical JDs onto one representative, and replay any score
    already held for an unchanged posting."""
    plan = _ReusePlan()
    rep_by_fp: dict[str, int] = {}
    for c in candidates:
        jid = int(c["job_id"])
        fp = reuse_key(c.get("company") or "", c.get("title") or "", c.get("jd") or "")
        if fp is None:
            plan.to_score.append(c)
            continue
        prior = prior_scores.get(fp)
        if prior is not None:
            plan.reused[jid] = normalize_score(prior)
            continue
        rep = rep_by_fp.get(fp)
        if rep is not None:
            plan.followers.setdefault(rep, []).append(jid)
            continue
        rep_by_fp[fp] = jid
        plan.to_score.append(c)
    return plan


async def score_candidates(
    cfg: BlastJobConfig,
    work_history_md: str,
    candidates: list[dict],
    cost_tracker: CostTracker,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    jd_char_cap: int = DEFAULT_JD_CHAR_CAP,
    on_progress: Callable[[int, int], None] | None = None,
    rejections: list[dict] | None = None,
    prior_scores: dict[str, dict] | None = None,
) -> ScoringResult:
    """Score every candidate (each: {job_id, title, company, jd}).

    `prior_scores` maps a `reuse_key` to a score already recorded for that exact
    employer/title/posting text; a match is replayed instead of re-scored, which is what
    stops a daily repost from being re-judged (and landing on the other side of
    the threshold) four mornings running.

    A batch whose response fails to parse — or which answers about only some of
    its jobs — falls back to single-job calls, under a per-run budget and a
    breaker so a systemic provider fault can't turn one lost batch into hundreds
    of futile calls. `failed_batches` still counts only batches that ended with
    nothing at all.
    """
    out = ScoringResult()
    plan = _plan_reuse(candidates, prior_scores or {})
    out.scores.update(plan.reused)
    out.reused += len(plan.reused)

    batches = list(_batches(plan.to_score, batch_size))
    budget = FALLBACK_JOB_BUDGET
    for idx, batch in enumerate(batches, 1):
        t0 = time.monotonic()
        parsed = await _score_one_batch(
            cfg, work_history_md, batch, cost_tracker, jd_char_cap, rejections=rejections
        )
        missing = [c for c in batch if int(c["job_id"]) not in parsed]

        if missing and len(batch) > 1 and budget > 0:
            attempt = missing[:budget]
            budget -= len(attempt)
            recovered = await _score_per_job(
                cfg, work_history_md, attempt, cost_tracker, jd_char_cap, rejections
            )
            if recovered:
                parsed.update(recovered)
                out.recovered_jobs += len(recovered)
                missing = [c for c in batch if int(c["job_id"]) not in parsed]
            else:
                # Nothing came back one-at-a-time either: the provider is down,
                # not the batch format. Stop paying for fallbacks this run.
                budget = 0
                log.warning("per-job fallback recovered nothing; disabling it for this run")

        log.info(
            "scoring batch %d/%d: %d jobs, %d scored, %d unrecovered, %.1fs",
            idx,
            len(batches),
            len(batch),
            len(parsed),
            len(missing),
            time.monotonic() - t0,
        )
        out.unrecovered_jobs += len(missing)
        if parsed:
            out.scores.update(parsed)
        else:
            out.failed_batches += 1
        if on_progress:
            on_progress(idx, len(batches))

    # A repost shares its representative's score verbatim — spread 0, by
    # construction. If the representative went unscored, so do its siblings.
    for rep, siblings in plan.followers.items():
        score = out.scores.get(rep)
        if score is None:
            out.unrecovered_jobs += len(siblings)
            continue
        for jid in siblings:
            out.scores[jid] = dict(score)
        out.reused += len(siblings)

    return out


async def _score_per_job(
    cfg: BlastJobConfig,
    work_history_md: str,
    candidates: list[dict],
    cost_tracker: CostTracker,
    jd_char_cap: int,
    rejections: list[dict] | None,
) -> dict[int, dict]:
    """Last resort after a batch response failed or came back short: one call per
    job. The batch already had its retries, so each job gets a single attempt."""
    out: dict[int, dict] = {}
    for c in candidates:
        parsed = await _score_one_batch(
            cfg, work_history_md, [c], cost_tracker, jd_char_cap, attempts=1, rejections=rejections
        )
        out.update(parsed)
    return out


async def _score_one_batch(
    cfg: BlastJobConfig,
    work_history_md: str,
    batch: list[dict],
    cost_tracker: CostTracker,
    jd_char_cap: int,
    attempts: int = 2,
    rejections: list[dict] | None = None,
) -> dict[int, dict]:
    """Score a single batch with a retry. The CLI provider occasionally returns
    an unparseable response (~1 in 5 for tiny batches); one retry drops that to
    near zero. Whatever is still missing afterwards is handled by the caller's
    per-job fallback rather than being written off."""
    valid_ids = {int(c["job_id"]) for c in batch}
    excerpted = [{**c, "jd": select_jd_excerpt(c.get("jd") or "", jd_char_cap)} for c in batch]
    system_prompt = system_for(cfg.candidate) + SCORING_DETERMINISM_RULES
    system = [caching.cached_text(system_prompt)]
    user_content = [
        caching.plain_text(build_user_content(work_history_md, excerpted, jd_char_cap, rejections))
    ]
    extra = _temperature_kwargs(make_stream)

    for _ in range(attempts):
        try:
            full = ""
            stream = await make_stream(
                cfg,
                system,
                [{"role": "user", "content": user_content}],
                max_tokens=4096,
                **extra,
            )
            async with stream:
                async for chunk in stream.text_stream:
                    full += chunk
                final = await stream.get_final_message()
            cost_tracker.record(cost_from_usage(final.usage, cfg.pricing))
            parsed = parse_scores(full, valid_ids)
            if parsed:
                return parsed
        except Exception as e:  # noqa: BLE001 — retry, then hand back to the caller
            log.warning("scoring batch failed for jobs %s: %s", sorted(valid_ids), e)
            continue
    log.warning(
        "scoring batch gave up after %d attempts: jobs %s",
        attempts,
        sorted(valid_ids),
    )
    return {}

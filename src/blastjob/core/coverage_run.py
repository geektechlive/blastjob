"""Wires the third bucket (`core/coverage_gap.py`) into an actual build.

`core/coverage.py::analyze_coverage` scores how well `work_history.md`
supports a job description. `core/coverage_gap.py::unused_support` joins that
report against a generated resume to find the requirements the history
PROVES but the resume never used. Until this module, nothing called either
of them from a build — the third bucket existed but produced nothing.
`coverage_gaps_for` is that call site's shared implementation; `build.py`
(owned by the integrator, see the INTEGRATION PATCH in this arc's report)
invokes it once per build and writes `render_gaps_md`'s output as a sidecar.

Fail-soft, deliberately: `coverage_gaps_for` returns `[]` on ANY failure —
a bad LLM response, a missing work_history.md, a cache write that can't
land, anything. A missing advisory (a gap sidecar that didn't get written)
costs the user nothing they did not already not have; a build that fails because
this optional check tripped costs them a packet they needed. The rest of the
packet-quality program (`packet_gate.py`, `prose_check.py`,
`core/build.py::_verify_packet`) already treats every checker this way — this
module holds the line.

NO LLM REWRITING OF GAP TEXT, EVER — and nothing here or downstream may
auto-insert a gap into a resume. This is the same discipline
`core/coverage_gap.py`'s module docstring lays out for `unused_support` and
`as_insertable_bullet`, and it applies with equal force to everything in this
module: `render_gaps_md` only ever echoes `gap.history_quote` back verbatim,
never rewords, summarizes, or reformats its words. The concrete failure this
guards against (2026-09-15 adversarial review, cited in `coverage_gap.py`):
feed a detected gap to an LLM and ask it to write a resume bullet for it, and
`work_history.md`'s "evaluated a message-queue platform migration" comes
back as "Led the message-queue platform migration" — a claim the candidate never
earned, wearing a genuine, substring-verified evidence quote as camouflage.
Every automated check in the pipeline (groundedness, the evidence-quote
substring check, this module's own cache) would wave that through, because
all of them verify that quoted text exists somewhere in the history, never
that the sentence built on top of it is true. `render_gaps_md` produces a
sidecar a HUMAN reads and manually copies from, in the candidate's own words, or not
at all. Any future change that has a model touch this text before a human
sees it must pass a claim-level diff first (see `coverage_gap.py`) — do not
add one casually.

CACHING AND ITS INVALIDATION RULE: `analyze_coverage` is one LLM call, and
its answer depends on the job description text and `work_history.md`
(which does not change mid-build, but can be edited between builds). A
re-tailor of the same job reuses the same job description, so re-running the
analysis on a re-tailor is a wasted call. The cache is content-addressed: the key is
`sha256(sha256(work_history) + ":" + job_description)[:20]`, stored as one JSON
file per distinct (history, JD) pair under `cache_dir` (default:
`{data_dir}/coverage_cache/`, stable across
builds and re-tailors — NOT the per-run packet folder, which changes date
and would defeat reuse across a re-tailor). Invalidation is therefore
automatic and exact: any change to the JD text or the work history, even one
character of whitespace, hashes to a different filename and is a clean cache
miss, never a stale hit (a history edit used to reuse the old analysis, whose
"your own words" quotes could no longer be in the history). Entries are never
evicted or expired — a hit is always correct for those exact texts, and one
file per distinct pair this app has ever seen is not enough volume to matter.
Files keyed by the old JD-only scheme are simply never read again. A corrupt
or unreadable cache file is treated as a miss (re-run the analysis, overwrite
the file) rather than an error.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from blastjob import config as cfg_mod
from blastjob.core.coverage import analyze_coverage
from blastjob.core.coverage_gap import unused_support
from blastjob.core.packet_report import CoverageGap
from blastjob.llm.cost import CostTracker
from blastjob.models.config import BlastJobConfig
from blastjob.models.coverage import CoverageReport

__all__ = ["coverage_gaps_for", "render_gaps_md"]


def _cache_key(job_description: str, work_history_md: str) -> str:
    history_hash = hashlib.sha256(work_history_md.encode("utf-8")).hexdigest()
    combined = f"{history_hash}:{job_description}"
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:20]


def _cache_path(cache_dir: Path, job_description: str, work_history_md: str) -> Path:
    return cache_dir / f"{_cache_key(job_description, work_history_md)}.json"


def _read_work_history(cfg: BlastJobConfig) -> str:
    """Current history text for the cache key. Missing reads as empty: the key
    still differs from any real history, and `analyze_coverage` raises (so the
    caller fails soft) before anything is cached."""
    path = cfg_mod.data_dir(cfg) / "work_history.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _load_cached_report(path: Path) -> CoverageReport | None:
    """A missing or corrupt cache file is a miss, not an error — see the
    module docstring's invalidation rule."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return CoverageReport.model_validate(data)
    except Exception:  # noqa: BLE001 — corrupt cache is a miss, never a crash
        return None


def _store_cached_report(path: Path, report: CoverageReport) -> None:
    """Best-effort. A cache write that fails costs a future re-tailor one
    extra LLM call, never the build in progress."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001 — see docstring above
        pass


async def coverage_gaps_for(
    job_description: str,
    resume_md: str,
    *,
    cfg: BlastJobConfig,
    cost_tracker: CostTracker,
    cache_dir: Path | None = None,
    on_text: Callable[[str], None] | None = None,
) -> list[CoverageGap]:
    """Run (or reuse a cached) `analyze_coverage`, then join it against
    `resume_md` with `unused_support` to find the third-bucket gaps.

    Fail-soft: ANY exception here — a malformed LLM response, a missing
    work_history.md, a disk error writing the cache — returns `[]` rather
    than propagating, because this is an advisory check layered onto a
    build that must still complete without it (see module docstring).
    """
    try:
        resolved_cache_dir = (
            cache_dir if cache_dir is not None else cfg_mod.data_dir(cfg) / "coverage_cache"
        )
        cache_path = _cache_path(resolved_cache_dir, job_description, _read_work_history(cfg))

        report = _load_cached_report(cache_path)
        if report is None:
            report = await analyze_coverage(job_description, cfg, cost_tracker, on_text=on_text)
            _store_cached_report(cache_path, report)

        return unused_support(report, resume_md)
    except Exception:  # noqa: BLE001 — a missing advisory must never cost a packet
        return []


def render_gaps_md(gaps: list[CoverageGap]) -> str:
    """A sidecar a human reads: each requirement, next to the candidate's own
    history sentence that would answer it, quoted verbatim.

    `gap.history_quote` is reproduced exactly as `unused_support` returned
    it — no summarizing, no rewording, no truncation. See the module
    docstring for why that discipline is non-negotiable here.
    """
    if not gaps:
        return (
            "# Coverage Gaps\n\n"
            "No gaps found — every requirement the work history could prove "
            "is already used somewhere in the resume.\n"
        )

    lines = [
        "# Coverage Gaps",
        "",
        "Requirements this job description asks for that `work_history.md` "
        "can prove the candidate meets, but that the generated resume did not use. "
        "Each quote below is the candidate's own words, copied verbatim from their work "
        "history — nothing here was written or reworded by a model. Insert a "
        "gap into the resume only by hand, using the quote as-is.",
        "",
    ]
    for gap in gaps:
        lines.append(f"## {gap.requirement}")
        lines.append("")
        lines.append(f"**Priority:** {gap.priority}")
        lines.append("")
        lines.append("**The candidate's own words (work_history.md, verbatim):**")
        lines.append("")
        lines.append(f"> {gap.history_quote}")
        lines.append("")

    return "\n".join(lines)

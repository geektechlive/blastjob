"""Offline end-to-end regression for a settings migration: does a database
copy, configured ONLY by its own settings table, still match, prompt and
discover the way the pre-migration baseline did?

    BLASTJOB_DATA_DIR=<scratch dir> uv run python scripts/regression_e2e.py \\
        <db_path> --baseline <golden-before-full.json> [--sample 8]

Never touches the input file: it is copied (SQLite online backup) into a temp
dir and everything runs on the copy. No network and no real LLM: the scorer's
`make_stream` is replaced by a fake that records the system prompt and answers
alignment 80 for every job. Checks:

1. the candidate profile is configured (matching would otherwise be skipped);
2. the real `run_matching` scores the sampled jobs (not skipped, no
   "no candidate profile" note) and its system prompt carries the profile;
3. the tailoring system prompts (resume, ATS resume, cover letter) carry it;
4. discovery adapters, company-key aliases and former-employer matches equal
   the baseline's (scripts/golden_matching.py --full).

Exit 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import golden_matching as golden  # noqa: E402

from blastjob import config as cfg_mod  # noqa: E402
from blastjob.llm import prompts as llm_prompts  # noqa: E402
from blastjob.llm.cost import CostTracker  # noqa: E402
from blastjob.matching import pipeline  # noqa: E402
from blastjob.models.config import DATA_DIR_ENV, DEFAULT_DATA_DIR, BlastJobConfig  # noqa: E402

_JOB_ID = re.compile(r"<JOB id=(\d+)>")
FAKE_ALIGNMENT = 80


@dataclass
class Report:
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, ok, detail))

    @property
    def ok(self) -> bool:
        return all(ok for _n, ok, _d in self.checks)

    def text(self) -> str:
        return "\n".join(
            f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else "")
            for name, ok, detail in self.checks
        )


def refuse_unsafe(db_path: Path) -> str | None:
    """The reason to refuse, or None. Never run against the default data dir."""
    real = Path(os.path.expanduser(DEFAULT_DATA_DIR)).resolve()
    raw = os.environ.get(DATA_DIR_ENV, "").strip()
    if not raw:
        return f"set {DATA_DIR_ENV} to a scratch directory first"
    if Path(raw).expanduser().resolve().is_relative_to(real):
        return f"{DATA_DIR_ENV} points into {real}; use a scratch directory"
    if db_path.resolve().is_relative_to(real):
        return f"{db_path} is inside {real}; pass a COPY"
    return None


def copy_db(src: Path, dest_dir: Path) -> Path:
    dest = dest_dir / cfg_mod.DB_NAME
    with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as s, sqlite3.connect(dest) as d:
        s.backup(d)
    return dest


class _Final:
    class usage:  # noqa: N801 -- shape of an SDK usage object
        input_tokens = output_tokens = 0
        cache_creation_input_tokens = cache_read_input_tokens = 0


class _FakeStream:
    def __init__(self, text: str):
        self._text = text

    async def __aenter__(self) -> _FakeStream:
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False

    @property
    def text_stream(self) -> Any:
        async def gen() -> Any:
            yield self._text

        return gen()

    async def get_final_message(self) -> type[_Final]:
        return _Final


def fake_make_stream(systems: list[str]) -> Callable[..., Any]:
    async def make_stream(cfg: Any, system: Any, messages: Any, **_kw: Any) -> _FakeStream:
        systems.append("".join(b.get("text", "") for b in system))
        text = "".join(b.get("text", "") for b in messages[0]["content"])
        answers = [
            {
                "job_id": int(j),
                "alignment": FAKE_ALIGNMENT,
                "rationale": "regression stub",
                "strengths": [],
                "gaps": [],
                "comp_min": 0,
                "comp_max": 0,
            }
            for j in _JOB_ID.findall(text)
        ]
        return _FakeStream(json.dumps(answers))

    return make_stream


def _sample(conn: sqlite3.Connection, cfg: BlastJobConfig, n: int) -> list[int]:
    rows = pipeline._load_candidates(conn, only_unscored=False)
    picked: list[int] = []
    for r in rows:
        status, _d, passes, _c = golden._geo(r, cfg)
        if (passes or status == "unknown") and golden._scope(r, cfg)[0] and (r["raw_jd"] or ""):
            picked.append(int(r["id"]))
        if len(picked) >= n:
            break
    return picked


def check_matching(cfg: BlastJobConfig, db: Path, n: int, report: Report) -> None:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    systems: list[str] = []
    try:
        ids = _sample(conn, cfg, n)
        report.add("sampled jobs to score", bool(ids), f"{len(ids)} job(s)")
        with (
            mock.patch("blastjob.matching.scorer.make_stream", fake_make_stream(systems)),
            mock.patch.object(pipeline, "load_prior_scores", lambda _c, _fp: {}),
        ):
            res = asyncio.run(pipeline.run_matching(cfg, conn, CostTracker(), job_ids=ids))
    finally:
        conn.close()
    profile = cfg.candidate.profile_text()
    skipped = pipeline.NO_CANDIDATE_PROFILE_NOTE in res.notes
    report.add("matching not skipped", not skipped and res.scored > 0, f"scored {res.scored}")
    report.add(
        "scoring prompt carries the profile",
        bool(systems) and all(profile in s for s in systems),
        f"{len(systems)} prompt(s)",
    )


def check_tailoring(cfg: BlastJobConfig, report: Report) -> None:
    profile = cfg.candidate.profile_text()
    for name in ("RESUME_SYSTEM", "RESUME_ATS_SYSTEM", "COVER_LETTER_SYSTEM"):
        system = llm_prompts.with_profile(getattr(llm_prompts, name), cfg.candidate)
        report.add(f"{name} carries the profile", profile in system)


def check_baseline(cfg: BlastJobConfig, db: Path, baseline: dict, report: Report) -> None:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        companies = golden._companies(conn)
    finally:
        conn.close()
    current = {
        "adapters": golden._adapters(cfg),
        "aliases": {k: golden.company_key(k) for k in golden._alias_inputs(cfg)},
        "alumni": {
            "checked": len(companies),
            "matches": [c for c in companies if golden._is_alumni(c, cfg)],
        },
    }
    for section, value in current.items():
        same = json.loads(json.dumps(value, default=str)) == baseline.get(section)
        report.add(f"{section} equal the baseline", same)


def run(db_path: Path, baseline: dict, sample: int = 8) -> Report:
    report = Report()
    with tempfile.TemporaryDirectory(prefix="blastjob-e2e-") as tmp:
        work = Path(tmp)
        db = copy_db(db_path, work)
        cfg = cfg_mod.apply_runtime_settings(cfg_mod.load_config_from_db(db))
        cfg.paths.data_dir = str(work)
        (work / "work_history.md").write_text("# Work history (regression stub)\n")
        report.add("candidate profile configured", cfg.candidate.is_configured())
        check_baseline(cfg, db, baseline, report)
        check_tailoring(cfg, report)
        if cfg.candidate.is_configured():
            check_matching(cfg, db, sample, report)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("db_path", type=Path)
    ap.add_argument("--baseline", type=Path, required=True)
    ap.add_argument("--sample", type=int, default=8)
    args = ap.parse_args(argv)
    if reason := refuse_unsafe(args.db_path):
        print(f"refusing: {reason}", file=sys.stderr)
        return 2
    report = run(args.db_path, json.loads(args.baseline.read_text()), args.sample)
    print(report.text())
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())

"""Score fingerprint: which inputs a stored alignment score was computed from (#29).

Every score write stamps `jobs.score_model_version` with this fingerprint, and
`pipeline.load_prior_scores` replays a stored score only when its fingerprint
equals the current one. Without it, the Re-score button replayed every unchanged
posting's old score after a profile, work-history or rubric edit, so no band
ever moved.

Inputs (each verified to reach the model):
  * `SCORER_SCHEMA_VERSION`, a manual constant;
  * `scorer.scoring_system_prompt(cfg)`, the exact system prompt text (ALIGN_SYSTEM,
    the seniority weighting, the candidate profile, the determinism rules);
  * `cfg.candidate.profile_text()`, already inside that prompt, listed on its own
    as the decided design asks so a future prompt refactor cannot drop it;
  * the `work_history.md` text passed to the scorer;
  * provider + configured model (`providers.active_model`).

Deliberately EXCLUDED:
  * the archive-reason calibration block (`pipeline.load_rejections`): it changes
    on every level/domain archive, so including it would invalidate every score
    after each archive;
  * batch peers (the other jobs sharing a prompt);
  * the claude-cli backend: it runs with no explicit model, so a change of the
    model behind `claude -p` is invisible here. Accepted.

Only the cockpit's Re-score changed button re-scores old-fingerprint rows across the backlog
(job_ids-scoped passes, e.g. a pasted JD or a manual job, redo just their rows).
The daily run and Find + Score keep `only_unscored` and never treat an old
fingerprint as unscored (a full re-score of a live backlog is ~2 s/job on
claude-cli). An empty `score_model_version` is a
score from before this existed: old, not unknown.
"""

from __future__ import annotations

import hashlib
import json

from blastjob import config as cfg_mod
from blastjob import secrets as secrets_mod
from blastjob.llm.providers import active_model
from blastjob.matching import scorer
from blastjob.models.config import BlastJobConfig

# Bump by hand when anything else that shapes a score changes outside the system
# prompt: the user-content template (`prompts.build_user_content`), JD excerpting
# (`scorer.select_jd_excerpt`, `JD_HEAD_CHARS`, `DEFAULT_JD_CHAR_CAP`), the score
# schema or its normalization. A bump makes every stored score old.
SCORER_SCHEMA_VERSION = "1"

FINGERPRINT_CHARS = 16


def scoring_fingerprint(cfg: BlastJobConfig, work_history_md: str) -> str:
    """Short hash of every input a score depends on (see the module docstring)."""
    # provider=auto reads API keys from os.environ, and the cockpit loads the
    # secrets file lazily (discovery, research, people search). Load it first,
    # so one process never stamps "claude-cli" before that load and
    # "anthropic / <model>" after it, which would make every earlier score old.
    secrets_mod.load_secrets_env(cfg_mod.secrets_path(cfg))
    parts = [
        SCORER_SCHEMA_VERSION,
        scorer.scoring_system_prompt(cfg),
        cfg.candidate.profile_text(),
        work_history_md,
        active_model(cfg),
    ]
    blob = json.dumps(parts, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:FINGERPRINT_CHARS]

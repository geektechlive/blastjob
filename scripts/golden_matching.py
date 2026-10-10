"""Golden comparison for refactors of the matching path: geo, scope and gate
verdicts for every job in a database, with no LLM and no network.

    uv run python scripts/golden_matching.py <db_path> <out.json> [--full] [--config <toml>]
    uv run python scripts/golden_matching.py --diff before.json after.json [--normalize-scope]

Run it against a COPY of a real database with the code before and after a
change; the diff must be empty (or every line explained). Signature changes in
the matching functions are absorbed by the small shims below, so the same
script runs unchanged on old and new code.

`--full` adds what the per-job verdicts cannot see: the discovery adapters
built from the config (type + board/query settings, never a secret value),
`company_key` for every alias input, `is_agency` and the clustering map
placement per job, and which companies in the database count as a former
employer.

Config: the database's own `settings` table when the code supports it, else
the legacy config file. `--config <toml>` layers a TOML file over that,
in memory only, for debugging; nothing is written anywhere.
"""

from __future__ import annotations

import inspect
import json
import os
import sqlite3
import sys
import tomllib
from datetime import date
from pathlib import Path
from typing import Any

from blastjob import config as cfg_mod
from blastjob.core import agencies, company, network
from blastjob.core.company import company_key
from blastjob.matching import gate, geo, prefilter
from blastjob.models.config import BlastJobConfig

# Two fixed dates so the weekly search-firm adapter is captured on its day
# and absent on another; 2026-10-05 is a Monday, 2026-10-06 a Tuesday.
_ADAPTER_DATES = (date(2026, 10, 5), date(2026, 10, 6))

# Adapter attributes that describe WHAT is searched. Anything else (users,
# passwords, API keys, known-id sets, transports) is never written out.
_ADAPTER_ATTRS = (
    "source",
    "company",
    "token",
    "queries",
    "where",
    "distance_km",
    "salary_min",
    "app_id_env",
    "app_key_env",
    "results_per_page",
    "max_days_old",
    "timeout_s",
    "max_retries",
    "locations",
    "max_reads",
    "max_pages",
    "page_size",
    "culture",
    "regions",
    "host",
    "mailbox",
    "lookback_days",
    "dead_threshold",
    "snapshot",
)


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _load_cfg(db_path: str, overlay: str | None) -> BlastJobConfig:
    loader = getattr(cfg_mod, "load_config_from_db", None)
    cfg = loader(Path(db_path)) if loader is not None else cfg_mod.load_config()
    if overlay:
        with open(overlay, "rb") as f:
            raw = _merge(cfg.model_dump(exclude={"anthropic"}), tomllib.load(f))
        cfg = BlastJobConfig.model_validate(raw)
    install = getattr(cfg_mod, "apply_runtime_settings", None)
    if install is not None:
        install(cfg)
    return cfg


def _geo(r: sqlite3.Row, cfg: Any) -> tuple[str, float | None, bool, bool]:
    status, dist, passes, conditional = geo.assess_geo(
        remote=bool(r["geo_remote"]),
        city=r["geo_city"] or "",
        region=r["geo_region"] or "",
        location_raw=r["geo_location_raw"] or "",
        cfg=cfg.discovery,
    )
    return str(status), (round(dist, 1) if dist is not None else None), passes, conditional


def _scope(r: sqlite3.Row, cfg: Any) -> tuple[bool, str]:
    params = inspect.signature(prefilter.classify_scope).parameters
    args: list[Any] = [r["title"] or "", r["raw_jd"] or ""]
    if "keep_terms" in params:
        args += [cfg.discovery.title_keep_terms, cfg.discovery.title_drop_terms]
    return prefilter.classify_scope(*args)


def _is_alumni(name: str, cfg: Any) -> bool:
    if "candidate" in inspect.signature(network.is_alumni_company).parameters:
        return network.is_alumni_company(name, cfg.candidate)
    return network.is_alumni_company(name)


def _place(r: sqlite3.Row, cfg: Any) -> list[Any]:
    """Where same-role clustering and closure put a posting on the map."""
    from blastjob.core import role_clusters

    params = list(inspect.signature(role_clusters.place_of).parameters)
    p = role_clusters.place_of(r, cfg.discovery) if len(params) > 1 else role_clusters.place_of(r)
    return [p.remote, p.city, list(p.centroid) if p.centroid else None]


def _alias_inputs(cfg: Any) -> list[str]:
    configured = getattr(cfg.discovery, "company_aliases", None)
    if configured is not None:
        return sorted(company._base_company_key(k) for k in configured)
    return sorted(company._COMPANY_ALIASES)


def _jobs(conn: sqlite3.Connection, cfg: Any, full: bool) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in conn.execute("SELECT * FROM jobs ORDER BY id"):
        status, dist, passes, conditional = _geo(r, cfg)
        keep, reason = _scope(r, cfg)
        verdict = None
        if r["alignment_score"] is not None:
            outcome = gate.decide(
                alignment=int(r["alignment_score"]),
                comp_provenance=r["comp_provenance"] or "",
                comp_max=int(r["comp_max"] or 0),
                geo_status=geo.GeoStatus(status) if hasattr(geo, "GeoStatus") else status,
                geo_passes=passes,
                cfg=cfg.discovery,
                geo_conditional=conditional,
            )
            verdict = [str(outcome.review_state), outcome.reason]
        row: dict[str, Any] = {
            "geo": [status, dist, passes, conditional],
            "scope": [keep, reason],
            "gate": verdict,
            "company_key": company_key(r["company"] or ""),
        }
        if full:
            row["is_agency"] = agencies.is_agency(r["company"] or "")
            row["place"] = _place(r, cfg)
        out[str(r["id"])] = row
    return out


def _adapter_view(a: Any) -> dict[str, Any]:
    view: dict[str, Any] = {"class": type(a).__name__}
    for name in _ADAPTER_ATTRS:
        if hasattr(a, name):
            value = getattr(a, name)
            view[name] = list(value) if isinstance(value, (list, tuple)) else value
    firms = getattr(a, "firms", None)
    if firms is not None:
        view["firms"] = [f.model_dump() for f in firms]
    return view


def _adapters(cfg: Any) -> dict[str, list[dict]]:
    from blastjob.discovery.pipeline import build_adapters

    d = cfg.discovery
    for env in (d.mail_user_env, d.mail_password_env, "JINA_API_KEY"):
        os.environ.pop(env, None)
    return {
        day.isoformat(): [_adapter_view(a) for a in build_adapters(cfg, None, today=day)]
        for day in _ADAPTER_DATES
    }


def _companies(conn: sqlite3.Connection) -> list[str]:
    names: set[str] = set()
    for table in ("jobs", "network_contacts", "company_profiles", "company_follows"):
        try:
            rows = conn.execute(f"SELECT DISTINCT company FROM {table}").fetchall()
        except sqlite3.OperationalError:
            continue
        names.update(r[0] for r in rows if (r[0] or "").strip())
    return sorted(names)


def snapshot(db_path: str, *, full: bool = False, overlay: str | None = None) -> dict:
    cfg = _load_cfg(db_path, overlay)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    jobs = _jobs(conn, cfg, full)
    if not full:
        return jobs
    companies = _companies(conn)
    return {
        "jobs": jobs,
        "adapters": _adapters(cfg),
        "aliases": {k: company_key(k) for k in _alias_inputs(cfg)},
        "alumni": {
            "checked": len(companies),
            "matches": [c for c in companies if _is_alumni(c, cfg)],
        },
    }


def _norm_scope(row: dict | None) -> dict | None:
    """Scope as (keep, quoted matched terms) -- the reason WORDING may change."""
    if row is None:
        return None
    keep, reason = row["scope"]
    terms = [p for i, p in enumerate(reason.split("'")) if i % 2 == 1]
    return {**row, "scope": [keep, terms]}


def _diff_jobs(before: dict, after: dict, normalize: bool) -> int:
    changed = 0
    for jid in sorted(set(before) | set(after), key=int):
        b, a = before.get(jid), after.get(jid)
        if normalize:
            b, a = _norm_scope(b), _norm_scope(a)
        if b != a:
            changed += 1
            print(f"#{jid}\n  before: {b}\n  after:  {a}")
    print(f"{changed} of {len(before)} jobs differ")
    return changed


def diff(before_path: str, after_path: str, normalize: bool = False) -> int:
    before = json.load(open(before_path))
    after = json.load(open(after_path))
    if "jobs" not in before:
        after_jobs = after.get("jobs", after)
        trimmed = {
            k: {f: v[f] for f in ("geo", "scope", "gate", "company_key")}
            for k, v in after_jobs.items()
        }
        return 1 if _diff_jobs(before, trimmed, normalize) else 0
    changed = _diff_jobs(before["jobs"], after["jobs"], normalize)
    for section in ("adapters", "aliases", "alumni"):
        if before.get(section) != after.get(section):
            changed += 1
            print(
                f"[{section}] differs\n  before: {before.get(section)}\n"
                f"  after:  {after.get(section)}"
            )
        else:
            print(f"[{section}] identical")
    return 1 if changed else 0


def _flag(args: list[str], name: str) -> str | None:
    if name not in args:
        return None
    return args[args.index(name) + 1]


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv[0] == "--diff":
        sys.exit(diff(argv[1], argv[2], "--normalize-scope" in argv))
    snap = snapshot(argv[0], full="--full" in argv, overlay=_flag(argv, "--config"))
    json.dump(snap, open(argv[1], "w"), indent=1, sort_keys=True, default=str)
    print(f"{len(snap.get('jobs', snap))} jobs written to {argv[1]}")

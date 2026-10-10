"""The /settings form <-> `BlastJobConfig`.

The form is split into named sections and posts the list it carries in
`_sections`; only those sections are applied, so a submission that does not
carry a section (an older page, a test, the mail form) never resets it -- an
unchecked checkbox is only "off" inside a section that was submitted. A post
with no `_sections` at all is the original single block ("core").

List fields are one entry per line. Towns are `town, lat, lon` lines and
boards `company, ats, token` lines; a malformed line is an error naming the
line, never silently dropped. The result is re-validated through the model,
so the same bounds as `blastjob setup set` apply.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pydantic import ValidationError

from blastjob.models.config import BlastJobConfig, TargetBoard

SECTIONS = ("core", "profile", "location", "search", "service")
Form = Mapping[str, str]


class FormError(ValueError):
    pass


def lines(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def towns_text(towns: Mapping[str, list[float]]) -> str:
    return "\n".join(f"{name}, {lat}, {lon}" for name, (lat, lon) in towns.items())


def boards_text(boards: list[TargetBoard]) -> str:
    return "\n".join(f"{b.company}, {b.ats}, {b.token}" for b in boards)


def parse_towns(text: str) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for n, line in enumerate(lines(text), 1):
        parts = [p.strip() for p in line.rsplit(",", 2)]
        try:
            name, lat, lon = parts[0].lower(), float(parts[1]), float(parts[2])
        except (IndexError, ValueError) as e:
            raise FormError(f"towns line {n}: expected 'town, lat, lon'") from e
        if not name:
            raise FormError(f"towns line {n}: the town name is empty")
        out[name] = [lat, lon]
    return out


def parse_boards(text: str) -> list[TargetBoard]:
    out = []
    for n, line in enumerate(lines(text), 1):
        parts = [p.strip() for p in line.rsplit(",", 2)]
        if len(parts) != 3 or not all(parts):
            raise FormError(f"boards line {n}: expected 'company, ats, token'")
        try:
            out.append(TargetBoard(company=parts[0], ats=parts[1], token=parts[2]))
        except ValidationError as e:
            raise FormError(f"boards line {n}: {e.errors()[0]['msg']}") from e
    return out


def _num(form: Form, key: str, kind: Callable[[str], Any]) -> Any:
    try:
        return kind(form[key])
    except (KeyError, ValueError) as e:
        raise FormError(f"{key}: expected a number") from e


def _core(cfg: BlastJobConfig, form: Form) -> None:
    d = cfg.discovery
    d.comp_floor_usd = _num(form, "comp_floor_usd", int)
    d.alignment_threshold = _num(form, "alignment_threshold", int)
    d.commute_radius_miles = _num(form, "commute_radius_miles", float)
    d.remote_ok = "remote_ok" in form
    d.enable_aggregators = "enable_aggregators" in form
    d.exclude_companies = [
        x.strip() for x in form.get("exclude_companies", "").split(",") if x.strip()
    ]


def _profile(cfg: BlastJobConfig, form: Form) -> None:
    c = cfg.candidate
    c.target_titles = lines(form.get("target_titles", ""))
    c.seniority = form.get("seniority", "").strip()
    c.field = form.get("field", "").strip()
    c.former_employers = lines(form.get("former_employers", ""))


def _location(cfg: BlastJobConfig, form: Form) -> None:
    d = cfg.discovery
    d.home_location = form.get("home_location", "").strip()
    d.home_state = form.get("home_state", "").strip().upper()
    d.home_lat = _num(form, "home_lat", float)
    d.home_lon = _num(form, "home_lon", float)
    d.extended_radius_miles = _num(form, "extended_radius_miles", float)
    d.towns = parse_towns(form.get("towns", ""))


def _search(cfg: BlastJobConfig, form: Form) -> None:
    d = cfg.discovery
    d.target_boards = parse_boards(form.get("target_boards", ""))
    d.adzuna_queries = lines(form.get("adzuna_queries", ""))
    d.adzuna_where = form.get("adzuna_where", "").strip()
    d.title_keep_terms = lines(form.get("title_keep_terms", ""))
    d.title_drop_terms = lines(form.get("title_drop_terms", ""))
    d.search_firm_queries = lines(form.get("search_firm_queries", ""))
    d.search_firm_regions = lines(form.get("search_firm_regions", ""))
    d.apple_queries = lines(form.get("apple_queries", ""))


def _service(cfg: BlastJobConfig, form: Form) -> None:
    cfg.notify.mode = form.get("notify_mode", cfg.notify.mode)  # type: ignore[assignment]
    cfg.web.host = form.get("web_host", "").strip()
    cfg.web.port = _num(form, "web_port", int)


_APPLY: dict[str, Callable[[BlastJobConfig, Form], None]] = {
    "core": _core,
    "profile": _profile,
    "location": _location,
    "search": _search,
    "service": _service,
}


def submitted_sections(form: Form) -> list[str]:
    raw = form.get("_sections")
    if raw is None:
        return ["core"]
    return [s for s in (p.strip() for p in raw.split(",")) if s in _APPLY]


def apply_form(cfg: BlastJobConfig, form: Form) -> BlastJobConfig:
    """A NEW config: `cfg` with the submitted sections applied and the whole
    thing re-validated. Raises FormError with a message naming the field."""
    draft = cfg.model_copy(deep=True)
    for section in submitted_sections(form):
        _APPLY[section](draft, form)
    try:
        out = BlastJobConfig.model_validate(draft.model_dump(exclude={"anthropic"}))
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        raise FormError(f"{where}: {first['msg']}") from e
    return out.with_revision(cfg.settings_revision)

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

from blastjob.core import employer_domains
from blastjob.core.company import company_key
from blastjob.models.config import (
    CALLS_PER_DAY_MAX,
    CALLS_PER_DAY_MIN,
    CONCURRENCY_MAX,
    CONCURRENCY_MIN,
    DAILY_DEADLINE_MAX_MINUTES,
    DAILY_DEADLINE_MIN_MINUTES,
    QUIET_AFTER_DAYS_MAX,
    QUIET_AFTER_DAYS_MIN,
    RECRUITER_LOOKBACK_DAYS_MAX,
    RECRUITER_LOOKBACK_DAYS_MIN,
    RECRUITER_TOUCH_DAYS_MAX,
    RECRUITER_TOUCH_DAYS_MIN,
    SDK_TIMEOUT_MAX,
    SDK_TIMEOUT_MIN,
    BlastJobConfig,
    LimitsConfig,
    NotifyConfig,
    TargetBoard,
)

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


def employer_domains_text(domains: Mapping[str, list[str]]) -> str:
    return "\n".join(", ".join([name, *hosts]) for name, hosts in domains.items())


def parse_employer_domains(text: str) -> dict[str, list[str]]:
    """'company, domain[, domain...]' per line. Bare hostnames only (#23)."""
    out: dict[str, list[str]] = {}
    for n, line in enumerate(lines(text), 1):
        name, *hosts = [p.strip() for p in line.split(",")]
        hosts = [h for h in hosts if h]
        if not name or not hosts:
            raise FormError(f"employer domains line {n}: expected 'company, domain[, domain...]'")
        try:
            out[name] = [employer_domains.normalize_domain(h) for h in hosts]
        except ValueError as e:
            raise FormError(f"employer domains line {n}: {e}") from e
    return out


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


def _board_identity(b: TargetBoard) -> tuple[str, str, str]:
    return company_key(b.company), b.ats.strip().lower(), b.token.strip().lower()


def parse_boards(text: str, existing: list[TargetBoard] | None = None) -> list[TargetBoard]:
    """Boards from 'company, ats, token' lines. A line that is still the same board (same
    company, ats and token) keeps the identity basis it was saved with (#90): the text form
    does not show it, so rebuilding from text alone would erase it on every settings save."""
    bases = {_board_identity(b): b.identity_basis for b in existing or []}
    out = []
    for n, line in enumerate(lines(text), 1):
        parts = [p.strip() for p in line.rsplit(",", 2)]
        if len(parts) != 3 or not all(parts):
            raise FormError(f"boards line {n}: expected 'company, ats, token'")
        try:
            board = TargetBoard(company=parts[0], ats=parts[1], token=parts[2])
            out.append(
                board.model_copy(update={"identity_basis": bases.get(_board_identity(board), "")})
            )
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
    d.target_boards = parse_boards(form.get("target_boards", ""), d.target_boards)
    d.employer_domains = parse_employer_domains(form.get("employer_domains", ""))
    d.adzuna_queries = lines(form.get("adzuna_queries", ""))
    d.adzuna_where = form.get("adzuna_where", "").strip()
    d.title_keep_terms = lines(form.get("title_keep_terms", ""))
    d.title_drop_terms = lines(form.get("title_drop_terms", ""))
    d.search_firm_queries = lines(form.get("search_firm_queries", ""))
    d.search_firm_regions = lines(form.get("search_firm_regions", ""))
    d.apple_queries = lines(form.get("apple_queries", ""))


def _service(cfg: BlastJobConfig, form: Form) -> None:
    cfg.notify.mode = form.get("notify_mode", cfg.notify.mode)  # type: ignore[assignment]
    _notify_cadence(cfg, form)
    cfg.web.host = form.get("web_host", "").strip()
    cfg.web.port = _num(form, "web_port", int)
    cfg.web.public_url = form.get("web_public_url", cfg.web.public_url).strip()
    cfg.limits = _limits(cfg.limits, form)
    cfg.notify = _notify_recruiter(cfg.notify, form)


# (NotifyConfig field, label, min, max). Applied only when submitted, each reporting its own error.
_RECRUITER_FIELDS = (
    (
        "recruiter_touch_days",
        "Recruiter touch-again reminder",
        RECRUITER_TOUCH_DAYS_MIN,
        RECRUITER_TOUCH_DAYS_MAX,
    ),
    (
        "recruiter_lookback_days",
        "Recruiter reminder lookback",
        RECRUITER_LOOKBACK_DAYS_MIN,
        RECRUITER_LOOKBACK_DAYS_MAX,
    ),
)


def _notify_recruiter(current: NotifyConfig, form: Form) -> NotifyConfig:
    """The notify settings with each submitted `notify_<field>` recruiter number applied; a form
    that omits them keeps the stored values."""
    values = current.model_dump()
    for field, label, low, high in _RECRUITER_FIELDS:
        raw = form.get(f"notify_{field}")
        if raw is None:
            continue
        try:
            number = int(raw)
        except ValueError as e:
            raise FormError(f"{label}: whole days from {low} to {high}") from e
        if not low <= number <= high:
            raise FormError(f"{label}: whole days from {low} to {high}")
        values[field] = number
    try:
        return NotifyConfig(**values)
    except ValidationError as e:
        loc = e.errors()[0]["loc"]
        if loc and loc[0] == "pace_weekday":
            raise FormError(_PACE_ERROR) from e
        if loc and loc[0] == "quiet_after_days":
            raise FormError(_QUIET_ERROR) from e
        raise FormError(
            "Recruiter reminder lookback: at least as many days as the touch-again reminder"
        ) from e


def _notify_cadence(cfg: BlastJobConfig, form: Form) -> None:
    """The quiet-application threshold and the pace weekday; absent fields keep their value."""
    raw = form.get("notify_quiet_after_days")
    if raw is not None:
        try:
            days = int(raw)
        except ValueError as e:
            raise FormError(_QUIET_ERROR) from e
        if not QUIET_AFTER_DAYS_MIN <= days <= QUIET_AFTER_DAYS_MAX:
            raise FormError(_QUIET_ERROR)
        cfg.notify.quiet_after_days = days
    weekday = form.get("notify_pace_weekday", cfg.notify.pace_weekday)
    if weekday not in _PACE_WEEKDAYS:
        raise FormError(_PACE_ERROR)
    cfg.notify.pace_weekday = weekday  # type: ignore[assignment]


_PACE_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun", "off")
_PACE_ERROR = "Weekly pace line: choose mon to sun, or off"
_QUIET_ERROR = f"Quiet days: whole days from {QUIET_AFTER_DAYS_MIN} to {QUIET_AFTER_DAYS_MAX}"

# (LimitsConfig field, form field, label, unit, min, max). Each field reports its own error.
_LIMIT_FIELDS = (
    (
        "daily_deadline_minutes",
        "Daily run time limit",
        "minutes",
        DAILY_DEADLINE_MIN_MINUTES,
        DAILY_DEADLINE_MAX_MINUTES,
    ),
    (
        "scheduled_calls_per_day",
        "Scheduled model calls per day",
        "calls",
        CALLS_PER_DAY_MIN,
        CALLS_PER_DAY_MAX,
    ),
    (
        "clicked_calls_per_day",
        "Clicked model calls per day",
        "calls",
        CALLS_PER_DAY_MIN,
        CALLS_PER_DAY_MAX,
    ),
    ("cockpit_concurrency", "Cockpit runs at once", "runs", CONCURRENCY_MIN, CONCURRENCY_MAX),
    ("sdk_timeout_seconds", "API call timeout", "seconds", SDK_TIMEOUT_MIN, SDK_TIMEOUT_MAX),
)


def _limits(current: LimitsConfig, form: Form) -> LimitsConfig:
    """The limits with every submitted `limits_<field>` applied on top of `current`, so a form
    that carries only some of them never resets the others to their defaults."""
    values = current.model_dump()
    for field, label, unit, low, high in _LIMIT_FIELDS:
        raw = form.get(f"limits_{field}")
        if raw is None:
            continue
        try:
            values[field] = int(raw)
            LimitsConfig(**values)
        except (ValueError, ValidationError) as e:
            raise FormError(f"{label}: whole {unit} from {low} to {high}") from e
    return LimitsConfig(**values)


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

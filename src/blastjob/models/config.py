import os
from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator

from blastjob.discovery.textutil import us_state_code

DATA_DIR_ENV = "BLASTJOB_DATA_DIR"
DEFAULT_DATA_DIR = "~/.blastjob"


def _default_data_dir() -> str:
    """The data dir is the one setting that is not in the database (the database
    lives in it). `config.resolve_data_dir` is the full lookup; this default
    only honours the env var, so a bare `BlastJobConfig()` never points at a
    real home directory when `BLASTJOB_DATA_DIR` is set (as every test does)."""
    return os.environ.get(DATA_DIR_ENV, "").strip() or DEFAULT_DATA_DIR


class PathsConfig(BaseModel):
    data_dir: str = Field(default_factory=_default_data_dir)
    output_dir: str = "~/Documents/blastjob"
    # KEY=VALUE file loaded into the environment at runtime (API keys, app
    # passwords). Empty = `<data_dir>/secrets.env`. A path, never a secret.
    secrets_file: str = ""


class LLMConfig(BaseModel):
    # "auto" checks ANTHROPIC_API_KEY, then OPENAI_API_KEY, then claude-cli
    provider: Literal["auto", "anthropic", "openai", "claude-cli"] = "auto"

    # Anthropic settings
    anthropic_api_key_env: str = "ANTHROPIC_API_KEY"
    anthropic_model: str = "claude-sonnet-4-6"

    # OpenAI settings
    openai_api_key_env: str = "OPENAI_API_KEY"
    openai_model: str = "gpt-4o"


class GenerationConfig(BaseModel):
    max_web_searches: int = 3


class PricingConfig(BaseModel):
    input_per_mtok: float = 3.00
    output_per_mtok: float = 15.00
    cache_write_per_mtok: float = 3.75
    cache_read_per_mtok: float = 0.30


SUPPORTED_ATS: tuple[str, ...] = ("greenhouse", "lever", "ashby", "workday", "dayforce")


class TargetBoard(BaseModel):
    """One curated company's ATS board. `ats` selects the adapter; `token` is the
    company's board slug (e.g. Greenhouse boards-api token, Lever/Ashby handle)."""

    company: str = ""
    ats: str = ""  # one of SUPPORTED_ATS
    token: str = ""

    @field_validator("ats")
    @classmethod
    def _supported_ats(cls, ats: str) -> str:
        """An unsupported ATS would be silently skipped by discovery forever."""
        value = ats.strip().lower()
        if value and value not in SUPPORTED_ATS:
            raise ValueError(f"unsupported ats {ats!r}; supported: {', '.join(SUPPORTED_ATS)}")
        return value


class SearchFirm(BaseModel):
    """One retained executive-search firm whose public site is polled via Jina
    search (no scraping — see DiscoveryConfig.search_firms below)."""

    name: str = ""
    slug: str = ""
    domain: str = ""


def is_set(value: str) -> bool:
    """False for blanks and for config.example.toml's "<PLACEHOLDER>" values, so a
    copied-but-unfilled template counts as not configured."""
    v = value.strip()
    return bool(v) and not (v.startswith("<") and v.endswith(">"))


class CandidateConfig(BaseModel):
    """Who the job search is for. Every field starts empty: a fresh install has no
    candidate profile, and matching will not spend LLM calls until
    `target_titles` is filled in (see CUSTOMIZING.md for the setup interview)."""

    # Titles you would accept, e.g. ["Senior Data Engineer", "Data Engineering Manager"].
    target_titles: list[str] = Field(default_factory=list)
    # Free text, e.g. "individual contributor", "manager", "director", "executive".
    # Drives how the fit score weighs hands-on skills vs. scope of leadership.
    seniority: str = ""
    # Free text, e.g. "data engineering", "nursing", "corporate finance".
    field: str = ""
    # Your former employers, as written in work_history.md. A job at one is the
    # strongest warm-introduction signal the network view can show.
    former_employers: list[str] = Field(default_factory=list)

    def _real(self, values: list[str]) -> list[str]:
        return [v.strip() for v in values if is_set(v)]

    def is_configured(self) -> bool:
        return bool(self._real(self.target_titles))

    def profile_text(self) -> str:
        """One constant-per-install block for system prompts (never mixed with
        per-job data, so it does not disturb prompt caching)."""
        lines = []
        titles = self._real(self.target_titles)
        if titles:
            lines.append("Target roles: " + "; ".join(titles) + ".")
        if is_set(self.seniority):
            lines.append(f"Seniority level: {self.seniority.strip()}.")
        if is_set(self.field):
            lines.append(f"Field: {self.field.strip()}.")
        return "CANDIDATE PROFILE\n" + "\n".join(lines) if lines else ""


class DiscoveryConfig(BaseModel):
    """Job-discovery + matching settings. Editable from the web Settings page."""

    # Curated company ATS boards we poll (per-company public JSON feeds, no key).
    target_boards: list[TargetBoard] = Field(default_factory=list)

    # Query seeds for the optional Adzuna aggregator. Multiple targeted title
    # queries are merged + deduped — this catches title variants with high
    # signal, vs. one broad OR query that returns tens of thousands of
    # off-domain hits. These are SEARCH seeds, not gates: the scope prefilter and
    # the LLM still decide fit.
    # Empty by default -- a fresh install targets nothing until you add your
    # own title queries (see config.example.toml for the shape of an entry).
    adzuna_queries: list[str] = Field(default_factory=list)
    adzuna_where: str = ""

    # Never surface or draft anything toward these (current + prior employers,
    # and their recruiters). Fill in your own.
    exclude_companies: list[str] = Field(default_factory=list)

    # Compensation gate. Hard-reject only VERIFIED (posted) total comp below this;
    # unknown/estimated comp is queued for review, never silently dropped.
    # 0 disables the gate; set your own floor in config.toml or /settings.
    comp_floor_usd: int = Field(default=0, ge=0)

    # Requirements-alignment gate (0-100). UNCALIBRATED for a new profile: score
    # ~20 real postings you know you'd want and adjust so they land above it.
    alignment_threshold: int = Field(default=78, ge=0, le=100)

    # Deterministic title prefilter, run before any LLM call (matching/prefilter.py).
    # keep: titles containing any of these terms are always scored.
    # drop: job FAMILIES you never want (e.g. "sales representative" for an
    #       engineer); dropped unless the description shows your target scope.
    # Both empty = keep everything and let the fit score decide.
    title_keep_terms: list[str] = Field(default_factory=list)
    title_drop_terms: list[str] = Field(default_factory=list)

    # Geo scope (US only): within a straight-line radius of home, OR remote.
    # Keyless -- no geocode/routing API. You list the towns you'd consider in
    # `towns` (name -> [lat, lon]); haversine from home_lat/home_lon decides
    # commute vs. extended. A posting in another state/country is "far"; a
    # posting in your state but in a town you didn't list goes to the review
    # queue rather than being guessed. home_state "" = geo filter not configured:
    # every non-remote job goes to the review queue, none is rejected.
    home_location: str = ""  # display only, e.g. "Springfield, OH"
    home_state: str = ""  # two-letter US state code, e.g. "OH"
    home_lat: float = Field(default=0.0, ge=-90, le=90)
    home_lon: float = Field(default=0.0, ge=-180, le=180)
    towns: dict[str, list[float]] = Field(default_factory=dict)  # "springfield": [39.92, -83.81]
    commute_radius_miles: float = Field(default=35.0, ge=0)  # normal commute
    # Beyond the normal radius but still reachable: surfaced
    # to the review queue, flagged "OK only if hybrid <=2 days/wk — verify."
    extended_radius_miles: float = Field(default=55.0, ge=0)
    remote_ok: bool = True

    # Optional aggregator discovery (Adzuna; a SerpAPI adapter was considered and
    # never built, so its key setting is gone too). OFF by default:
    # v1 needs NO API keys — LLM runs via the Claude CLI and discovery uses public
    # ATS board JSON (Greenhouse/Lever/Ashby/Workable). Enable only to widen search
    # beyond curated target companies; keys then live in the secrets file / env.
    enable_aggregators: bool = False
    adzuna_app_id_env: str = "ADZUNA_APP_ID"
    adzuna_app_key_env: str = "ADZUNA_APP_KEY"
    provider_timeout_s: int = 15
    provider_max_retries: int = 3

    # LinkedIn / Indeed job-alert emails over IMAP (the sanctioned LinkedIn feed;
    # no scraping). The defaults assume an iCloud inbox read with an Apple
    # app-specific password; any IMAP host works (Gmail: imap.gmail.com with a
    # Google app password). Off until the password exists. Env var NAMES live
    # here; values in the secrets file (`paths.secrets_file`).
    mail_alerts_enabled: bool = False
    mail_imap_host: str = "imap.mail.me.com"
    mail_user_env: str = "ICLOUD_IMAP_USER"
    mail_password_env: str = "ICLOUD_IMAP_APP_PASSWORD"
    mail_lookback_days: int = 14
    mail_mailbox: str = "INBOX"

    # The user's PERSONAL mailbox, which may be a different account from the
    # alert one above: recruiters write to the address on the resume, while
    # LinkedIn/Indeed alerts may land elsewhere. The reply scanner reads this
    # account, so pointing it at the alert mailbox when they differ makes the
    # scanner structurally unable to see a recruiter reply. Each field is
    # blank-means-fall-back-to-the-alert-account, so an unconfigured install
    # behaves exactly as before.
    personal_mail_imap_host: str = ""
    personal_mail_user_env: str = ""
    personal_mail_password_env: str = ""
    personal_mail_mailbox: str = ""

    # Weekly retained-search-firm monitor (senior roles only). Retained searches
    # rarely hit ATS boards or aggregators — the firm's own site (or a related placement
    # notice) is often the only public trace, and it isn't scrapable in any
    # sanctioned way. Jina search (already wired for people-search) is the one
    # sanctioned path here too; the pipeline builds this adapter only on
    # `search_firm_weekday` so it isn't re-querying the same firms daily.
    # Empty by default -- see config.example.toml for the shape of a firm
    # entry: SearchFirm(name="Acme Executive Search", slug="acmesearch",
    # domain="acmesearch.example"). The adapter is only built (build_adapters)
    # when this list is non-empty, so an empty default never fires a request.
    search_firms_enabled: bool = True
    search_firm_weekday: int = 0  # Monday
    search_firms: list[SearchFirm] = Field(default_factory=list)
    search_firm_queries: list[str] = Field(default_factory=list)
    search_firm_regions: list[str] = Field(default_factory=list)
    # Place names read back out of a search-firm result as its rough location
    # (a state name, or a town assumed to be in `home_state`). Empty = use
    # `search_firm_regions`.
    search_firm_places: list[str] = Field(default_factory=list)

    # Company-name pairs no normalization can join: legal name -> brand name,
    # e.g. {"Contoso Mutual Life Insurance Company" = "Contoso Mutual"}.
    # See core/company.py; re-key stored rows with `rekey_companies` after a change.
    company_aliases: dict[str, str] = Field(default_factory=dict)
    # Staffing agencies and job-board resellers in your feed (beyond the
    # built-in platforms): their postings are never treated as the employer's.
    agency_companies: list[str] = Field(default_factory=list)
    # Extra lowercase substrings that mark a network contact's employer as an
    # agency or search firm (core/network_views.py).
    network_agency_keywords: list[str] = Field(default_factory=list)
    # Title words that make an email subject a job alert: "New <term> jobs".
    mail_alert_subject_terms: list[str] = Field(default_factory=list)

    # Apple careers site (jobs.apple.com), via the Jina reader (results are
    # client-rendered; only r.jina.ai renders real titles + detail links).
    # apple_queries empty by default -- no query means no search-link reads
    # (see discovery/apple.py's itertools.product over queries x locations),
    # so this adapter is a safe no-op until you add your own title queries.
    apple_enabled: bool = True
    apple_queries: list[str] = Field(default_factory=list)
    apple_locations: list[str] = Field(
        default_factory=lambda: ["united-states-USA", "home-office-HOME"]
    )
    apple_max_reads: int = 10

    # Posting-closure lifecycle (core/closure.py). Off = shadow mode: verdicts
    # and employer links are recorded, but no application_state ever moves
    # automatically. Manual "Posting closed"/"Still open" buttons work either way.
    closure_auto: bool = False

    # Employer full-description lookup (core/employer_jd.py). Free copies from a
    # sibling posting we already hold always run; this switch turns on the
    # request-metered network step for aggregator snippet rows. Undecided rows
    # are looked up only at or above `employer_jd_min_score`; kept rows always.
    employer_jd_lookup: bool = False
    employer_jd_min_score: int = Field(default=60, ge=0, le=100)
    # Request UNITS per daily run (each metered call weighted by its worst-case HTTP
    # fan-out, see closure_links.budgeted): about 15 board-only lookups.
    employer_jd_daily_requests: int = 300

    @field_validator("home_state")
    @classmethod
    def _home_state_is_a_code(cls, state: str) -> str:
        """Geo matches two-letter codes only: a stored "Ohio" would make every
        in-state posting read as out-of-state and be hard-rejected. A state
        name is folded to its code; anything else is refused."""
        if not is_set(state):  # blank, or config.example.toml's "<ST>": not configured
            return ""
        code = us_state_code(state)
        if not code:
            raise ValueError(
                f"home_state {state!r} is not a US state: use its two-letter code "
                "(e.g. OH) or its full name"
            )
        return code

    @field_validator("towns")
    @classmethod
    def _towns_are_coordinates(cls, towns: dict[str, list[float]]) -> dict[str, list[float]]:
        for name, point in towns.items():
            if not name.strip() or len(point) != 2:
                raise ValueError(f"town {name!r} must be [lat, lon]")
            lat, lon = point
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError(f"town {name!r} has an out-of-range coordinate")
        return towns

    @model_validator(mode="after")
    def _extended_covers_commute(self) -> "DiscoveryConfig":
        if self.extended_radius_miles < self.commute_radius_miles:
            raise ValueError("extended_radius_miles must be at least commute_radius_miles")
        return self


class WebConfig(BaseModel):
    """Where `blastjob-web` binds. Read once at startup: a change takes effect
    after the cockpit restarts. Anything but loopback exposes a tool with no
    login to the network."""

    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)


class NotifyConfig(BaseModel):
    """Where the daily digest goes. `stdout` prints it (and the run log keeps
    it); `discord` posts to a channel and needs the two env vars below."""

    mode: Literal["discord", "stdout", "off"] = "stdout"
    discord_token_env: str = "DISCORD_BOT_TOKEN"
    discord_channel_env: str = "DISCORD_CHANNEL_ID"


class BlastJobConfig(BaseModel):
    model_config = {"extra": "ignore"}

    paths: PathsConfig = Field(default_factory=PathsConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    candidate: CandidateConfig = Field(default_factory=CandidateConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)

    # The settings revision this object was loaded at (max `updated_at` in the
    # settings table), so a save can refuse to overwrite a newer edit.
    _revision: str | None = PrivateAttr(default=None)

    @property
    def settings_revision(self) -> str | None:
        return self._revision

    def with_revision(self, revision: str | None) -> "BlastJobConfig":
        self._revision = revision
        return self

    # Back-compat alias so old code still works during migration
    @property
    def anthropic(self) -> "LLMConfig":
        return self.llm

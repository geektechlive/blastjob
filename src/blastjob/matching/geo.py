"""Keyless geo filter: is a job within commuting range of home, or remote?

No maps API, and no bundled town table — the owner lists their own home state
and towns in `[discovery]` / `[discovery.towns]` (see models/config.py). A
small haversine gives distance from the configured home reference point. The
location decision is THREE-WAY, never boolean — "not obviously home" is not
the same as "somewhere else":

- remote                            -> pass (verified)
- affirmative other-state/country,
  on-site                           -> fail "far" (verified — nothing across
                                        a border is a short commute here)
- home state, town in config        -> pass/fail by radius (approximate)
- home state, town not configured   -> unknown -> review queue
- geo not configured at all         -> unknown -> review queue (never a
                                        silent reject with nothing to measure
                                        against)
- can't tell                        -> unknown -> review queue (never
                                        silently dropped)

The "home state, town not configured" case is load-bearing. Board data is
messy: Workday writes ``USA-OH-Springfield`` and ``5 Locations``, Dayforce
writes ``Fabrikam Corporate Offices (0900)``, alert emails write ``Greater
Springfield area``. Asserting "not home" on any of those hard-rejects the job
BEFORE it is ever scored, and because a rejected job keeps a NULL
alignment_score it is re-selected and re-rejected on every subsequent run — a
permanent loop. Only an affirmative other-place signal (a US state that isn't
the configured home state, or a foreign country) may reject.

Coordinates in `[discovery.towns]` are town centroids, as precise as the
owner bothers to make them — plenty for a commute-radius decision.

This logic assumes US states (full names + USPS two-letter codes); a posting
outside the US is only ever recognized through the separate country list
below, never placed by state.
"""

from __future__ import annotations

import math
import re
from collections.abc import Container, Mapping, Sequence

from blastjob.discovery.textutil import US_STATE_CODES, US_TERRITORY_CODES, us_state_code
from blastjob.models.config import DiscoveryConfig
from blastjob.models.job import GeoStatus

# Full US state (+ DC, Puerto Rico) name -> USPS code, used both to recognize
# the owner's configured home state and to recognize every OTHER state as an
# affirmative "not home" signal. Pulled from the same table the discovery
# adapters use to normalize a region string, so there is exactly one place
# that knows what a US state is spelled like.
_STATE_NAME_TO_ABBR: dict[str, str] = {**US_STATE_CODES, **US_TERRITORY_CODES}
_ALL_STATE_NAMES = frozenset(_STATE_NAME_TO_ABBR)
_ALL_STATE_ABBR = frozenset(_STATE_NAME_TO_ABBR.values())
_ABBR_TO_NAME = {v: k for k, v in _STATE_NAME_TO_ABBR.items()}

# Countries seen in this kind of board data plus the common outsourcing hubs.
# Not exhaustive by design: an unrecognised foreign town lands in UNKNOWN (one
# review-queue row) rather than risking a wrong reject.
_COUNTRIES = frozenset(
    p.strip()
    for p in """canada|mexico|united kingdom|england|scotland|ireland|northern ireland|
    germany|france|italy|spain|portugal|netherlands|belgium|luxembourg|switzerland|
    austria|poland|czechia|czech republic|slovakia|hungary|romania|bulgaria|greece|
    sweden|norway|denmark|finland|iceland|estonia|latvia|lithuania|ukraine|russia|
    turkey|israel|india|china|hong kong|taiwan|japan|south korea|korea|singapore|
    malaysia|philippines|indonesia|vietnam|thailand|australia|new zealand|brazil|
    argentina|chile|colombia|peru|panama|costa rica|south africa|egypt|morocco|nigeria|
    kenya|saudi arabia|united arab emirates|qatar""".replace("\n", "").split("|")
)

# A place name directly followed by one of these is part of a street or county
# name, not the place itself: "750 W Virginia St" is in Springfield, "Delaware
# County" is in Ohio, and "801 S Oakbrook Ave" is in a different state entirely.
_ADDRESS_WORDS = frozenset(
    "st street ave avenue rd road dr drive blvd boulevard pkwy parkway ln lane ct court "
    "way hwy highway cir circle ter terrace pl place trl trail pike plaza sq square "
    "county parish borough".split()
)

_LONGEST_STATE_PHRASE = 3  # "district of columbia", "new hampshire"
_LONGEST_COUNTRY_PHRASE = 3  # "united arab emirates"
_MAX_TOWN_PHRASE = 4  # generous cap; most configured towns are 1-2 words

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.8  # earth radius, miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return r * 2 * math.asin(math.sqrt(a))


def _tokens(text: str) -> list[str]:
    """Delimiter-agnostic split. Board data uses commas, hyphens, slashes and
    parentheses interchangeably -- `str.split()` only sees spaces, which is why
    `USA-OH-Springfield` would otherwise read as one opaque token."""
    return _TOKEN_RE.findall(text)


def _is_address_word(tokens: list[str], idx: int) -> bool:
    return idx < len(tokens) and tokens[idx].lower() in _ADDRESS_WORDS


def _find_phrase(tokens: list[str], phrases: Container[str], max_len: int) -> str | None:
    """First phrase from `phrases` appearing in `tokens`, longest match at each
    position, skipping matches that are really street or county names."""
    lowered = [t.lower() for t in tokens]
    for i in range(len(lowered)):
        for n in range(min(max_len, len(lowered) - i), 0, -1):
            candidate = " ".join(lowered[i : i + n])
            if candidate in phrases and not _is_address_word(tokens, i + n):
                return candidate
    return None


def _normalize_town_key(name: str) -> str:
    """Lowercase, punctuation-split, space-joined -- so a `[discovery.towns]`
    entry written as "Springfield" or "SPRINGFIELD" still matches; the config
    doc says "lowercase" but nothing enforces it before it reaches here."""
    return " ".join(_TOKEN_RE.findall(name.lower()))


def _normalized_towns(
    towns: Mapping[str, Sequence[float]],
) -> dict[str, Sequence[float]]:
    return {_normalize_town_key(k): v for k, v in towns.items()}


def _town_max_words(towns: Mapping[str, Sequence[float]]) -> int:
    if not towns:
        return 1
    return min(_MAX_TOWN_PHRASE, max(len(k.split()) for k in towns))


def find_town(
    city: str, region: str, location_raw: str, towns: Mapping[str, Sequence[float]]
) -> str | None:
    """The configured town (a normalized key into `towns`) found in this
    location text, if any -- independent of state/country classification.
    Used wherever two postings just need to be placed relative to EACH OTHER
    (same-role clustering), not relative to home."""
    if not towns:
        return None
    tokens = _tokens(" , ".join(p for p in (city, region, location_raw) if p.strip()))
    if not tokens:
        return None
    normalized = _normalized_towns(towns)
    return _find_phrase(tokens, normalized, _town_max_words(normalized))


def town_centroid(towns: Mapping[str, Sequence[float]], key: str) -> tuple[float, float] | None:
    """`towns[key]` (normalized the same way `find_town` normalizes its own
    keys) coerced to a `(lat, lon)` float pair, or None if the entry is
    missing or malformed (never guess at a bad config value)."""
    value = _normalized_towns(towns).get(_normalize_town_key(key))
    if not isinstance(value, Sequence) or isinstance(value, str) or len(value) != 2:
        return None
    try:
        return float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None


def _has_other_state(
    tokens: list[str], other_names: Container[str], other_abbrs: Container[str]
) -> bool:
    if _find_phrase(tokens, other_names, _LONGEST_STATE_PHRASE) is not None:
        return True
    if _find_phrase(tokens, _COUNTRIES, _LONGEST_COUNTRY_PHRASE) is not None:
        return True
    # US state abbreviations are matched UPPERCASE-ONLY on purpose -- "in",
    # "or", "me", "hi", "ok", "la" and "de" are ordinary English words, and
    # reading a lowercase one as a state turns "Openings in Technology" into
    # Indiana and hard-rejects the job forever. Do not "fix" this to be
    # case-insensitive.
    return any(t in other_abbrs and not _is_address_word(tokens, i) for i, t in enumerate(tokens))


def _is_home_state(tokens: list[str], home_abbr: str, home_name: str) -> bool:
    if home_name and _find_phrase(tokens, {home_name}, _LONGEST_STATE_PHRASE) is not None:
        return True
    # Same uppercase-only rule as the other-state abbreviations above, so a
    # home state coded "OH"/"IN"/"OR"/"ME" doesn't swallow the ordinary word.
    return bool(home_abbr) and any(
        t == home_abbr and not _is_address_word(tokens, i) for i, t in enumerate(tokens)
    )


def classify_location(
    city: str, region: str, location_raw: str, cfg: DiscoveryConfig
) -> tuple[str, str | None]:
    """Three-way classification -> ("home" | "other" | "unsure", town_key).

    Precedence matters. An explicit home-state token wins outright; only then
    may an affirmative other-state/country signal reject (and only when a home
    state is actually configured -- with no home state there is nothing to be
    "other" than); a bare town name is the weakest evidence and is consulted
    last, so "Jackson, MS" cannot be placed at a configured home town also
    named Jackson.
    """
    tokens = _tokens(" , ".join(p for p in (city, region, location_raw) if p.strip()))
    if not tokens:
        return "unsure", None

    # Fold here too: a config assembled in code skips the model validator.
    home_abbr = us_state_code(cfg.home_state) or cfg.home_state.strip().upper()
    home_name = _ABBR_TO_NAME.get(home_abbr, "")

    if (home_abbr or home_name) and _is_home_state(tokens, home_abbr, home_name):
        return "home", find_town(city, region, location_raw, cfg.towns)

    if home_abbr:
        # A home state is configured, so an affirmative mention of ANY other
        # state or country is evidence this posting is not local. With no home
        # state configured there is nothing to compare against, so this check
        # is skipped entirely -- see classify_location's module-level contract
        # and the "geo not configured" test.
        other_names = _ALL_STATE_NAMES - {home_name} if home_name else _ALL_STATE_NAMES
        other_abbrs = _ALL_STATE_ABBR - {home_abbr}
        if _has_other_state(tokens, other_names, other_abbrs):
            return "other", None

    key = find_town(city, region, location_raw, cfg.towns)
    return ("home", key) if key else ("unsure", None)


def _home_point_configured(cfg: DiscoveryConfig) -> bool:
    """(0.0, 0.0) is the field default, not a real place on Earth -- treat an
    unset home point the same as "not configured" rather than computing a
    bogus ~5,000-mile distance and hard-rejecting every placed town."""
    return cfg.home_lat != 0.0 or cfg.home_lon != 0.0


def assess_geo(
    *,
    remote: bool,
    city: str,
    region: str,
    location_raw: str,
    cfg: DiscoveryConfig,
) -> tuple[GeoStatus, float | None, bool, bool]:
    """Return (status, distance_mi, passes, conditional).

    - passes True means keep for scoring; an UNKNOWN status still passes so
      ambiguous locations reach the review queue rather than being dropped.
    - conditional True means it's beyond the normal commute radius but within
      the extended radius -- acceptable only if the role is hybrid with
      limited on-site days, so it's routed to the queue to verify.

    Callers unpack exactly four values (see web/service.py) -- keep the arity.
    """
    if remote and cfg.remote_ok:
        return GeoStatus.VERIFIED, None, True, False

    places = [p.strip() for p in location_raw.split(";") if p.strip()]
    if len(places) > 1:
        # "A; B; C" lists several places the job is open in. Judge each on its
        # own text (a town in one segment must not borrow another segment's
        # state). Only a segment that is actually placed within range changes
        # the verdict; otherwise the whole string is judged as before, so an
        # unplaceable segment ("London", "Corporate HQ") can't turn an
        # out-of-area posting into a review item.
        best = max((_assess_place("", "", p, cfg) for p in places), key=_verdict_rank)
        if _verdict_rank(best)[0] >= _PLACED_PASS:
            return best
    return _assess_place(city, region, location_raw, cfg)


_PLACED_PASS = 2  # _verdict_rank tier of a pass with a known distance


def _verdict_rank(v: tuple[GeoStatus, float | None, bool, bool]) -> tuple[int, float]:
    """Order for picking the best of several locations: an in-radius pass,
    then a conditional pass, then an unknown, then a reject; nearer first."""
    status, dist, passes, conditional = v
    if passes and status != GeoStatus.UNKNOWN:
        return (3 if not conditional else 2, -(dist or 0.0))
    return (1 if passes else 0, 0.0)


def _assess_place(
    city: str, region: str, location_raw: str, cfg: DiscoveryConfig
) -> tuple[GeoStatus, float | None, bool, bool]:
    signal, key = classify_location(city, region, location_raw, cfg)
    if signal == "other":
        # A concrete other place: nothing across a border is a short commute.
        return GeoStatus.VERIFIED, None, False, False

    if key is not None and _home_point_configured(cfg):
        centroid = town_centroid(cfg.towns, key)
        if centroid is not None:
            d = haversine_miles(cfg.home_lat, cfg.home_lon, *centroid)
            if d <= cfg.commute_radius_miles:
                return GeoStatus.APPROXIMATE, d, True, False
            if d <= cfg.extended_radius_miles:
                return GeoStatus.APPROXIMATE, d, True, True  # verify hybrid
            return GeoStatus.APPROXIMATE, d, False, False  # too far

    # Home-state-ish but unplaceable, geo not configured, or no usable
    # location signal: don't guess -- review it.
    return GeoStatus.UNKNOWN, None, True, False


GeoVerdict = tuple[GeoStatus, float | None, bool, bool]


def _is_far(verdict: GeoVerdict) -> bool:
    """The location gate would turn this role away: a concrete other place, or beyond the
    extended radius. An unplaceable location is never far (it goes to review)."""
    status, _dist, passes, _conditional = verdict
    return not passes and status != GeoStatus.UNKNOWN


def is_geo_reject(verdict: GeoVerdict, *, dream: bool) -> bool:
    """The ONE rule for "rejected on location", shared by matching and /funnel so the funnel
    never says rejected for a role the pipeline kept. A company on the dream list is never
    rejected on location alone (#90, ADR 0056); every other gate still applies."""
    return _is_far(verdict) and not dream


def is_dream_bypass(verdict: GeoVerdict, *, dream: bool) -> bool:
    """True when the dream exception is what keeps this role: it is far, and its company is
    on the dream list. This is what `jobs.dream_far` stores."""
    return _is_far(verdict) and dream

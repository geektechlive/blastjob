"""Find YOUR personal data in files you are about to publish.

Generic: it reads the running user's own blastjob data and looks for it in a
repo's tracked files (default), in every reachable object of a repo's history
(`--git-objects <repo>`: blobs, commit and tag text, path names), or in
explicit `--paths`. Nothing is written; the database is opened read-only.

Tiers
  FAIL (high confidence; exit 1):
    - email addresses outside example.com/.org/.net and other reserved names
      (a short built-in list of job platforms' no-reply sender addresses,
      exact addresses only, is allowed: alert-mail fixtures need them)
    - US phone numbers outside 555-0100..555-0199
    - /Users/<name> paths and linkedin.com/in/<handle> URLs (handles
      `example` and `example-*` are reserved for fixtures)
    - your own name (work_history.md frontmatter `name:`, else its first
      heading) and your surname alone when it is distinctive
    - people from the `contacts` and `network_contacts` tables (2+ name
      words of 2+ letters each: initials alone match regexes and minified
      code)
    - distinctive, rare company names from `jobs` / `company_profiles`:
      after dropping legal suffixes, 2+ words that are not all generic
      business words, or one word of 6+ characters that is not a common
      English word; and named by at most 3 jobs
    - any company you kept or applied to (same distinctiveness rule, any
      job count: your application history is the personal part)
    Company names are also tried without parentheticals and without a
    trailing " - Division" segment.
  WARN (review; does not fail):
    - other company names (single common words and names under 4
      characters are skipped: they would fire on ordinary code)
    - any 8-word run shared with work_history.md

Matching is on normalized text: lowercase, punctuation and whitespace
collapsed, apostrophes dropped. Terms listed one per line in
`<repo>/local/leak_allowlist.txt` (gitignored; `#` comments) are ignored, as
are the built-in vendor/integration names below. Minified vendor files
(`*.min.js`) are not read: their content is third-party and full of
two-letter identifier runs; their path names are still scanned.

Data dir: --data-dir, else $BLASTJOB_DATA_DIR, else ~/.blastjob. With no data
there, only the generic detectors run.

    uv run python scripts/check_no_personal_data.py               # tracked files
    uv run python scripts/check_no_personal_data.py --summary     # counts + files only
    uv run python scripts/check_no_personal_data.py --git-objects /path/to/new-repo
    uv run python scripts/check_no_personal_data.py --paths README.md docs/
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

FAIL = "FAIL"
WARN = "WARN"
RARE_JOB_COUNT = 3
MIN_SINGLE_WORD_CHARS = 6
MIN_WARN_CHARS = 4
MIN_NAME_WORD_CHARS = 2
MIN_CONTACT_NAME_WORDS = 2
SHINGLE_WORDS = 8
BINARY_SNIFF_BYTES = 8192
DEFAULT_ALLOWLIST = Path("local") / "leak_allowlist.txt"
SKIP_DIRS = frozenset({".git", ".venv", "venv", "__pycache__", ".mypy_cache", ".ruff_cache"})
MINIFIED_SUFFIX = ".min.js"

# --- generic detectors -------------------------------------------------------

EMAIL_RE = re.compile(
    r"(?<![\w.%+-])([A-Za-z0-9._%+-]+)@([A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)*\.([A-Za-z]{2,}))\b"
)
PHONE_RE = re.compile(
    r"(?<![\w.-])(?:\+?1[\s.-]?)?(?:\(([2-9]\d{2})\)\s?|([2-9]\d{2})[\s.-])([2-9]\d{2})[\s.-](\d{4})(?![\w-]|\.\d)"
)
USERS_PATH_RE = re.compile(r"/Users/([A-Za-z0-9._-]+)")
LINKEDIN_RE = re.compile(r"linkedin\.com/in/([A-Za-z0-9_%-]+)", re.IGNORECASE)

ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "users.noreply.github.com")
RESERVED_TLDS = frozenset({"test", "invalid", "example", "localhost", "local"})
FILE_EXT_TLDS = frozenset({"png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js", "py", "md"})
ALLOWED_EMAILS = frozenset({"noreply@anthropic.com", "git@github.com"})
# Job platforms' own no-reply senders: alert-mail and reply-filter fixtures
# need the real From address to exercise the parser. EXACT addresses only,
# never a whole domain: any other address at these domains still fails.
PLATFORM_SENDERS = frozenset(
    {
        "alert@indeed.com",
        "jobs-noreply@linkedin.com",
        "jobalerts-noreply@linkedin.com",
        "notifications-noreply@linkedin.com",
        "messages-noreply@linkedin.com",
        "no-reply@greenhouse.io",
        "no-reply@dayforcehcm.com",
        "donotreply@myworkday.com",
    }
)
# tests/test_replies.py: a reply from a webmail domain must never match a
# company by domain, so its fixture needs a real webmail domain. This one
# generic address is the only webmail address allowed.
WEBMAIL_FIXTURES = frozenset({"person@yahoo.com"})
PLACEHOLDER_USERS = frozenset({"shared", "user", "username", "you", "yourname", "me", "runner"})
PLACEHOLDER_HANDLES = frozenset({"example", "your-handle", "yourhandle", "yourname", "handle"})
# Fixture profile URLs use linkedin.com/in/example-<anything>; the hyphen is
# required, so a real handle such as `examplecorp` still fails.
FIXTURE_HANDLE_PREFIX = "example-"
FICTIONAL_EXCHANGE = "555"
FICTIONAL_LINES = range(100, 200)


def _email_allowed(local: str, domain: str, tld: str) -> bool:
    domain = domain.lower()
    address = f"{local}@{domain}".lower()
    if address in ALLOWED_EMAILS | PLATFORM_SENDERS | WEBMAIL_FIXTURES:
        return True
    if tld.lower() in RESERVED_TLDS | FILE_EXT_TLDS:
        return True
    return any(domain == d or domain.endswith("." + d) for d in ALLOWED_EMAIL_DOMAINS)


def _handle_is_placeholder(handle: str) -> bool:
    handle = handle.lower()
    return handle in PLACEHOLDER_HANDLES or handle.startswith(FIXTURE_HANDLE_PREFIX)


def find_generic(line: str) -> list[tuple[str, str]]:
    """(kind, value) for every email/phone/home path/LinkedIn profile in `line`."""
    hits: list[tuple[str, str]] = []
    for m in EMAIL_RE.finditer(line):
        if not _email_allowed(m.group(1), m.group(2), m.group(3)):
            hits.append(("email", m.group(0)))
    for m in PHONE_RE.finditer(line):
        if m.group(3) == FICTIONAL_EXCHANGE and int(m.group(4)) in FICTIONAL_LINES:
            continue
        hits.append(("phone", m.group(0).strip()))
    for m in USERS_PATH_RE.finditer(line):
        if m.group(1).lower() not in PLACEHOLDER_USERS:
            hits.append(("home_path", m.group(0)))
    for m in LINKEDIN_RE.finditer(line):
        if not _handle_is_placeholder(m.group(1)):
            hits.append(("linkedin", m.group(0)))
    return hits


# --- normalization + word lists ----------------------------------------------

_APOSTROPHE_RE = re.compile(r"['’]")
_NON_ALNUM_RE = re.compile(r"[^0-9a-z]+")


def normalize(text: str) -> str:
    return _NON_ALNUM_RE.sub(" ", _APOSTROPHE_RE.sub("", text.lower())).strip()


def tokens(text: str) -> tuple[str, ...]:
    return tuple(normalize(text).split())


LEGAL_SUFFIXES = frozenset(
    "inc incorporated llc ltd limited corp corporation co plc lp llp pc pllc na the".split()
)
NAME_SUFFIXES = frozenset("jr sr ii iii iv phd mba cpa pmp md esq cissp pe".split())
PLACEHOLDER_COMPANIES = frozenset(
    "confidential undisclosed stealth unknown anonymous company client employer".split()
)
GENERIC_BUSINESS_WORDS = frozenset(
    """staffing solutions services service group consulting consultants technology
    technologies tech systems partners partner company companies recruiting recruiters
    resources global international associates health healthcare financial finance bank
    banking insurance holdings industries manufacturing logistics brands management
    marketing media labs digital data software network networks security capital
    enterprise enterprises national american america united general advanced first
    community regional credit union trust mutual medical energy care home life world
    search talent agency hire hiring careers jobs job new north south east west central
    us usa info information it of and for at in on a an systems integrators""".split()
)
COMMON_WORDS = GENERIC_BUSINESS_WORDS | frozenset(
    """target apple amazon oracle indeed summit pioneer harbor liberty frontier insight
    premier alliance strategic southern northern western eastern midwest pacific atlantic
    county state city university college school hospital clinic express direct mercury
    beacon bridge anchor compass keystone horizon vision vector catalyst element
    platform portal cloud remote hybrid office manager director leader leadership
    engineer engineering analyst program project product operations support people
    culture value values impact public private office partners shield guard quality
    square circle signal anthem humana option options select choice spirit vantage
    benefit benefits better simple smart bright clear true trusted modern future
    innovation innovations creative design studio works industries market markets
    meta affirm concurrency ladders one call net work works way path point line link
    docker python kubernetes salesforce servicenow azure splunk cisco okta slack zoom
    atlassian jira notion stripe twilio datadog snowflake tableau workiva""".split()
)
# The repo's own integrations and vendors: a posting FROM one of them must
# not make the code that talks to it fail.
VENDOR_ALLOWLIST = frozenset(
    normalize(v)
    for v in (
        "Workday",
        "Greenhouse",
        "Lever",
        "Ashby",
        "Dayforce",
        "Adzuna",
        "LinkedIn",
        "Indeed",
        "Jina",
        "Jina AI",
        "Discord",
        "Anthropic",
        "OpenAI",
        "Textkernel",
        "GitHub",
        "Google",
        "Microsoft",
        "Cloudflare",
        "Buffer",
        "Korn Ferry",
        "Heidrick & Struggles",
        "Spencer Stuart",
        "Russell Reynolds",
        "Russell Reynolds Associates",
    )
)


# --- terms -------------------------------------------------------------------


@dataclass(frozen=True)
class Term:
    words: tuple[str, ...]
    tier: str
    kind: str
    display: str


def _strip_legal(words: tuple[str, ...]) -> tuple[str, ...]:
    out = list(words)
    while out and out[0] == "the":
        out.pop(0)
    while out and out[-1] in LEGAL_SUFFIXES:
        out.pop()
    return tuple(out)


def is_distinctive_company(words: tuple[str, ...]) -> bool:
    if not words or " ".join(words) in PLACEHOLDER_COMPANIES:
        return False
    if len(words) >= 2:
        return not all(w in GENERIC_BUSINESS_WORDS or w in COMMON_WORDS for w in words)
    word = words[0]
    return len(word) >= MIN_SINGLE_WORD_CHARS and word not in COMMON_WORDS


_PARENTHETICAL_RE = re.compile(r"\([^)]*\)")
_DIVISION_RE = re.compile(r"\s+[-–—|]\s+")


def company_variants(display: str) -> set[tuple[str, ...]]:
    """The name as words, minus parentheticals and legal suffixes, plus the
    part before a division delimiter ("Acme Corp - Global Division" -> acme)."""
    base = _PARENTHETICAL_RE.sub(" ", display)
    head = _DIVISION_RE.split(base, maxsplit=1)[0]
    return {w for w in (_strip_legal(tokens(base)), _strip_legal(tokens(head))) if w}


def company_terms(
    names: Counter[str], allow: frozenset[str], applied: frozenset[str] = frozenset()
) -> list[Term]:
    """`names` maps a company display name to how many jobs name it; `applied`
    holds the display names of companies with a kept or applied job."""
    by_words: dict[tuple[str, ...], tuple[str, int, bool]] = {}
    for display, count in names.items():
        for words in company_variants(display):
            if " ".join(words) in allow:
                continue
            _, prev_count, prev_applied = by_words.get(words, (display, 0, False))
            by_words[words] = (display, prev_count + count, prev_applied or display in applied)
    terms: list[Term] = []
    for words, (display, count, is_applied) in by_words.items():
        distinctive = is_distinctive_company(words)
        if is_applied and (distinctive or _worth_a_warning(words)):
            terms.append(Term(words, FAIL, "applied_company", display))
        elif distinctive and count <= RARE_JOB_COUNT:
            terms.append(Term(words, FAIL, "company", display))
        elif distinctive or _worth_a_warning(words):
            terms.append(Term(words, WARN, "company", display))
    return terms


def _worth_a_warning(words: tuple[str, ...]) -> bool:
    """A short or generic name ("Target", "Global Staffing") would fire on
    ordinary code and prose; only a non-common single word of 4+ chars warns."""
    if len(words) != 1 or words[0] in PLACEHOLDER_COMPANIES:
        return False
    return len(words[0]) >= MIN_WARN_CHARS and words[0] not in COMMON_WORDS


def _real_name_words(words: tuple[str, ...]) -> int:
    return sum(1 for w in words if w.isalpha() and len(w) >= MIN_NAME_WORD_CHARS)


def person_terms(
    names: Iterable[str], kind: str, allow: frozenset[str], min_real_words: int = 0
) -> list[Term]:
    """Each name as a term (plus first+last for 3+ words). With
    `min_real_words`, a variant needs that many words of 2+ letters: a contact
    stored as initials ("Z. W.") would otherwise fire inside regexes and
    minified code. Matching is on whole normalized tokens, so a term never
    matches inside a longer word."""
    terms: dict[tuple[str, ...], Term] = {}
    for display in names:
        words = tuple(w for w in tokens(display) if w not in NAME_SUFFIXES)
        variants = {words}
        if len(words) >= 3:
            variants.add((words[0], words[-1]))
        for v in variants:
            if len(v) < 2 or _real_name_words(v) < min_real_words:
                continue
            if " ".join(v) not in allow:
                terms.setdefault(v, Term(v, FAIL, kind, display))
    return list(terms.values())


def owner_terms(name: str, allow: frozenset[str]) -> list[Term]:
    terms = person_terms([name], "own_name", allow)
    words = tuple(w for w in tokens(name) if w not in NAME_SUFFIXES)
    surname = words[-1] if len(words) >= 2 else ""
    if len(surname) >= MIN_SINGLE_WORD_CHARS and surname not in COMMON_WORDS | allow:
        terms.append(Term((surname,), FAIL, "own_surname", name))
    return terms


# --- reading the user's data -------------------------------------------------


def resolve_data_dir(arg: str | None) -> Path:
    if arg:
        return Path(arg).expanduser()
    env = os.environ.get("BLASTJOB_DATA_DIR", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".blastjob"


def owner_name(work_history: str) -> str:
    lines = work_history.splitlines()
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            key, _, value = line.partition(":")
            if key.strip().lower() == "name" and value.strip():
                return value.strip().strip("'\"")
    for line in lines:
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}  # noqa: S608


@dataclass(frozen=True)
class UserData:
    companies: Counter[str]  # display name -> jobs naming it
    applied: frozenset[str]  # companies with a kept or applied job
    people: list[str]


_APPLIED_SQL = (
    "SELECT DISTINCT company FROM jobs WHERE review_state = 'kept'"
    " OR COALESCE(application_state, '') != '' OR COALESCE(applied_at, '') != ''"
)


def read_db(db: Path) -> UserData:
    """Company names, applied-to companies and people's names, opened read-only.
    Contact emails need no lookup: the email detector fails on any real one."""
    companies: Counter[str] = Counter()
    applied: frozenset[str] = frozenset()
    people: list[str] = []
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        job_cols = _columns(conn, "jobs")
        if "company" in job_cols:
            for name, n in conn.execute("SELECT company, COUNT(*) FROM jobs GROUP BY company"):
                if name:
                    companies[name] += n
        if {"company", "review_state", "application_state", "applied_at"} <= job_cols:
            applied = frozenset(n for (n,) in conn.execute(_APPLIED_SQL) if n)
        for col in ("company", "legal_name"):
            if col in _columns(conn, "company_profiles"):
                for (name,) in conn.execute(f"SELECT {col} FROM company_profiles"):  # noqa: S608
                    if name:
                        companies[name] += 0
        if "name" in _columns(conn, "contacts"):
            people += [n for (n,) in conn.execute("SELECT name FROM contacts") if n]
        if "full_name" in _columns(conn, "network_contacts"):
            people += [n for (n,) in conn.execute("SELECT full_name FROM network_contacts") if n]
    finally:
        conn.close()
    return UserData(companies, applied, people)


def load_allowlist(path: Path) -> frozenset[str]:
    if not path.is_file():
        return VENDOR_ALLOWLIST
    lines = path.read_text(encoding="utf-8").splitlines()
    user = {normalize(x) for x in lines if x.strip() and not x.lstrip().startswith("#")}
    return VENDOR_ALLOWLIST | frozenset(user)


@dataclass(frozen=True)
class Personal:
    terms: dict[str, list[Term]]  # first word -> terms starting with it
    literals: tuple[str, ...]  # raw, case-insensitive (a non-macOS home dir)
    shingles: frozenset[tuple[str, ...]]


def build_personal(data_dir: Path, allow: frozenset[str]) -> Personal:
    terms: list[Term] = []
    literals: set[str] = set()
    shingles: frozenset[tuple[str, ...]] = frozenset()
    wh_path = data_dir / "work_history.md"
    if wh_path.is_file():
        text = wh_path.read_text(encoding="utf-8", errors="replace")
        name = owner_name(text)
        if name:
            terms += owner_terms(name, allow)
        words = normalize(text).split()
        shingles = frozenset(
            tuple(words[i : i + SHINGLE_WORDS]) for i in range(len(words) - SHINGLE_WORDS + 1)
        )
    db = data_dir / "blastjob.db"
    if db.is_file():
        user = read_db(db)
        terms += company_terms(user.companies, allow, user.applied)
        terms += person_terms(user.people, "contact", allow, MIN_CONTACT_NAME_WORDS)
    # macOS homes are already caught by the /Users/<name> detector.
    home = str(Path.home())
    if home not in ("/", "") and not home.startswith("/Users/"):
        literals.add(home.lower())
    index: dict[str, list[Term]] = defaultdict(list)
    for t in terms:
        index[t.words[0]].append(t)
    return Personal(dict(index), tuple(sorted(literals)), shingles)


# --- scanning ----------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    source: str
    line: int
    tier: str
    kind: str
    value: str


def _term_hits(words: list[str], index: dict[str, list[Term]]) -> Iterator[Term]:
    for i, w in enumerate(words):
        for term in index.get(w, ()):
            n = len(term.words)
            if tuple(words[i : i + n]) == term.words:
                yield term


def _generic_allowed(value: str, allow: frozenset[str]) -> bool:
    return normalize(value) in allow


def scan_text(source: str, text: str, personal: Personal, allow: frozenset[str]) -> list[Finding]:
    found: dict[tuple[int, str, str], Finding] = {}

    def add(line: int, tier: str, kind: str, value: str) -> None:
        found.setdefault((line, kind, value), Finding(source, line, tier, kind, value))

    stream: list[tuple[str, int]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for kind, value in find_generic(line):
            if not _generic_allowed(value, allow):
                add(lineno, FAIL, kind, value)
        lowered = line.lower()
        for lit in personal.literals:
            if lit in lowered:
                add(lineno, FAIL, "literal", lit)
        words = normalize(line).split()
        for term in _term_hits(words, personal.terms):
            add(lineno, term.tier, term.kind, term.display)
        stream += [(w, lineno) for w in words]
    for lineno in _shingle_lines(stream, personal.shingles):
        add(lineno, WARN, "work_history_overlap", f"{SHINGLE_WORDS}+ words shared")
    return sorted(found.values(), key=lambda f: (f.line, f.tier, f.kind))


def _shingle_lines(stream: list[tuple[str, int]], shingles: frozenset[tuple[str, ...]]) -> set[int]:
    """Start line of each run of overlapping 8-word windows found in `shingles`."""
    if not shingles:
        return set()
    words = [w for w, _ in stream]
    lines: set[int] = set()
    in_run = False
    for i in range(len(words) - SHINGLE_WORDS + 1):
        hit = tuple(words[i : i + SHINGLE_WORDS]) in shingles
        if hit and not in_run:
            lines.add(stream[i][1])
        in_run = hit
    return lines


def _decode(data: bytes) -> str | None:
    if b"\0" in data[:BINARY_SNIFF_BYTES]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], input=stdin, capture_output=True, check=True
    )
    return proc.stdout


def is_minified_vendor(path: str) -> bool:
    """Minified third-party bundles (htmx.min.js): content is not scanned."""
    return path.lower().endswith(MINIFIED_SUFFIX)


def tracked_files(repo: Path) -> Iterator[tuple[str, str]]:
    for rel in _git(repo, "ls-files", "-z").decode("utf-8").split("\0"):
        path = repo / rel
        if not rel or not path.is_file():
            continue
        yield f"{rel} (path)", rel
        if is_minified_vendor(rel):
            continue
        text = _decode(path.read_bytes())
        if text is not None:
            yield rel, text


def path_files(paths: Iterable[str]) -> Iterator[tuple[str, str]]:
    for raw in paths:
        root = Path(raw)
        files = [root] if root.is_file() else sorted(root.rglob("*"))
        for path in files:
            if not path.is_file() or SKIP_DIRS & set(path.parts):
                continue
            if is_minified_vendor(path.name):
                continue
            text = _decode(path.read_bytes())
            if text is not None:
                yield str(path), text


def git_objects(repo: Path) -> Iterator[tuple[str, str]]:
    """Every object reachable from any ref: blobs, commit/tag text, path names."""
    listing = _git(repo, "rev-list", "--objects", "--all").decode("utf-8", "replace")
    names: dict[str, str] = {}
    for row in listing.splitlines():
        sha, _, path = row.partition(" ")
        names[sha] = path
        if path:
            yield f"{sha[:12]} (path)", path
    if not names:
        return
    batch = _git(repo, "cat-file", "--batch", stdin="\n".join(names).encode() + b"\n")
    pos = 0
    while pos < len(batch):
        end = batch.index(b"\n", pos)
        header = batch[pos:end].decode().split(" ")
        if len(header) != 3:  # "<sha> missing": nothing to read
            pos = end + 1
            continue
        sha, kind, size = header
        body = batch[end + 1 : end + 1 + int(size)]
        pos = end + 1 + int(size) + 1
        if kind == "tree" or is_minified_vendor(names.get(sha, "")):
            continue
        text = _decode(body)
        if text is not None:
            label = names.get(sha) or kind
            yield f"{kind} {sha[:12]} ({label})", text


# --- report ------------------------------------------------------------------


def report(findings: list[Finding], summary: bool) -> str:
    by_source: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        by_source[f.source].append(f)
    tiers = Counter(f.tier for f in findings)
    out = [f"FAIL: {tiers[FAIL]}  WARN: {tiers[WARN]}  files: {len(by_source)}"]
    for source in sorted(by_source):
        items = by_source[source]
        counts = Counter(f.tier for f in items)
        out.append(f"\n{source}  (FAIL {counts[FAIL]}, WARN {counts[WARN]})")
        if summary:
            continue
        out += [f"  L{f.line:<5} {f.tier} {f.kind}: {f.value}" for f in items]
    return "\n".join(out)


def _parse(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--git-objects", metavar="REPO", help="scan every reachable object")
    mode.add_argument("--paths", nargs="+", metavar="PATH", help="scan these files/dirs")
    p.add_argument(
        "--repo", default=".", help="your checkout: tracked files + allowlist (default .)"
    )
    p.add_argument("--data-dir", help="your blastjob data dir (else $BLASTJOB_DATA_DIR)")
    p.add_argument("--allowlist", help=f"allowlist file (default <repo>/{DEFAULT_ALLOWLIST})")
    p.add_argument("--summary", action="store_true", help="counts and file names only")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    # The allowlist always comes from --repo (your working checkout), never
    # from the scanned target: a fresh publication repo has no local/ dir.
    repo = Path(args.repo).resolve()
    allow_path = Path(args.allowlist) if args.allowlist else repo / DEFAULT_ALLOWLIST
    allow = load_allowlist(allow_path)
    personal = build_personal(resolve_data_dir(args.data_dir), allow)
    if args.paths:
        sources = path_files(args.paths)
    elif args.git_objects:
        sources = git_objects(Path(args.git_objects).resolve())
    else:
        sources = tracked_files(repo)
    findings = [f for label, text in sources for f in scan_text(label, text, personal, allow)]
    print(report(findings, args.summary))
    n_user = len(allow - VENDOR_ALLOWLIST)
    found = "" if allow_path.is_file() else ", file not found"
    print(f"\nallowlist: {allow_path} ({n_user} terms{found})")
    return 1 if any(f.tier == FAIL for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())

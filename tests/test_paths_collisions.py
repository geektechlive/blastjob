"""Collision-proofing for role-slug sidecar filenames.

`application_dir` deliberately shares one folder across every role tailored
for a company on a given day (see test_paths.py); `core/build.py` and
`core/prep.py` disambiguate the sidecars they write there with
`slugify(role)`. `slugify` is a lossy, human-readable transform: punctuation
is stripped rather than preserved, and long strings are truncated to 64
chars. Two distinct role titles can therefore land on the identical slug:

    'VP, Ops'       -> 'vp-ops'        'VP Ops'       -> 'vp-ops'         COLLIDE
    'C++ Engineer' -> 'c-engineer'   'C# Engineer' -> 'c-engineer'    COLLIDE
    two 64+ char titles differing only past char 64                  COLLIDE

Silently reusing a claimed slug lets one role's job_description/fit_score/
metadata/interview_prep overwrite another's -- a document that then reads as
fact about the wrong job. `resolve_role_slug` closes that hole: it returns
the plain slug in the common (no-collision) case, and only appends a short,
deterministic disambiguator when the plain slug is already claimed by a
*different* role.
"""

import json

from blastjob.core.paths import resolve_role_slug, slugify

# ---------------------------------------------------------------------------
# common case: no collision -> plain, human-readable slug
# ---------------------------------------------------------------------------


def test_resolve_role_slug_common_case_is_plain_and_readable(tmp_path):
    assert resolve_role_slug(tmp_path, "Vice President of Engineering") == (
        "vice-president-of-engineering"
    )


def test_resolve_role_slug_empty_dir_matches_plain_slugify(tmp_path):
    assert resolve_role_slug(tmp_path, "Acme Corp") == slugify("Acme Corp")


# ---------------------------------------------------------------------------
# re-tailoring the same role must keep returning the same slug (overwrite is
# intended there, not a collision)
# ---------------------------------------------------------------------------


def _claim(out_dir, slug: str, role: str) -> None:
    """Simulate a completed build: writes just the metadata.json marker."""
    (out_dir / f"{slug}.metadata.json").write_text(json.dumps({"role": role}), encoding="utf-8")


def test_resolve_role_slug_reuses_slug_for_same_role(tmp_path):
    _claim(tmp_path, "vp-ops", "VP, Ops")
    assert resolve_role_slug(tmp_path, "VP, Ops") == "vp-ops"


def test_resolve_role_slug_reuses_slug_across_case_and_whitespace(tmp_path):
    _claim(tmp_path, "vp-ops", "VP, Ops")
    # Same role, re-submitted with different case/spacing -> still "the same role".
    assert resolve_role_slug(tmp_path, "vp,   ops") == "vp-ops"


# ---------------------------------------------------------------------------
# genuine collisions: different roles landing on the same base slug
# ---------------------------------------------------------------------------


def test_resolve_role_slug_disambiguates_comma_vs_space_collision(tmp_path):
    assert slugify("VP, Ops") == slugify("VP Ops") == "vp-ops"
    _claim(tmp_path, "vp-ops", "VP, Ops")

    result = resolve_role_slug(tmp_path, "VP Ops")

    assert result != "vp-ops"
    assert result.startswith("vp-ops")
    # deterministic: same colliding title always resolves to the same slug
    assert resolve_role_slug(tmp_path, "VP Ops") == result
    # the original role is unaffected
    assert resolve_role_slug(tmp_path, "VP, Ops") == "vp-ops"


def test_resolve_role_slug_disambiguates_punctuation_collision(tmp_path):
    assert slugify("C++ Engineer") == slugify("C# Engineer") == "c-engineer"
    _claim(tmp_path, "c-engineer", "C++ Engineer")

    result = resolve_role_slug(tmp_path, "C# Engineer")

    assert result != "c-engineer"
    assert resolve_role_slug(tmp_path, "C++ Engineer") == "c-engineer"


def test_resolve_role_slug_disambiguates_truncation_collision(tmp_path):
    title_a = "A" * 80 + " Alpha Division Leadership Role"
    title_b = "A" * 80 + " Beta Division Leadership Role"
    assert slugify(title_a) == slugify(title_b)  # collide past char 64

    base = slugify(title_a)
    _claim(tmp_path, base, title_a)

    result = resolve_role_slug(tmp_path, title_b)

    assert result != base
    assert resolve_role_slug(tmp_path, title_a) == base


def test_resolve_role_slug_two_different_titles_get_two_different_slugs(tmp_path):
    slug_a = resolve_role_slug(tmp_path, "VP, Ops")
    _claim(tmp_path, slug_a, "VP, Ops")
    slug_b = resolve_role_slug(tmp_path, "VP Ops")
    assert slug_a != slug_b


# ---------------------------------------------------------------------------
# a claimed-but-unowned slug (e.g. a crashed build left sidecars with no
# metadata.json marker) must not be silently reused by a different title
# ---------------------------------------------------------------------------


def test_resolve_role_slug_claimed_without_metadata_is_treated_as_taken(tmp_path):
    # No metadata.json -- e.g. the build died after writing job_description.md
    # but before the metadata step.
    (tmp_path / "vp-ops.job_description.md").write_text("stub", encoding="utf-8")

    result = resolve_role_slug(tmp_path, "VP Ops")

    assert result != "vp-ops"


# ---------------------------------------------------------------------------
# prefix confusion: a longer existing slug must not make a shorter one look
# claimed
# ---------------------------------------------------------------------------


def test_resolve_role_slug_does_not_confuse_slug_prefixes(tmp_path):
    _claim(tmp_path, "vp-ops-platforms", "VP Ops Platforms")
    # "vp-ops" is a prefix of "vp-ops-platforms" but is a distinct, unclaimed slug.
    assert resolve_role_slug(tmp_path, "VP Ops") == "vp-ops"


# ---------------------------------------------------------------------------
# empty / punctuation-only titles still yield something usable
# ---------------------------------------------------------------------------


def test_resolve_role_slug_empty_title_falls_back_to_role(tmp_path):
    assert resolve_role_slug(tmp_path, "") == "role"


def test_resolve_role_slug_punctuation_only_title_falls_back_to_role(tmp_path):
    assert resolve_role_slug(tmp_path, "!!!") == "role"


def test_resolve_role_slug_disambiguates_colliding_blank_titles(tmp_path):
    _claim(tmp_path, "role", "")
    result = resolve_role_slug(tmp_path, "!!!")
    assert result != "role"
    assert result.strip("-")  # usable, non-empty


# ---------------------------------------------------------------------------
# path escaping stays sound, including on the disambiguated branch
# ---------------------------------------------------------------------------


def test_slugify_traversal_still_neutralised():
    assert slugify("../../etc/passwd") == "etcpasswd"
    assert "/" not in slugify("../../etc/passwd")
    assert ".." not in slugify("../../etc/passwd")


def test_resolve_role_slug_traversal_safe_common_case(tmp_path):
    result = resolve_role_slug(tmp_path, "../../etc/passwd")
    assert result == "etcpasswd"
    assert "/" not in result and ".." not in result


def test_resolve_role_slug_traversal_safe_when_disambiguated(tmp_path):
    assert slugify("../../etc/passwd") == slugify("..etc/passwd!!") == "etcpasswd"
    _claim(tmp_path, "etcpasswd", "../../etc/passwd")

    result = resolve_role_slug(tmp_path, "..etc/passwd!!")

    assert result != "etcpasswd"
    assert "/" not in result and ".." not in result

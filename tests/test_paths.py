from datetime import date

from blastjob.core.paths import application_dir, name_token, person_slug, slugify


def test_slugify_basic():
    assert slugify("Acme Corp") == "acme-corp"


def test_slugify_special_chars():
    assert slugify("Hello, World! 2024") == "hello-world-2024"


def test_slugify_max_length():
    long = "a" * 100
    assert len(slugify(long)) <= 64


def test_slugify_leading_trailing_hyphens():
    result = slugify("  ---hello---  ")
    assert not result.startswith("-")
    assert not result.endswith("-")


# ---------------------------------------------------------------------------
# application_dir — one flat folder per company per day
# ---------------------------------------------------------------------------


def test_application_dir_is_flat_company_date(tmp_path):
    out = application_dir(tmp_path, "Fabrikam Equipment")
    assert out.exists() and out.is_dir()
    assert out.name == f"fabrikam-equipment-{date.today().isoformat()}"
    assert out.parent == tmp_path  # flat — no date/company/role nesting


def test_application_dir_shared_across_roles_same_day(tmp_path):
    a = application_dir(tmp_path, "Acme")
    b = application_dir(tmp_path, "Acme")
    assert a == b  # same company + day -> same folder (files disambiguate by role)


def test_application_dir_empty_company_fallback(tmp_path):
    out = application_dir(tmp_path, "")
    assert out.name.startswith("unknown-company-")


# ---------------------------------------------------------------------------
# filename helpers
# ---------------------------------------------------------------------------


def test_name_token_title_case_underscored():
    assert name_token("Fabrikam Equipment") == "Fabrikam_Equipment"
    assert name_token("Chief Operations Officer") == "Chief_Operations_Officer"


def test_person_slug_drops_middle():
    assert person_slug("Alexander R. Rivera") == "Alexander_Rivera"
    assert person_slug("Jane Doe") == "Jane_Doe"

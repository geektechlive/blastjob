from blastjob.core.paths import make_output_dir, slugify


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
# make_output_dir
# ---------------------------------------------------------------------------


def test_make_output_dir_creates_directory(tmp_path):
    out = make_output_dir(tmp_path, "Acme Corp", "Senior Engineer")
    assert out.exists()
    assert out.is_dir()
    assert "acme-corp" in str(out)
    assert "senior-engineer" in str(out)


def test_make_output_dir_collision_appends_suffix(tmp_path):
    first = make_output_dir(tmp_path, "Acme", "Engineer")
    second = make_output_dir(tmp_path, "Acme", "Engineer")
    assert first != second
    assert str(second).endswith("-2")


def test_make_output_dir_multiple_collisions(tmp_path):
    paths = [make_output_dir(tmp_path, "Acme", "Engineer") for _ in range(4)]
    assert len(set(str(p) for p in paths)) == 4
    assert str(paths[2]).endswith("-3")
    assert str(paths[3]).endswith("-4")


def test_make_output_dir_empty_company_fallback(tmp_path):
    out = make_output_dir(tmp_path, "", "Engineer")
    assert "unknown-company" in str(out)


def test_make_output_dir_empty_role_fallback(tmp_path):
    out = make_output_dir(tmp_path, "Acme", "")
    assert "unknown-role" in str(out)

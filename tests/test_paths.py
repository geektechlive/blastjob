from blastjob.core.paths import slugify


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

"""Unit tests for config.py — load_config, save_config, data_dir, output_dir."""

from pathlib import Path

from blastjob.models.config import BlastJobConfig

# We test the logic directly by patching the config path, not by importing the module-level
# constants, which are computed at import time using the real user home.


def _make_config_module(config_path: Path):
    """Return a fresh copy of the config module with its _CONFIG_FILE overridden."""

    import blastjob.config as mod

    original = mod._CONFIG_FILE
    mod._CONFIG_FILE = config_path
    try:
        yield mod
    finally:
        mod._CONFIG_FILE = original


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


def test_load_config_returns_defaults_when_missing(tmp_path):
    import blastjob.config as mod

    original = mod._CONFIG_FILE
    mod._CONFIG_FILE = tmp_path / "nonexistent.toml"
    try:
        cfg = mod.load_config()
        assert cfg.paths.data_dir == "~/.blastjob"
        assert cfg.paths.output_dir == "~/Documents/blastjob"
        assert cfg.llm.provider == "auto"
    finally:
        mod._CONFIG_FILE = original


def test_load_config_reads_existing_file(tmp_path):
    import tomli_w

    import blastjob.config as mod

    config_file = tmp_path / "config.toml"
    data = {
        "paths": {"data_dir": "/custom/data", "output_dir": "/custom/output"},
        "llm": {"provider": "openai"},
        "generation": {"max_web_searches": 5},
        "pricing": {},
    }
    with open(config_file, "wb") as f:
        tomli_w.dump(data, f)

    original = mod._CONFIG_FILE
    mod._CONFIG_FILE = config_file
    try:
        cfg = mod.load_config()
        assert cfg.paths.data_dir == "/custom/data"
        assert cfg.llm.provider == "openai"
        assert cfg.generation.max_web_searches == 5
    finally:
        mod._CONFIG_FILE = original


# ---------------------------------------------------------------------------
# save_config + load_config round-trip
# ---------------------------------------------------------------------------


def test_save_load_roundtrip(tmp_path):
    import blastjob.config as mod

    config_file = tmp_path / "subdir" / "config.toml"
    original = mod._CONFIG_FILE
    original_dir = mod._CONFIG_DIR
    mod._CONFIG_FILE = config_file
    mod._CONFIG_DIR = config_file.parent
    try:
        cfg = BlastJobConfig()
        cfg.paths.data_dir = "/tmp/blastjob_test"
        cfg.llm.provider = "anthropic"
        mod.save_config(cfg)

        assert config_file.exists()
        loaded = mod.load_config()
        assert loaded.paths.data_dir == "/tmp/blastjob_test"
        assert loaded.llm.provider == "anthropic"
    finally:
        mod._CONFIG_FILE = original
        mod._CONFIG_DIR = original_dir


# ---------------------------------------------------------------------------
# data_dir / output_dir
# ---------------------------------------------------------------------------


def test_data_dir_expands_tilde():
    import blastjob.config as mod

    cfg = BlastJobConfig()
    cfg.paths.data_dir = "~/.blastjob"
    result = mod.data_dir(cfg)
    assert not str(result).startswith("~")
    assert ".blastjob" in str(result)


def test_output_dir_expands_tilde():
    import blastjob.config as mod

    cfg = BlastJobConfig()
    cfg.paths.output_dir = "~/Documents/blastjob"
    result = mod.output_dir(cfg)
    assert not str(result).startswith("~")
    assert "blastjob" in str(result)


def test_data_dir_custom_absolute(tmp_path):
    import blastjob.config as mod

    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path / "custom_data")
    result = mod.data_dir(cfg)
    assert result == tmp_path / "custom_data"


def test_output_dir_custom_absolute(tmp_path):
    import blastjob.config as mod

    cfg = BlastJobConfig()
    cfg.paths.output_dir = str(tmp_path / "custom_output")
    result = mod.output_dir(cfg)
    assert result == tmp_path / "custom_output"


def test_blast_job_config_anthropic_alias():
    cfg = BlastJobConfig()
    assert cfg.anthropic is cfg.llm

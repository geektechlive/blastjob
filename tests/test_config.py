"""Unit tests for config.py — load_config, save_config, data_dir, output_dir."""

from blastjob.models.config import BlastJobConfig

# tests/conftest.py points BLASTJOB_DATA_DIR and the legacy config path at
# throwaway directories for every test; these tests narrow them further.


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


def test_load_config_returns_defaults_when_nothing_is_stored(tmp_path, monkeypatch):
    import blastjob.config as mod

    monkeypatch.setenv("BLASTJOB_DATA_DIR", str(tmp_path / "data"))
    cfg = mod.load_config()
    assert cfg.paths.data_dir == str(tmp_path / "data")
    assert cfg.paths.output_dir == "~/Documents/blastjob"
    assert cfg.llm.provider == "auto"
    assert (tmp_path / "data" / "blastjob.db").exists()


def test_load_config_imports_a_legacy_file_and_finds_its_data_dir(tmp_path, monkeypatch):
    """No env var: the legacy file's own paths.data_dir decides where the
    database lives, and its settings are imported into that database."""
    import tomli_w

    import blastjob.config as mod

    config_file = tmp_path / "config.toml"
    data = {
        "paths": {"data_dir": str(tmp_path / "custom-data"), "output_dir": "/custom/output"},
        "llm": {"provider": "openai"},
        "generation": {"max_web_searches": 5},
        "pricing": {},
    }
    config_file.write_bytes(tomli_w.dumps(data).encode())
    monkeypatch.setattr(mod, "_CONFIG_FILE", config_file)
    monkeypatch.delenv("BLASTJOB_DATA_DIR")

    cfg = mod.load_config()

    assert cfg.paths.data_dir == str(tmp_path / "custom-data")
    assert cfg.paths.output_dir == "/custom/output"
    assert cfg.llm.provider == "openai"
    assert cfg.generation.max_web_searches == 5
    assert (tmp_path / "custom-data" / "blastjob.db").exists()
    assert config_file.exists()  # left in place as a backup


# ---------------------------------------------------------------------------
# save_config + load_config round-trip
# ---------------------------------------------------------------------------


def test_save_load_roundtrip(tmp_path, monkeypatch):
    import blastjob.config as mod

    monkeypatch.setenv("BLASTJOB_DATA_DIR", str(tmp_path))
    cfg = mod.load_config()
    cfg.llm.provider = "anthropic"
    mod.save_config(cfg)

    loaded = mod.load_config()
    assert loaded.llm.provider == "anthropic"
    assert loaded.paths.data_dir == str(tmp_path)


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


def test_output_dir_expands_tilde(tmp_path, monkeypatch):
    import blastjob.config as mod

    # A throwaway HOME: conftest swaps the REAL default output folder for a temp
    # one, so the expansion is checked against a home that is not the owner's.
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = BlastJobConfig()
    cfg.paths.output_dir = "~/Documents/blastjob"
    result = mod.output_dir(cfg)
    assert result == tmp_path / "Documents" / "blastjob"


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


def test_candidate_and_towns_survive_a_save_load_roundtrip(tmp_path, monkeypatch):
    """A section the model does not declare would be dropped silently
    (extra="ignore"), so pin both."""
    import blastjob.config as mod

    monkeypatch.setenv("BLASTJOB_DATA_DIR", str(tmp_path))
    cfg = mod.load_config()
    cfg.candidate.target_titles = ["Data Engineer", "Analytics Engineer"]
    cfg.candidate.seniority = "individual contributor"
    cfg.candidate.field = "data engineering"
    cfg.candidate.former_employers = ["Northwind Logistics"]
    cfg.discovery.home_state = "OH"
    cfg.discovery.towns = {"harrowgate": [40.1, -82.9], "fairhaven": [40.3, -83.1]}
    mod.save_config(cfg)

    loaded = mod.load_config()
    assert loaded.candidate == cfg.candidate
    assert loaded.discovery.home_state == "OH"
    assert loaded.discovery.towns == cfg.discovery.towns


def test_fresh_config_has_no_candidate_profile():
    cand = BlastJobConfig().candidate
    assert not cand.is_configured()
    assert cand.profile_text() == ""


def test_profile_text_names_every_configured_field():
    cand = BlastJobConfig().candidate.model_copy(
        update={"target_titles": ["Charge Nurse"], "seniority": "manager", "field": "nursing"}
    )
    text = cand.profile_text()
    assert cand.is_configured()
    assert "Charge Nurse" in text and "manager" in text and "nursing" in text


def test_unfilled_example_placeholders_do_not_count_as_a_profile():
    import tomllib
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "config.example.toml"
    cfg = BlastJobConfig.model_validate(tomllib.loads(example.read_text()))
    assert not cfg.candidate.is_configured()
    assert cfg.candidate.profile_text() == ""

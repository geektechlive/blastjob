"""`blastjob setup doctor|get|set|import|export`, against a temp BLASTJOB_DATA_DIR."""

from __future__ import annotations

import json
import tomllib

import pytest

from blastjob import setup_cli, setup_doctor


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("BLASTJOB_DATA_DIR", str(d))
    return d


def _run(capsys, *argv: str) -> tuple[int, str, str]:
    code = setup_cli.main(["setup", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_set_then_get_one_key(data_dir, capsys):
    assert _run(capsys, "set", "discovery.comp_floor_usd", "150000")[0] == 0
    code, out, _ = _run(capsys, "get", "discovery.comp_floor_usd")
    assert code == 0 and json.loads(out) == 150000


def test_set_takes_bare_strings_and_single_quoted_json(data_dir, capsys):
    assert _run(capsys, "set", "discovery.home_state", "OH")[0] == 0
    assert _run(capsys, "set", "candidate.target_titles", '["Director of Operations"]')[0] == 0
    assert _run(capsys, "set", "discovery.towns", '{"fairhaven": [39.95, -83.05]}')[0] == 0
    _code, out, _ = _run(capsys, "get")
    values = tomllib.loads(out)
    assert values["candidate"]["target_titles"] == ["Director of Operations"]
    assert values["discovery"]["towns"] == {"fairhaven": [39.95, -83.05]}


def test_set_refuses_unknown_keys_and_secret_values(data_dir, capsys):
    code, _out, err = _run(capsys, "set", "discovery.nope", "1")
    assert code == 2 and "unknown setting" in err
    code, _out, err = _run(capsys, "set", "discovery.mail_password_env", "abcd efgh ijkl")
    assert code == 2 and "NAME of an environment variable" in err
    assert "abcd" not in err


def test_get_of_an_unknown_key_fails(data_dir, capsys):
    assert _run(capsys, "get", "discovery.nope")[0] == 2


def test_import_and_export(tmp_path, data_dir, capsys):
    src = tmp_path / "mine.toml"
    src.write_text('[discovery]\nhome_state = "OH"\ncomp_floor_usd = 90000\n')
    code, out, _ = _run(capsys, "import", str(src))
    assert code == 0 and "imported 2" in out

    _code, out, _ = _run(capsys, "export")
    assert tomllib.loads(out) == {"discovery": {"home_state": "OH", "comp_floor_usd": 90000}}
    _code, out, _ = _run(capsys, "export", "--all")
    assert tomllib.loads(out)["discovery"]["alignment_threshold"] == 78


def test_import_of_a_missing_file_fails_cleanly(tmp_path, data_dir, capsys):
    code, _out, err = _run(capsys, "import", str(tmp_path / "missing.toml"))
    assert code == 2 and "error" in err


def test_doctor_fails_with_a_fix_for_each_missing_piece(data_dir, monkeypatch, capsys):
    monkeypatch.setattr(setup_doctor.shutil, "which", lambda _name: None)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    code, out, _ = _run(capsys, "doctor")

    assert code == 1
    assert "[ok  ] database writable" in out
    assert "[ok  ] migrations current" in out
    assert "[FAIL] work_history.md present" in out
    assert "[FAIL] candidate.target_titles set" in out
    assert "[FAIL] LLM provider" in out
    assert out.count("fix:") >= 3


def test_doctor_passes_on_a_configured_install(data_dir, monkeypatch, capsys):
    data_dir.mkdir(parents=True)
    (data_dir / "work_history.md").write_text("# History\n")
    (data_dir / "secrets.env").write_text("")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "c")
    monkeypatch.setattr(setup_doctor.shutil, "which", lambda _name: "/usr/bin/claude")
    monkeypatch.setattr(setup_doctor, "_run", lambda argv: (0, "1.0.0 (Claude Code)"))
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    _run(capsys, "set", "candidate.target_titles", '["Director of Operations"]')
    _run(capsys, "set", "notify.mode", "discord")

    code, out, _ = _run(capsys, "doctor")

    assert code == 0, out
    assert "[ok  ] DISCORD_BOT_TOKEN set" in out
    assert "[ok  ] LLM provider -- claude-cli" in out


def test_doctor_reports_a_missing_discord_secret_only_in_discord_mode(data_dir, monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    from blastjob.models.config import BlastJobConfig

    cfg = BlastJobConfig()
    assert not any("DISCORD" in c.name for c in setup_doctor.check_secrets(cfg))
    cfg.notify.mode = "discord"
    failing = [c for c in setup_doctor.check_secrets(cfg) if not c.ok and c.required]
    assert {c.name for c in failing} == {"DISCORD_BOT_TOKEN set", "DISCORD_CHANNEL_ID set"}


def test_doctor_warns_on_a_saved_mail_name_that_breaks_the_rule():
    from blastjob.models.config import BlastJobConfig

    cfg = BlastJobConfig()
    assert setup_doctor.check_mail_names(cfg) == []  # shipped defaults conform
    cfg.discovery.mail_password_env = "ANTHROPIC_API_KEY"
    cfg.discovery.personal_mail_user_env = "GMAIL_IMAP_USER"
    (warn,) = setup_doctor.check_mail_names(cfg)
    assert not warn.ok and not warn.required
    assert "mail_password_env" in warn.name
    assert "_IMAP_APP_PASSWORD" in warn.fix
    assert "ANTHROPIC_API_KEY" not in warn.detail + warn.fix

import os
import tomllib
from pathlib import Path

import tomli_w
from platformdirs import user_config_dir

from blastjob.models.config import BlastJobConfig

_CONFIG_DIR = Path(user_config_dir("blastjob"))
_CONFIG_FILE = _CONFIG_DIR / "config.toml"


def config_path() -> Path:
    return _CONFIG_FILE


def load_config() -> BlastJobConfig:
    if not _CONFIG_FILE.exists():
        return BlastJobConfig()
    with open(_CONFIG_FILE, "rb") as f:
        raw = tomllib.load(f)
    return BlastJobConfig.model_validate(raw)


def save_config(cfg: BlastJobConfig) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    raw = cfg.model_dump(exclude={"anthropic"})  # exclude the back-compat alias
    with open(_CONFIG_FILE, "wb") as f:
        tomli_w.dump(raw, f)


def data_dir(cfg: BlastJobConfig) -> Path:
    return Path(os.path.expanduser(cfg.paths.data_dir))


def output_dir(cfg: BlastJobConfig) -> Path:
    return Path(os.path.expanduser(cfg.paths.output_dir))

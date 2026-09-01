"""Configuration loading and writing.

The config lives in ``config.toml`` next to the scripts. ``setup.py`` writes it
and ``draft.py`` reads it. Relative paths inside the file are resolved against
the directory that contains the config file, so the scripts work from any
working directory.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("config.toml")
VERBOSITY_LEVELS = ("quiet", "normal", "debug")


class ConfigError(Exception):
    """Raised when config.toml is missing or malformed."""


@dataclass
class Config:
    """All user-editable settings plus the IDs resolved by ``setup.py``."""

    username: str = ""
    league_id: str = ""
    season: int = 2026
    rankings_path: Path = Path("rankings.csv")
    overrides_path: Path = Path("overrides.json")
    poll_interval: float = 4.0
    k_def_round_threshold: int = 12
    verbosity: str = "normal"
    cache_dir: Path = Path("cache")
    log_dir: Path = Path("logs")
    user_id: str | None = None
    draft_id: str | None = None
    draft_slot: int | None = None
    config_path: Path = DEFAULT_CONFIG_PATH

    def _resolve(self, path: Path) -> Path:
        return path if path.is_absolute() else (self.config_path.parent / path)

    @property
    def rankings_file(self) -> Path:
        """Absolute path to the rankings CSV."""
        return self._resolve(self.rankings_path)

    @property
    def overrides_file(self) -> Path:
        """Absolute path to overrides.json."""
        return self._resolve(self.overrides_path)

    @property
    def cache_path(self) -> Path:
        """Absolute path to the cache directory."""
        return self._resolve(self.cache_dir)

    @property
    def logs_path(self) -> Path:
        """Absolute path to the logs directory."""
        return self._resolve(self.log_dir)


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] in config must be a table")
    return value


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    """Load ``config.toml``.

    Raises:
        ConfigError: if the file is missing, unparsable, or has bad values.
    """
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"{path} not found. Run `python setup.py --username <you> --league <id>` first."
        )
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc

    sleeper = _section(data, "sleeper")
    rankings = _section(data, "rankings")
    draft = _section(data, "draft")
    resolved = _section(data, "resolved")

    verbosity = str(draft.get("verbosity", "normal")).lower()
    if verbosity not in VERBOSITY_LEVELS:
        raise ConfigError(f"draft.verbosity must be one of {VERBOSITY_LEVELS}, got {verbosity!r}")

    try:
        cfg = Config(
            username=str(sleeper.get("username", "")),
            league_id=str(sleeper.get("league_id", "")),
            season=int(sleeper.get("season", 2026)),
            rankings_path=Path(str(rankings.get("path", "rankings.csv"))),
            overrides_path=Path(str(rankings.get("overrides", "overrides.json"))),
            poll_interval=float(draft.get("poll_interval", 4.0)),
            k_def_round_threshold=int(draft.get("k_def_round_threshold", 12)),
            verbosity=verbosity,
            cache_dir=Path(str(draft.get("cache_dir", "cache"))),
            log_dir=Path(str(draft.get("log_dir", "logs"))),
            user_id=(str(resolved["user_id"]) if resolved.get("user_id") else None),
            draft_id=(str(resolved["draft_id"]) if resolved.get("draft_id") else None),
            draft_slot=(int(resolved["draft_slot"]) if resolved.get("draft_slot") else None),
            config_path=path.resolve(),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Bad value in {path}: {exc}") from exc
    if cfg.poll_interval < 1:
        raise ConfigError("draft.poll_interval must be at least 1 second")
    return cfg


def _toml_str(value: Any) -> str:
    return json.dumps(str(value))


def write_config(cfg: Config, path: Path | str | None = None) -> Path:
    """Write ``cfg`` to ``path`` (defaults to ``cfg.config_path``) and return the path."""
    target = Path(path) if path is not None else cfg.config_path
    lines = [
        "# Sleeper draft assistant configuration. Written by setup.py; safe to hand-edit.",
        "",
        "[sleeper]",
        f"username = {_toml_str(cfg.username)}",
        f"league_id = {_toml_str(cfg.league_id)}",
        f"season = {int(cfg.season)}",
        "",
        "[rankings]",
        f"path = {_toml_str(cfg.rankings_path.as_posix())}",
        f"overrides = {_toml_str(cfg.overrides_path.as_posix())}",
        "",
        "[draft]",
        f"poll_interval = {float(cfg.poll_interval)}",
        "# Do not recommend K/DEF before this round (1-based).",
        f"k_def_round_threshold = {int(cfg.k_def_round_threshold)}",
        '# quiet | normal | debug',
        f"verbosity = {_toml_str(cfg.verbosity)}",
        f"cache_dir = {_toml_str(cfg.cache_dir.as_posix())}",
        f"log_dir = {_toml_str(cfg.log_dir.as_posix())}",
        "",
        "[resolved]",
        "# Filled in by setup.py. Re-run setup.py if the draft order changes.",
    ]
    if cfg.user_id:
        lines.append(f"user_id = {_toml_str(cfg.user_id)}")
    if cfg.draft_id:
        lines.append(f"draft_id = {_toml_str(cfg.draft_id)}")
    if cfg.draft_slot:
        lines.append(f"draft_slot = {int(cfg.draft_slot)}")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target

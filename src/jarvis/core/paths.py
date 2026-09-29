"""Filesystem locations. Everything JARVIS writes lives under per-user app dirs."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import PlatformDirs

APP_NAME = "Jarvis"
APP_AUTHOR = "Jarvis"


@dataclass(frozen=True)
class AppPaths:
    config_dir: Path
    data_dir: Path
    log_dir: Path
    cache_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def token_file(self) -> Path:
        return self.data_dir / "api-token"

    @property
    def lock_file(self) -> Path:
        return self.data_dir / "jarvis.lock"

    @property
    def audit_log(self) -> Path:
        return self.log_dir / "audit.jsonl"

    def ensure(self) -> AppPaths:
        for d in (self.config_dir, self.data_dir, self.log_dir, self.cache_dir):
            d.mkdir(parents=True, exist_ok=True)
        return self


def get_paths() -> AppPaths:
    """Resolve app paths. JARVIS_HOME overrides everything (used by tests and portable installs)."""
    home = os.environ.get("JARVIS_HOME")
    if home:
        root = Path(home)
        return AppPaths(root / "config", root / "data", root / "logs", root / "cache")
    dirs = PlatformDirs(APP_NAME, APP_AUTHOR, roaming=False)
    return AppPaths(
        config_dir=Path(dirs.user_config_dir),
        data_dir=Path(dirs.user_data_dir),
        log_dir=Path(dirs.user_log_dir),
        cache_dir=Path(dirs.user_cache_dir),
    )

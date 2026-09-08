"""Configuration loader with customer-specific deep merge."""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class Settings:
    """Immutable-ish view over merged YAML configuration."""

    def __init__(self, data: Dict[str, Any], config_dir: Path):
        self._data = data
        self.config_dir = config_dir

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def as_dict(self) -> Dict[str, Any]:
        return copy.deepcopy(self._data)

    def for_customer(self, customer: Optional[str]) -> "Settings":
        if not customer:
            return self
        candidate = self.config_dir / "customers" / f"{customer.lower().replace(' ', '_')}.yaml"
        if candidate.exists():
            with candidate.open("r", encoding="utf-8") as fh:
                override = yaml.safe_load(fh) or {}
            return Settings(_deep_merge(self._data, override), self.config_dir)
        return self


def load_settings(config_dir: Optional[os.PathLike | str] = None, customer: Optional[str] = None) -> Settings:
    cdir = Path(config_dir) if config_dir else Path(os.environ.get("PQM_AGENT_CONFIG_DIR", DEFAULT_CONFIG_DIR))
    with (cdir / "default.yaml").open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    settings = Settings(data, cdir)
    return settings.for_customer(customer)

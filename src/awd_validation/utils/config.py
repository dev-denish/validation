"""
Configuration loader for AWD Validation Pipeline.
"""
from pathlib import Path
import yaml
from loguru import logger


class Config:
    """Loads and provides access to settings.yaml configuration."""

    def __init__(self, config_path: str = "/app/config/settings.yaml"):
        self.config_path = Path(config_path)
        self._config = self._load()
        logger.debug(f"Configuration loaded from {self.config_path}")

    def _load(self) -> dict:
        if not self.config_path.exists():
            raise FileNotFoundError(
                f"Config file not found: {self.config_path}"
            )
        with open(self.config_path, "r") as f:
            return yaml.safe_load(f)

    def get(self, *keys, default=None):
        """
        Get nested config value using dot-notation keys.
        Example: config.get('crs', 'working_epsg')
        """
        value = self._config
        for key in keys:
            if isinstance(value, dict):
                value = value.get(key, default)
            else:
                return default
        return value

    @property
    def project(self) -> dict:
        return self._config.get("project", {})

    @property
    def input(self) -> dict:
        return self._config.get("input", {})

    @property
    def crs(self) -> dict:
        return self._config.get("crs", {})

    @property
    def schema(self) -> dict:
        return self._config.get("schema", {})

    @property
    def thresholds(self) -> dict:
        return self._config.get("thresholds", {})

    @property
    def checks(self) -> dict:
        return self._config.get("checks", {})

    @property
    def database(self) -> dict:
        return self._config.get("database", {})

"""
Config module for Dohtar Monitor Agent.
Handles loading, validating, and saving configuration from config.json.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """Raised when config validation fails."""
    pass


class Config:
    """Manages agent configuration."""

    DEFAULTS = {
        "agent_id": "ocm-default",
        "machine_name": "UnnamedMachine",
        "backend_url": "http://localhost:9090",
        "install_path": "/opt/ocm-agent",
        "poll_interval": 2,
        "collectors": {
            "system": True,
            "gpu": True,
            "processes": True,
            "disk": True,
            "docker": False,
            "custom_endpoints": [],
        },
        "services": [],
    }

    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize config from file.

        Args:
            config_path: Path to config.json. If None, uses install_path/config.json.
        """
        self.config_path = config_path
        self.data: Dict[str, Any] = self.DEFAULTS.copy()
        self.load()

    def load(self) -> None:
        """Load config from file. Merges with defaults."""
        if not self.config_path:
            # Try to find config.json in common locations
            candidates = [
                Path("/opt/ocm-agent/config.json"),
                Path("/etc/ocm-agent/config.json"),
                Path.home() / ".ocm-agent" / "config.json",
                Path(".") / "config.json",
            ]
            for candidate in candidates:
                if candidate.exists():
                    self.config_path = str(candidate)
                    break

        if not self.config_path or not Path(self.config_path).exists():
            logger.warning(
                f"Config file not found at {self.config_path}. Using defaults."
            )
            return

        try:
            with open(self.config_path, "r") as f:
                file_data = json.load(f)
            # Deep merge with defaults
            self._merge_config(self.data, file_data)
            logger.info(f"Loaded config from {self.config_path}")
        except json.JSONDecodeError as e:
            raise ConfigError(f"Invalid JSON in config file: {e}")
        except IOError as e:
            raise ConfigError(f"Failed to read config file: {e}")

    def save(self, path: Optional[str] = None) -> None:
        """Save current config to file."""
        target_path = path or self.config_path
        if not target_path:
            raise ConfigError("No config path specified for save.")

        try:
            Path(target_path).parent.mkdir(parents=True, exist_ok=True)
            with open(target_path, "w") as f:
                json.dump(self.data, f, indent=2)
            logger.info(f"Saved config to {target_path}")
        except IOError as e:
            raise ConfigError(f"Failed to save config: {e}")

    def _merge_config(self, base: Dict, override: Dict) -> None:
        """Recursively merge override config into base."""
        for key, value in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                self._merge_config(base[key], value)
            else:
                base[key] = value

    def validate(self) -> None:
        """Validate required fields."""
        required = ["agent_id", "machine_name", "backend_url", "poll_interval"]
        for field in required:
            if field not in self.data or not self.data[field]:
                raise ConfigError(f"Missing required config field: {field}")

        if not isinstance(self.data.get("poll_interval"), (int, float)):
            raise ConfigError("poll_interval must be a number")

        if self.data["poll_interval"] <= 0:
            raise ConfigError("poll_interval must be positive")

    def get(self, key: str, default: Any = None) -> Any:
        """Get config value."""
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Set config value."""
        self.data[key] = value

    @property
    def agent_id(self) -> str:
        return self.data["agent_id"]

    @property
    def machine_name(self) -> str:
        return self.data["machine_name"]

    @property
    def backend_url(self) -> str:
        return self.data["backend_url"]

    @property
    def poll_interval(self) -> float:
        return self.data["poll_interval"]

    @property
    def collectors(self) -> Dict[str, Any]:
        return self.data.get("collectors", {})

    @property
    def services(self) -> List[Dict[str, Any]]:
        return self.data.get("services", [])

    def is_collector_enabled(self, name: str) -> bool:
        """Check if a collector is enabled."""
        return self.collectors.get(name, False)

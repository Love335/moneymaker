"""
settings.py — Persistent user settings storage.

Stores non-credential user preferences that should survive restarts,
such as display and LED brightness.
Backed by a JSON file in the data directory.
"""

import json
import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SETTINGS_FILE = Path(__file__).resolve().parents[2] / "data" / "settings.json"

# Default values
DEFAULTS: dict[str, Any] = {
    "display_brightness": 128,
    "led_brightness":     0.3,
    "trading_mode":       "PAPER",
    "active_algorithm":   "dual_momentum",
    "real_pnl_baseline":  None,
}

class Settings:
    """
    Thread-safe persistent settings store.
    Reads from and writes to data/settings.json automatically.
    Falls back to defaults if the file is missing or corrupt.
    """

    def __init__(self, path: Path = SETTINGS_FILE) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data = self._load()

    def get(self, key: str) -> Any:
        """Return setting value, falling back to default if not set."""
        with self._lock:
            return self._data.get(key, DEFAULTS.get(key))

    def set(self, key: str, value: Any) -> None:
        """Persist a setting value immediately."""
        with self._lock:
            self._data[key] = value
            self._save()

    def _load(self) -> dict:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                logger.debug("Settings loaded from %s", self._path)
                return {**DEFAULTS, **data}   # merge so new defaults appear
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "Settings file corrupt or unreadable, "
                    "using defaults. Error: %s", exc
                )
        return dict(DEFAULTS)

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
            tmp.replace(self._path)
        except OSError as exc:
            logger.error("Failed to save settings: %s", exc)
from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from .core import ConfigError, parse_hhmm


DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "enabled": False,
    "production_approved": False,
    "timezone": "Asia/Kolkata",
    "base_url": "https://your-company.keka.com",
    "attendance_url": None,
    "in_time": "10:00",
    "out_time": "19:00",
    "working_days": [1, 2, 3, 4, 5],
    "schedule_grace_minutes": 90,
    "clock_in_grace_minutes": 30,
    "wake_schedule": {"enabled_after_approval": False, "time": "09:55"},
    "network_retries": 3,
    "browser_profile": str(Path.home() / ".keka-attendance" / "browser-profile"),
    "telegram": {
        "authorized_user_id": None,
        "authorized_chat_id": None,
        "remote_password_entry_enabled": False,
    },
    "selectors": {
        "authenticated_marker": None,
        "attendance_state": None,
        "clock_in": None,
        "clock_out": None,
        "success_marker": None,
    },
}


class ConfigManager:
    def __init__(self, path: Path):
        self.path = Path(path)

    def ensure_defaults(self) -> dict[str, Any]:
        if self.path.exists():
            return self.load()
        data = deepcopy(DEFAULT_CONFIG)
        self.save(data)
        return data

    def load(self) -> dict[str, Any]:
        with self.path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        self._validate(data)
        return data

    def save(self, data: dict[str, Any]) -> None:
        self._validate(data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="config.", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def update_schedule(self, action: str, value: str) -> dict[str, Any]:
        parse_hhmm(value)
        if action not in {"in", "out"}:
            raise ConfigError("Action must be 'in' or 'out'")
        data = self.load()
        data[f"{action}_time"] = value
        self.save(data)
        return data

    def update_working_days(self, values: list[int]) -> dict[str, Any]:
        if not values or len(values) != len(set(values)) or any(day < 1 or day > 7 for day in values):
            raise ConfigError("Working days must be unique ISO weekday numbers from 1 to 7")
        data = self.load()
        data["working_days"] = sorted(values)
        self.save(data)
        return data

    def update_clock_in_grace(self, minutes: int) -> dict[str, Any]:
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 0 or minutes > 180:
            raise ConfigError("Clock In grace must be an integer from 0 to 180 minutes")
        data = self.load()
        data["clock_in_grace_minutes"] = minutes
        self.save(data)
        return data

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        data = self.load()
        data["enabled"] = bool(enabled)
        self.save(data)
        return data

    def set_telegram_authorization(self, user_id: int, chat_id: int) -> dict[str, Any]:
        if user_id <= 0 or chat_id <= 0:
            raise ConfigError("Telegram user and chat IDs must be positive integers")
        data = self.load()
        data["telegram"]["authorized_user_id"] = user_id
        data["telegram"]["authorized_chat_id"] = chat_id
        self.save(data)
        return data

    @staticmethod
    def _validate(data: dict[str, Any]) -> None:
        parse_hhmm(data["in_time"])
        parse_hhmm(data["out_time"])
        days = data["working_days"]
        if not days or len(days) != len(set(days)) or any(day < 1 or day > 7 for day in days):
            raise ConfigError("Invalid working_days")
        if data.get("timezone") != "Asia/Kolkata":
            raise ConfigError("This installation must use Asia/Kolkata")
        grace = data.get("clock_in_grace_minutes")
        if isinstance(grace, bool) or not isinstance(grace, int) or grace < 0 or grace > 180:
            raise ConfigError("Invalid clock_in_grace_minutes")

from __future__ import annotations

import os
import plistlib
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from .launchd import SCHEDULE_LABELS, TELEGRAM_LABEL, build_scheduled_plist, build_telegram_plist


class LaunchdError(RuntimeError):
    pass


def _run_launchctl(args: list[str]) -> int:
    completed = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return completed.returncode


class LaunchdManager:
    def __init__(
        self,
        root: Path,
        launch_agents_dir: Path | None = None,
        uid: int | None = None,
        runner: Callable[[list[str]], int] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ):
        self.root = Path(root)
        self.launch_agents_dir = Path(launch_agents_dir or (Path.home() / "Library" / "LaunchAgents"))
        self.uid = os.getuid() if uid is None else uid
        self.runner = runner or _run_launchctl
        self.sleeper = sleeper or time.sleep

    @property
    def domain(self) -> str:
        return f"gui/{self.uid}"

    def scheduled_paths(self) -> list[Path]:
        return [self.launch_agents_dir / f"{label}.plist" for label in SCHEDULE_LABELS.values()]

    def telegram_path(self) -> Path:
        return self.launch_agents_dir / f"{TELEGRAM_LABEL}.plist"

    def install_scheduled_plists(self, config: dict[str, Any]) -> list[Path]:
        paths = self.scheduled_paths()
        self._write_plist(
            paths[0],
            build_scheduled_plist(
                self.root,
                "CLOCK_IN",
                config["in_time"],
                config["working_days"],
                config["clock_in_grace_minutes"],
            ),
        )
        self._write_plist(
            paths[1],
            build_scheduled_plist(
                self.root,
                "CLOCK_OUT",
                config["out_time"],
                config["working_days"],
                config["schedule_grace_minutes"],
            ),
        )
        return paths

    def install_telegram_service(self) -> Path:
        path = self.telegram_path()
        self._write_plist(path, build_telegram_plist(self.root))
        self._bootout(TELEGRAM_LABEL)
        self._bootstrap(path)
        return path

    def sync_schedules(self, config: dict[str, Any]) -> None:
        paths = self.scheduled_paths()
        if not any(path.exists() for path in paths):
            return
        self.install_scheduled_plists(config)
        if config.get("enabled"):
            self.enable_schedules(config)

    def enable_schedules(self, config: dict[str, Any]) -> None:
        paths = self.install_scheduled_plists(config)
        for label, path in zip(SCHEDULE_LABELS.values(), paths):
            self._bootout(label)
            self._bootstrap(path)

    def disable_schedules(self) -> None:
        for label in SCHEDULE_LABELS.values():
            self._bootout(label)

    def _bootstrap(self, path: Path) -> None:
        args = ["/bin/launchctl", "bootstrap", self.domain, str(path)]
        for attempt in range(2):
            code = self.runner(args)
            if code == 0:
                return
            if attempt == 0:
                self.sleeper(0.5)
        raise LaunchdError(f"Could not load {path.name}")

    def _bootout(self, label: str) -> None:
        self.runner(["/bin/launchctl", "bootout", f"{self.domain}/{label}"])

    def _write_plist(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                plistlib.dump(data, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o644)
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

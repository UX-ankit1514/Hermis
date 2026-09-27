from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .actions import clock_in_dispatch_trigger, macos_boot_time
from .browser import KekaBrowser, interactive_login
from .config import ConfigManager
from .launchd import SCHEDULE_LABELS, TELEGRAM_LABEL
from .launchd_manager import LaunchdManager
from .operations import Operations, scheduled_notification, startup_catchup_notification
from .telegram import (
    TELEGRAM_KEYCHAIN_ACCOUNT,
    TELEGRAM_KEYCHAIN_SERVICE,
    TelegramAPI,
    TelegramError,
    delete_keychain_secret,
    read_keychain_secret,
    run_polling_bot,
)
from .telegram_commands import CommandRouter


ROOT = Path.home() / ".keka-attendance"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="keka-attendance")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("telegram")
    sub.add_parser("clockin-dispatch")
    scheduled = sub.add_parser("scheduled")
    scheduled.add_argument("action", choices=["CLOCK_IN", "CLOCK_OUT"])
    manual = sub.add_parser("manual")
    manual.add_argument("action", choices=["CLOCK_IN", "CLOCK_OUT"])
    sub.add_parser("login-worker")
    sub.add_parser("inspect-ui")
    sub.add_parser("test")
    sub.add_parser("status")
    sub.add_parser("schedule")
    sub.add_parser("logs")
    sub.add_parser("enable")
    sub.add_parser("disable")
    sub.add_parser("install-telegram")
    sub.add_parser("verify-telegram")
    sub.add_parser("install-schedules")
    sub.add_parser("approve-production")
    auth = sub.add_parser("configure-telegram")
    auth.add_argument("user_id", type=int)
    auth.add_argument("chat_id", type=int)
    set_time = sub.add_parser("set-time")
    set_time.add_argument("action", choices=["in", "out"])
    set_time.add_argument("value")
    uninstall = sub.add_parser("uninstall")
    uninstall.add_argument("--yes", action="store_true")
    return parser


def _api_and_chat(config: dict) -> tuple[TelegramAPI, int]:
    token = read_keychain_secret(TELEGRAM_KEYCHAIN_SERVICE, TELEGRAM_KEYCHAIN_ACCOUNT)
    chat_id = config["telegram"].get("authorized_chat_id")
    if not isinstance(chat_id, int):
        raise TelegramError("Authorized Telegram chat is not configured")
    return TelegramAPI(token), chat_id


def _notify(config: dict, text: str | None) -> None:
    if not text:
        return
    try:
        api, chat_id = _api_and_chat(config)
        api.send_message(chat_id, text)
    except Exception:
        pass


def _write_ui_observation(config: dict) -> Path:
    browser = KekaBrowser(config, headless=True)
    try:
        path = ROOT / "state" / "ui-observation.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "url": browser.page.url,
            "title": browser.page.title()[:200],
            "accessibility": browser.adapter.accessibility_snapshot(),
        }
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        return path
    finally:
        browser.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_manager = ConfigManager(ROOT / "config.json")
    config = config_manager.ensure_defaults()
    manager = LaunchdManager(ROOT)
    ops = Operations(ROOT, manager)

    if args.command == "init":
        for path in (ROOT / "logs", ROOT / "state", ROOT / "browser-profile"):
            path.mkdir(parents=True, exist_ok=True)
        os.chmod(ROOT / "browser-profile", 0o700)
        print("Initialized (production scheduling disabled)")
        return 0
    if args.command == "configure-telegram":
        config_manager.set_telegram_authorization(args.user_id, args.chat_id)
        print("Telegram numeric allowlist configured")
        return 0
    if args.command == "verify-telegram":
        api, chat_id = _api_and_chat(config_manager.load())
        bot = api.verify_bot()
        api.send_message(chat_id, "Keka attendance controller paired")
        print(f"Telegram bot verified: id={bot.get('id')}")
        return 0
    if args.command == "install-telegram":
        manager.install_telegram_service()
        print("Telegram launchd controller installed")
        return 0
    if args.command == "telegram":
        current = config_manager.load()
        user_id = current["telegram"].get("authorized_user_id")
        chat_id = current["telegram"].get("authorized_chat_id")
        if not isinstance(user_id, int) or not isinstance(chat_id, int):
            raise SystemExit("Telegram numeric allowlist is not configured")
        api, _ = _api_and_chat(current)
        router = CommandRouter(user_id, chat_id, ops)
        run_polling_bot(api, router)
        return 0
    if args.command == "clockin-dispatch":
        current = config_manager.load()
        now = datetime.now(ZoneInfo(current["timezone"]))
        trigger = clock_in_dispatch_trigger(current, now, macos_boot_time(now.tzinfo))
        result = ops.run_action_result("CLOCK_IN", trigger)
        notification = (
            startup_catchup_notification(
                result,
                current["in_time"],
                current["clock_in_grace_minutes"],
            )
            if trigger == "startup_catchup"
            else scheduled_notification(result)
        )
        _notify(config_manager.load(), notification)
        return 0 if result.result in {
            "SUCCESS",
            "ALREADY_DONE",
            "DISABLED",
            "NON_WORKING_DAY",
            "MISSED_WINDOW",
            "NOT_DUE",
            "NOT_APPLICABLE",
        } else 1
    if args.command == "scheduled":
        result = ops.run_action_result(args.action, "scheduled")
        _notify(config_manager.load(), scheduled_notification(result))
        return 0 if result.result in {"SUCCESS", "ALREADY_DONE", "DISABLED", "NON_WORKING_DAY"} else 1
    if args.command == "manual":
        print(ops.run_action(args.action, "manual"))
        return 0
    if args.command == "login-worker":
        observation = ROOT / "state" / "login-observation.json"
        success = interactive_login(config_manager.load(), observation)
        _notify(
            config_manager.load(),
            "Keka session saved — run /test" if success else "Keka login was not completed",
        )
        return 0 if success else 1
    if args.command == "inspect-ui":
        print(_write_ui_observation(config_manager.load()))
        return 0
    if args.command == "test":
        print(ops.test())
        return 0
    if args.command == "status":
        print(ops.status())
        return 0
    if args.command == "schedule":
        print(ops.schedule())
        return 0
    if args.command == "logs":
        print(ops.logs())
        return 0
    if args.command == "enable":
        print(ops.enable())
        return 0
    if args.command == "disable":
        print(ops.disable())
        return 0
    if args.command == "set-time":
        print(ops.set_schedule(args.action, args.value))
        return 0
    if args.command == "install-schedules":
        manager.install_scheduled_plists(config_manager.load())
        print("Scheduled launchd jobs installed but not enabled")
        return 0
    if args.command == "approve-production":
        current = config_manager.load()
        current["production_approved"] = True
        config_manager.save(current)
        print("Production approval recorded; schedules remain disabled")
        return 0
    if args.command == "uninstall":
        if not args.yes:
            print("Refusing uninstall without --yes", file=sys.stderr)
            return 2
        manager.disable_schedules()
        manager._bootout(TELEGRAM_LABEL)
        for label in [*SCHEDULE_LABELS.values(), TELEGRAM_LABEL]:
            (Path.home() / "Library" / "LaunchAgents" / f"{label}.plist").unlink(missing_ok=True)
        delete_keychain_secret(TELEGRAM_KEYCHAIN_SERVICE, TELEGRAM_KEYCHAIN_ACCOUNT)
        shutil.rmtree(ROOT)
        print("Keka attendance automation and its Keychain bot token were removed")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

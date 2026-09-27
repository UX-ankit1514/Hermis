from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from typing import Any

from .keychain import KeychainError, delete_secret, read_secret


TELEGRAM_KEYCHAIN_SERVICE = "com.hermes.keka-attendance.telegram-bot"
TELEGRAM_KEYCHAIN_ACCOUNT = "bot-token"


class TelegramError(RuntimeError):
    pass


def read_keychain_secret(service: str, account: str) -> str:
    try:
        return read_secret(service, account)
    except KeychainError as exc:
        raise TelegramError(str(exc)) from None


def delete_keychain_secret(service: str, account: str, runner=None) -> None:
    if runner is not None:
        runner(["/usr/bin/security", "delete-generic-password", "-s", service, "-a", account])
        return
    delete_secret(service, account)


class TelegramAPI:
    def __init__(self, token: str):
        self._base = f"https://api.telegram.org/bot{token}/"

    def request(self, method: str, payload: dict[str, Any] | None = None, timeout: int = 60) -> Any:
        body = json.dumps(payload or {}).encode("utf-8")
        request = urllib.request.Request(
            self._base + method,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except (
            urllib.error.URLError,
            http.client.HTTPException,
            ConnectionError,
            TimeoutError,
            json.JSONDecodeError,
        ) as exc:
            raise TelegramError(f"Telegram request failed: {type(exc).__name__}") from None
        if not parsed.get("ok"):
            raise TelegramError("Telegram rejected the request")
        return parsed.get("result")

    def verify_bot(self) -> dict[str, Any]:
        return self.request("getMe", timeout=20)

    def get_updates(self, offset: int | None, timeout: int = 50) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        return self.request("getUpdates", payload, timeout=timeout + 10)

    def send_message(self, chat_id: int, text: str) -> None:
        self.request(
            "sendMessage",
            {"chat_id": chat_id, "text": text[:4096], "disable_web_page_preview": True},
            timeout=20,
        )

    def delete_message(self, chat_id: int, message_id: int) -> bool:
        return bool(self.request("deleteMessage", {"chat_id": chat_id, "message_id": message_id}, timeout=20))


def process_update_batch(api: TelegramAPI, router: Any, updates: list[dict[str, Any]], offset: int | None = None) -> int | None:
    next_offset = offset
    for update in updates:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            next_offset = max(next_offset or 0, update_id + 1)
        message = update.get("message")
        if not isinstance(message, dict):
            continue
        response = router.handle(message)
        if response is not None:
            api.send_message(message["chat"]["id"], response)
    return next_offset


def run_polling_bot(api: TelegramAPI, router: Any) -> None:
    offset = None
    delay = 2
    while True:
        try:
            updates = api.get_updates(offset)
            offset = process_update_batch(api, router, updates, offset)
            delay = 2
        except TelegramError:
            time.sleep(delay)
            delay = min(delay * 2, 60)

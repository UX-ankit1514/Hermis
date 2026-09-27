from __future__ import annotations

import getpass
import re

from .keychain import store_secret
from .telegram import TELEGRAM_KEYCHAIN_ACCOUNT, TELEGRAM_KEYCHAIN_SERVICE


BOT_TOKEN_PATTERN = re.compile(r"^[0-9]{6,}:[A-Za-z0-9_-]{20,}$")


def validate_bot_token(value: str) -> bool:
    return bool(BOT_TOKEN_PATTERN.fullmatch(value))


def main() -> int:
    print("Enter the Telegram bot token. Input is hidden and stored directly in macOS Keychain.")
    token = getpass.getpass("Bot token: ")
    if not validate_bot_token(token):
        print("Invalid Telegram bot token format; nothing was stored.")
        return 2
    store_secret(TELEGRAM_KEYCHAIN_SERVICE, TELEGRAM_KEYCHAIN_ACCOUNT, token)
    del token
    print("Telegram bot token stored in macOS Keychain.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

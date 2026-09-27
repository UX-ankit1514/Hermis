import http.client
import unittest
from unittest.mock import patch

from keka_automation.telegram import TelegramAPI, TelegramError, delete_keychain_secret, process_update_batch
from keka_automation.telegram_commands import CommandRouter


class FakeAPI:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


class FakeOperations:
    def status(self):
        return "safe status"


class TelegramBatchTests(unittest.TestCase):
    def test_batch_replies_only_to_authorized_private_chat_and_advances_offset(self):
        api = FakeAPI()
        router = CommandRouter(123, 456, FakeOperations())
        updates = [
            {"update_id": 10, "message": {"text": "/status", "from": {"id": 999}, "chat": {"id": 999, "type": "private"}}},
            {"update_id": 11, "message": {"text": "/status", "from": {"id": 123}, "chat": {"id": 456, "type": "private"}}},
        ]
        offset = process_update_batch(api, router, updates)
        self.assertEqual(offset, 12)
        self.assertEqual(api.sent, [(456, "safe status")])

    def test_empty_batch_preserves_offset(self):
        self.assertEqual(process_update_batch(FakeAPI(), CommandRouter(1, 2, FakeOperations()), [], 42), 42)

    def test_keychain_deletion_uses_identifiers_without_secret_value(self):
        calls = []
        delete_keychain_secret("service", "account", runner=lambda args: calls.append(args) or 0)
        self.assertEqual(
            calls,
            [["/usr/bin/security", "delete-generic-password", "-s", "service", "-a", "account"]],
        )

    def test_remote_disconnect_becomes_retryable_telegram_error(self):
        api = TelegramAPI("123456:abcdefghijklmnopqrstuvwxyz")
        with patch(
            "keka_automation.telegram.urllib.request.urlopen",
            side_effect=http.client.RemoteDisconnected("remote closed"),
        ):
            try:
                api.get_updates(None, timeout=1)
            except Exception as exc:
                self.assertIsInstance(exc, TelegramError)
            else:
                self.fail("Remote disconnect should raise TelegramError")


if __name__ == "__main__":
    unittest.main()

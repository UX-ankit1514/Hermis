import os
import secrets
import unittest

from keka_automation.keychain import delete_secret, read_secret, store_secret
from keka_automation.secure_setup import validate_bot_token


@unittest.skipUnless(os.uname().sysname == "Darwin", "macOS Keychain test")
class KeychainTests(unittest.TestCase):
    def test_secret_round_trip_uses_native_keychain(self):
        service = f"com.hermes.keka-attendance.test.{os.getpid()}"
        account = "unit-test"
        value = secrets.token_urlsafe(24)
        try:
            store_secret(service, account, value)
            self.assertEqual(read_secret(service, account), value)
        finally:
            delete_secret(service, account)

    def test_empty_secret_is_rejected(self):
        with self.assertRaises(ValueError):
            store_secret("com.hermes.keka-attendance.test", "unit-test", "")

    def test_bot_token_format_validation_does_not_repair_input(self):
        self.assertTrue(validate_bot_token("123456789:abcdefghijklmnopqrstuvwxyzABCDE"))
        self.assertFalse(validate_bot_token(" 123456789:abcdefghijklmnopqrstuvwxyzABCDE "))
        self.assertFalse(validate_bot_token("not-a-token"))


if __name__ == "__main__":
    unittest.main()

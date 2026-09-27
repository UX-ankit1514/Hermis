import unittest

from keka_automation.cli import build_parser


class CliTests(unittest.TestCase):
    def test_parser_accepts_clock_in_dispatch_command(self):
        parsed = build_parser().parse_args(["clockin-dispatch"])
        self.assertEqual(parsed.command, "clockin-dispatch")


if __name__ == "__main__":
    unittest.main()

import io
import unittest
from unittest import mock

from parley import cli


class CLITests(unittest.TestCase):
    def test_relay_command_is_retired_with_desktop_direction(self):
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            code = cli.main(["parley", "relay"])

        self.assertEqual(code, 2)
        rendered = output.getvalue()
        self.assertIn("interactive relay CLI has been retired", rendered)
        self.assertIn("parley app", rendered)

    def test_no_args_help_describes_desktop_as_supported_relay_surface(self):
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            code = cli.main(["parley"])

        self.assertEqual(code, 0)
        rendered = output.getvalue()
        self.assertIn("parley app", rendered)
        self.assertIn("parley relay", rendered)
        self.assertIn("retired", rendered)


if __name__ == "__main__":
    unittest.main()

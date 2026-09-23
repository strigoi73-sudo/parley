import os
import unittest
from unittest import mock

from parley import cli
from parley.gui import (
    _display_reply,
    _result_ok,
    _tab_label,
)


class ParleyGUIHelperTests(unittest.TestCase):
    def test_display_reply_removes_transport_markers(self):
        self.assertEqual(
            _display_reply(
                "A REPLY: Useful answer\n\nA REPLY END",
                "A",
            ),
            "Useful answer",
        )
        self.assertEqual(
            _display_reply(
                "B REPLY: Counterpoint\n\nB REPLY END",
                "B",
            ),
            "Counterpoint",
        )

    def test_display_reply_preserves_reset(self):
        self.assertEqual(_display_reply("RESET CHAT", "A"), "RESET CHAT")

    def test_result_ok_requires_completed_non_error_response(self):
        self.assertTrue(_result_ok({
            "response_complete": True,
            "response_text": "A REPLY: ok\nA REPLY END",
        }))
        self.assertFalse(_result_ok({
            "response_complete": False,
        }))
        self.assertFalse(_result_ok({
            "response_complete": True,
            "error": "failed",
        }))

    def test_tab_label_is_human_readable_and_disambiguated(self):
        label = _tab_label({
            "title": "Peggy",
            "id": "ABCDEF1234567890",
        })
        self.assertIn("Peggy", label)
        self.assertTrue(label.endswith("34567890"))


class ParleyGUICLITests(unittest.TestCase):
    def test_gui_command_uses_live_mode_and_launches_ui(self):
        old = os.environ.pop("PARLEY_CONNECTION_MODE", None)
        try:
            with mock.patch(
                "parley.gui.launch",
                return_value=0,
            ) as launch:
                code = cli.main(["parley", "gui"])

            self.assertEqual(code, 0)
            launch.assert_called_once_with()
            self.assertEqual(
                os.environ.get("PARLEY_CONNECTION_MODE"),
                "live",
            )
        finally:
            if old is None:
                os.environ.pop("PARLEY_CONNECTION_MODE", None)
            else:
                os.environ["PARLEY_CONNECTION_MODE"] = old


if __name__ == "__main__":
    unittest.main()

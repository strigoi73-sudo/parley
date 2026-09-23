import os
import threading
import unittest
from unittest import mock

from parley import cli
from parley.gui import (
    ParleyApp,
    _display_reply,
    _result_ok,
    _tab_label,
)


class ParleyGUIHelperTests(unittest.TestCase):
    def test_initialize_both_uses_one_serial_bootstrap_worker(self):
        app = mock.Mock(spec=ParleyApp)
        app._selected_tab.side_effect = lambda label: {"id": label}
        app._operation_stop = threading.Event()
        workers = []
        app._run_worker.side_effect = lambda fn, name: workers.append((fn, name))

        ParleyApp.initialize_both(app)

        self.assertEqual(app._init_pending, {"A", "B"})
        self.assertEqual(
            [(call.args[0], call.args[1])
             for call in app._set_protocol_status.call_args_list],
            [("A", "Queued"), ("B", "Queued")],
        )
        self.assertEqual(len(workers), 1)
        self.assertEqual(workers[0][1], "parley-gui-initialize")

        with mock.patch(
            "parley.gui.workflows.initialize_parley_pair",
            return_value={"ok": True, "participants": {}},
        ) as initialize:
            workers[0][0]()

        initialize.assert_called_once()
        args, kwargs = initialize.call_args
        self.assertEqual(args, ("A", "B"))
        self.assertEqual(kwargs["should_stop"], app._operation_stop.is_set)
        self.assertTrue(callable(kwargs["progress"]))
        app._post.assert_called()

    def test_pair_finished_marks_both_ready_only_after_pair_success(self):
        app = ParleyApp.__new__(ParleyApp)
        app.ready = {"A": False, "B": False}
        app._init_pending = {"A", "B"}
        app._init_errors = {}
        app._set_busy = mock.Mock()
        app._set_protocol_status = mock.Mock()
        app._activity = mock.Mock()
        app._set_phase = mock.Mock()
        app._update_controls = mock.Mock()
        app._append_transcript = mock.Mock()
        app.a_name = mock.Mock()
        app.a_name.get.return_value = "Chat A"
        app.b_name = mock.Mock()
        app.b_name.get.return_value = "Chat B"

        result = {
            "ok": True,
            "participants": {
                "A": {
                    "activation": {
                        "response_complete": True,
                        "response_text": "A REPLY: Ready\nA REPLY END",
                    },
                },
                "B": {
                    "activation": {
                        "response_complete": True,
                        "response_text": "B REPLY: Ready\nB REPLY END",
                    },
                },
            },
        }

        app._protocol_pair_finished(result)

        self.assertEqual(app.ready, {"A": True, "B": True})
        self.assertEqual(app._init_pending, set())
        app._set_busy.assert_called_once_with(False)
        app._set_phase.assert_called_once_with("Ready")
        self.assertEqual(app._append_transcript.call_count, 2)

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

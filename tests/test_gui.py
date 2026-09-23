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
    def test_initialization_workers_submit_independently(self):
        app = mock.Mock(spec=ParleyApp)
        app._selected_tab.side_effect = lambda label: {"id": label}
        app._operation_stop = threading.Event()
        workers = []
        app._run_worker.side_effect = lambda fn, name: workers.append(fn)
        ParleyApp.initialize_both(app)
        self.assertEqual(app._init_pending, {"A", "B"})
        self.assertEqual(
            [(call.args[0], call.args[1]) for call in app._set_protocol_status.call_args_list],
            [("A", "Initializing…"), ("B", "Initializing…")],
        )
        with mock.patch("parley.gui.workflows.send_and_wait", return_value={}) as send:
            # B is runnable without executing or completing A first.
            for worker in reversed(workers):
                worker()
        self.assertEqual([call.args for call in send.call_args_list], [
            ("B", "INITIALIZE PARLEY TEST CHAT B"),
            ("A", "INITIALIZE PARLEY TEST CHAT A"),
        ])
        for call in send.call_args_list:
            self.assertIsNone(call.kwargs["wait_timeout_ms"])
            self.assertEqual(call.kwargs["should_stop"], app._operation_stop.is_set)

    def test_start_requires_both_initialization_results_in_either_order(self):
        for order in (("A", "B"), ("B", "A")):
            with self.subTest(order=order):
                app = ParleyApp.__new__(ParleyApp)
                app.ready = {"A": False, "B": False}
                app._init_pending = {"A", "B"}
                app._init_errors = {}
                app._operation_stop = threading.Event()
                app._busy = True
                app.session = None
                app._selected_tab = lambda label: {"id": label}
                for name in ("refresh", "init", "start", "pause", "resume", "stop",
                             "extend", "finish", "reset"):
                    setattr(app, name + "_button", mock.Mock())
                app._set_protocol_status = mock.Mock()
                app._activity = mock.Mock()
                app._set_phase = mock.Mock()
                app.a_name = mock.Mock()
                app.a_name.get.return_value = "Chat A"
                app.b_name = mock.Mock()
                app.b_name.get.return_value = "Chat B"
                app._append_transcript = mock.Mock()
                result = {"response_complete": True, "response_text": "Initialized"}
                app._protocol_finished(order[0], result)
                app._append_transcript.assert_called_once_with(
                    order[0], f"Chat {order[0]} · Initialization", "Initialized",
                )
                app.start_button.configure.assert_called_with(state="disabled")
                self.assertTrue(app._busy)
                self.assertTrue(app.ready[order[0]])
                self.assertFalse(app.ready[order[1]])
                app._protocol_finished(order[1], result)
                app.start_button.configure.assert_called_with(state="normal")
                self.assertFalse(app._busy)

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

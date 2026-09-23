import unittest
from unittest import mock

from parley import workflows


class PrepareFreshParleySessionTests(unittest.TestCase):
    def setUp(self):
        self.fresh = {
            "ok": True,
            "A": {
                "id": "TAB-A",
                "title": "Fresh ChatGPT A",
                "url": "https://chatgpt.com/c/a",
            },
            "B": {
                "id": "TAB-B",
                "title": "Fresh ChatGPT B",
                "url": "https://chatgpt.com/c/b",
            },
        }

    def test_happy_path_uses_proven_sequence_and_timeout_free_model_waits(self):
        progress = []
        calls = []

        def create_pair(**kwargs):
            calls.append("fresh")
            callback = kwargs.get("progress")
            if callback:
                callback({
                    "label": "A",
                    "stage": "initial_prompt",
                    "status": "complete",
                })
            return self.fresh

        def initialize(tab_a, tab_b, **kwargs):
            calls.append(("protocols", tab_a, tab_b))
            self.assertIsNone(kwargs["wait_timeout_ms"])
            self.assertTrue(callable(kwargs["should_stop"]))
            callback = kwargs.get("progress")
            if callback:
                callback({
                    "label": "A",
                    "stage": "ready",
                    "status": "complete",
                })
            return {
                "ok": True,
                "response_complete": True,
                "stage": "ready",
                "participants": {},
            }

        def send(tab_id, text, **kwargs):
            calls.append(("prompt", tab_id, text))
            self.assertEqual(tab_id, "TAB-A")
            self.assertEqual(text, "Discuss culture.")
            self.assertIsNone(kwargs["wait_timeout_ms"])
            self.assertEqual(
                kwargs["expected_reply_prefix"],
                "A REPLY:",
            )
            self.assertEqual(
                kwargs["expected_reply_suffix"],
                "A REPLY END",
            )
            self.assertTrue(callable(kwargs["should_stop"]))
            return {
                "response_complete": True,
                "response_text": (
                    "A REPLY: Initial answer.\n\n"
                    "A REPLY END"
                ),
            }

        with mock.patch.object(
            workflows,
            "create_fresh_chatgpt_pair",
            side_effect=create_pair,
        ), mock.patch.object(
            workflows,
            "initialize_parley_pair",
            side_effect=initialize,
        ), mock.patch.object(
            workflows,
            "send_and_wait",
            side_effect=send,
        ):
            result = workflows.prepare_fresh_parley_session(
                "  Discuss culture.  ",
                should_stop=lambda: False,
                progress=progress.append,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["A"]["id"], "TAB-A")
        self.assertEqual(result["B"]["id"], "TAB-B")
        self.assertEqual(
            result["initial_response_text"],
            "A REPLY: Initial answer.\n\nA REPLY END",
        )
        self.assertEqual(
            calls,
            [
                "fresh",
                ("protocols", "TAB-A", "TAB-B"),
                ("prompt", "TAB-A", "Discuss culture."),
            ],
        )
        self.assertIn(
            {"stage": "fresh_chats", "status": "starting"},
            progress,
        )
        self.assertIn(
            {"stage": "protocols", "status": "complete"},
            progress,
        )

    def test_protocol_failure_stops_before_initial_session_prompt(self):
        failure = {
            "ok": False,
            "error": "protocol_failed",
            "stage": "protocol_ack",
            "participant": "B",
        }

        with mock.patch.object(
            workflows,
            "create_fresh_chatgpt_pair",
            return_value=self.fresh,
        ), mock.patch.object(
            workflows,
            "initialize_parley_pair",
            return_value=failure,
        ), mock.patch.object(
            workflows,
            "send_and_wait",
        ) as send:
            result = workflows.prepare_fresh_parley_session(
                "Discuss culture.",
                should_stop=lambda: False,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "protocol_failed")
        self.assertEqual(result["stage"], "protocol_ack")
        self.assertEqual(result["participant"], "B")
        send.assert_not_called()

    def test_stop_after_fresh_chat_creation_skips_protocol_bootstrap(self):
        with mock.patch.object(
            workflows,
            "create_fresh_chatgpt_pair",
            return_value=self.fresh,
        ), mock.patch.object(
            workflows,
            "initialize_parley_pair",
        ) as initialize:
            result = workflows.prepare_fresh_parley_session(
                "Discuss culture.",
                should_stop=lambda: True,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "chatgpt_wait_stopped")
        self.assertEqual(result["stage"], "fresh_chats")
        initialize.assert_not_called()

    def test_blank_prompt_fails_before_browser_work(self):
        with mock.patch.object(
            workflows,
            "create_fresh_chatgpt_pair",
        ) as create:
            result = workflows.prepare_fresh_parley_session("   ")

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error"],
            "parley_initial_prompt_required",
        )
        create.assert_not_called()


if __name__ == "__main__":
    unittest.main()

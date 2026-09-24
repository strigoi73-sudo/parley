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
        self.assertIs(result["fresh"], self.fresh)
        self.assertNotIn("sources", result)
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
        self.assertNotIn("fresh", result)
        self.assertNotIn("sources", result)
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

    def test_fresh_wrapper_delegates_resolved_startup_without_sources(self):
        common = {
            "ok": True,
            "stage": "ready",
            "A": self.fresh["A"],
            "B": self.fresh["B"],
            "protocols": {"ok": True},
            "initial_prompt": "Discuss culture.",
            "initial_response": {"response_complete": True},
            "initial_response_text": "A REPLY: Ready.\n\nA REPLY END",
        }

        with mock.patch.object(
            workflows,
            "create_fresh_chatgpt_pair",
            return_value=self.fresh,
        ), mock.patch.object(
            workflows,
            "_prepare_resolved_parley_session",
            return_value=common,
        ) as prepare:
            result = workflows.prepare_fresh_parley_session(
                "  Discuss culture.  ",
                should_stop=lambda: False,
                progress=mock.sentinel.progress,
            )

        prepare.assert_called_once_with(
            "Discuss culture.",
            self.fresh["A"],
            self.fresh["B"],
            should_stop=mock.ANY,
            progress=mock.sentinel.progress,
        )
        self.assertIs(result["fresh"], self.fresh)
        self.assertNotIn("sources", result)


class PrepareMixedParleySessionTests(unittest.TestCase):
    def _run_case(self, source_a, source_b):
        existing = {
            "A": {
                "id": "EXISTING-A",
                "title": "Existing A",
                "url": "https://chatgpt.com/c/existing-a",
            },
            "B": {
                "id": "EXISTING-B",
                "title": "Existing B",
                "url": "https://chatgpt.com/c/existing-b",
            },
        }
        specs = {}
        for label, source in (("A", source_a), ("B", source_b)):
            if source == "fresh":
                specs[label] = {"source": "fresh"}
            else:
                specs[label] = {
                    "source": "existing",
                    "tab": existing[label],
                }

        created = []

        def create(label, **kwargs):
            created.append(label)
            return {
                "ok": True,
                "participant": label,
                "tab": {
                    "id": "FRESH-" + label,
                    "title": "Fresh ChatGPT " + label,
                    "url": "https://chatgpt.com/c/fresh-" + label.lower(),
                    "source": "fresh",
                },
            }

        with mock.patch.object(
            workflows,
            "create_fresh_chatgpt_participant",
            side_effect=create,
        ), mock.patch.object(
            workflows,
            "initialize_parley_pair",
            return_value={
                "ok": True,
                "response_complete": True,
                "stage": "ready",
                "participants": {},
            },
        ) as initialize, mock.patch.object(
            workflows,
            "send_and_wait",
            return_value={
                "response_complete": True,
                "response_text": "A REPLY: Ready.\n\nA REPLY END",
            },
        ) as send:
            result = workflows.prepare_parley_session(
                "Discuss culture.",
                specs,
                should_stop=lambda: False,
            )

        expected_a = (
            "FRESH-A" if source_a == "fresh" else "EXISTING-A"
        )
        expected_b = (
            "FRESH-B" if source_b == "fresh" else "EXISTING-B"
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["A"]["id"], expected_a)
        self.assertEqual(result["B"]["id"], expected_b)
        self.assertEqual(
            created,
            [
                label
                for label, source in (("A", source_a), ("B", source_b))
                if source == "fresh"
            ],
        )
        initialize.assert_called_once_with(
            expected_a,
            expected_b,
            wait_timeout_ms=None,
            should_stop=mock.ANY,
            progress=None,
        )
        send.assert_called_once()
        return result

    def test_all_four_participant_source_combinations(self):
        for source_a, source_b in (
            ("existing", "existing"),
            ("existing", "fresh"),
            ("fresh", "existing"),
            ("fresh", "fresh"),
        ):
            with self.subTest(A=source_a, B=source_b):
                result = self._run_case(source_a, source_b)
                self.assertEqual(
                    result["sources"],
                    {"A": source_a, "B": source_b},
                )

    def test_general_wrapper_passes_sources_to_resolved_startup(self):
        specs = {
            "A": {
                "source": "existing",
                "tab": {
                    "id": "EXISTING-A",
                    "title": "Existing A",
                    "url": "https://chatgpt.com/c/a",
                },
            },
            "B": {
                "source": "existing",
                "tab": {
                    "id": "EXISTING-B",
                    "title": "Existing B",
                    "url": "https://chatgpt.com/c/b",
                },
            },
        }

        with mock.patch.object(
            workflows,
            "_prepare_resolved_parley_session",
            return_value={"ok": True, "stage": "ready"},
        ) as prepare:
            result = workflows.prepare_parley_session(
                "Discuss culture.",
                specs,
                should_stop=lambda: False,
                progress=mock.sentinel.progress,
            )

        self.assertTrue(result["ok"])
        prepare.assert_called_once()
        args = prepare.call_args.args
        kwargs = prepare.call_args.kwargs
        self.assertEqual(args[0], "Discuss culture.")
        self.assertEqual(args[1]["id"], "EXISTING-A")
        self.assertEqual(args[2]["id"], "EXISTING-B")
        self.assertEqual(
            kwargs["sources"],
            {"A": "existing", "B": "existing"},
        )
        self.assertIs(kwargs["progress"], mock.sentinel.progress)
        self.assertTrue(callable(kwargs["should_stop"]))

    def test_same_existing_tab_is_rejected_before_protocol_bootstrap(self):
        tab = {
            "id": "SAME",
            "title": "Same tab",
            "url": "https://chatgpt.com/c/same",
        }
        specs = {
            "A": {"source": "existing", "tab": tab},
            "B": {"source": "existing", "tab": tab},
        }

        with mock.patch.object(
            workflows,
            "initialize_parley_pair",
        ) as initialize:
            result = workflows.prepare_parley_session(
                "Discuss culture.",
                specs,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "parley_same_tab")
        initialize.assert_not_called()


if __name__ == "__main__":
    unittest.main()

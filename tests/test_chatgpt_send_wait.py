import unittest
from unittest import mock

from parley import workflows
from parley.adapters.chatgpt import ChatGPTAdapter


class FakeSocket:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def runtime_value(value):
    return {"result": {"value": value}}


class ChatGPTSendAndWaitTests(unittest.TestCase):
    def setUp(self):
        self.adapter = ChatGPTAdapter()

    def run_transaction(
        self,
        states,
        timeout_ms=3000,
        silence_ms=200,
        expected_reply_prefix=None,
        expected_reply_suffix=None,
    ):
        ws = FakeSocket()
        clock = FakeClock()
        state_queue = list(states)
        calls = []

        def fake_cdp_send(_ws, method, params=None, timeout=10):
            calls.append((method, params))
            if method == "Input.insertText":
                return {}

            expression = (params or {}).get("expression")
            if expression == self.adapter.prepare_composer_js:
                return runtime_value({
                    "ok": True,
                    "source": "chatgpt-strict",
                })
            if expression == self.adapter.click_send_js:
                return runtime_value({
                    "ok": True,
                    "source": "chatgpt-strict",
                    "method": "chatgpt-send-button",
                })
            if expression == self.adapter.turn_state_js:
                if len(state_queue) > 1:
                    value = state_queue.pop(0)
                else:
                    value = state_queue[0]
                return runtime_value(value)

            raise AssertionError("unexpected CDP call: %r" % ((method, params),))

        with mock.patch.object(
            workflows,
            "cdp_connect",
            return_value=ws,
        ) as connect, mock.patch.object(
            workflows,
            "cdp_send",
            side_effect=fake_cdp_send,
        ), mock.patch.object(
            workflows.time,
            "monotonic",
            side_effect=clock.monotonic,
        ), mock.patch.object(
            workflows.time,
            "sleep",
            side_effect=clock.sleep,
        ):
            result = workflows._chatgpt_send_and_wait(
                "tab-a",
                "hello",
                wait_timeout_ms=timeout_ms,
                silence_ms=silence_ms,
                adapter=self.adapter,
                expected_reply_prefix=expected_reply_prefix,
                expected_reply_suffix=expected_reply_suffix,
            )

        return result, ws, connect, calls

    def test_completed_new_turn_uses_one_connection(self):
        old = {
            "ok": True,
            "source": "chatgpt-strict",
            "assistant_count": 1,
            "user_count": 1,
            "assistant": {
                "text": "old reply",
                "turn_id": "assistant-old",
                "turn_index": 0,
                "hasStreaming": False,
            },
            "hasStopButton": False,
        }
        submitted = {
            **old,
            "user_count": 2,
        }
        started = {
            "ok": True,
            "source": "chatgpt-strict",
            "assistant_count": 2,
            "user_count": 2,
            "assistant": {
                "text": "new reply",
                "turn_id": "assistant-new",
                "turn_index": 1,
                "hasStreaming": True,
            },
            "hasStopButton": True,
        }
        finished = {
            **started,
            "assistant": {
                **started["assistant"],
                "hasStreaming": False,
            },
            "hasStopButton": False,
        }

        result, ws, connect, _ = self.run_transaction(
            [old, submitted, started, finished, finished],
            silence_ms=200,
        )

        connect.assert_called_once_with("tab-a")
        self.assertTrue(ws.closed)
        self.assertTrue(result["response_complete"])
        self.assertEqual(result["response_text"], "new reply")
        self.assertEqual(result["response_turn_id"], "assistant-new")
        self.assertEqual(result["response_turn_index"], 1)
        self.assertEqual(result["pre_msg_count"], 1)
        self.assertEqual(result["pre_user_count"], 1)
        self.assertEqual(result["post_user_count"], 2)

    def test_old_assistant_turn_cannot_satisfy_wait(self):
        old = {
            "ok": True,
            "source": "chatgpt-strict",
            "assistant_count": 1,
            "user_count": 1,
            "assistant": {
                "text": "same old reply",
                "turn_id": "assistant-old",
                "turn_index": 0,
                "hasStreaming": False,
            },
            "hasStopButton": False,
        }
        submitted = {
            **old,
            "user_count": 2,
        }

        result, _, _, _ = self.run_transaction(
            [old, submitted, submitted],
            timeout_ms=700,
            silence_ms=100,
        )

        self.assertFalse(result["response_complete"])
        self.assertEqual(
            result["error"],
            "chatgpt_new_assistant_turn_timeout",
        )
        self.assertEqual(result["stage"], "wait_for_response")

    def test_same_user_id_with_exact_new_text_still_proves_submission(self):
        pre = {
            "ok": True,
            "source": "chatgpt-strict",
            "assistant_count": 1,
            "user_count": 1,
            "assistant": {
                "text": "old reply",
                "turn_id": "assistant-old",
                "turn_index": 0,
                "hasStreaming": False,
            },
            "user": {
                "text": "old prompt",
                "turn_id": "user-reused",
                "turn_index": 0,
            },
            "hasStopButton": False,
        }
        submitted = {
            **pre,
            "user": {
                "text": "hello",
                "turn_id": "user-reused",
                "turn_index": 0,
            },
        }
        reply = {
            **submitted,
            "assistant_count": 2,
            "assistant": {
                "text": "new reply",
                "turn_id": "assistant-new",
                "turn_index": 1,
                "hasStreaming": False,
            },
        }

        result, _, _, _ = self.run_transaction(
            [pre, submitted, reply, reply, reply],
            silence_ms=200,
        )

        self.assertTrue(result["response_complete"])
        self.assertEqual(result["response_text"], "new reply")

    def test_submission_must_be_proven_by_new_user_turn(self):
        pre = {
            "ok": True,
            "source": "chatgpt-strict",
            "assistant_count": 0,
            "user_count": 0,
            "assistant": None,
            "hasStopButton": False,
        }

        result, _, _, _ = self.run_transaction(
            [pre, pre],
            timeout_ms=500,
            silence_ms=100,
        )

        self.assertFalse(result["response_complete"])
        self.assertEqual(
            result["error"],
            "chatgpt_submission_not_verified",
        )
        self.assertEqual(result["stage"], "verify_submission")

    def test_blank_chat_accepts_first_assistant_turn(self):
        pre = {
            "ok": True,
            "source": "chatgpt-strict",
            "assistant_count": 0,
            "user_count": 0,
            "assistant": None,
            "hasStopButton": False,
        }
        submitted = {
            **pre,
            "user_count": 1,
        }
        reply = {
            "ok": True,
            "source": "chatgpt-strict",
            "assistant_count": 1,
            "user_count": 1,
            "assistant": {
                "text": "first reply",
                "turn_id": None,
                "turn_index": 0,
                "hasStreaming": False,
            },
            "hasStopButton": False,
        }

        result, _, _, _ = self.run_transaction(
            [pre, submitted, reply, reply, reply],
            silence_ms=200,
        )

        self.assertTrue(result["response_complete"])
        self.assertEqual(result["response_text"], "first reply")
        self.assertEqual(result["response_turn_index"], 0)

    def test_transient_thinking_turn_may_be_replaced_by_final_answer(self):
        old = {
            "ok": True,
            "source": "chatgpt-strict",
            "assistant_count": 1,
            "user_count": 1,
            "assistant": {
                "text": "old reply",
                "turn_id": "assistant-old",
                "turn_index": 0,
                "hasStreaming": False,
            },
            "hasStopButton": False,
        }
        submitted = {
            **old,
            "user_count": 2,
        }
        thinking = {
            "ok": True,
            "source": "chatgpt-strict",
            "assistant_count": 2,
            "user_count": 2,
            "assistant": {
                "text": "Thinking",
                "turn_id": "assistant-thinking",
                "turn_index": 1,
                "hasStreaming": True,
            },
            "hasStopButton": True,
        }
        final = {
            "ok": True,
            "source": "chatgpt-strict",
            "assistant_count": 2,
            "user_count": 2,
            "assistant": {
                "text": "real final reply",
                "turn_id": "assistant-final",
                "turn_index": 1,
                "hasStreaming": False,
            },
            "hasStopButton": False,
        }

        result, _, _, _ = self.run_transaction(
            [old, submitted, thinking, final, final],
            silence_ms=200,
        )

        self.assertTrue(result["response_complete"])
        self.assertEqual(result["response_text"], "real final reply")
        self.assertEqual(result["response_turn_id"], "assistant-final")
        self.assertEqual(result["response_turn_index"], 1)
        self.assertEqual(result["response_candidate_replacements"], 1)

    def test_exact_reset_message_interrupts_marked_response_cleanly(self):
        old = {
            "ok": True,
            "source": "chatgpt-strict",
            "assistant_count": 1,
            "user_count": 1,
            "assistant": {
                "text": "A REPLY: old answer\n\nA REPLY END",
                "turn_id": "assistant-old",
                "turn_index": 0,
                "hasStreaming": False,
            },
            "user": {
                "text": "old prompt",
                "turn_id": "user-old",
                "turn_index": 0,
            },
            "hasStopButton": False,
        }
        submitted = {
            **old,
            "user_count": 2,
            "user": {
                "text": "hello",
                "turn_id": "user-submitted",
                "turn_index": 1,
            },
        }
        started = {
            **submitted,
            "assistant_count": 2,
            "assistant": {
                "text": "B REPLY: partial",
                "turn_id": "assistant-partial",
                "turn_index": 1,
                "hasStreaming": True,
            },
            "hasStopButton": True,
        }
        reset_user = {
            **started,
            "user_count": 3,
            "user": {
                "text": "RESET CHAT",
                "turn_id": "user-reset",
                "turn_index": 2,
            },
        }
        reset_reply = {
            **reset_user,
            "assistant_count": 3,
            "assistant": {
                "text": "RESET CHAT",
                "turn_id": "assistant-reset",
                "turn_index": 2,
                "hasStreaming": False,
            },
            "hasStopButton": False,
        }

        result, _, _, _ = self.run_transaction(
            [
                old,
                submitted,
                started,
                reset_user,
                reset_reply,
                reset_reply,
            ],
            silence_ms=200,
            expected_reply_prefix="B REPLY:",
            expected_reply_suffix="B REPLY END",
        )

        self.assertTrue(result["response_complete"])
        self.assertEqual(result["response_text"], "RESET CHAT")
        self.assertTrue(result["human_reset_requested"])

    def test_new_user_turn_during_response_still_fails_closed(self):
        pre = {
            "ok": True,
            "source": "chatgpt-strict",
            "assistant_count": 0,
            "user_count": 0,
            "assistant": None,
            "hasStopButton": False,
        }
        submitted = {
            **pre,
            "user_count": 1,
        }
        started = {
            "ok": True,
            "source": "chatgpt-strict",
            "assistant_count": 1,
            "user_count": 1,
            "assistant": {
                "text": "partial reply",
                "turn_id": "assistant-new",
                "turn_index": 0,
                "hasStreaming": True,
            },
            "hasStopButton": True,
        }
        extra_user = {
            **started,
            "user_count": 2,
        }

        result, _, _, _ = self.run_transaction(
            [pre, submitted, started, extra_user],
            silence_ms=100,
        )

        self.assertFalse(result["response_complete"])
        self.assertEqual(
            result["error"],
            "chatgpt_user_turn_changed_during_response",
        )
        self.assertEqual(result["stage"], "track_response")

    def test_user_count_rerender_keeps_same_submitted_user_turn(self):
        old = {
            "ok": True,
            "source": "chatgpt-strict",
            "assistant_count": 1,
            "user_count": 1,
            "assistant": {
                "text": "old reply",
                "turn_id": "assistant-old",
                "turn_index": 0,
                "hasStreaming": False,
            },
            "user": {
                "text": "old prompt",
                "turn_id": "user-old",
                "turn_index": 0,
            },
            "hasStopButton": False,
        }
        submitted = {
            **old,
            "user_count": 3,
            "user": {
                "text": "hello",
                "turn_id": "user-submitted",
                "turn_index": 2,
            },
        }
        started = {
            "ok": True,
            "source": "chatgpt-strict",
            "assistant_count": 2,
            "user_count": 3,
            "assistant": {
                "text": "partial",
                "turn_id": "assistant-new",
                "turn_index": 1,
                "hasStreaming": True,
            },
            "user": {
                "text": "hello",
                "turn_id": "user-submitted",
                "turn_index": 2,
            },
            "hasStopButton": True,
        }
        rerendered = {
            **started,
            "user_count": 2,
            "user": {
                "text": "hello",
                "turn_id": "user-rerendered",
                "turn_index": 1,
            },
            "assistant": {
                "text": "final reply",
                "turn_id": "assistant-new",
                "turn_index": 1,
                "hasStreaming": False,
            },
            "hasStopButton": False,
        }

        result, _, _, _ = self.run_transaction(
            [
                old,
                submitted,
                started,
                rerendered,
                rerendered,
            ],
            silence_ms=200,
        )

        self.assertTrue(result["response_complete"])
        self.assertEqual(result["response_text"], "final reply")
        self.assertEqual(result["post_user_count"], 2)
        self.assertEqual(
            result["submitted_user_turn_id"],
            "user-submitted",
        )

    def test_different_user_turn_id_fails_even_if_count_is_unchanged(self):
        pre = {
            "ok": True,
            "source": "chatgpt-strict",
            "assistant_count": 0,
            "user_count": 1,
            "assistant": None,
            "user": {
                "text": "old prompt",
                "turn_id": "user-old",
                "turn_index": 0,
            },
            "hasStopButton": False,
        }
        submitted = {
            **pre,
            "user_count": 2,
            "user": {
                "text": "hello",
                "turn_id": "user-submitted",
                "turn_index": 1,
            },
        }
        started = {
            "ok": True,
            "source": "chatgpt-strict",
            "assistant_count": 1,
            "user_count": 2,
            "assistant": {
                "text": "partial",
                "turn_id": "assistant-new",
                "turn_index": 0,
                "hasStreaming": True,
            },
            "user": {
                "text": "hello",
                "turn_id": "user-submitted",
                "turn_index": 1,
            },
            "hasStopButton": True,
        }
        changed_user = {
            **started,
            "user": {
                "text": "another message",
                "turn_id": "user-other",
                "turn_index": 1,
            },
        }

        result, _, _, _ = self.run_transaction(
            [pre, submitted, started, changed_user],
            silence_ms=100,
        )

        self.assertFalse(result["response_complete"])
        self.assertEqual(
            result["error"],
            "chatgpt_user_turn_changed_during_response",
        )
        self.assertEqual(
            result["expected_user_turn_id"],
            "user-submitted",
        )
        self.assertEqual(
            result["current_user_turn_id"],
            "user-other",
        )

    def test_marked_reply_ignores_thinking_and_temporary_old_turn(self):
        old = {
            "ok": True,
            "source": "chatgpt-strict",
            "assistant_count": 1,
            "user_count": 1,
            "assistant": {
                "text": "A REPLY: old answer",
                "turn_id": "assistant-old",
                "turn_index": 0,
                "hasStreaming": False,
            },
            "user": {
                "text": "old prompt",
                "turn_id": "user-old",
                "turn_index": 0,
            },
            "hasStopButton": False,
        }
        submitted = {
            **old,
            "user_count": 2,
            "user": {
                "text": "hello",
                "turn_id": "user-submitted",
                "turn_index": 1,
            },
        }
        thinking = {
            **submitted,
            "assistant_count": 2,
            "assistant": {
                "text": "Thinking",
                "turn_id": "assistant-thinking",
                "turn_index": 1,
                "hasStreaming": True,
            },
            "hasStopButton": True,
        }
        rollback = {
            **submitted,
            "assistant_count": 1,
            "assistant": old["assistant"],
            "hasStopButton": False,
        }
        partial_marked = {
            **submitted,
            "assistant_count": 2,
            "assistant": {
                "text": "B REPLY: final answer is still being rendered",
                "turn_id": "assistant-final",
                "turn_index": 1,
                "hasStreaming": False,
            },
            "hasStopButton": False,
        }
        marker_not_terminal = {
            **submitted,
            "assistant_count": 2,
            "assistant": {
                "text": (
                    "B REPLY: answer body\n\n"
                    "B REPLY END\nextra streamed text"
                ),
                "turn_id": "assistant-final",
                "turn_index": 1,
                "hasStreaming": False,
            },
            "hasStopButton": False,
        }
        final = {
            **submitted,
            "assistant_count": 2,
            "assistant": {
                "text": (
                    "B REPLY: final answer is complete\n\n"
                    "B REPLY END"
                ),
                "turn_id": "assistant-final",
                "turn_index": 1,
                "hasStreaming": False,
            },
            "hasStopButton": False,
        }

        result, _, _, _ = self.run_transaction(
            [
                old,
                submitted,
                thinking,
                rollback,
                partial_marked,
                marker_not_terminal,
                final,
                final,
            ],
            silence_ms=200,
            expected_reply_prefix="B REPLY:",
            expected_reply_suffix="B REPLY END",
        )

        self.assertTrue(result["response_complete"])
        self.assertEqual(
            result["response_text"],
            "B REPLY: final answer is complete\n\nB REPLY END",
        )
        self.assertEqual(result["response_turn_id"], "assistant-final")
        self.assertEqual(result["expected_reply_prefix"], "B REPLY:")
        self.assertEqual(result["expected_reply_suffix"], "B REPLY END")

    def test_prefix_only_marked_reply_ignores_thinking_and_rollback(self):
        old = {
            "ok": True,
            "source": "chatgpt-strict",
            "assistant_count": 1,
            "user_count": 1,
            "assistant": {
                "text": "A REPLY: old answer",
                "turn_id": "assistant-old",
                "turn_index": 0,
                "hasStreaming": False,
            },
            "user": {
                "text": "old prompt",
                "turn_id": "user-old",
                "turn_index": 0,
            },
            "hasStopButton": False,
        }
        submitted = {
            **old,
            "user_count": 2,
            "user": {
                "text": "hello",
                "turn_id": "user-submitted",
                "turn_index": 1,
            },
        }
        thinking = {
            **submitted,
            "assistant_count": 2,
            "assistant": {
                "text": "Thinking",
                "turn_id": "assistant-thinking",
                "turn_index": 1,
                "hasStreaming": True,
            },
            "hasStopButton": True,
        }
        rollback = {
            **submitted,
            "assistant_count": 1,
            "assistant": old["assistant"],
            "hasStopButton": False,
        }
        final = {
            **submitted,
            "assistant_count": 2,
            "assistant": {
                "text": "B REPLY: final answer",
                "turn_id": "assistant-final",
                "turn_index": 1,
                "hasStreaming": False,
            },
            "hasStopButton": False,
        }

        result, _, _, _ = self.run_transaction(
            [
                old,
                submitted,
                thinking,
                rollback,
                final,
                final,
            ],
            silence_ms=200,
            expected_reply_prefix="B REPLY:",
        )

        self.assertTrue(result["response_complete"])
        self.assertEqual(result["response_text"], "B REPLY: final answer")
        self.assertEqual(result["response_turn_id"], "assistant-final")
        self.assertEqual(result["expected_reply_prefix"], "B REPLY:")
        self.assertIsNone(result["expected_reply_suffix"])

    def test_dispatcher_routes_chatgpt_to_strict_transaction(self):
        expected = {
            "response_text": "reply",
            "response_complete": True,
        }
        with mock.patch.object(
            workflows,
            "_adapter_for_tab",
            return_value=self.adapter,
        ), mock.patch.object(
            workflows,
            "_chatgpt_send_and_wait",
            return_value=expected,
        ) as strict:
            result = workflows.send_and_wait(
                "tab-a",
                "hello",
                wait_timeout_ms=1234,
                silence_ms=321,
            )

        self.assertEqual(result, expected)
        strict.assert_called_once_with(
            "tab-a",
            "hello",
            wait_timeout_ms=1234,
            silence_ms=321,
            adapter=self.adapter,
            expected_reply_prefix=None,
            expected_reply_suffix=None,
        )


if __name__ == "__main__":
    unittest.main()

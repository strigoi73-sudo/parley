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

    def run_transaction(self, states, timeout_ms=3000, silence_ms=200):
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
        )


if __name__ == "__main__":
    unittest.main()

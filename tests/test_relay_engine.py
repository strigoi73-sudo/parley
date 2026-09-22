import unittest
from unittest import mock

from parley import workflows
from parley.relay import (
    COMPLETE,
    ERROR,
    READ_A,
    TRANSFER_A_TO_B,
    TRANSFER_B_TO_A,
    run_bidirectional_relay,
)


def strict_turn(text, turn_id, turn_index):
    return {
        "ok": True,
        "text": text,
        "turn_id": turn_id,
        "turn_index": turn_index,
        "source": "chatgpt-strict",
        "hasStreaming": False,
        "hasStopButton": False,
    }


def completed_reply(text, turn_id, turn_index):
    return {
        "response_text": text,
        "response_turn_id": turn_id,
        "response_turn_index": turn_index,
        "response_source": "chatgpt-strict",
        "response_complete": True,
    }


class BidirectionalRelayTests(unittest.TestCase):
    def test_one_round_is_exactly_two_transfers(self):
        calls = []

        def read_response(tab_id):
            self.assertEqual(tab_id, "A")
            return strict_turn("A1", "a1", 0)

        replies = {
            "B": completed_reply("B1", "b1", 0),
            "A": completed_reply("A2", "a2", 1),
        }

        def send_and_wait(tab_id, text):
            calls.append((tab_id, text))
            return replies[tab_id]

        result = run_bidirectional_relay(
            "A",
            "B",
            1,
            read_response=read_response,
            send_and_wait=send_and_wait,
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["state"], COMPLETE)
        self.assertEqual(result["rounds_completed"], 1)
        self.assertEqual(calls, [("B", "A1"), ("A", "B1")])
        self.assertEqual(len(result["transfers"]), 2)
        self.assertEqual(
            [x["direction"] for x in result["transfers"]],
            ["A->B", "B->A"],
        )
        self.assertEqual(result["latest_a"]["text"], "A2")
        self.assertEqual(result["latest_b"]["text"], "B1")

    def test_two_rounds_have_exact_required_order(self):
        calls = []
        response_queue = [
            ("B", "A1", completed_reply("B1", "b1", 0)),
            ("A", "B1", completed_reply("A2", "a2", 1)),
            ("B", "A2", completed_reply("B2", "b2", 1)),
            ("A", "B2", completed_reply("A3", "a3", 2)),
        ]

        def send_and_wait(tab_id, text):
            expected_tab, expected_text, reply = response_queue.pop(0)
            self.assertEqual((tab_id, text), (expected_tab, expected_text))
            calls.append((tab_id, text))
            return reply

        result = run_bidirectional_relay(
            "A",
            "B",
            2,
            read_response=lambda _: strict_turn("A1", "a1", 0),
            send_and_wait=send_and_wait,
        )

        self.assertEqual(
            calls,
            [
                ("B", "A1"),
                ("A", "B1"),
                ("B", "A2"),
                ("A", "B2"),
            ],
        )
        self.assertEqual(result["rounds_completed"], 2)
        self.assertEqual(len(result["transfers"]), 4)
        self.assertEqual(
            [x["direction"] for x in result["transfers"]],
            ["A->B", "B->A", "A->B", "B->A"],
        )
        self.assertEqual(result["latest_a"]["text"], "A3")
        self.assertEqual(result["latest_b"]["text"], "B2")

    def test_initial_context_is_attached_only_to_first_a_to_b_transfer(self):
        calls = []
        response_queue = [
            completed_reply("B1", "b1", 0),
            completed_reply("A2", "a2", 1),
            completed_reply("B2", "b2", 1),
            completed_reply("A3", "a3", 2),
        ]

        def send_and_wait(tab_id, text):
            calls.append((tab_id, text))
            return response_queue.pop(0)

        result = run_bidirectional_relay(
            "A",
            "B",
            2,
            read_response=lambda _: strict_turn("A1", "a1", 0),
            send_and_wait=send_and_wait,
            initial_context=(
                "A argues more privacy concerns; "
                "B argues fewer privacy concerns."
            ),
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(calls[0][0], "B")
        self.assertIn(
            "PARLEY SESSION BRIEF (human-provided; "
            "applies to both Chat A and Chat B):",
            calls[0][1],
        )
        self.assertIn(
            "B argues fewer privacy concerns.",
            calls[0][1],
        )
        self.assertIn("CHAT A OPENING REPLY:\nA1", calls[0][1])
        self.assertEqual(calls[1], ("A", "B1"))
        self.assertEqual(calls[2], ("B", "A2"))
        self.assertEqual(calls[3], ("A", "B2"))
        self.assertEqual(
            sum(
                item.get("event") == "initial_context_attached"
                for item in result["audit"]
            ),
            1,
        )

    def test_state_history_reflects_directional_sequence(self):
        result = run_bidirectional_relay(
            "A",
            "B",
            1,
            read_response=lambda _: strict_turn("A1", "a1", 0),
            send_and_wait=lambda tab, text: (
                completed_reply("B1", "b1", 0)
                if tab == "B"
                else completed_reply("A2", "a2", 1)
            ),
        )

        self.assertEqual(
            result["state_history"],
            [
                "IDLE",
                "PREPARE",
                READ_A,
                TRANSFER_A_TO_B,
                TRANSFER_B_TO_A,
                COMPLETE,
            ],
        )

    def test_event_sink_receives_state_and_transfer_events(self):
        events = []

        result = run_bidirectional_relay(
            "A",
            "B",
            1,
            read_response=lambda _: strict_turn("A1", "a1", 0),
            send_and_wait=lambda tab, text: (
                completed_reply("B1", "b1", 0)
                if tab == "B"
                else completed_reply("A2", "a2", 1)
            ),
            event_sink=events.append,
        )

        self.assertEqual(result["status"], "complete")
        names = [item.get("event") for item in events]
        self.assertIn("state_changed", names)
        self.assertEqual(
            names.count("transfer_completed"),
            2,
        )

    def test_initial_incomplete_turn_fails_before_any_send(self):
        send = mock.Mock()
        result = run_bidirectional_relay(
            "A",
            "B",
            1,
            read_response=lambda _: {
                **strict_turn("A1", "a1", 0),
                "hasStreaming": True,
            },
            send_and_wait=send,
        )

        send.assert_not_called()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["state"], ERROR)
        self.assertEqual(result["error"], "initial_response_incomplete")
        self.assertEqual(result["stage"], "read_a")

    def test_failed_a_to_b_stops_before_b_to_a(self):
        calls = []

        def send_and_wait(tab_id, text):
            calls.append((tab_id, text))
            return {
                "error": "chatgpt_new_assistant_turn_timeout",
                "response_complete": False,
            }

        result = run_bidirectional_relay(
            "A",
            "B",
            2,
            read_response=lambda _: strict_turn("A1", "a1", 0),
            send_and_wait=send_and_wait,
        )

        self.assertEqual(calls, [("B", "A1")])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["rounds_completed"], 0)
        self.assertEqual(result["stage"], "a_to_b")
        self.assertEqual(
            result["error"],
            "chatgpt_new_assistant_turn_timeout",
        )

    def test_failed_b_to_a_preserves_first_transfer_only(self):
        calls = []

        def send_and_wait(tab_id, text):
            calls.append((tab_id, text))
            if tab_id == "B":
                return completed_reply("B1", "b1", 0)
            return {
                "error": "chatgpt_response_completion_timeout",
                "response_complete": False,
            }

        result = run_bidirectional_relay(
            "A",
            "B",
            1,
            read_response=lambda _: strict_turn("A1", "a1", 0),
            send_and_wait=send_and_wait,
        )

        self.assertEqual(calls, [("B", "A1"), ("A", "B1")])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["rounds_completed"], 0)
        self.assertEqual(len(result["transfers"]), 1)
        self.assertEqual(result["transfers"][0]["direction"], "A->B")
        self.assertEqual(result["stage"], "b_to_a")

    def test_same_tab_is_rejected(self):
        result = run_bidirectional_relay(
            "A",
            "A",
            1,
            read_response=mock.Mock(),
            send_and_wait=mock.Mock(),
        )
        self.assertEqual(result["error"], "relay_tabs_must_be_distinct")

    def test_round_count_must_be_positive_integer(self):
        for rounds in (0, -1, 1.5, True):
            result = run_bidirectional_relay(
                "A",
                "B",
                rounds,
                read_response=mock.Mock(),
                send_and_wait=mock.Mock(),
            )
            self.assertEqual(
                result["error"],
                "relay_rounds_must_be_positive_integer",
            )

    def test_marked_a_reply_activates_test_protocol_and_expected_markers(self):
        calls = []

        def send_and_wait(tab_id, text, **kwargs):
            calls.append((tab_id, text, kwargs))
            if tab_id == "B":
                self.assertEqual(
                    kwargs.get("expected_reply_prefix"),
                    "B REPLY:",
                )
                self.assertEqual(
                    kwargs.get("expected_reply_suffix"),
                    "B REPLY END",
                )
                return completed_reply(
                    "B REPLY: response from B\n\nB REPLY END",
                    "b1",
                    0,
                )
            self.assertEqual(
                kwargs.get("expected_reply_prefix"),
                "A REPLY:",
            )
            self.assertEqual(
                kwargs.get("expected_reply_suffix"),
                "A REPLY END",
            )
            return completed_reply(
                "A REPLY: response from A\n\nA REPLY END",
                "a2",
                1,
            )

        result = run_bidirectional_relay(
            "A",
            "B",
            1,
            read_response=lambda _: strict_turn(
                "A REPLY: starting answer\n\nA REPLY END",
                "a1",
                0,
            ),
            send_and_wait=send_and_wait,
        )

        self.assertEqual(result["status"], "complete")
        self.assertTrue(result["test_protocol"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            calls[0][2]["expected_reply_prefix"],
            "B REPLY:",
        )
        self.assertEqual(
            calls[0][2]["expected_reply_suffix"],
            "B REPLY END",
        )
        self.assertEqual(
            calls[1][2]["expected_reply_prefix"],
            "A REPLY:",
        )
        self.assertEqual(
            calls[1][2]["expected_reply_suffix"],
            "A REPLY END",
        )

    def test_marked_initial_a_reply_requires_terminal_marker(self):
        send = mock.Mock()
        result = run_bidirectional_relay(
            "A",
            "B",
            1,
            read_response=lambda _: strict_turn(
                "A REPLY: incomplete starting answer",
                "a1",
                0,
            ),
            send_and_wait=send,
        )

        send.assert_not_called()
        self.assertEqual(result["status"], "error")
        self.assertEqual(
            result["error"],
            "initial_test_reply_end_marker_missing",
        )
        self.assertEqual(result["stage"], "read_a")

    def test_test_protocol_reset_reply_propagates_once_and_stops(self):
        calls = []

        def send_and_wait(tab_id, text, **kwargs):
            calls.append((tab_id, text, kwargs))
            if len(calls) == 1:
                self.assertEqual(tab_id, "B")
                self.assertEqual(
                    kwargs.get("expected_reply_prefix"),
                    "B REPLY:",
                )
                self.assertEqual(
                    kwargs.get("expected_reply_suffix"),
                    "B REPLY END",
                )
                return completed_reply("RESET CHAT", "b-reset", 0)

            self.assertEqual(tab_id, "A")
            self.assertEqual(text, "RESET CHAT")
            self.assertEqual(
                kwargs.get("expected_reply_prefix"),
                "A REPLY:",
            )
            self.assertEqual(
                kwargs.get("expected_reply_suffix"),
                "A REPLY END",
            )
            return completed_reply("RESET CHAT", "a-reset", 1)

        result = run_bidirectional_relay(
            "A",
            "B",
            1,
            read_response=lambda _: strict_turn(
                "A REPLY: starting answer\n\nA REPLY END",
                "a1",
                0,
            ),
            send_and_wait=send_and_wait,
        )

        self.assertEqual(result["status"], "stopped")
        self.assertTrue(result["reset_requested"])
        self.assertEqual(result["reset_by"], "B")
        self.assertTrue(result["reset_propagated"])
        self.assertEqual(result["reset_acknowledged_by"], "A")
        self.assertEqual(result["restart_point"], "A")
        self.assertTrue(result["awaiting_human_restart"])
        self.assertEqual(result["stage"], "reset")
        self.assertEqual(result["rounds_completed"], 0)
        self.assertEqual(len(result["transfers"]), 0)
        self.assertEqual(len(calls), 2)
        self.assertTrue(
            any(
                item["event"] == "relay_reset_propagated"
                for item in result["audit"]
            )
        )

    def test_a_reset_reply_propagates_once_to_b(self):
        calls = []

        def send_and_wait(tab_id, text, **kwargs):
            calls.append((tab_id, text, kwargs))
            if len(calls) == 1:
                return completed_reply(
                    "B REPLY: response from B\n\nB REPLY END",
                    "b1",
                    0,
                )
            if len(calls) == 2:
                return completed_reply("RESET CHAT", "a-reset", 1)

            self.assertEqual(tab_id, "B")
            self.assertEqual(text, "RESET CHAT")
            self.assertEqual(
                kwargs.get("expected_reply_prefix"),
                "B REPLY:",
            )
            self.assertEqual(
                kwargs.get("expected_reply_suffix"),
                "B REPLY END",
            )
            return completed_reply("RESET CHAT", "b-reset", 1)

        result = run_bidirectional_relay(
            "A",
            "B",
            1,
            read_response=lambda _: strict_turn(
                "A REPLY: starting answer\n\nA REPLY END",
                "a1",
                0,
            ),
            send_and_wait=send_and_wait,
        )

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["reset_by"], "A")
        self.assertEqual(result["reset_acknowledged_by"], "B")
        self.assertTrue(result["reset_propagated"])
        self.assertEqual(result["restart_point"], "A")
        self.assertEqual(len(result["transfers"]), 1)
        self.assertEqual(len(calls), 3)

    def test_user_reset_is_detected_before_assistant_reset_finishes(self):
        calls = []
        a_state_checks = 0

        def read_turn_state(tab_id):
            nonlocal a_state_checks
            if tab_id == "A":
                a_state_checks += 1
                if a_state_checks >= 2:
                    return {
                        "ok": True,
                        "user": {
                            "text": "RESET CHAT",
                            "turn_id": "user-reset",
                        },
                    }
            return {
                "ok": True,
                "user": {
                    "text": "ordinary message",
                    "turn_id": "user-normal",
                },
            }

        def send_and_wait(tab_id, text, **kwargs):
            calls.append((tab_id, text, kwargs))
            if len(calls) == 1:
                return completed_reply(
                    "B REPLY: response from B\n\nB REPLY END",
                    "b1",
                    0,
                )
            self.assertEqual(tab_id, "B")
            self.assertEqual(text, "RESET CHAT")
            return completed_reply("RESET CHAT", "b-reset", 1)

        result = run_bidirectional_relay(
            "A",
            "B",
            1,
            read_response=lambda tab_id: (
                strict_turn(
                    "A REPLY: starting answer\n\nA REPLY END",
                    "a1",
                    0,
                )
                if tab_id == "A"
                else strict_turn(
                    "B REPLY: response from B\n\nB REPLY END",
                    "b1",
                    0,
                )
            ),
            read_turn_state=read_turn_state,
            send_and_wait=send_and_wait,
        )

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["reset_by"], "A")
        self.assertEqual(result["reset_acknowledged_by"], "B")
        self.assertTrue(result["reset_propagated"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(result["transfers"]), 1)

    def test_external_a_reset_is_detected_at_safe_checkpoint(self):
        a_reads = 0
        latest_b = strict_turn(
            "B REPLY: response from B\n\nB REPLY END",
            "b1",
            0,
        )
        calls = []

        def read_response(tab_id):
            nonlocal a_reads
            if tab_id == "A":
                a_reads += 1
                if a_reads >= 3:
                    return strict_turn("RESET CHAT", "a-reset", 2)
                return strict_turn(
                    "A REPLY: starting answer\n\nA REPLY END",
                    "a1",
                    0,
                )
            return latest_b

        def send_and_wait(tab_id, text, **kwargs):
            calls.append((tab_id, text, kwargs))
            if len(calls) == 1:
                return completed_reply(
                    "B REPLY: response from B\n\nB REPLY END",
                    "b1",
                    0,
                )
            self.assertEqual(tab_id, "B")
            self.assertEqual(text, "RESET CHAT")
            return completed_reply("RESET CHAT", "b-reset", 1)

        result = run_bidirectional_relay(
            "A",
            "B",
            1,
            read_response=read_response,
            send_and_wait=send_and_wait,
        )

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["reset_by"], "A")
        self.assertEqual(result["reset_acknowledged_by"], "B")
        self.assertTrue(result["reset_propagated"])
        self.assertEqual(result["restart_point"], "A")
        self.assertEqual(len(result["transfers"]), 1)
        self.assertEqual(len(calls), 2)

    def test_reset_propagation_fails_closed_without_exact_ack(self):
        calls = []

        def send_and_wait(tab_id, text, **kwargs):
            calls.append((tab_id, text))
            if len(calls) == 1:
                return completed_reply("RESET CHAT", "b-reset", 0)
            return completed_reply(
                "A REPLY: not reset\n\nA REPLY END",
                "a2",
                1,
            )

        result = run_bidirectional_relay(
            "A",
            "B",
            1,
            read_response=lambda _: strict_turn(
                "A REPLY: starting answer\n\nA REPLY END",
                "a1",
                0,
            ),
            send_and_wait=send_and_wait,
        )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "relay_reset_propagation_failed")
        self.assertTrue(result["reset_requested"])
        self.assertFalse(result["reset_propagated"])
        self.assertEqual(result["restart_point"], "A")
        self.assertEqual(len(result["transfers"]), 0)

    def test_workflows_bridge_delegates_to_relay_engine(self):
        with mock.patch(
            "parley.relay.run_bidirectional_relay",
            return_value={"status": "complete"},
        ) as relay:
            result = workflows.bridge("A", "B", rounds=2)

        self.assertEqual(result, {"status": "complete"})
        relay.assert_called_once_with(
            "A",
            "B",
            2,
            read_response=workflows.read_response,
            read_turn_state=workflows.read_turn_state,
            send_and_wait=workflows.send_and_wait,
            validate_tab=workflows._validate_chatgpt_tab,
            control=None,
            include_text=False,
            event_sink=None,
            initial_context=None,
        )


if __name__ == "__main__":
    unittest.main()

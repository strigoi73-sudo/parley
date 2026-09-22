import threading
import time
import unittest
from unittest import mock

from parley import workflows
from parley.relay import RelayControl, run_bidirectional_relay


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


class SequencedControl:
    def __init__(self, outcomes, initial_state="running"):
        self.outcomes = list(outcomes)
        self._state = initial_state

    @property
    def state(self):
        return self._state

    def wait_until_runnable(self):
        if not self.outcomes:
            return True
        outcome = self.outcomes.pop(0)
        if outcome == "resume":
            self._state = "running"
            return True
        if outcome == "stop":
            self._state = "stopped"
            return False
        return bool(outcome)


class RelaySafetyTests(unittest.TestCase):
    def test_duplicate_source_turn_is_blocked_before_second_delivery(self):
        calls = []
        replies = [
            completed_reply("B1", "b1", 0),
            # A incorrectly returns the same A1 source identity/text again.
            completed_reply("A1", "a1", 0),
        ]

        def send_and_wait(tab_id, text):
            calls.append((tab_id, text))
            return replies.pop(0)

        result = run_bidirectional_relay(
            "A",
            "B",
            2,
            read_response=lambda _: strict_turn("A1", "a1", 0),
            send_and_wait=send_and_wait,
        )

        self.assertEqual(calls, [("B", "A1"), ("A", "B1")])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "relay_duplicate_source_turn")
        self.assertEqual(result["round"], 2)
        self.assertEqual(len(result["transfers"]), 2)
        self.assertTrue(
            any(
                item["event"] == "duplicate_blocked"
                for item in result["audit"]
            )
        )

    def test_stop_checkpoint_prevents_next_send(self):
        calls = []
        # Checkpoints: prepare, read_a, a_to_b, then stop at b_to_a.
        control = SequencedControl([True, True, True, "stop"])

        def send_and_wait(tab_id, text):
            calls.append((tab_id, text))
            return completed_reply("B1", "b1", 0)

        result = run_bidirectional_relay(
            "A",
            "B",
            1,
            read_response=lambda _: strict_turn("A1", "a1", 0),
            send_and_wait=send_and_wait,
            control=control,
        )

        self.assertEqual(calls, [("B", "A1")])
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["state"], "STOPPED")
        self.assertEqual(result["stage"], "b_to_a")
        self.assertEqual(len(result["transfers"]), 1)

    def test_pause_resumes_without_duplicate_send(self):
        calls = []
        # Start paused; first checkpoint transitions through PAUSED then resumes.
        control = SequencedControl(["resume"], initial_state="paused")

        def send_and_wait(tab_id, text):
            calls.append((tab_id, text))
            if tab_id == "B":
                return completed_reply("B1", "b1", 0)
            return completed_reply("A2", "a2", 1)

        result = run_bidirectional_relay(
            "A",
            "B",
            1,
            read_response=lambda _: strict_turn("A1", "a1", 0),
            send_and_wait=send_and_wait,
            control=control,
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(calls, [("B", "A1"), ("A", "B1")])
        self.assertIn("PAUSED", result["state_history"])
        events = [item["event"] for item in result["audit"]]
        self.assertIn("relay_paused", events)
        self.assertIn("relay_resumed", events)

    def test_tab_validation_failure_stops_before_b_to_a_send(self):
        calls = []
        b_checks = 0

        def validate_tab(tab_id):
            nonlocal b_checks
            if tab_id == "B":
                b_checks += 1
                if b_checks >= 3:
                    return {
                        "ok": False,
                        "error": "relay_tab_not_chatgpt",
                    }
            return {"ok": True}

        def send_and_wait(tab_id, text):
            calls.append((tab_id, text))
            return completed_reply("B1", "b1", 0)

        result = run_bidirectional_relay(
            "A",
            "B",
            1,
            read_response=lambda _: strict_turn("A1", "a1", 0),
            send_and_wait=send_and_wait,
            validate_tab=validate_tab,
        )

        self.assertEqual(calls, [("B", "A1")])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "relay_tab_not_chatgpt")
        self.assertEqual(result["stage"], "b_to_a")

    def test_callback_exception_becomes_structured_error(self):
        def boom(tab_id, text):
            raise RuntimeError("connection vanished")

        result = run_bidirectional_relay(
            "A",
            "B",
            1,
            read_response=lambda _: strict_turn("A1", "a1", 0),
            send_and_wait=boom,
        )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "relay_transfer_exception")
        self.assertEqual(result["stage"], "a_to_b")
        self.assertIn("connection vanished", result["detail"])

    def test_audit_and_transfer_records_hide_text_by_default(self):
        def send_and_wait(tab_id, text):
            if tab_id == "B":
                return completed_reply("SECRET B1", "b1", 0)
            return completed_reply("SECRET A2", "a2", 1)

        result = run_bidirectional_relay(
            "A",
            "B",
            1,
            read_response=lambda _: strict_turn(
                "SECRET A1",
                "a1",
                0,
            ),
            send_and_wait=send_and_wait,
        )

        self.assertEqual(result["status"], "complete")
        for transfer in result["transfers"]:
            self.assertNotIn("source_text", transfer)
            self.assertNotIn("response_text", transfer)
            self.assertIn("source_hash", transfer)
            self.assertIn("response_hash", transfer)

        audit_text = repr(result["audit"])
        self.assertNotIn("SECRET A1", audit_text)
        self.assertNotIn("SECRET B1", audit_text)
        self.assertNotIn("SECRET A2", audit_text)

    def test_include_text_must_be_explicit(self):
        def send_and_wait(tab_id, text):
            if tab_id == "B":
                return completed_reply("B1", "b1", 0)
            return completed_reply("A2", "a2", 1)

        result = run_bidirectional_relay(
            "A",
            "B",
            1,
            read_response=lambda _: strict_turn("A1", "a1", 0),
            send_and_wait=send_and_wait,
            include_text=True,
        )

        self.assertEqual(
            result["transfers"][0]["source_text"],
            "A1",
        )
        self.assertEqual(
            result["transfers"][0]["response_text"],
            "B1",
        )

    def test_workflow_tab_validator_rejects_navigation_away(self):
        with mock.patch.object(
            workflows.core,
            "tab_url",
            return_value="https://example.com/",
        ):
            result = workflows._validate_chatgpt_tab("tab-a")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "relay_tab_not_chatgpt")

    def test_workflow_tab_validator_rejects_missing_tab(self):
        with mock.patch.object(
            workflows.core,
            "tab_url",
            return_value="",
        ):
            result = workflows._validate_chatgpt_tab("tab-a")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "relay_tab_unavailable")

    def test_interactive_round_limit_waits_for_user_confirmation(self):
        control = RelayControl(
            round_limit=1,
            confirm_round_limit=True,
        )
        holder = {}

        def run():
            holder["result"] = run_bidirectional_relay(
                "A",
                "B",
                1,
                read_response=lambda _: strict_turn("A1", "a1", 0),
                send_and_wait=lambda tab, text: (
                    completed_reply("B1", "b1", 0)
                    if tab == "B"
                    else completed_reply("A2", "a2", 1)
                ),
                control=control,
            )

        thread = threading.Thread(target=run, daemon=True)
        thread.start()

        try:
            deadline = time.monotonic() + 2
            while (
                not control.awaiting_round_extension
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)

            self.assertTrue(control.awaiting_round_extension)
            self.assertTrue(thread.is_alive())
            self.assertNotIn("result", holder)

            # Human decision waits are indefinite: idling must not
            # auto-finish or time out the relay.
            time.sleep(0.25)
            self.assertTrue(control.awaiting_round_extension)
            self.assertTrue(thread.is_alive())
            self.assertNotIn("result", holder)

            control.finish_at_round_limit()
            thread.join(timeout=2)

            self.assertFalse(thread.is_alive())
            self.assertEqual(holder["result"]["status"], "complete")
            self.assertEqual(holder["result"]["rounds_completed"], 1)
        finally:
            if thread.is_alive():
                control.stop()
                thread.join(timeout=1)

    def test_round_limit_wait_wakes_for_reset_request(self):
        control = RelayControl(
            round_limit=1,
            confirm_round_limit=True,
        )
        holder = {}

        def wait():
            holder["decision"] = control.wait_for_round_limit_decision(1)

        thread = threading.Thread(target=wait, daemon=True)
        thread.start()

        try:
            deadline = time.monotonic() + 2
            while (
                not control.awaiting_round_extension
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)

            self.assertTrue(control.awaiting_round_extension)
            control.request_reset("B")
            thread.join(timeout=2)

            self.assertFalse(thread.is_alive())
            self.assertEqual(holder["decision"], "reset:B")
        finally:
            if thread.is_alive():
                control.stop()
                thread.join(timeout=1)

    def test_interactive_round_limit_extension_resumes_same_relay(self):
        control = RelayControl(
            round_limit=1,
            confirm_round_limit=True,
        )
        holder = {}
        replies = [
            completed_reply("B1", "b1", 0),
            completed_reply("A2", "a2", 1),
            completed_reply("B2", "b2", 1),
            completed_reply("A3", "a3", 2),
        ]

        def send_and_wait(tab_id, text):
            return replies.pop(0)

        def run():
            holder["result"] = run_bidirectional_relay(
                "A",
                "B",
                1,
                read_response=lambda _: strict_turn("A1", "a1", 0),
                send_and_wait=send_and_wait,
                control=control,
            )

        thread = threading.Thread(target=run, daemon=True)
        thread.start()

        try:
            deadline = time.monotonic() + 2
            while (
                not control.awaiting_round_extension
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)

            self.assertTrue(control.awaiting_round_extension)
            control.extend_round_limit(2)

            deadline = time.monotonic() + 2
            saw_second_prompt = False
            while time.monotonic() < deadline:
                if (
                    control.awaiting_round_extension
                    and control.round_limit == 2
                ):
                    saw_second_prompt = True
                    break
                time.sleep(0.01)

            self.assertTrue(saw_second_prompt)
            control.finish_at_round_limit()
            thread.join(timeout=2)

            self.assertFalse(thread.is_alive())
            result = holder["result"]
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["rounds_requested"], 2)
            self.assertEqual(result["rounds_completed"], 2)
            self.assertEqual(len(result["transfers"]), 4)
        finally:
            if thread.is_alive():
                control.stop()
                thread.join(timeout=1)

    def test_round_limit_can_extend_while_relay_is_running(self):
        control = RelayControl(round_limit=1)
        calls = []
        replies = [
            completed_reply("B1", "b1", 0),
            completed_reply("A2", "a2", 1),
            completed_reply("B2", "b2", 1),
            completed_reply("A3", "a3", 2),
        ]

        def send_and_wait(tab_id, text):
            calls.append((tab_id, text))
            reply = replies.pop(0)
            if len(calls) == 2:
                control.extend_round_limit(2)
            return reply

        result = run_bidirectional_relay(
            "A",
            "B",
            1,
            read_response=lambda _: strict_turn("A1", "a1", 0),
            send_and_wait=send_and_wait,
            control=control,
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["rounds_requested"], 2)
        self.assertEqual(result["rounds_completed"], 2)
        self.assertEqual(
            calls,
            [
                ("B", "A1"),
                ("A", "B1"),
                ("B", "A2"),
                ("A", "B2"),
            ],
        )
        self.assertTrue(
            any(
                item["event"] == "relay_round_limit_extended"
                for item in result["audit"]
            )
        )

    def test_round_limit_extension_cannot_reduce_limit(self):
        control = RelayControl(round_limit=3)
        with self.assertRaises(ValueError):
            control.extend_round_limit(3)
        with self.assertRaises(ValueError):
            control.extend_round_limit(2)
        self.assertEqual(control.round_limit, 3)

    def test_relay_control_stop_is_terminal(self):
        control = RelayControl()
        control.pause()
        self.assertEqual(control.state, "paused")
        control.resume()
        self.assertEqual(control.state, "running")
        control.stop()
        self.assertEqual(control.state, "stopped")
        control.resume()
        self.assertEqual(control.state, "stopped")


if __name__ == "__main__":
    unittest.main()

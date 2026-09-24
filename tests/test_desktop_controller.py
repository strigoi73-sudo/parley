import threading
import unittest
from unittest import mock

from parley.desktop_controller import (
    DesktopController,
    DesktopControllerOperations,
)


class FakeSession:
    def __init__(
        self,
        bridge,
        tab_a,
        tab_b,
        rounds,
        *,
        include_text=False,
        initial_context=None,
    ):
        self.bridge = bridge
        self.tab_a = tab_a
        self.tab_b = tab_b
        self.rounds = rounds
        self.include_text = include_text
        self.initial_context = initial_context
        self.started = False
        self.alive = False
        self.paused = False
        self.stopped = False
        self.finished_boundary = False
        self.round_limit = rounds
        self._transfers = []
        self._events = []
        self.result = None
        self.exception = None

    def start(self):
        self.started = True
        self.alive = True
        return self

    def is_alive(self):
        return self.alive

    def status(self):
        return {
            "status": "running" if self.alive else "complete",
            "state": "TRANSFER_A_TO_B" if self.alive else "COMPLETE",
            "control": "paused" if self.paused else "running",
            "rounds_completed": 0,
            "rounds_requested": self.round_limit,
            "awaiting_extension": False,
            "transfers_completed": len(self._transfers),
            "last_transfer": self._transfers[-1] if self._transfers else None,
        }

    def transfers(self):
        return [dict(item) for item in self._transfers]

    def events(self):
        return [dict(item) for item in self._events]

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def stop(self):
        self.stopped = True
        self.alive = False

    def extend_rounds(self, new_total):
        self.round_limit = new_total
        return new_total

    def finish_at_round_limit(self):
        self.finished_boundary = True


class DesktopControllerTests(unittest.TestCase):
    def make_controller(self, prepare_session=None, monotonic=None):
        if prepare_session is None:
            prepare_session = mock.Mock(
                return_value={
                    "ok": True,
                    "A": {"id": "TAB-A"},
                    "B": {"id": "TAB-B"},
                    "initial_response_text": "A REPLY: ready\nA REPLY END",
                }
            )
        if monotonic is None:
            monotonic = mock.Mock(return_value=10.0)

        operations = DesktopControllerOperations(
            prepare_session=prepare_session,
            bridge=mock.sentinel.bridge,
            session_factory=FakeSession,
            monotonic=monotonic,
        )
        return DesktopController(operations), prepare_session

    def test_start_prepares_in_worker_without_starting_relay(self):
        controller, prepare = self.make_controller()
        progress = mock.Mock()
        finished = mock.Mock()

        thread = controller.start(
            "Discuss culture.",
            {
                "A": {"source": "existing", "tab": {"id": "TAB-A"}},
                "B": {"source": "existing", "tab": {"id": "TAB-B"}},
            },
            3,
            progress=progress,
            finished=finished,
        )
        thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertIsNone(controller.session)
        self.assertEqual(controller.tabs["A"]["id"], "TAB-A")
        self.assertEqual(controller.tabs["B"]["id"], "TAB-B")
        prepare.assert_called_once()
        self.assertEqual(prepare.call_args.args[0], "Discuss culture.")
        self.assertTrue(callable(prepare.call_args.kwargs["should_stop"]))
        self.assertIs(prepare.call_args.kwargs["progress"], progress)
        finished.assert_called_once()
        self.assertEqual(finished.call_args.args[1:], ("Discuss culture.", 3))

    def test_create_and_start_relay_preserves_existing_boundary(self):
        controller, _ = self.make_controller()
        result = {
            "ok": True,
            "A": {"id": "TAB-A"},
            "B": {"id": "TAB-B"},
        }

        session = controller.create_relay_session(
            result,
            "Discuss culture.",
            4,
        )

        self.assertFalse(session.started)
        self.assertFalse(session.is_alive())
        self.assertIs(session.bridge, mock.sentinel.bridge)
        self.assertEqual(session.tab_a, "TAB-A")
        self.assertEqual(session.tab_b, "TAB-B")
        self.assertEqual(session.rounds, 4)
        self.assertTrue(session.include_text)
        self.assertEqual(session.initial_context, "Discuss culture.")

        self.assertIs(controller.start_relay_session(), session)
        self.assertTrue(session.started)
        self.assertTrue(controller.session_alive)

    def test_controls_delegate_to_active_session(self):
        controller, _ = self.make_controller()
        controller.create_relay_session(
            {
                "ok": True,
                "A": {"id": "TAB-A"},
                "B": {"id": "TAB-B"},
            },
            "Prompt",
            2,
        )
        controller.start_relay_session()

        self.assertTrue(controller.pause())
        self.assertTrue(controller.session.paused)
        self.assertTrue(controller.resume())
        self.assertFalse(controller.session.paused)
        self.assertEqual(controller.extend_rounds(5), 5)
        self.assertEqual(controller.status()["rounds_requested"], 5)
        self.assertTrue(controller.finish_at_round_limit())
        self.assertTrue(controller.session.finished_boundary)

        self.assertEqual(controller.stop(), "session")
        self.assertTrue(controller.user_stop_requested)
        self.assertTrue(controller.session.stopped)
        self.assertFalse(controller.session_alive)

    def test_poll_returns_only_new_items_and_marks_finished_once(self):
        controller, _ = self.make_controller()
        session = controller.create_relay_session(
            {
                "ok": True,
                "A": {"id": "TAB-A"},
                "B": {"id": "TAB-B"},
            },
            "Prompt",
            2,
        )
        controller.start_relay_session()

        session._transfers.append({
            "event": "transfer_completed",
            "direction": "A->B",
        })
        session._events.append({"event": "state_changed"})

        first = controller.poll()
        self.assertEqual(len(first["transfers"]), 1)
        self.assertEqual(len(first["events"]), 1)
        self.assertFalse(first["finished"])

        second = controller.poll()
        self.assertEqual(second["transfers"], [])
        self.assertEqual(second["events"], [])
        self.assertFalse(second["finished"])

        session.result = {
            "status": "complete",
            "rounds_completed": 1,
        }
        session.alive = False

        finished = controller.poll()
        self.assertTrue(finished["finished"])
        self.assertEqual(finished["result"]["status"], "complete")
        self.assertTrue(controller.session_done_seen)

        again = controller.poll()
        self.assertFalse(again["finished"])

    def test_stop_cancels_active_startup(self):
        entered = threading.Event()
        release = threading.Event()

        def prepare_session(
            prompt,
            specs,
            *,
            should_stop,
            progress,
        ):
            entered.set()
            release.wait(2)
            return {
                "ok": False,
                "error": (
                    "chatgpt_wait_stopped"
                    if should_stop()
                    else "unexpected"
                ),
                "stage": "participants",
            }

        controller, _ = self.make_controller(
            prepare_session=prepare_session,
        )
        finished = mock.Mock()
        thread = controller.start(
            "Prompt",
            {
                "A": {"source": "fresh"},
                "B": {"source": "fresh"},
            },
            1,
            progress=mock.Mock(),
            finished=finished,
        )
        self.assertTrue(entered.wait(1))

        self.assertEqual(controller.stop(), "startup")
        self.assertTrue(controller.operation_stop.is_set())
        self.assertTrue(controller.user_stop_requested)
        release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        finished.assert_called_once()

    def test_reset_clears_completed_runtime_state(self):
        clock = mock.Mock(side_effect=[10.0, 16.9])
        controller, _ = self.make_controller(monotonic=clock)
        controller.started_at = 10.0
        controller.tabs = {
            "A": {"id": "TAB-A"},
            "B": {"id": "TAB-B"},
        }
        controller.user_stop_requested = True

        self.assertEqual(controller.elapsed_seconds(), 6)
        self.assertTrue(controller.reset())
        self.assertIsNone(controller.session)
        self.assertIsNone(controller.tabs)
        self.assertIsNone(controller.started_at)
        self.assertFalse(controller.user_stop_requested)
        self.assertFalse(controller.operation_stop.is_set())


if __name__ == "__main__":
    unittest.main()

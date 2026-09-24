import unittest
from unittest import mock

from parley.relay import RelaySession


class RelaySessionTests(unittest.TestCase):
    def test_session_tracks_state_and_transfer_events(self):
        def bridge(
            tab_a,
            tab_b,
            rounds,
            *,
            control,
            include_text,
            event_sink,
            initial_context,
        ):
            event_sink({
                "event": "state_changed",
                "state": "TRANSFER_A_TO_B",
            })
            event_sink({
                "event": "transfer_completed",
                "round": 1,
                "direction": "A->B",
                "source_chars": 2,
                "response_chars": 2,
            })
            event_sink({
                "event": "state_changed",
                "state": "COMPLETE",
            })
            return {
                "status": "complete",
                "state": "COMPLETE",
                "rounds_requested": rounds,
                "rounds_completed": 1,
                "transfers": [{}, {}],
            }

        session = RelaySession(
            bridge,
            "A",
            "B",
            1,
        ).start()

        result = session.join(timeout=2)
        status = session.status()

        self.assertEqual(result["status"], "complete")
        self.assertEqual(status["state"], "COMPLETE")
        self.assertEqual(status["rounds_completed"], 1)
        self.assertEqual(status["transfers_completed"], 1)
        self.assertEqual(
            status["last_transfer"]["direction"],
            "A->B",
        )

    def test_session_tracks_completed_rounds_from_b_to_a_event(self):
        session = RelaySession(
            mock.Mock(),
            "A",
            "B",
            3,
        )
        session._on_event({
            "event": "transfer_completed",
            "round": 1,
            "direction": "A->B",
            "source_chars": 2,
            "response_chars": 2,
        })
        session._on_event({
            "event": "transfer_completed",
            "round": 1,
            "direction": "B->A",
            "source_chars": 2,
            "response_chars": 2,
        })

        status = session.status()
        self.assertEqual(status["rounds_completed"], 1)
        self.assertEqual(status["transfers_completed"], 2)

    def test_session_exposes_defensive_event_and_transfer_snapshots(self):
        session = RelaySession(
            mock.Mock(),
            "A",
            "B",
            2,
        )
        session._on_event({
            "event": "state_changed",
            "state": "TRANSFER_A_TO_B",
        })
        session._on_event({
            "event": "transfer_completed",
            "round": 1,
            "direction": "A->B",
            "response_text": "B REPLY: hello\n\nB REPLY END",
        })

        events = session.events()
        transfers = session.transfers()

        self.assertEqual(events[0]["event"], "state_changed")
        self.assertEqual(transfers[0]["direction"], "A->B")

        events[0]["event"] = "mutated"
        transfers[0]["direction"] = "mutated"

        self.assertEqual(
            session.events()[0]["event"],
            "state_changed",
        )
        self.assertEqual(
            session.transfers()[0]["direction"],
            "A->B",
        )

    def test_session_extend_rounds_updates_status(self):
        session = RelaySession(
            mock.Mock(),
            "A",
            "B",
            2,
        )

        updated = session.extend_rounds(5)

        self.assertEqual(updated, 5)
        self.assertEqual(session.status()["rounds_requested"], 5)

    def test_session_control_methods_delegate(self):
        session = RelaySession(
            mock.Mock(),
            "A",
            "B",
            1,
        )
        with mock.patch.object(
            session.control,
            "pause",
        ) as pause, mock.patch.object(
            session.control,
            "resume",
        ) as resume, mock.patch.object(
            session.control,
            "stop",
        ) as stop:
            session.pause()
            session.resume()
            session.stop()
            session.request_reset("B")

        pause.assert_called_once()
        resume.assert_called_once()
        stop.assert_called_once()
        self.assertEqual(session.control._reset_request, "B")


if __name__ == "__main__":
    unittest.main()

"""Reply ownership, cancellation, and connection lifecycle regressions."""

import json
import queue
import threading
import unittest
from unittest import mock

import websocket

from parley.live_browser import LiveBrowserManager


class ControlledSocket:
    connected = True

    def __init__(self):
        self.sent = queue.Queue()
        self.replies = queue.Queue()
        self.timeout = 0.25
        self.reader_ids = set()

    def settimeout(self, timeout):
        self.timeout = timeout

    def send(self, payload):
        self.sent.put(json.loads(payload))

    def recv(self):
        self.reader_ids.add(threading.get_ident())
        try:
            reply = self.replies.get(timeout=self.timeout)
        except queue.Empty:
            raise websocket.WebSocketTimeoutException()
        if isinstance(reply, Exception):
            raise reply
        return json.dumps(reply)

    def close(self, timeout=None):
        assert timeout == 0, "close must not become a second socket reader"
        self.connected = False


class LiveDispatchTests(unittest.TestCase):
    def setUp(self):
        self.socket = ControlledSocket()
        self.manager = LiveBrowserManager(endpoint_fn=lambda: "ws://test")
        self.threads = []
        self.create = mock.patch(
            "parley.live_browser.websocket.create_connection", return_value=self.socket,
        ).start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.cleanup_manager)

    def cleanup_manager(self):
        self.manager.close()
        for thread in self.threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def command(self, session_id, **kwargs):
        result = []
        done = threading.Event()

        def run():
            result.append(self.manager.command(
                "Runtime.evaluate", {"expression": "1"},
                session_id=session_id, **kwargs,
            ))
            done.set()

        thread = threading.Thread(target=run, daemon=True)
        self.threads.append(thread)
        thread.start()
        message = self.socket.sent.get(timeout=2)
        return message, done, result

    def reply(self, message, result):
        self.socket.replies.put({
            "id": message["id"], "sessionId": message["sessionId"], "result": result,
        })

    def test_out_of_order_replies_events_and_wrong_sessions(self):
        a, a_done, a_result = self.command("a")
        b, b_done, b_result = self.command("b")
        self.socket.replies.put({"method": "Runtime.consoleAPICalled", "sessionId": "a"})
        self.socket.replies.put({"id": a["id"], "sessionId": "b", "result": "wrong"})
        self.reply(b, "B")
        self.assertTrue(b_done.wait(2))
        self.assertFalse(a_done.is_set())
        self.reply(a, "A")
        self.assertTrue(a_done.wait(2))
        self.assertEqual(a_result, ["A"])
        self.assertEqual(b_result, ["B"])
        self.assertEqual(len(self.socket.reader_ids), 1)
        self.create.assert_called_once()

    def test_stop_does_not_cancel_other_waiter_or_consume_its_reply(self):
        stop = threading.Event()
        a, a_done, a_result = self.command("a", should_stop=stop.is_set)
        b, b_done, b_result = self.command("b")
        # Several socket polls cannot become an implicit failure deadline.
        self.assertFalse(a_done.wait(0.6))
        self.assertFalse(b_done.is_set())
        stop.set()
        self.assertTrue(a_done.wait(2))
        self.assertEqual(a_result, [{"error": "stopped"}])
        self.reply(a, "late A")
        self.reply(b, "B")
        self.assertTrue(b_done.wait(2))
        self.assertEqual(b_result, ["B"])
        self.assertTrue(self.socket.connected)
        self.assertTrue(self.socket.sent.empty())  # No retransmission.

    def test_expired_reply_cannot_satisfy_later_command_on_same_session(self):
        old, old_done, old_result = self.command("a", timeout=0.05)
        self.assertTrue(old_done.wait(2))
        self.assertEqual(old_result, [{"error": "timeout"}])
        new, new_done, new_result = self.command("a")
        self.reply(old, "stale")
        self.reply(new, "current")
        self.assertTrue(new_done.wait(2))
        self.assertEqual(new_result, ["current"])

    def test_disconnect_releases_all_waiters_and_next_command_reconnects(self):
        _, a_done, a_result = self.command("a")
        _, b_done, b_result = self.command("b")
        self.socket.replies.put(OSError("socket lost"))
        self.assertTrue(a_done.wait(2))
        self.assertTrue(b_done.wait(2))
        self.assertEqual(a_result, [{"error": "socket lost"}])
        self.assertEqual(b_result, [{"error": "socket lost"}])
        self.socket = ControlledSocket()
        self.create.return_value = self.socket
        new, done, result = self.command("new-session")
        self.reply(new, "new connection")
        self.assertTrue(done.wait(2))
        self.assertEqual(result, ["new connection"])
        self.assertEqual(self.create.call_count, 2)

    def test_close_releases_all_waiters(self):
        _, a_done, a_result = self.command("a")
        _, b_done, b_result = self.command("b")
        self.manager.close()
        self.assertTrue(a_done.wait(2))
        self.assertTrue(b_done.wait(2))
        self.assertEqual(a_result, [{"error": "live browser connection closed"}])
        self.assertEqual(b_result, a_result)

    def test_stop_before_submission_sends_nothing(self):
        self.assertEqual(
            self.manager.command("Runtime.evaluate", should_stop=lambda: True),
            {"error": "stopped"},
        )
        self.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()

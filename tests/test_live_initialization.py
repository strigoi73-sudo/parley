"""Exercise real workflow/transport concurrency without any model turns."""

import json
import queue
import threading
import unittest
from unittest import mock

import websocket

from parley import workflows
from parley.adapters.chatgpt import ChatGPTAdapter
from parley.live_browser import LiveBrowserManager


class InitializationSocket:
    connected = True

    def __init__(self):
        self.queue = queue.Queue()
        self.sent = []
        self.timeout = 0.25
        self.inserted = {}
        self.submitted = set()
        self.a_waiting = threading.Event()
        self.delayed = None
        self.adapter = ChatGPTAdapter()

    def settimeout(self, timeout):
        self.timeout = timeout

    def reply(self, message, result):
        response = {"id": message["id"], "result": result}
        if "sessionId" in message:
            response["sessionId"] = message["sessionId"]
        self.queue.put(json.dumps(response))

    def send(self, payload):
        message = json.loads(payload)
        self.sent.append(message)
        method = message["method"]
        params = message.get("params", {})
        label = message.get("sessionId")
        if method == "Target.getTargets":
            result = {"targetInfos": [
                {"targetId": key, "type": "page", "title": key,
                 "url": "https://chatgpt.com/c/" + key}
                for key in ("A", "B")
            ]}
        elif method == "Target.attachToTarget":
            result = {"sessionId": params["targetId"]}
        elif method == "Target.detachFromTarget":
            result = {}
        elif method == "Input.insertText":
            self.inserted[label] = params["text"]
            result = {}
        elif params.get("expression") == self.adapter.turn_state_js:
            submitted = label in self.submitted
            state = {
                "ok": True, "user_count": int(submitted),
                "assistant_count": int(submitted), "hasStopButton": False,
                "user": {"text": self.inserted.get(label),
                         "turn_id": "user-" + label, "turn_index": 0}
                        if submitted else None,
                "assistant": {"text": f"{label} REPLY: Ready\n{label} REPLY END",
                              "turn_id": "reply-" + label, "turn_index": 0,
                              "hasStreaming": False} if submitted else None,
            }
            result = {"result": {"value": state}}
            if label == "A" and submitted and not self.a_waiting.is_set():
                self.delayed = (message, result)
                self.a_waiting.set()
                return
        elif params.get("expression") in (
            self.adapter.prepare_composer_js, self.adapter.click_send_js,
        ):
            if params["expression"] == self.adapter.click_send_js:
                self.submitted.add(label)
            result = {"result": {"value": {"ok": True}}}
        else:
            raise AssertionError(message)
        self.reply(message, result)

    def recv(self):
        try:
            return self.queue.get(timeout=self.timeout)
        except queue.Empty:
            raise websocket.WebSocketTimeoutException()

    def close(self, timeout=None):
        self.connected = False


class LiveInitializationTests(unittest.TestCase):
    def test_b_initializes_while_a_cdp_response_is_pending(self):
        fake = InitializationSocket()
        manager = LiveBrowserManager(endpoint_fn=lambda: "ws://test")
        results = {}
        finished = {label: threading.Event() for label in ("A", "B")}

        def initialize(label):
            try:
                results[label] = workflows.send_and_wait(
                    label, f"INITIALIZE PARLEY TEST CHAT {label}",
                    wait_timeout_ms=None, silence_ms=0,
                    expected_reply_prefix=f"{label} REPLY:",
                    expected_reply_suffix=f"{label} REPLY END",
                )
            finally:
                finished[label].set()

        with mock.patch.dict("os.environ", {"PARLEY_CONNECTION_MODE": "live"}), \
                mock.patch("parley.live_browser._MANAGER", manager), \
                mock.patch("parley.live_browser.websocket.create_connection",
                           return_value=fake) as create:
            threads = [threading.Thread(target=initialize, args=(label,), daemon=True)
                       for label in ("A", "B")]
            threads[0].start()
            try:
                self.assertTrue(fake.a_waiting.wait(2), "A never reached post-send CDP read")
                threads[1].start()
                self.assertTrue(finished["B"].wait(2),
                                "B blocked behind A's pending CDP response")
                self.assertFalse(finished["A"].is_set())
                self.assertTrue(results["B"]["response_complete"])
            finally:
                if fake.delayed:
                    fake.reply(*fake.delayed)
                for thread in threads:
                    if thread.ident is not None:
                        thread.join(3)
                manager.close()

        self.assertTrue(results["A"]["response_complete"])
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        create.assert_called_once()
        inserts = [message["params"]["text"] for message in fake.sent
                   if message["method"] == "Input.insertText"]
        self.assertEqual(inserts, ["INITIALIZE PARLEY TEST CHAT A",
                                   "INITIALIZE PARLEY TEST CHAT B"])


if __name__ == "__main__":
    unittest.main()

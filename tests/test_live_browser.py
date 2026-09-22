import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from parley import core


class FakeBrowserWebSocket:
    def __init__(self):
        self.sent = []
        self.queue = []
        self.timeout = None
        self.closed = False

    def send(self, payload):
        msg = json.loads(payload)
        self.sent.append(msg)

        if msg["method"] == "Target.attachToTarget":
            self.queue.append(json.dumps({
                "id": msg["id"],
                "result": {"sessionId": "session-1"},
            }))
        elif msg["method"] == "Target.detachFromTarget":
            self.queue.append(json.dumps({
                "id": msg["id"],
                "result": {},
            }))
        else:
            self.queue.append(json.dumps({
                "id": msg["id"],
                "sessionId": msg.get("sessionId"),
                "result": {
                    "result": {
                        "type": "string",
                        "value": "ok",
                    }
                },
            }))

    def recv(self):
        if not self.queue:
            raise AssertionError("fake websocket receive queue is empty")
        return self.queue.pop(0)

    def settimeout(self, timeout):
        self.timeout = timeout

    def close(self):
        self.closed = True


class LiveBrowserTests(unittest.TestCase):
    def test_live_endpoint_reads_devtools_active_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "DevToolsActivePort").write_text(
                "49321\n/devtools/browser/test-id\n",
                encoding="utf-8",
            )
            endpoint = core.live_devtools_endpoint(tmp)

        self.assertEqual(
            endpoint,
            "ws://127.0.0.1:49321/devtools/browser/test-id",
        )

    def test_live_endpoint_rejects_bad_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "DevToolsActivePort").write_text(
                "not-a-port\n/devtools/browser/test-id\n",
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                core.live_devtools_endpoint(tmp)

    def test_live_list_tabs_uses_target_domain(self):
        targets = [
            {
                "targetId": "page-a",
                "type": "page",
                "title": "ChatGPT A",
                "url": "https://chatgpt.com/c/a",
            },
            {
                "targetId": "worker-a",
                "type": "service_worker",
                "title": "worker",
                "url": "https://chatgpt.com/sw.js",
            },
        ]

        with mock.patch.dict(
            os.environ,
            {"PARLEY_CONNECTION_MODE": "live"},
            clear=False,
        ), mock.patch.object(
            core,
            "_live_target_infos",
            return_value=targets,
        ):
            result = core.list_tabs()

        self.assertEqual(
            result,
            [{
                "id": "page-a",
                "title": "ChatGPT A",
                "url": "https://chatgpt.com/c/a",
            }],
        )

    def test_live_tab_connection_routes_commands_through_session(self):
        fake = FakeBrowserWebSocket()

        with mock.patch.object(
            core,
            "_open_live_browser_ws",
            return_value=fake,
        ):
            ws = core.LiveTabConnection("page-a")
            result = core.cdp_send(
                ws,
                "Runtime.evaluate",
                {"expression": "document.title", "returnByValue": True},
            )
            ws.close()

        self.assertEqual(ws.session_id, "session-1")
        self.assertEqual(result["result"]["value"], "ok")

        runtime_message = next(
            msg for msg in fake.sent
            if msg["method"] == "Runtime.evaluate"
        )
        self.assertEqual(runtime_message["sessionId"], "session-1")

        attach_message = fake.sent[0]
        self.assertEqual(attach_message["method"], "Target.attachToTarget")
        self.assertEqual(
            attach_message["params"],
            {"targetId": "page-a", "flatten": True},
        )

    def test_classic_mode_remains_default(self):
        old = os.environ.pop("PARLEY_CONNECTION_MODE", None)
        try:
            self.assertEqual(core.connection_mode(), "classic")
        finally:
            if old is not None:
                os.environ["PARLEY_CONNECTION_MODE"] = old


if __name__ == "__main__":
    unittest.main()

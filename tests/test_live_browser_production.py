import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from parley import cli, core
from parley.live_browser import (
    LiveBrowserManager,
    LiveTabConnection,
    live_devtools_endpoint,
)


class FakeBrowserWebSocket:
    def __init__(self, delay=0.0):
        self.sent = []
        self.queue = []
        self.timeout = None
        self.closed = False
        self.connected = True
        self.delay = delay
        self.active_transactions = 0
        self.max_active_transactions = 0
        self._lock = threading.Lock()

    def send(self, payload):
        message = json.loads(payload)
        with self._lock:
            self.active_transactions += 1
            self.max_active_transactions = max(
                self.max_active_transactions,
                self.active_transactions,
            )
        self.sent.append(message)

        method = message["method"]
        if method == "Target.getTargets":
            result = {
                "targetInfos": [
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
            }
        elif method == "Target.attachToTarget":
            result = {
                "sessionId": "session-" + message["params"]["targetId"]
            }
        elif method == "Target.detachFromTarget":
            result = {}
        else:
            result = {
                "result": {
                    "type": "string",
                    "value": message.get("sessionId") or "browser",
                }
            }

        response = {
            "id": message["id"],
            "result": result,
        }
        if message.get("sessionId"):
            response["sessionId"] = message["sessionId"]
        self.queue.append(json.dumps(response))

    def recv(self):
        if self.delay:
            time.sleep(self.delay)
        if not self.queue:
            raise AssertionError("fake websocket receive queue is empty")
        value = self.queue.pop(0)
        with self._lock:
            self.active_transactions -= 1
        return value

    def settimeout(self, timeout):
        self.timeout = timeout

    def close(self):
        self.closed = True
        self.connected = False


class LiveBrowserProductionTests(unittest.TestCase):
    def test_live_endpoint_reads_devtools_active_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "DevToolsActivePort").write_text(
                "49321\n/devtools/browser/test-id\n",
                encoding="utf-8",
            )
            endpoint = live_devtools_endpoint(tmp)

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
                live_devtools_endpoint(tmp)

    def test_manager_reuses_one_browser_websocket(self):
        fake = FakeBrowserWebSocket()
        manager = LiveBrowserManager(
            endpoint_fn=lambda: "ws://127.0.0.1:9222/devtools/browser/test"
        )

        with mock.patch(
            "parley.live_browser.websocket.create_connection",
            return_value=fake,
        ) as create:
            targets = manager.target_infos()
            tab = LiveTabConnection(
                "page-a",
                manager=manager,
            )
            result = tab.command(
                "Runtime.evaluate",
                {"expression": "document.title"},
            )
            tab.close()

        self.assertEqual(targets[0]["targetId"], "page-a")
        self.assertEqual(
            result["result"]["value"],
            "session-page-a",
        )
        create.assert_called_once()

        methods = [message["method"] for message in fake.sent]
        self.assertEqual(
            methods,
            [
                "Target.getTargets",
                "Target.attachToTarget",
                "Runtime.evaluate",
                "Target.detachFromTarget",
            ],
        )

    def test_shared_browser_socket_serializes_two_target_sessions(self):
        fake = FakeBrowserWebSocket(delay=0.03)
        manager = LiveBrowserManager(
            endpoint_fn=lambda: "ws://127.0.0.1:9222/devtools/browser/test"
        )

        with mock.patch(
            "parley.live_browser.websocket.create_connection",
            return_value=fake,
        ):
            tab_a = LiveTabConnection("a", manager=manager)
            tab_b = LiveTabConnection("b", manager=manager)

            results = []

            def run(tab):
                results.append(
                    tab.command(
                        "Runtime.evaluate",
                        {"expression": "1"},
                    )
                )

            t1 = threading.Thread(target=run, args=(tab_a,))
            t2 = threading.Thread(target=run, args=(tab_b,))
            t1.start()
            t2.start()
            t1.join(2)
            t2.join(2)

        self.assertEqual(len(results), 2)
        self.assertEqual(fake.max_active_transactions, 1)

    def test_live_connection_routes_command_through_session(self):
        fake = FakeBrowserWebSocket()
        manager = LiveBrowserManager(
            endpoint_fn=lambda: "ws://127.0.0.1:9222/devtools/browser/test"
        )

        with mock.patch(
            "parley.live_browser.websocket.create_connection",
            return_value=fake,
        ):
            tab = LiveTabConnection("page-a", manager=manager)
            result = core.cdp_send(
                tab,
                "Runtime.evaluate",
                {"expression": "document.title"},
            )

        self.assertEqual(
            result["result"]["value"],
            "session-page-a",
        )
        runtime_message = next(
            message
            for message in fake.sent
            if message["method"] == "Runtime.evaluate"
        )
        self.assertEqual(
            runtime_message["sessionId"],
            "session-page-a",
        )

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
            "live_target_infos",
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

    def test_live_retry_reattaches_same_handle(self):
        class RecoveringConnection:
            def __init__(self):
                self.calls = 0
                self.reconnects = 0

            def command(self, method, params=None, timeout=10):
                self.calls += 1
                if self.calls == 1:
                    return {"error": "session closed"}
                return {"value": "ok"}

            def reconnect(self):
                self.reconnects += 1
                return True

        connection = RecoveringConnection()
        result = core.cdp_send_with_retry(
            connection,
            "Runtime.evaluate",
            {"expression": "1"},
            tab_id="page-a",
            retries=2,
        )

        self.assertEqual(result, {"value": "ok"})
        self.assertEqual(connection.reconnects, 1)
        self.assertEqual(connection.calls, 2)

    def test_classic_mode_remains_core_default(self):
        old = os.environ.pop("PARLEY_CONNECTION_MODE", None)
        try:
            self.assertEqual(core.connection_mode(), "classic")
        finally:
            if old is not None:
                os.environ["PARLEY_CONNECTION_MODE"] = old


class LiveBrowserCLITests(unittest.TestCase):
    def setUp(self):
        self.old_mode = os.environ.pop(
            "PARLEY_CONNECTION_MODE",
            None,
        )

    def tearDown(self):
        if self.old_mode is None:
            os.environ.pop("PARLEY_CONNECTION_MODE", None)
        else:
            os.environ["PARLEY_CONNECTION_MODE"] = self.old_mode

    def test_chats_defaults_to_live_mode(self):
        observed = []

        def list_tabs():
            observed.append(core.connection_mode())
            return []

        with mock.patch.object(core, "list_tabs", side_effect=list_tabs):
            code = cli.main(["parley", "chats"])

        self.assertEqual(code, 0)
        self.assertEqual(observed, ["live"])

    def test_relay_defaults_to_live_before_tab_discovery(self):
        observed = []

        def list_tabs():
            observed.append(core.connection_mode())
            return []

        with mock.patch.object(core, "list_tabs", side_effect=list_tabs):
            code = cli.main(["parley", "relay"])

        self.assertEqual(code, 1)
        self.assertEqual(observed, ["live"])

    def test_classic_flag_overrides_relay_default(self):
        observed = []

        def list_tabs():
            observed.append(core.connection_mode())
            return []

        with mock.patch.object(core, "list_tabs", side_effect=list_tabs):
            code = cli.main(["parley", "--classic", "relay"])

        self.assertEqual(code, 1)
        self.assertEqual(observed, ["classic"])

    def test_live_flag_works_for_low_level_list(self):
        observed = []

        def list_tabs():
            observed.append(core.connection_mode())
            return []

        with mock.patch.object(core, "list_tabs", side_effect=list_tabs):
            code = cli.main(["parley", "--live", "list"])

        self.assertEqual(code, 0)
        self.assertEqual(observed, ["live"])


if __name__ == "__main__":
    unittest.main()

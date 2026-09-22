import unittest
from unittest import mock

from parley import workflows
from parley.adapters import js


class FakeConnection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class SendAndWaitTests(unittest.TestCase):
    def test_send_and_wait_reuses_one_connection(self):
        ws = FakeConnection()
        responses = [
            {"result": {"value": {"count": 0, "source": "none"}}},
            {
                "result": {
                    "value": {
                        "text": "Live Parley connection successful.",
                        "count": 1,
                        "source": "chatgpt-article-turn",
                        "hasStreaming": False,
                        "hasStopButton": False,
                    }
                }
            },
        ]

        with mock.patch.object(
            workflows,
            "cdp_connect",
            return_value=ws,
        ) as connect, mock.patch.object(
            workflows,
            "robust_send",
            return_value=({
                "send_method": "chatgpt",
                "reloaded": False,
                "prev_text": "",
            }, ws),
        ) as send, mock.patch.object(
            workflows,
            "cdp_send",
            side_effect=responses,
        ):
            result = workflows.send_and_wait(
                "tab-a",
                "test",
                wait_timeout_ms=1000,
                silence_ms=0,
            )

        connect.assert_called_once_with("tab-a")
        send.assert_called_once_with(ws, "tab-a", "test")
        self.assertTrue(ws.closed)
        self.assertTrue(result["response_complete"])
        self.assertEqual(
            result["response_text"],
            "Live Parley connection successful.",
        )
        self.assertEqual(
            result["response_source"],
            "chatgpt-article-turn",
        )

    def test_chatgpt_article_turn_fallback_is_present_everywhere(self):
        selector = 'article[data-turn="assistant"]'
        self.assertIn(selector, js.UNIVERSAL_GET_RESPONSE)
        self.assertIn(selector, js.UNIVERSAL_GET_MSG_COUNT)
        observer = js.make_mutation_observer_js(1000, 100)
        self.assertIn(selector, observer)


if __name__ == "__main__":
    unittest.main()

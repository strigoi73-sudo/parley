import unittest
from unittest import mock

from parley import workflows
from parley.adapters.chatgpt import ChatGPTAdapter
from parley.adapters.chatgpt_strict import (
    CHATGPT_GET_MSG_COUNT_JS,
    CHATGPT_GET_RESPONSE_JS,
    CHATGPT_GET_TURN_STATE_JS,
)
from parley.adapters.js import (
    UNIVERSAL_GET_MSG_COUNT,
    UNIVERSAL_GET_RESPONSE,
)


class StrictChatGPTExtractorTests(unittest.TestCase):
    def test_adapter_uses_only_explicit_assistant_turn_selectors(self):
        adapter = ChatGPTAdapter()
        self.assertEqual(
            adapter.response_selectors,
            (
                'article[data-turn="assistant"]',
                '[data-message-author-role="assistant"]',
            ),
        )
        self.assertNotIn(".markdown", adapter.response_selectors)
        self.assertEqual(adapter.response_js, CHATGPT_GET_RESPONSE_JS)
        self.assertEqual(adapter.message_count_js, CHATGPT_GET_MSG_COUNT_JS)

    def test_response_extractor_has_no_page_wide_fallback(self):
        forbidden = (
            "document.body.innerText",
            "querySelectorAll('div, p, section')",
            "longestText",
            "longestEl",
            "source: 'generic'",
            'source: "generic"',
        )
        for token in forbidden:
            self.assertNotIn(token, CHATGPT_GET_RESPONSE_JS)

    def test_response_extractor_fails_closed(self):
        self.assertIn(
            "chatgpt_assistant_turn_not_found",
            CHATGPT_GET_RESPONSE_JS,
        )
        self.assertIn(
            "chatgpt_message_body_not_found",
            CHATGPT_GET_RESPONSE_JS,
        )
        self.assertIn("not_chatgpt_page", CHATGPT_GET_RESPONSE_JS)

    def test_turn_state_exposes_explicit_latest_user_turn(self):
        self.assertIn(
            'const articleSelector = \'article[data-turn="\' + roleName + \'"]\';',
            CHATGPT_GET_TURN_STATE_JS,
        )
        self.assertIn(
            'const roleSelector = \'[data-message-author-role="\' + roleName + \'"]\';',
            CHATGPT_GET_TURN_STATE_JS,
        )
        self.assertIn(
            "const userTurns = collectTurns('user');",
            CHATGPT_GET_TURN_STATE_JS,
        )
        self.assertIn(
            "const user = latestTurn(userTurns, 'user');",
            CHATGPT_GET_TURN_STATE_JS,
        )
        self.assertNotIn("document.body.innerText", CHATGPT_GET_TURN_STATE_JS)

    def test_mixed_turn_structures_are_merged_in_dom_order(self):
        for script in (
            CHATGPT_GET_RESPONSE_JS,
            CHATGPT_GET_MSG_COUNT_JS,
            CHATGPT_GET_TURN_STATE_JS,
        ):
            self.assertIn("collectTurns", script)
            self.assertIn("compareDocumentPosition", script)
            self.assertIn("role.closest('article')", script)

        self.assertNotIn(
            "assistantArticles.length || assistantRoles.length",
            CHATGPT_GET_TURN_STATE_JS,
        )
        self.assertNotIn(
            "userArticles.length || userRoles.length",
            CHATGPT_GET_TURN_STATE_JS,
        )

    def test_current_and_role_based_turn_structures_are_supported(self):
        for script in (
            CHATGPT_GET_RESPONSE_JS,
            CHATGPT_GET_MSG_COUNT_JS,
        ):
            self.assertIn(
                'const articleSelector = \'article[data-turn="\' + roleName + \'"]\';',
                script,
            )
            self.assertIn(
                'const roleSelector = \'[data-message-author-role="\' + roleName + \'"]\';',
                script,
            )
            self.assertIn("collectTurns('assistant')", script)


class WorkflowRoutingTests(unittest.TestCase):
    def test_chatgpt_routes_to_strict_response_extractor(self):
        with mock.patch.object(
            workflows.core,
            "tab_url",
            return_value="https://chatgpt.com/c/test",
        ):
            self.assertEqual(
                workflows._response_js("tab-a"),
                CHATGPT_GET_RESPONSE_JS,
            )
            self.assertEqual(
                workflows._message_count_js("tab-a"),
                CHATGPT_GET_MSG_COUNT_JS,
            )

    def test_non_chatgpt_keeps_universal_detector_for_now(self):
        with mock.patch.object(
            workflows.core,
            "tab_url",
            return_value="https://gemini.google.com/app/test",
        ):
            self.assertEqual(
                workflows._response_js("tab-b"),
                UNIVERSAL_GET_RESPONSE,
            )
            self.assertEqual(
                workflows._message_count_js("tab-b"),
                UNIVERSAL_GET_MSG_COUNT,
            )

    def test_unknown_tab_identity_fails_closed(self):
        with mock.patch.object(
            workflows.core,
            "tab_url",
            return_value="",
        ):
            response_js = workflows._response_js("missing-tab")
            count_js = workflows._message_count_js("missing-tab")

        self.assertIn("tab_url_unavailable", response_js)
        self.assertIn("tab_url_unavailable", count_js)
        self.assertNotEqual(response_js, UNIVERSAL_GET_RESPONSE)
        self.assertNotEqual(count_js, UNIVERSAL_GET_MSG_COUNT)

    def test_read_response_uses_strict_chatgpt_script(self):
        expected = {
            "ok": True,
            "text": "assistant only",
            "count": 1,
            "source": "chatgpt-strict",
        }

        with mock.patch.object(
            workflows.core,
            "tab_url",
            return_value="https://chatgpt.com/c/test",
        ), mock.patch.object(
            workflows.core,
            "evaluate",
            return_value=expected,
        ) as evaluate:
            result = workflows.read_response("tab-a")

        self.assertEqual(result, expected)
        evaluate.assert_called_once_with("tab-a", CHATGPT_GET_RESPONSE_JS)

    def test_robust_send_snapshots_previous_text_with_strict_script(self):
        ws = mock.Mock()
        calls = []

        def fake_send(_ws, method, params=None, timeout=10, tab_id=None, retries=3):
            calls.append((method, params))
            expression = (params or {}).get("expression")

            if expression == workflows.GEMINI_STUCK_STATE_JS:
                return {"result": {"value": {"stuck": False}}}
            if expression == workflows.FOCUS_AND_CLEAR_JS:
                return {"result": {"value": {"ok": True}}}
            if expression == CHATGPT_GET_RESPONSE_JS:
                return {
                    "result": {
                        "value": {
                            "ok": True,
                            "text": "previous assistant reply",
                            "count": 1,
                        }
                    }
                }
            if method == "Input.insertText":
                return {}
            if expression == workflows.SEND_CLICK_JS:
                return {
                    "result": {
                        "value": {
                            "ok": True,
                            "method": "chatgpt",
                        }
                    }
                }
            return {}

        with mock.patch.object(
            workflows.core,
            "tab_url",
            return_value="https://chatgpt.com/c/test",
        ), mock.patch.object(
            workflows,
            "cdp_send_with_retry",
            side_effect=fake_send,
        ), mock.patch.object(
            workflows.time,
            "sleep",
        ):
            result, returned_ws = workflows.robust_send(
                ws,
                "tab-a",
                "hello",
            )

        self.assertIs(returned_ws, ws)
        self.assertEqual(
            result["prev_text"],
            "previous assistant reply",
        )
        expressions = [
            params.get("expression")
            for _, params in calls
            if isinstance(params, dict) and "expression" in params
        ]
        self.assertIn(CHATGPT_GET_RESPONSE_JS, expressions)
        self.assertNotIn(UNIVERSAL_GET_RESPONSE, expressions)



if __name__ == "__main__":
    unittest.main()

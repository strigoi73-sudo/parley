import unittest
from unittest import mock

from parley import cli
from parley.participants import (
    CHATGPT_HOSTS,
    eligible_chatgpt_tabs,
    is_eligible_chatgpt_tab,
)


class ParticipantDiscoveryTests(unittest.TestCase):
    def test_approved_hosts_are_eligible(self):
        for index, host in enumerate(sorted(CHATGPT_HOSTS), 1):
            with self.subTest(host=host):
                self.assertTrue(
                    is_eligible_chatgpt_tab({
                        "id": f"TAB-{index}",
                        "url": f"https://{host}/c/example",
                    })
                )

    def test_host_matching_is_case_insensitive(self):
        self.assertTrue(
            is_eligible_chatgpt_tab({
                "id": "TAB-A",
                "url": "https://CHATGPT.COM/c/example",
            })
        )

    def test_similar_but_unapproved_hosts_are_rejected(self):
        for url in (
            "https://chatgpt.com.evil.example/c/example",
            "https://example.com/?next=https://chatgpt.com/",
            "https://openai.com/",
        ):
            with self.subTest(url=url):
                self.assertFalse(
                    is_eligible_chatgpt_tab({
                        "id": "TAB-A",
                        "url": url,
                    })
                )

    def test_missing_target_id_is_rejected(self):
        self.assertFalse(
            is_eligible_chatgpt_tab({
                "url": "https://chatgpt.com/c/example",
            })
        )

    def test_malformed_or_non_mapping_targets_are_rejected(self):
        for tab in (
            None,
            "https://chatgpt.com/",
            {"id": "TAB-A", "url": "http://[invalid"},
        ):
            with self.subTest(tab=tab):
                self.assertFalse(is_eligible_chatgpt_tab(tab))

    def test_filter_returns_defensive_copies(self):
        original = {
            "id": "TAB-A",
            "title": "Chat A",
            "url": "https://chatgpt.com/c/example",
        }
        result = eligible_chatgpt_tabs([
            original,
            {
                "id": "TAB-B",
                "url": "https://example.com/",
            },
        ])

        self.assertEqual(result, [original])
        self.assertIsNot(result[0], original)

    def test_cli_preserves_core_errors(self):
        error = {"error": "browser unavailable"}
        with mock.patch.object(cli.core, "list_tabs", return_value=error):
            self.assertIs(cli._chatgpt_tabs(), error)

    def test_cli_uses_shared_eligibility_rules(self):
        tabs = [
            {
                "id": "TAB-A",
                "url": "https://chatgpt.com/c/a",
            },
            {
                "id": "TAB-B",
                "url": "https://example.com/",
            },
            {
                "url": "https://chatgpt.com/c/missing-id",
            },
        ]
        with mock.patch.object(cli.core, "list_tabs", return_value=tabs):
            result = cli._chatgpt_tabs()

        self.assertEqual([tab["id"] for tab in result], ["TAB-A"])


if __name__ == "__main__":
    unittest.main()

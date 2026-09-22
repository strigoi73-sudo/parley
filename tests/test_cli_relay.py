import unittest
from unittest import mock

from parley import cli
from parley.relay import RelaySession


class RelayCLITests(unittest.TestCase):
    def test_parse_relay_args(self):
        options = cli._parse_relay_args([
            "1",
            "2",
            "--rounds",
            "4",
            "--include-text",
            "--json",
        ])

        self.assertEqual(options["tab_a"], "1")
        self.assertEqual(options["tab_b"], "2")
        self.assertEqual(options["rounds"], 4)
        self.assertTrue(options["include_text"])
        self.assertTrue(options["json_output"])

    def test_resolve_tab_by_number_or_id(self):
        tabs = [
            {"id": "AAA", "title": "A", "url": "https://chatgpt.com/c/a"},
            {"id": "BBB", "title": "B", "url": "https://chatgpt.com/c/b"},
        ]

        self.assertEqual(
            cli._resolve_tab_reference("1", tabs)["id"],
            "AAA",
        )
        self.assertEqual(
            cli._resolve_tab_reference("BBB", tabs)["id"],
            "BBB",
        )
        self.assertIsNone(cli._resolve_tab_reference("9", tabs))

    def test_chatgpt_tabs_filter_excludes_other_sites(self):
        tabs = [
            {"id": "A", "url": "https://chatgpt.com/c/a"},
            {"id": "B", "url": "https://chat.openai.com/c/b"},
            {"id": "X", "url": "https://example.com/"},
        ]
        with mock.patch.object(
            cli.core,
            "list_tabs",
            return_value=tabs,
        ):
            result = cli._chatgpt_tabs()

        self.assertEqual([tab["id"] for tab in result], ["A", "B"])

    def test_relay_commands_map_to_session_controls(self):
        session = mock.Mock()
        session.status.return_value = {
            "status": "running",
            "state": "TRANSFER_A_TO_B",
            "control": "running",
            "rounds_completed": 0,
            "rounds_requested": 2,
            "transfers_completed": 0,
        }

        self.assertIn(
            "Pause requested",
            cli._handle_relay_command("p", session),
        )
        session.pause.assert_called_once()

        self.assertEqual(
            cli._handle_relay_command("r", session),
            "Relay resumed.",
        )
        session.resume.assert_called_once()

        status_message = cli._handle_relay_command("s", session)
        self.assertIn("state=TRANSFER_A_TO_B", status_message)

        self.assertIn(
            "Stop requested",
            cli._handle_relay_command("q", session),
        )
        session.stop.assert_called_once()

    def test_cmd_relay_accepts_numbered_selection(self):
        tabs = [
            {
                "id": "TAB-A",
                "title": "Chat A",
                "url": "https://chatgpt.com/c/a",
            },
            {
                "id": "TAB-B",
                "title": "Chat B",
                "url": "https://chatgpt.com/c/b",
            },
        ]

        with mock.patch.object(
            cli,
            "_chatgpt_tabs",
            return_value=tabs,
        ), mock.patch.object(
            cli,
            "RelaySession",
        ) as session_cls, mock.patch.object(
            cli,
            "_run_interactive_relay",
            return_value=0,
        ) as run:
            session = session_cls.return_value
            code = cli.cmd_relay([
                "1",
                "2",
                "--rounds=2",
            ])

        self.assertEqual(code, 0)
        session_cls.assert_called_once_with(
            cli.workflows.bridge,
            "TAB-A",
            "TAB-B",
            2,
            include_text=False,
        )
        run.assert_called_once_with(
            session,
            input_stream=None,
            output_json=False,
        )

    def test_cmd_relay_rejects_same_tab(self):
        tabs = [
            {
                "id": "TAB-A",
                "title": "Chat A",
                "url": "https://chatgpt.com/c/a",
            },
            {
                "id": "TAB-B",
                "title": "Chat B",
                "url": "https://chatgpt.com/c/b",
            },
        ]
        with mock.patch.object(
            cli,
            "_chatgpt_tabs",
            return_value=tabs,
        ):
            code = cli.cmd_relay(["1", "1"])

        self.assertEqual(code, 1)


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

        pause.assert_called_once()
        resume.assert_called_once()
        stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()

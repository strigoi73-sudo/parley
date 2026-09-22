import io
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
            "--prompt-a",
        ])

        self.assertEqual(options["tab_a"], "1")
        self.assertEqual(options["tab_b"], "2")
        self.assertEqual(options["rounds"], 4)
        self.assertTrue(options["include_text"])
        self.assertTrue(options["json_output"])
        self.assertTrue(options["prompt_a"])

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
            {"id": "BAD", "url": "https://chatgpt.com.evil.example/c/x"},
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

    def test_extend_chat_command_raises_total_round_limit(self):
        session = mock.Mock()
        session.status.return_value = {"rounds_requested": 2}
        session.extend_rounds.return_value = 5

        message = cli._handle_relay_command(
            "EXTEND CHAT 5",
            session,
        )

        session.extend_rounds.assert_called_once_with(5)
        self.assertEqual(message, "Round limit extended to 5.")

    def test_extend_chat_command_rejects_non_extension(self):
        session = mock.Mock()
        session.status.return_value = {"rounds_requested": 4}

        message = cli._handle_relay_command(
            "EXTEND CHAT 4",
            session,
        )

        session.extend_rounds.assert_not_called()
        self.assertIn("already 4", message)

    def test_select_rounds_reprompts_until_positive_integer(self):
        answers = iter(["nope", "0", "3"])
        rounds = cli._select_rounds(input_fn=lambda _: next(answers))
        self.assertEqual(rounds, 3)

    def test_interactive_limit_prompt_accepts_yes_and_new_total(self):
        class FakeSession:
            def __init__(self):
                self.alive = True
                self.exception = None
                self.limit = 1
                self.extended_to = None
                self._result = {
                    "status": "complete",
                    "state": "COMPLETE",
                    "rounds_requested": 3,
                    "rounds_completed": 1,
                    "transfers": [],
                }

            def start(self):
                return self

            def is_alive(self):
                return self.alive

            def status(self):
                return {
                    "status": "running" if self.alive else "complete",
                    "state": "TRANSFER_B_TO_A",
                    "control": "running",
                    "rounds_completed": 1,
                    "rounds_requested": self.limit,
                    "awaiting_extension": self.alive,
                    "transfers_completed": 0,
                    "last_transfer": None,
                }

            def extend_rounds(self, new_total):
                self.extended_to = new_total
                self.limit = new_total
                self.alive = False
                return new_total

            def finish_at_round_limit(self):
                self.alive = False

            def join(self):
                return self._result

        session = FakeSession()
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            code = cli._run_interactive_relay(
                session,
                input_stream=io.StringIO("y\n3\n"),
                output_json=False,
                sleep=lambda _: None,
            )

        self.assertEqual(code, 0)
        self.assertEqual(session.extended_to, 3)
        rendered = output.getvalue()
        self.assertIn(
            "Round limit reached at 1. "
            "Extend session? [y/N] (no timeout)",
            rendered,
        )
        self.assertIn(
            "New total rounds (must be greater than 1):",
            rendered,
        )
        self.assertIn("Round limit extended to 3.", rendered)

    def test_select_initial_prompt_reprompts_until_nonblank(self):
        answers = iter(["", "   ", "Start the discussion"])
        prompt = cli._select_initial_prompt(
            input_fn=lambda _: next(answers)
        )
        self.assertEqual(prompt, "Start the discussion")

    def test_cmd_relay_prompt_a_sends_initial_prompt_before_session(self):
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
        answers = iter(["Discuss whether Pluto is a planet."])

        with mock.patch.object(
            cli,
            "_chatgpt_tabs",
            return_value=tabs,
        ), mock.patch.object(
            cli.workflows,
            "send_and_wait",
            return_value={
                "response_text": (
                    "A REPLY: Starting reply\n\nA REPLY END"
                ),
                "response_complete": True,
            },
        ) as send_wait, mock.patch.object(
            cli,
            "RelaySession",
        ) as session_cls, mock.patch.object(
            cli,
            "_run_interactive_relay",
            return_value=0,
        ) as run:
            session = session_cls.return_value
            code = cli.cmd_relay(
                ["1", "2", "--rounds=2", "--prompt-a"],
                input_fn=lambda _: next(answers),
            )

        self.assertEqual(code, 0)
        send_wait.assert_called_once_with(
            "TAB-A",
            "Discuss whether Pluto is a planet.",
            expected_reply_prefix="A REPLY:",
            expected_reply_suffix="A REPLY END",
        )
        session_cls.assert_called_once_with(
            cli.workflows.bridge,
            "TAB-A",
            "TAB-B",
            2,
            include_text=False,
            initial_context="Discuss whether Pluto is a planet.",
        )
        run.assert_called_once_with(
            session,
            input_stream=None,
            output_json=False,
        )

    def test_cmd_relay_prompt_a_failure_does_not_start_session(self):
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
            cli.workflows,
            "send_and_wait",
            return_value={
                "error": "chatgpt_marked_response_timeout",
                "response_complete": False,
            },
        ), mock.patch.object(
            cli,
            "RelaySession",
        ) as session_cls:
            code = cli.cmd_relay(
                ["1", "2", "--rounds=2", "--prompt-a"],
                input_fn=lambda _: "Start",
            )

        self.assertEqual(code, 1)
        session_cls.assert_not_called()

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
            initial_context=None,
        )
        run.assert_called_once_with(
            session,
            input_stream=None,
            output_json=False,
        )

    def test_cmd_relay_prompts_for_rounds_when_option_omitted(self):
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
        prompts = []
        answers = iter(["4"])

        def input_fn(prompt):
            prompts.append(prompt)
            return next(answers)

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
        ):
            code = cli.cmd_relay(
                ["1", "2"],
                input_fn=input_fn,
            )

        self.assertEqual(code, 0)
        self.assertIn("Number of rounds: ", prompts)
        session_cls.assert_called_once_with(
            cli.workflows.bridge,
            "TAB-A",
            "TAB-B",
            4,
            include_text=False,
            initial_context=None,
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

        pause.assert_called_once()
        resume.assert_called_once()
        stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()

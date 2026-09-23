"""Protocol-file provisioning and startup-barrier regressions."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from parley import core, workflows


class CoreFileInputTests(unittest.TestCase):
    def test_set_file_input_files_uses_cdp_dom_primitive(self):
        with tempfile.NamedTemporaryFile(suffix=".md") as handle:
            path = str(Path(handle.name).resolve())
            ws = mock.Mock()
            responses = [
                {"root": {"nodeId": 11}},
                {"nodeId": 22},
                {},
            ]

            with mock.patch.object(
                core,
                "cdp_connect",
                return_value=ws,
            ), mock.patch.object(
                core,
                "cdp_send_with_retry",
                side_effect=responses,
            ) as send:
                result = core.set_file_input_files("TAB", path)

        self.assertTrue(result["ok"])
        self.assertEqual(
            [call.args[1] for call in send.call_args_list],
            [
                "DOM.getDocument",
                "DOM.querySelector",
                "DOM.setFileInputFiles",
            ],
        )
        self.assertEqual(
            send.call_args_list[2].args[2]["files"],
            [path],
        )
        self.assertEqual(
            send.call_args_list[2].args[2]["nodeId"],
            22,
        )
        ws.close.assert_called_once()

    def test_chatgpt_attachment_reveals_input_then_verifies_chip(self):
        adapter = mock.Mock()
        adapter.name = "chatgpt"

        with tempfile.NamedTemporaryFile(suffix=".md") as handle, \
                mock.patch.object(
                    workflows,
                    "_adapter_for_tab",
                    return_value=adapter,
                ), mock.patch.object(
                    workflows.core,
                    "set_file_input_files",
                    side_effect=[
                        {
                            "ok": False,
                            "error": "file_input_not_found",
                        },
                        {
                            "ok": True,
                            "filename": Path(handle.name).name,
                        },
                    ],
                ) as set_file, mock.patch.object(
                    workflows.core,
                    "evaluate",
                    side_effect=[
                        {"ok": True, "action": "clicked-composer-control"},
                        {
                            "ok": True,
                            "filename": Path(handle.name).name,
                            "exactText": True,
                        },
                    ],
                ) as evaluate, mock.patch.object(
                    workflows.time,
                    "sleep",
                ):
            result = workflows.attach_chatgpt_file(
                "TAB",
                handle.name,
                timeout_ms=1000,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(set_file.call_count, 2)
        for call in set_file.call_args_list:
            self.assertEqual(
                call.kwargs["selector"],
                "input#upload-files",
            )
        self.assertEqual(evaluate.call_count, 2)


class AttachmentStabilizationTests(unittest.TestCase):
    def test_stabilization_wait_is_interruptible(self):
        stop = mock.Mock(side_effect=[False, True])
        with mock.patch.object(
            workflows.time,
            "monotonic",
            side_effect=[0.0, 0.0],
        ), mock.patch.object(
            workflows.time,
            "sleep",
        ):
            result = workflows._wait_protocol_attachment_stable(
                should_stop=stop,
                seconds=3.0,
            )

        self.assertFalse(result)




class ProtocolBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.validation = mock.patch.object(
            workflows,
            "_validate_chatgpt_tab",
            return_value={"ok": True},
        )
        self.validation.start()
        self.addCleanup(self.validation.stop)

    def test_pair_bootstrap_has_protocol_barrier_before_activation(self):
        operations = []

        def attach(tab_id, path, **kwargs):
            operations.append(("attach", tab_id))
            return {
                "ok": True,
                "filename": Path(path).name,
            }

        def send(tab_id, text, **kwargs):
            kind = (
                "ack"
                if text.startswith("Read the attached Parley protocol file.")
                else "activation"
            )
            operations.append((kind, tab_id))
            if kind == "ack":
                return {
                    "response_complete": True,
                    "response_text": (
                        "PARLEY PROTOCOL A RECEIVED"
                        if tab_id == "A"
                        else "PARLEY PROTOCOL B RECEIVED"
                    ),
                }
            return {
                "response_complete": True,
                "response_text": (
                    f"{tab_id} REPLY: Ready\n"
                    f"{tab_id} REPLY END"
                ),
            }

        progress = []
        with mock.patch.object(
            workflows,
            "_parley_protocol_active",
            return_value=False,
        ), mock.patch.object(
            workflows,
            "_snapshot_chatgpt_state",
            return_value={
                "ok": True,
                "assistant_count": 0,
                "user_count": 0,
                "assistant": None,
                "user": None,
            },
        ), mock.patch.object(
            workflows,
            "attach_chatgpt_file",
            side_effect=attach,
        ), mock.patch.object(
            workflows,
            "_wait_protocol_attachment_stable",
            side_effect=lambda **kwargs: (
                operations.append(("stabilize", None))
                or True
            ),
        ) as stabilize, mock.patch.object(
            workflows,
            "_chatgpt_send_and_wait",
            side_effect=send,
        ) as ack_send, mock.patch.object(
            workflows,
            "send_and_wait",
            side_effect=send,
        ) as activation_send:
            result = workflows.initialize_parley_pair(
                "A",
                "B",
                wait_timeout_ms=12345,
                progress=progress.append,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            operations,
            [
                ("attach", "A"),
                ("stabilize", None),
                ("ack", "A"),
                ("attach", "B"),
                ("stabilize", None),
                ("ack", "B"),
                ("activation", "A"),
                ("activation", "B"),
            ],
        )
        self.assertEqual(stabilize.call_count, 2)
        self.assertEqual(ack_send.call_count, 2)
        self.assertEqual(activation_send.call_count, 2)
        for call in ack_send.call_args_list:
            self.assertFalse(
                call.kwargs["require_user_text_match"]
            )
            self.assertEqual(
                call.kwargs["submission_timeout_ms"],
                60000,
            )
            self.assertTrue(
                call.kwargs["pre_state_override"]["ok"]
            )
        self.assertEqual(
            activation_send.call_args_list[0].kwargs["expected_reply_prefix"],
            "A REPLY:",
        )
        self.assertEqual(
            activation_send.call_args_list[1].kwargs["expected_reply_suffix"],
            "B REPLY END",
        )

    def test_attachment_failure_stops_before_ack_or_other_chat(self):
        with mock.patch.object(
            workflows,
            "_parley_protocol_active",
            return_value=False,
        ), mock.patch.object(
            workflows,
            "_snapshot_chatgpt_state",
            return_value={
                "ok": True,
                "assistant_count": 0,
                "user_count": 0,
                "assistant": None,
                "user": None,
            },
        ), mock.patch.object(
            workflows,
            "attach_chatgpt_file",
            return_value={
                "ok": False,
                "error": "chatgpt_file_input_timeout",
            },
        ) as attach, mock.patch.object(
            workflows,
            "_wait_protocol_attachment_stable",
        ) as stabilize, mock.patch.object(
            workflows,
            "send_and_wait",
        ) as send_wait:
            result = workflows.initialize_parley_pair("A", "B")

        self.assertFalse(result["ok"])
        self.assertEqual(result["participant"], "A")
        self.assertEqual(result["stage"], "protocol_attachment")
        attach.assert_called_once()
        stabilize.assert_not_called()
        send_wait.assert_not_called()

    def test_existing_active_chat_is_not_reprovisioned(self):
        def send(tab_id, text, **kwargs):
            if text.startswith("Read the attached Parley protocol file."):
                return {
                    "response_complete": True,
                    "response_text": "PARLEY PROTOCOL B RECEIVED",
                }
            return {
                "response_complete": True,
                "response_text": "B REPLY: Ready\nB REPLY END",
            }

        with mock.patch.object(
            workflows,
            "_parley_protocol_active",
            side_effect=[True, False],
        ), mock.patch.object(
            workflows,
            "_snapshot_chatgpt_state",
            return_value={
                "ok": True,
                "assistant_count": 0,
                "user_count": 0,
                "assistant": None,
                "user": None,
            },
        ), mock.patch.object(
            workflows,
            "attach_chatgpt_file",
            return_value={"ok": True},
        ) as attach, mock.patch.object(
            workflows,
            "_wait_protocol_attachment_stable",
            return_value=True,
        ) as stabilize, mock.patch.object(
            workflows,
            "_chatgpt_send_and_wait",
            side_effect=send,
        ) as ack_send, mock.patch.object(
            workflows,
            "send_and_wait",
            side_effect=send,
        ) as activation_send:
            result = workflows.initialize_parley_pair("A", "B")

        self.assertTrue(result["ok"])
        self.assertTrue(result["participants"]["A"]["already_active"])
        attach.assert_called_once()
        self.assertEqual(attach.call_args.args[0], "B")
        stabilize.assert_called_once()
        self.assertEqual(ack_send.call_count, 1)
        self.assertTrue(
            ack_send.call_args.args[1].startswith(
                "Read the attached Parley protocol file."
            )
        )
        self.assertFalse(
            ack_send.call_args.kwargs["require_user_text_match"]
        )
        self.assertEqual(activation_send.call_count, 1)
        self.assertEqual(
            activation_send.call_args.args,
            ("B", "INITIALIZE PARLEY TEST CHAT B"),
        )


if __name__ == "__main__":
    unittest.main()

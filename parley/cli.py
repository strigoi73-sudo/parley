"""Parley CLI - browser automation and two-ChatGPT relay controls.

Common commands:
    parley [--live|--classic] list
    parley chats
    parley app
    parley relay [tab_a] [tab_b] [--rounds N] [--initialize] [--fresh-chats] [--include-text] [--json]

The human-facing `chats`, `app`, and `relay` commands use live mode by default
unless PARLEY_CONNECTION_MODE or --classic explicitly says otherwise.

Other commands:
    parley list
    parley read <tab_id>
    parley send <tab_id> <text...>
    parley send-wait <tab_id> <text...> [--timeout N]
    parley bridge <tab_a> <tab_b> [rounds=3]

Interactive relay controls:
    p   pause after the current browser transaction
    r   resume
    s   show current status
    q   stop after the current browser transaction

Generic browser-automation commands:
    parley type <tab_id> <text...>
    parley click <tab_id> <selector>
    parley navigate <tab_id> <url>
    parley eval <tab_id> <js...>
    parley wait-stream <tab_id> [timeout_ms=60000] [silence_ms=1500]
    parley poll <tab_id> [interval_ms=2000]
    parley read-dom <tab_id> [selector]
    parley extract <tab_id> <selector> [attr]
    parley wait-for <tab_id> <selector> [timeout_ms=10000]
    parley cookies <tab_id> [domain]
    parley set-cookie <tab_id> <name> <value> <domain> [path=/]

Global transport options:
    --live      attach to the already-running Chrome via DevToolsActivePort
    --classic   use the inherited localhost CDP transport

Environment:
    PARLEY_CONNECTION_MODE  classic or live
    PARLEY_CHROME_USER_DATA_DIR  override Chrome user-data directory
    PARLEY_CDP_HOST   CDP host (default: localhost)
    PARLEY_CDP_PORT   CDP port (default: 9222)
"""

import json
import os
import queue
import sys
import threading
import time
from urllib.parse import urlparse

from . import core
from . import workflows
from .adapters.js import make_focus_and_type_js
from .relay import RelaySession
from .relay.engine import (
    RESET_CHAT_COMMAND,
    RELAY_RESPONSE_TIMEOUT_MS,
    TEST_A_REPLY_PREFIX,
    TEST_A_REPLY_SUFFIX,
    TEST_B_REPLY_PREFIX,
    TEST_B_REPLY_SUFFIX,
)


def _print(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def cmd_type(tab_id, text):
    val = core.evaluate(tab_id, make_focus_and_type_js(text))
    _print(val)


def _chatgpt_tabs():
    """Return only targets whose hostname is an approved ChatGPT host."""
    tabs = core.list_tabs()
    if isinstance(tabs, dict):
        return tabs

    allowed_hosts = {
        "chatgpt.com",
        "www.chatgpt.com",
        "chat.openai.com",
    }
    result = []
    for tab in tabs:
        try:
            host = (urlparse(tab.get("url") or "").hostname or "").lower()
        except ValueError:
            host = ""
        if host in allowed_hosts:
            result.append(tab)
    return result


def _print_chatgpt_tabs(tabs):
    if isinstance(tabs, dict):
        _print(tabs)
        return

    if not tabs:
        print("No ChatGPT tabs found.")
        return

    print("ChatGPT tabs:")
    for index, tab in enumerate(tabs, 1):
        title = (tab.get("title") or "(untitled)").strip()
        url = tab.get("url") or ""
        tab_id = tab.get("id") or ""
        print("  %d. %s" % (index, title))
        print("     ID: %s" % tab_id)
        print("     %s" % url)


def _resolve_tab_reference(reference, tabs):
    """Resolve a 1-based displayed number or an exact tab ID."""
    if not reference:
        return None

    for tab in tabs:
        if tab.get("id") == reference:
            return tab

    try:
        index = int(reference)
    except (TypeError, ValueError):
        return None

    if 1 <= index <= len(tabs):
        return tabs[index - 1]
    return None


def _select_tab(label, tabs, input_fn=input):
    while True:
        value = input_fn(
            "Select ChatGPT %s by number or tab ID: " % label
        ).strip()
        tab = _resolve_tab_reference(value, tabs)
        if tab:
            return tab
        print("Invalid selection. Choose one of the listed ChatGPT tabs.")


def _select_rounds(input_fn=input):
    while True:
        value = input_fn("Number of rounds: ").strip()
        try:
            rounds = int(value)
        except ValueError:
            rounds = 0
        if rounds >= 1:
            return rounds
        print("Enter a whole number of rounds greater than zero.")


def _flush_pending_console_input():
    """Best-effort discard of queued Windows console input after Ctrl+C."""
    if os.name != "nt" or not getattr(sys.stdin, "isatty", lambda: False)():
        return False

    try:
        import ctypes

        std_input_handle = -10
        handle = ctypes.windll.kernel32.GetStdHandle(std_input_handle)
        if not handle or handle == -1:
            return False
        return bool(
            ctypes.windll.kernel32.FlushConsoleInputBuffer(handle)
        )
    except Exception:
        return False


def _select_initial_prompt(input_fn=input):
    """Read a complete multiline initial prompt terminated explicitly."""
    terminator = "END PROMPT"

    while True:
        print(
            "Initial prompt (multiline; enter %s on its own line when done):"
            % terminator
        )
        lines = []

        try:
            while True:
                value = input_fn("> ")
                if value.strip() == terminator:
                    break
                lines.append(value)
        except KeyboardInterrupt:
            _flush_pending_console_input()
            print()
            print("Initial prompt cancelled.")
            raise

        prompt = "\n".join(lines).strip()
        if prompt:
            return prompt

        print("Initial prompt cannot be blank.")


def _parse_relay_args(parts):
    positional = []
    rounds = None
    include_text = False
    json_output = False
    prompt_a = False
    initialize = False
    fresh_chats = False
    i = 0

    while i < len(parts):
        part = parts[i]

        if part == "--rounds":
            if i + 1 >= len(parts):
                raise ValueError("--rounds requires a value")
            rounds = int(parts[i + 1])
            i += 2
            continue

        if part.startswith("--rounds="):
            rounds = int(part.split("=", 1)[1])
            i += 1
            continue

        if part == "--include-text":
            include_text = True
            i += 1
            continue

        if part == "--json":
            json_output = True
            i += 1
            continue

        if part == "--prompt-a":
            prompt_a = True
            i += 1
            continue

        if part == "--initialize":
            initialize = True
            i += 1
            continue

        if part == "--fresh-chats":
            fresh_chats = True
            i += 1
            continue

        if part.startswith("--"):
            raise ValueError("unknown relay option: %s" % part)

        positional.append(part)
        i += 1

    if len(positional) > 2:
        raise ValueError(
            "relay accepts at most two tab references"
        )
    if rounds is not None and rounds < 1:
        raise ValueError("--rounds must be at least 1")

    return {
        "tab_a": positional[0] if len(positional) > 0 else None,
        "tab_b": positional[1] if len(positional) > 1 else None,
        "rounds": rounds,
        "include_text": include_text,
        "json_output": json_output,
        "prompt_a": prompt_a,
        "initialize": initialize,
        "fresh_chats": fresh_chats,
    }




def _print_fresh_chat_progress(event):
    label = event.get("label", "?")
    stage = event.get("stage")
    status = event.get("status")

    if stage == "initial_prompt" and status == "starting":
        print(
            f"Chat {label}: submitting INITIAL PROMPT.  DO NOT REPLY."
        )
    elif stage == "initial_prompt" and status == "complete":
        print(f"Chat {label}: conversation established.")


def _print_protocol_progress(event):
    label = event.get("label", "?")
    stage = event.get("stage")
    status = event.get("status")

    if stage == "preflight" and status == "already_active":
        print(f"Chat {label}: protocol already active.")
    elif stage == "provision":
        print(f"Chat {label}: uploading protocol file...")
    elif stage == "attachment_stabilizing":
        seconds = event.get("seconds", 0)
        print(
            f"Chat {label}: waiting {seconds:g}s for protocol upload to settle..."
        )
    elif stage == "protocol_ack":
        print(f"Chat {label}: verifying protocol receipt...")
    elif stage == "protocol_ready":
        print(f"Chat {label}: protocol file verified.")
    elif stage == "activation":
        print(f"Chat {label}: activating protocol...")
    elif stage == "ready":
        print(f"Chat {label}: ready.")


def _status_line(status):
    line = (
        "status={status} state={state} control={control} "
        "rounds={rounds_completed}/{rounds_requested} "
        "transfers={transfers_completed}"
    ).format(**status)

    last = status.get("last_transfer")
    if last:
        line += " last=round-{round}-{direction}".format(**last)
    return line


def _print_transfer_progress(item):
    if not item:
        return
    print(
        "Completed round {round} {direction}: "
        "{source_chars} chars -> {response_chars} chars".format(
            **item
        )
    )


def _print_relay_summary(result, user_stop_requested=False):
    if not isinstance(result, dict):
        print("Relay ended without a structured result.")
        return

    print()
    if user_stop_requested and result.get("status") == "stopped":
        print("Relay stopped by user.")
    else:
        print("Relay finished: %s" % result.get("status", "unknown"))
    print(
        "Rounds completed: %s/%s"
        % (
            result.get("rounds_completed", 0),
            result.get("rounds_requested", "?"),
        )
    )
    print("Transfers completed: %d" % len(result.get("transfers", [])))

    if result.get("error"):
        print(
            "Error: %s (stage: %s)"
            % (
                result.get("error"),
                result.get("stage", "unknown"),
            )
        )

    if result.get("reset_requested"):
        if result.get("reset_propagated"):
            print(
                "Reset coordinated: %s -> %s. Restart point: %s."
                % (
                    result.get("reset_by", "?"),
                    result.get("reset_acknowledged_by", "?"),
                    result.get("restart_point", "A"),
                )
            )
        else:
            print(
                "Reset requested by %s but propagation was not confirmed."
                % result.get("reset_by", "?")
            )


def _command_reader(command_queue, stream):
    """Read interactive commands without blocking relay completion."""
    while True:
        try:
            line = stream.readline()
        except Exception:
            return
        if not line:
            return
        command_queue.put(line.strip().lower())


def _handle_relay_command(command, session):
    """Apply one interactive command; return a user-facing message."""
    command = str(command or "").strip().lower()
    if command in ("p", "pause"):
        session.pause()
        return (
            "Pause requested. It will take effect at the next safe "
            "checkpoint."
        )

    if command in ("r", "resume"):
        session.resume()
        return "Relay resumed."

    if command in ("q", "x", "stop"):
        session.stop()
        return (
            "Stop requested. Any in-flight browser transaction may finish, "
            "but no later transfer will start."
        )

    if command in ("s", "status"):
        return _status_line(session.status())

    if command.startswith("extend chat "):
        value = command[len("extend chat "):].strip()
        try:
            new_total = int(value)
        except ValueError:
            return "Usage: EXTEND CHAT <new total rounds>"

        if new_total < 1:
            return "Usage: EXTEND CHAT <new total rounds>"

        current = session.status().get("rounds_requested", 0)
        if new_total <= current:
            return (
                "Round limit is already %s. EXTEND CHAT must set a "
                "higher total." % current
            )

        try:
            updated = session.extend_rounds(new_total)
        except ValueError as exc:
            return "Could not extend chat: %s" % exc

        return "Round limit extended to %d." % updated

    if not command:
        return None

    return (
        "Commands: p=pause, r=resume, s=status, q=stop, "
        "EXTEND CHAT <new total rounds>"
    )


def _poll_chat_reset(session):
    """Detect exact RESET CHAT user input while the relay is parked."""
    request_reset = getattr(session, "request_reset", None)
    if not callable(request_reset):
        return None

    for label, attr in (("A", "tab_a"), ("B", "tab_b")):
        tab_id = getattr(session, attr, None)
        if not tab_id:
            continue
        try:
            state = workflows.read_turn_state(tab_id)
        except Exception:
            continue
        if not isinstance(state, dict) or not state.get("ok"):
            continue
        user = state.get("user") or {}
        if (user.get("text") or "").strip() == RESET_CHAT_COMMAND:
            request_reset(label)
            return label

    return None


def _run_interactive_relay(
    session,
    *,
    input_stream=None,
    output_json=False,
    sleep=time.sleep,
):
    if input_stream is None:
        input_stream = sys.stdin

    session.start()
    print("Relay started.")
    print(
        "Commands: p=pause, r=resume, s=status, q=stop, "
        "EXTEND CHAT <new total rounds>"
    )

    commands = queue.Queue()
    reader = threading.Thread(
        target=_command_reader,
        args=(commands, input_stream),
        name="parley-relay-input",
        daemon=True,
    )
    reader.start()

    seen_transfers = 0
    round_limit_prompted = False
    awaiting_extension_total = False
    reset_detection_requested = False
    user_stop_requested = False

    try:
        while session.is_alive():
            status = session.status()
            transfer_count = status["transfers_completed"]

            if status.get("awaiting_extension"):
                if not reset_detection_requested:
                    reset_label = _poll_chat_reset(session)
                    if reset_label:
                        print()
                        print(
                            "RESET CHAT detected in ChatGPT %s. "
                            "Coordinating reset..." % reset_label
                        )
                        reset_detection_requested = True
                        sleep(0)
                        continue

                if not round_limit_prompted:
                    print()
                    print(
                        "Round limit reached at %d. "
                        "Extend session? [y/N] (no timeout)"
                        % status.get("rounds_requested", 0)
                    )
                    round_limit_prompted = True
            else:
                round_limit_prompted = False
                awaiting_extension_total = False
                reset_detection_requested = False

            if transfer_count > seen_transfers:
                last_transfer = status.get("last_transfer")
                _print_transfer_progress(last_transfer)
                seen_transfers = transfer_count

            try:
                command = commands.get(timeout=0.2)
            except queue.Empty:
                sleep(0)
                continue

            if status.get("awaiting_extension"):
                current_limit = status.get("rounds_requested", 0)

                if awaiting_extension_total:
                    if command in ("n", "no", ""):
                        session.finish_at_round_limit()
                        print(
                            "Round limit accepted. "
                            "Finishing the relay cleanly."
                        )
                        awaiting_extension_total = False
                        continue

                    if command in ("q", "x", "stop"):
                        user_stop_requested = True
                        message = _handle_relay_command(command, session)
                        if message:
                            print(message)
                        awaiting_extension_total = False
                        continue

                    if command.startswith("extend chat "):
                        message = _handle_relay_command(command, session)
                        if message:
                            print(message)
                        awaiting_extension_total = False
                        round_limit_prompted = False
                        continue

                    try:
                        new_total = int(command)
                    except ValueError:
                        new_total = 0

                    if new_total <= current_limit:
                        print(
                            "Enter a new total round limit greater than %d:"
                            % current_limit
                        )
                        continue

                    try:
                        updated = session.extend_rounds(new_total)
                    except ValueError as exc:
                        print("Could not extend chat: %s" % exc)
                        continue

                    print("Round limit extended to %d." % updated)
                    awaiting_extension_total = False
                    round_limit_prompted = False
                    continue

                if command in ("y", "yes"):
                    print(
                        "New total rounds (must be greater than %d):"
                        % current_limit
                    )
                    awaiting_extension_total = True
                    continue

                if command in ("n", "no", ""):
                    session.finish_at_round_limit()
                    print(
                        "Round limit accepted. Finishing the relay cleanly."
                    )
                    continue

                if command.startswith("extend chat "):
                    message = _handle_relay_command(command, session)
                    if message:
                        print(message)
                    round_limit_prompted = False
                    continue

                if command in ("q", "x", "stop", "s", "status"):
                    if command in ("q", "x", "stop"):
                        user_stop_requested = True
                    message = _handle_relay_command(command, session)
                    if message:
                        print(message)
                    continue

                print(
                    "Answer y or n, or use "
                    "EXTEND CHAT <new total rounds>."
                )
                continue

            if command in ("q", "x", "stop"):
                user_stop_requested = True
            message = _handle_relay_command(command, session)
            if message:
                print(message)
    except KeyboardInterrupt:
        print()
        print(
            "Interrupt received. Requesting safe stop after the "
            "current browser transaction."
        )
        user_stop_requested = True
        session.stop()

    result = session.join()

    # Pick up a final transfer that may have completed between loop polls.
    status = session.status()
    if status["transfers_completed"] > seen_transfers:
        _print_transfer_progress(status.get("last_transfer"))

    if session.exception is not None:
        print("Relay worker failed: %s" % session.exception)
        return 1

    _print_relay_summary(
        result,
        user_stop_requested=user_stop_requested,
    )
    if output_json:
        _print(result)
    clean_result = bool(
        isinstance(result, dict)
        and (
            result.get("status") == "complete"
            or (
                result.get("status") == "stopped"
                and user_stop_requested
            )
            or (
                result.get("status") == "stopped"
                and result.get("reset_requested")
                and result.get("reset_propagated")
            )
        )
    )
    return 0 if clean_result else 1


def cmd_relay(parts, input_fn=input, input_stream=None):
    options = _parse_relay_args(parts)

    if options["fresh_chats"]:
        if options["tab_a"] or options["tab_b"]:
            print("--fresh-chats cannot be combined with tab references.")
            return 2
        if not options["initialize"]:
            print("--fresh-chats requires --initialize.")
            return 2

        print("Creating fresh ChatGPT A and B tabs...")
        fresh = workflows.create_fresh_chatgpt_pair(
            progress=_print_fresh_chat_progress,
        )
        if not isinstance(fresh, dict) or not fresh.get("ok"):
            error = (
                fresh.get("error")
                if isinstance(fresh, dict)
                else str(fresh)
            )
            stage = (
                fresh.get("stage", "create_fresh_chat")
                if isinstance(fresh, dict)
                else "create_fresh_chat"
            )
            participant = (
                fresh.get("participant")
                if isinstance(fresh, dict)
                else None
            )
            print(
                "Could not create fresh ChatGPT tabs"
                + (f" for Chat {participant}" if participant else "")
                + f" during {stage}: {error}"
            )
            if options["json_output"]:
                _print(fresh)
            return 1

        tab_a = fresh["A"]
        tab_b = fresh["B"]
        print("Fresh ChatGPT A: %s" % tab_a["id"])
        print("Fresh ChatGPT B: %s" % tab_b["id"])
    else:
        tabs = _chatgpt_tabs()

        if isinstance(tabs, dict):
            _print(tabs)
            return 1

        if len(tabs) < 2:
            print("At least two ChatGPT tabs are required for a relay.")
            _print_chatgpt_tabs(tabs)
            return 1

        tab_a = _resolve_tab_reference(options["tab_a"], tabs)
        tab_b = _resolve_tab_reference(options["tab_b"], tabs)

        if options["tab_a"] and tab_a is None:
            print("Could not resolve ChatGPT A: %s" % options["tab_a"])
            _print_chatgpt_tabs(tabs)
            return 1

        if options["tab_b"] and tab_b is None:
            print("Could not resolve ChatGPT B: %s" % options["tab_b"])
            _print_chatgpt_tabs(tabs)
            return 1

        if tab_a is None or tab_b is None:
            _print_chatgpt_tabs(tabs)

        if tab_a is None:
            tab_a = _select_tab("A", tabs, input_fn=input_fn)

        if tab_b is None:
            tab_b = _select_tab("B", tabs, input_fn=input_fn)

    if tab_a["id"] == tab_b["id"]:
        print("ChatGPT A and B must be different tabs.")
        return 1

    if options["initialize"]:
        print()
        print("Provisioning and initializing Parley protocols...")
        init_result = workflows.initialize_parley_pair(
            tab_a["id"],
            tab_b["id"],
            progress=_print_protocol_progress,
        )
        if (
            not isinstance(init_result, dict)
            or not init_result.get("ok")
        ):
            error = (
                init_result.get("error")
                if isinstance(init_result, dict)
                else str(init_result)
            )
            stage = (
                init_result.get("stage", "bootstrap")
                if isinstance(init_result, dict)
                else "bootstrap"
            )
            participant = (
                init_result.get("participant")
                if isinstance(init_result, dict)
                else None
            )
            print(
                "Protocol initialization failed"
                + (f" for Chat {participant}" if participant else "")
                + f" during {stage}: {error}"
            )
            if options["json_output"]:
                _print(init_result)
            return 1
        print("Parley protocol bootstrap complete.")

    if options["rounds"] is None:
        options["rounds"] = _select_rounds(input_fn=input_fn)

    initial_context = None
    if options["prompt_a"]:
        initial_prompt = _select_initial_prompt(input_fn=input_fn)
        print()
        print("Sending initial prompt to ChatGPT A...")
        initial_result = workflows.send_and_wait(
            tab_a["id"],
            initial_prompt,
            wait_timeout_ms=RELAY_RESPONSE_TIMEOUT_MS,
            expected_reply_prefix=TEST_A_REPLY_PREFIX,
            expected_reply_suffix=TEST_A_REPLY_SUFFIX,
        )
        if (
            not isinstance(initial_result, dict)
            or initial_result.get("error")
            or not initial_result.get("response_complete")
        ):
            print("Initial prompt to ChatGPT A failed.")
            if options["json_output"]:
                _print(initial_result)
            return 1

        initial_text = (initial_result.get("response_text") or "").strip()
        if initial_text == RESET_CHAT_COMMAND:
            print(
                "ChatGPT A returned RESET CHAT. "
                "Synchronizing reset to ChatGPT B..."
            )
            reset_result = workflows.send_and_wait(
                tab_b["id"],
                RESET_CHAT_COMMAND,
                wait_timeout_ms=RELAY_RESPONSE_TIMEOUT_MS,
                expected_reply_prefix=TEST_B_REPLY_PREFIX,
                expected_reply_suffix=TEST_B_REPLY_SUFFIX,
            )
            reset_text = (
                (reset_result.get("response_text") or "").strip()
                if isinstance(reset_result, dict)
                else ""
            )
            if (
                not isinstance(reset_result, dict)
                or reset_result.get("error")
                or not reset_result.get("response_complete")
                or reset_text != RESET_CHAT_COMMAND
            ):
                print("Could not confirm RESET CHAT from ChatGPT B.")
                if options["json_output"]:
                    _print(reset_result)
                return 1

            print(
                "Reset coordinated: A -> B. "
                "Restart point: A."
            )
            return 0

        print(
            "ChatGPT A completed the starting reply (%d chars)."
            % len(initial_text)
        )
        initial_context = initial_prompt

    print()
    print("ChatGPT A: %s" % (tab_a.get("title") or tab_a["id"]))
    print("ChatGPT B: %s" % (tab_b.get("title") or tab_b["id"]))
    print("Rounds: %d" % options["rounds"])
    print()

    session = RelaySession(
        workflows.bridge,
        tab_a["id"],
        tab_b["id"],
        options["rounds"],
        include_text=options["include_text"],
        initial_context=initial_context,
    )

    return _run_interactive_relay(
        session,
        input_stream=input_stream,
        output_json=options["json_output"],
    )


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)

    explicit_mode = None
    if "--live" in argv[1:]:
        explicit_mode = "live"
        argv.remove("--live")
    if "--classic" in argv[1:]:
        if explicit_mode is not None:
            print("Command error: choose only one of --live or --classic")
            return 2
        explicit_mode = "classic"
        argv.remove("--classic")

    if len(argv) < 2:
        print(__doc__)
        return 0

    cmd = argv[1]

    if explicit_mode is not None:
        os.environ["PARLEY_CONNECTION_MODE"] = explicit_mode
    elif (
        cmd in ("chats", "app", "relay")
        and "PARLEY_CONNECTION_MODE" not in os.environ
    ):
        # The primary relay workflow targets the user's already-running,
        # signed-in Chrome session.
        os.environ["PARLEY_CONNECTION_MODE"] = "live"

    try:
        if cmd == "list":
            _print(core.list_tabs())

        elif cmd == "chats":
            _print_chatgpt_tabs(_chatgpt_tabs())

        elif cmd == "read":
            _print(workflows.read_response(argv[2]))

        elif cmd == "send":
            _print(workflows.send(argv[2], " ".join(argv[3:])))

        elif cmd == "send-wait":
            parts = argv[3:]
            timeout_ms = 60000
            text_parts = []
            i = 0
            while i < len(parts):
                p = parts[i]
                if p == "--timeout":
                    timeout_ms = int(parts[i + 1])
                    i += 2
                    continue
                if p.startswith("--timeout="):
                    timeout_ms = int(p.split("=", 1)[1])
                    i += 1
                    continue
                text_parts.append(p)
                i += 1
            _print(
                workflows.send_and_wait(
                    argv[2],
                    " ".join(text_parts),
                    wait_timeout_ms=timeout_ms,
                )
            )

        elif cmd == "type":
            cmd_type(argv[2], " ".join(argv[3:]))

        elif cmd == "click":
            _print(core.click(argv[2], argv[3]))

        elif cmd == "navigate":
            _print(core.navigate(argv[2], argv[3]))

        elif cmd == "eval":
            _print(
                core.evaluate(
                    argv[2],
                    " ".join(argv[3:]),
                    await_promise=True,
                )
            )

        elif cmd == "wait-stream":
            timeout_ms = int(argv[3]) if len(argv) > 3 else 60000
            silence_ms = int(argv[4]) if len(argv) > 4 else 1500
            _print(
                workflows.wait_stream(
                    argv[2],
                    timeout_ms=timeout_ms,
                    silence_ms=silence_ms,
                )
            )

        elif cmd == "poll":
            interval_ms = int(argv[3]) if len(argv) > 3 else 2000
            _print(workflows.poll(argv[2], interval_ms=interval_ms))

        elif cmd == "bridge":
            rounds = int(argv[4]) if len(argv) > 4 else 3
            _print(
                workflows.bridge(
                    argv[2],
                    argv[3],
                    rounds=rounds,
                )
            )

        elif cmd == "relay":
            return cmd_relay(argv[2:])

        elif cmd == "app":
            from .app import launch
            return launch()

        elif cmd == "read-dom":
            selector = argv[3] if len(argv) > 3 else None
            _print(core.read_dom(argv[2], selector))

        elif cmd == "extract":
            attr = argv[4] if len(argv) > 4 else None
            _print(core.extract(argv[2], argv[3], attr=attr))

        elif cmd == "wait-for":
            timeout_ms = int(argv[4]) if len(argv) > 4 else 10000
            _print(
                core.wait_for(
                    argv[2],
                    argv[3],
                    timeout_ms=timeout_ms,
                )
            )

        elif cmd == "cookies":
            domain = argv[3] if len(argv) > 3 else None
            _print(core.get_cookies(argv[2], domain=domain))

        elif cmd == "set-cookie":
            path = argv[6] if len(argv) > 6 else "/"
            _print(
                core.set_cookie(
                    argv[2],
                    argv[3],
                    argv[4],
                    argv[5],
                    path=path,
                )
            )

        else:
            print("Unknown command: %s" % cmd)
            print(__doc__)
            return 2

    except (IndexError, ValueError) as exc:
        print("Command error: %s" % exc)
        print()
        print(__doc__)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())

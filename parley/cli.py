"""Parley CLI - browser automation and two-ChatGPT relay controls.

Common commands:
    parley list
    parley chats
    parley read <tab_id>
    parley send <tab_id> <text...>
    parley send-wait <tab_id> <text...> [--timeout N]
    parley bridge <tab_a> <tab_b> [rounds=3]
    parley relay [tab_a] [tab_b] [--rounds N] [--include-text] [--json]

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

Environment:
    PARLEY_CDP_HOST   CDP host (default: localhost)
    PARLEY_CDP_PORT   CDP port (default: 9222)
"""

import json
import queue
import sys
import threading
import time

from . import core
from . import workflows
from .adapters.js import make_focus_and_type_js
from .relay import RelaySession


def _print(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def cmd_type(tab_id, text):
    val = core.evaluate(tab_id, make_focus_and_type_js(text))
    _print(val)


def _chatgpt_tabs():
    """Return only identifiable ChatGPT page targets."""
    tabs = core.list_tabs()
    if isinstance(tabs, dict):
        return tabs
    return [
        tab for tab in tabs
        if (tab.get("url") or "").startswith(
            ("https://chatgpt.com", "https://chat.openai.com")
        )
    ]


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


def _parse_relay_args(parts):
    positional = []
    rounds = 3
    include_text = False
    json_output = False
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

        if part.startswith("--"):
            raise ValueError("unknown relay option: %s" % part)

        positional.append(part)
        i += 1

    if len(positional) > 2:
        raise ValueError(
            "relay accepts at most two tab references"
        )
    if rounds < 1:
        raise ValueError("--rounds must be at least 1")

    return {
        "tab_a": positional[0] if len(positional) > 0 else None,
        "tab_b": positional[1] if len(positional) > 1 else None,
        "rounds": rounds,
        "include_text": include_text,
        "json_output": json_output,
    }


def _status_line(status):
    return (
        "status={status} state={state} control={control} "
        "rounds={rounds_completed}/{rounds_requested} "
        "transfers={transfers_completed}"
    ).format(**status)


def _print_transfer_progress(item):
    if not item:
        return
    print(
        "Completed round {round} {direction}: "
        "{source_chars} chars -> {response_chars} chars".format(
            **item
        )
    )


def _print_relay_summary(result):
    if not isinstance(result, dict):
        print("Relay ended without a structured result.")
        return

    print()
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

    if not command:
        return None

    return "Commands: p=pause, r=resume, s=status, q=stop"


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
    print("Commands: p=pause, r=resume, s=status, q=stop")

    commands = queue.Queue()
    reader = threading.Thread(
        target=_command_reader,
        args=(commands, input_stream),
        name="parley-relay-input",
        daemon=True,
    )
    reader.start()

    seen_transfers = 0

    while session.is_alive():
        status = session.status()
        transfer_count = status["transfers_completed"]

        if transfer_count > seen_transfers:
            last_transfer = status.get("last_transfer")
            _print_transfer_progress(last_transfer)
            seen_transfers = transfer_count

        try:
            command = commands.get(timeout=0.2)
        except queue.Empty:
            sleep(0)
            continue

        message = _handle_relay_command(command, session)
        if message:
            print(message)

    result = session.join()

    # Pick up a final transfer that may have completed between loop polls.
    status = session.status()
    if status["transfers_completed"] > seen_transfers:
        _print_transfer_progress(status.get("last_transfer"))

    if session.exception is not None:
        print("Relay worker failed: %s" % session.exception)
        return 1

    _print_relay_summary(result)
    if output_json:
        _print(result)
    return 0 if isinstance(result, dict) and result.get("status") == "complete" else 1


def cmd_relay(parts, input_fn=input, input_stream=None):
    options = _parse_relay_args(parts)
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
    )

    return _run_interactive_relay(
        session,
        input_stream=input_stream,
        output_json=options["json_output"],
    )


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    if len(argv) < 2:
        print(__doc__)
        return 0

    cmd = argv[1]

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

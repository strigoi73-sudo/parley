"""Safety-hardened deterministic two-ChatGPT relay engine.

This module owns conversation sequencing and relay-level safety only.
Browser/CDP behavior and ChatGPT DOM extraction remain in their own layers.
"""

from .audit import audit_event
from .control import (
    PAUSED as CONTROL_PAUSED,
    RelayControl,
)
from .dedupe import DuplicateGuard, turn_text_hash
from .state import (
    COMPLETE,
    ERROR,
    IDLE,
    PAUSED,
    PREPARE,
    READ_A,
    STOPPED,
    TRANSFER_A_TO_B,
    TRANSFER_B_TO_A,
)


TEST_A_REPLY_PREFIX = "A REPLY:"
TEST_A_REPLY_SUFFIX = "A REPLY END"
TEST_B_REPLY_PREFIX = "B REPLY:"
TEST_B_REPLY_SUFFIX = "B REPLY END"
RESET_CHAT_COMMAND = "RESET CHAT"


def _is_reset_command(text):
    return str(text or "").strip() == RESET_CHAT_COMMAND


def _has_reply_prefix(text, prefix):
    return str(text or "").lstrip().startswith(prefix)


def _has_reply_suffix(text, suffix):
    return str(text or "").rstrip().endswith(suffix)


def _has_reply_markers(text, prefix, suffix):
    return (
        _has_reply_prefix(text, prefix)
        and _has_reply_suffix(text, suffix)
    )


def _initial_turn(value):
    """Normalize the starting completed assistant turn from ChatGPT A."""
    if not isinstance(value, dict):
        return None, "initial_response_invalid"

    if value.get("ok") is False:
        return None, value.get("error", "initial_response_error")

    if value.get("source") != "chatgpt-strict":
        return None, "initial_response_not_strict_chatgpt"

    text = (value.get("text") or "").strip()
    if not text:
        return None, "initial_response_empty"

    if value.get("hasStreaming") or value.get("hasStopButton"):
        return None, "initial_response_incomplete"

    return {
        "text": text,
        "turn_id": value.get("turn_id"),
        "turn_index": value.get("turn_index"),
        "source": "chatgpt-strict",
    }, None


def _completed_reply(value):
    """Normalize a completed send_and_wait result."""
    if not isinstance(value, dict):
        return None, "relay_response_invalid"

    if value.get("error"):
        return None, value.get("error")

    if not value.get("response_complete"):
        return None, "relay_response_incomplete"

    if value.get("response_source") != "chatgpt-strict":
        return None, "relay_response_not_strict_chatgpt"

    text = (value.get("response_text") or "").strip()
    if not text:
        return None, "relay_response_empty"

    return {
        "text": text,
        "turn_id": value.get("response_turn_id"),
        "turn_index": value.get("response_turn_index"),
        "source": "chatgpt-strict",
    }, None


def _transfer_record(
    round_number,
    direction,
    source_tab,
    destination_tab,
    source_turn,
    response_turn,
    include_text=False,
):
    record = {
        "round": round_number,
        "direction": direction,
        "source_tab": source_tab,
        "destination_tab": destination_tab,
        "source_turn_id": source_turn.get("turn_id"),
        "source_turn_index": source_turn.get("turn_index"),
        "source_chars": len(source_turn["text"]),
        "source_hash": turn_text_hash(source_turn),
        "response_turn_id": response_turn.get("turn_id"),
        "response_turn_index": response_turn.get("turn_index"),
        "response_chars": len(response_turn["text"]),
        "response_hash": turn_text_hash(response_turn),
    }
    if include_text:
        record["source_text"] = source_turn["text"]
        record["response_text"] = response_turn["text"]
    return record


def _validation_result(validate_tab, tab_id):
    """Normalize an optional tab-validation callback."""
    if validate_tab is None:
        return {"ok": True}

    try:
        value = validate_tab(tab_id)
    except Exception as exc:
        return {
            "ok": False,
            "error": "relay_tab_validation_exception",
            "detail": str(exc),
        }

    if value is True:
        return {"ok": True}
    if value is False or value is None:
        return {
            "ok": False,
            "error": "relay_tab_validation_failed",
        }
    if isinstance(value, dict):
        if value.get("ok"):
            return value
        return {
            "ok": False,
            "error": value.get(
                "error",
                "relay_tab_validation_failed",
            ),
            "detail": value,
        }

    return {
        "ok": False,
        "error": "relay_tab_validation_invalid_result",
        "detail": repr(value),
    }


def run_bidirectional_relay(
    tab_a,
    tab_b,
    rounds,
    *,
    read_response,
    send_and_wait,
    read_turn_state=None,
    validate_tab=None,
    control=None,
    include_text=False,
    event_sink=None,
    initial_context=None,
):
    """Run a guarded A -> B -> A relay for a fixed number of rounds.

    One round is one full back-and-forth exchange:
        A(n) -> B => B(n)
        B(n) -> A => A(n+1)

    Phase-5 safety properties:
    - duplicate source turns are never delivered twice;
    - pause/stop are checked between every externally visible operation;
    - source/destination tabs may be validated before every transfer;
    - callback failures become structured relay errors;
    - audit records default to hashes/metadata rather than full message text.
    """
    if control is None:
        control = RelayControl()

    guard = DuplicateGuard()

    def current_round_limit():
        value = getattr(control, "round_limit", None)
        return rounds if value is None else value

    result = {
        "status": "running",
        "state": IDLE,
        "state_history": [IDLE],
        "tab_a": tab_a,
        "tab_b": tab_b,
        "rounds_requested": current_round_limit(),
        "rounds_completed": 0,
        "transfers": [],
        "audit": [],
    }

    def emit(item):
        if event_sink is None:
            return
        try:
            event_sink(dict(item))
        except Exception:
            # Observability must never be able to break relay execution.
            pass

    def record(event, **fields):
        item = audit_event(event, **fields)
        result["audit"].append(item)
        emit(item)

    def transition(state):
        result["state"] = state
        if not result["state_history"] or result["state_history"][-1] != state:
            result["state_history"].append(state)
        emit({
            "event": "state_changed",
            "state": state,
        })

    def fail(error, stage, round_number=None, detail=None):
        transition(ERROR)
        result["status"] = "error"
        result["error"] = error
        result["stage"] = stage
        if round_number is not None:
            result["round"] = round_number
        if detail is not None:
            result["detail"] = detail
        record(
            "relay_error",
            error=error,
            stage=stage,
            round=round_number,
        )
        return result

    def stopped(stage, round_number=None):
        transition(STOPPED)
        result["status"] = "stopped"
        result["stage"] = stage
        if round_number is not None:
            result["round"] = round_number
        record(
            "relay_stopped",
            stage=stage,
            round=round_number,
        )
        return result

    def coordinated_reset(label, stage, round_number=None):
        counterpart_label = "B" if label == "A" else "A"
        counterpart_tab = tab_b if label == "A" else tab_a
        expected_prefix = (
            TEST_B_REPLY_PREFIX if label == "A" else TEST_A_REPLY_PREFIX
        )
        expected_suffix = (
            TEST_B_REPLY_SUFFIX if label == "A" else TEST_A_REPLY_SUFFIX
        )

        result["reset_requested"] = True
        result["reset_by"] = label
        result["reset_propagated"] = False
        result["restart_point"] = "A"
        result["awaiting_human_restart"] = True
        if round_number is not None:
            result["round"] = round_number

        record(
            "relay_reset_requested",
            chat=label,
            stage=stage,
            round=round_number,
        )

        validation_failure = validate(
            counterpart_label,
            counterpart_tab,
            "reset_propagation",
            round_number,
        )
        if validation_failure:
            return validation_failure

        record(
            "relay_reset_propagation_started",
            source_chat=label,
            destination_chat=counterpart_label,
            round=round_number,
        )

        try:
            raw = send_and_wait(
                counterpart_tab,
                RESET_CHAT_COMMAND,
                expected_reply_prefix=expected_prefix,
                expected_reply_suffix=expected_suffix,
            )
        except Exception as exc:
            return fail(
                "relay_reset_propagation_failed",
                "reset_propagation",
                round_number=round_number,
                detail={
                    "source_chat": label,
                    "destination_chat": counterpart_label,
                    "exception": str(exc),
                },
            )

        reply, error = _completed_reply(raw)
        if error or not _is_reset_command((reply or {}).get("text")):
            return fail(
                "relay_reset_propagation_failed",
                "reset_propagation",
                round_number=round_number,
                detail={
                    "source_chat": label,
                    "destination_chat": counterpart_label,
                    "response_error": error,
                    "response_text": (
                        (reply or {}).get("text")
                        if reply is not None
                        else raw.get("response_text")
                        if isinstance(raw, dict)
                        else None
                    ),
                },
            )

        result["reset_propagated"] = True
        result["reset_acknowledged_by"] = counterpart_label
        record(
            "relay_reset_propagated",
            source_chat=label,
            destination_chat=counterpart_label,
            round=round_number,
        )

        transition(STOPPED)
        result["status"] = "stopped"
        result["stage"] = "reset"
        record(
            "relay_stopped",
            stage="reset",
            round=round_number,
        )
        return result

    def external_reset_requested(label, tab_id, cached_turn):
        if cached_turn is None:
            return False

        if callable(read_turn_state):
            try:
                state = read_turn_state(tab_id)
            except Exception:
                state = None
            if isinstance(state, dict) and state.get("ok"):
                user = state.get("user") or {}
                if _is_reset_command(user.get("text")):
                    return True

        try:
            raw = read_response(tab_id)
        except Exception:
            return False
        latest, error = _initial_turn(raw)
        if error or latest is None:
            return False
        if not _is_reset_command(latest.get("text")):
            return False
        return (
            latest.get("turn_id") != cached_turn.get("turn_id")
            or latest.get("turn_index") != cached_turn.get("turn_index")
            or latest.get("text") != cached_turn.get("text")
        )

    def checkpoint(stage, round_number=None):
        resume_state = result["state"]
        was_paused = control.state == CONTROL_PAUSED
        if was_paused:
            transition(PAUSED)
            record(
                "relay_paused",
                stage=stage,
                round=round_number,
            )

        try:
            runnable = control.wait_until_runnable()
        except Exception as exc:
            return fail(
                "relay_control_exception",
                stage,
                round_number=round_number,
                detail=str(exc),
            )

        if not runnable:
            return stopped(stage, round_number)

        if was_paused:
            record(
                "relay_resumed",
                stage=stage,
                round=round_number,
            )
            transition(resume_state)
        return None

    def validate(label, tab_id, stage, round_number=None):
        checked = _validation_result(validate_tab, tab_id)
        if not checked.get("ok"):
            return fail(
                checked.get("error", "relay_tab_validation_failed"),
                stage,
                round_number=round_number,
                detail={
                    "label": label,
                    "tab_id": tab_id,
                    "validation": checked,
                },
            )
        record(
            "tab_validated",
            label=label,
            tab_id=tab_id,
            stage=stage,
            round=round_number,
        )
        return None

    def claim(source_tab, destination_tab, turn, direction, round_number):
        ok, fingerprint = guard.claim(
            source_tab,
            destination_tab,
            turn,
        )
        if not ok:
            record(
                "duplicate_blocked",
                direction=direction,
                round=round_number,
                source_tab=source_tab,
                destination_tab=destination_tab,
                fingerprint=fingerprint,
            )
            return fail(
                "relay_duplicate_source_turn",
                direction.lower().replace("->", "_to_"),
                round_number=round_number,
                detail={
                    "direction": direction,
                    "fingerprint": fingerprint,
                },
            )
        return fingerprint

    record(
        "relay_started",
        tab_a=tab_a,
        tab_b=tab_b,
        rounds=rounds,
    )
    transition(PREPARE)

    checkpoint_result = checkpoint("prepare")
    if checkpoint_result:
        return checkpoint_result

    if not tab_a or not tab_b:
        return fail("relay_tab_missing", "prepare")

    if tab_a == tab_b:
        return fail("relay_tabs_must_be_distinct", "prepare")

    if not isinstance(rounds, int) or isinstance(rounds, bool) or rounds < 1:
        return fail("relay_rounds_must_be_positive_integer", "prepare")

    if getattr(control, "round_limit", None) is None:
        setter = getattr(control, "set_round_limit", None)
        if callable(setter):
            setter(rounds)

    for label, tab_id in (("A", tab_a), ("B", tab_b)):
        validation_failure = validate(label, tab_id, "prepare")
        if validation_failure:
            return validation_failure

    transition(READ_A)
    checkpoint_result = checkpoint("read_a")
    if checkpoint_result:
        return checkpoint_result

    validation_failure = validate("A", tab_a, "read_a")
    if validation_failure:
        return validation_failure

    try:
        initial_raw = read_response(tab_a)
    except Exception as exc:
        return fail(
            "relay_read_exception",
            "read_a",
            detail=str(exc),
        )

    current_a, error = _initial_turn(initial_raw)
    if error:
        return fail(error, "read_a", detail=initial_raw)

    record(
        "initial_turn_read",
        tab_id=tab_a,
        turn_id=current_a.get("turn_id"),
        turn_index=current_a.get("turn_index"),
        chars=len(current_a["text"]),
        text_hash=turn_text_hash(current_a),
    )

    # A marked starting response opts this relay run into the conversation-local
    # Parley test protocol. Ordinary/production ChatGPT relays remain unchanged.
    test_protocol = _has_reply_prefix(
        current_a["text"],
        TEST_A_REPLY_PREFIX,
    )
    result["test_protocol"] = test_protocol
    if test_protocol:
        if not _has_reply_suffix(
            current_a["text"],
            TEST_A_REPLY_SUFFIX,
        ):
            return fail(
                "initial_test_reply_end_marker_missing",
                "read_a",
                detail={
                    "expected_suffix": TEST_A_REPLY_SUFFIX,
                    "response_text": current_a["text"],
                },
            )
        result["test_protocol_terminal_markers"] = True
        record(
            "test_protocol_activated",
            a_prefix=TEST_A_REPLY_PREFIX,
            a_suffix=TEST_A_REPLY_SUFFIX,
            b_prefix=TEST_B_REPLY_PREFIX,
            b_suffix=TEST_B_REPLY_SUFFIX,
        )

    current_b = None

    round_number = 1
    observed_round_limit = current_round_limit()
    while round_number <= current_round_limit():
        active_round_limit = current_round_limit()
        if active_round_limit != observed_round_limit:
            if active_round_limit > observed_round_limit:
                record(
                    "relay_round_limit_extended",
                    previous_round_limit=observed_round_limit,
                    round_limit=active_round_limit,
                    round=round_number,
                )
            observed_round_limit = active_round_limit
        result["rounds_requested"] = active_round_limit

        transition(TRANSFER_A_TO_B)

        checkpoint_result = checkpoint(
            "a_to_b",
            round_number,
        )
        if checkpoint_result:
            return checkpoint_result

        for label, tab_id in (("A", tab_a), ("B", tab_b)):
            validation_failure = validate(
                label,
                tab_id,
                "a_to_b",
                round_number,
            )
            if validation_failure:
                return validation_failure

        if test_protocol:
            if external_reset_requested("A", tab_a, current_a):
                return coordinated_reset(
                    "A",
                    "a_to_b",
                    round_number,
                )
            if (
                current_b is not None
                and external_reset_requested("B", tab_b, current_b)
            ):
                return coordinated_reset(
                    "B",
                    "a_to_b",
                    round_number,
                )

        fingerprint = claim(
            tab_a,
            tab_b,
            current_a,
            "A->B",
            round_number,
        )
        if isinstance(fingerprint, dict):
            return fingerprint

        record(
            "transfer_started",
            round=round_number,
            direction="A->B",
            source_tab=tab_a,
            destination_tab=tab_b,
            source_turn_id=current_a.get("turn_id"),
            source_turn_index=current_a.get("turn_index"),
            source_chars=len(current_a["text"]),
            source_hash=turn_text_hash(current_a),
            fingerprint=fingerprint,
        )

        delivery_text = current_a["text"]
        if round_number == 1 and str(initial_context or "").strip():
            shared_context = str(initial_context).strip()
            delivery_text = (
                "PARLEY SESSION BRIEF "
                "(human-provided; applies to both Chat A and Chat B):\n"
                + shared_context
                + "\n\nCHAT A OPENING REPLY:\n"
                + current_a["text"]
            )
            record(
                "initial_context_attached",
                round=round_number,
                direction="A->B",
                context_chars=len(shared_context),
                context_hash=turn_text_hash({"text": shared_context}),
                delivered_chars=len(delivery_text),
            )

        try:
            if test_protocol:
                b_raw = send_and_wait(
                    tab_b,
                    delivery_text,
                    expected_reply_prefix=TEST_B_REPLY_PREFIX,
                    expected_reply_suffix=TEST_B_REPLY_SUFFIX,
                )
            else:
                b_raw = send_and_wait(tab_b, delivery_text)
        except Exception as exc:
            return fail(
                "relay_transfer_exception",
                "a_to_b",
                round_number=round_number,
                detail=str(exc),
            )

        current_b, error = _completed_reply(b_raw)
        if error:
            return fail(
                error,
                "a_to_b",
                round_number=round_number,
                detail=b_raw,
            )

        if test_protocol:
            if _is_reset_command(current_b["text"]):
                return coordinated_reset(
                    "B",
                    "a_to_b",
                    round_number,
                )
            if not _has_reply_markers(
                current_b["text"],
                TEST_B_REPLY_PREFIX,
                TEST_B_REPLY_SUFFIX,
            ):
                return fail(
                    "relay_test_reply_marker_missing",
                    "a_to_b",
                    round_number=round_number,
                    detail={
                        "expected_prefix": TEST_B_REPLY_PREFIX,
                        "expected_suffix": TEST_B_REPLY_SUFFIX,
                        "response_text": current_b["text"],
                    },
                )

        transfer = _transfer_record(
            round_number,
            "A->B",
            tab_a,
            tab_b,
            current_a,
            current_b,
            include_text=include_text,
        )
        result["transfers"].append(transfer)
        record("transfer_completed", **transfer)

        transition(TRANSFER_B_TO_A)

        checkpoint_result = checkpoint(
            "b_to_a",
            round_number,
        )
        if checkpoint_result:
            return checkpoint_result

        for label, tab_id in (("B", tab_b), ("A", tab_a)):
            validation_failure = validate(
                label,
                tab_id,
                "b_to_a",
                round_number,
            )
            if validation_failure:
                return validation_failure

        if test_protocol:
            if external_reset_requested("B", tab_b, current_b):
                return coordinated_reset(
                    "B",
                    "b_to_a",
                    round_number,
                )
            if external_reset_requested("A", tab_a, current_a):
                return coordinated_reset(
                    "A",
                    "b_to_a",
                    round_number,
                )

        fingerprint = claim(
            tab_b,
            tab_a,
            current_b,
            "B->A",
            round_number,
        )
        if isinstance(fingerprint, dict):
            return fingerprint

        record(
            "transfer_started",
            round=round_number,
            direction="B->A",
            source_tab=tab_b,
            destination_tab=tab_a,
            source_turn_id=current_b.get("turn_id"),
            source_turn_index=current_b.get("turn_index"),
            source_chars=len(current_b["text"]),
            source_hash=turn_text_hash(current_b),
            fingerprint=fingerprint,
        )

        try:
            if test_protocol:
                a_raw = send_and_wait(
                    tab_a,
                    current_b["text"],
                    expected_reply_prefix=TEST_A_REPLY_PREFIX,
                    expected_reply_suffix=TEST_A_REPLY_SUFFIX,
                )
            else:
                a_raw = send_and_wait(tab_a, current_b["text"])
        except Exception as exc:
            return fail(
                "relay_transfer_exception",
                "b_to_a",
                round_number=round_number,
                detail=str(exc),
            )

        next_a, error = _completed_reply(a_raw)
        if error:
            return fail(
                error,
                "b_to_a",
                round_number=round_number,
                detail=a_raw,
            )

        if test_protocol:
            if _is_reset_command(next_a["text"]):
                return coordinated_reset(
                    "A",
                    "b_to_a",
                    round_number,
                )
            if not _has_reply_markers(
                next_a["text"],
                TEST_A_REPLY_PREFIX,
                TEST_A_REPLY_SUFFIX,
            ):
                return fail(
                    "relay_test_reply_marker_missing",
                    "b_to_a",
                    round_number=round_number,
                    detail={
                        "expected_prefix": TEST_A_REPLY_PREFIX,
                        "expected_suffix": TEST_A_REPLY_SUFFIX,
                        "response_text": next_a["text"],
                    },
                )

        transfer = _transfer_record(
            round_number,
            "B->A",
            tab_b,
            tab_a,
            current_b,
            next_a,
            include_text=include_text,
        )
        result["transfers"].append(transfer)
        record("transfer_completed", **transfer)

        current_a = next_a
        result["rounds_completed"] = round_number
        round_number += 1

        if round_number > current_round_limit():
            reached_limit = current_round_limit()
            result["rounds_requested"] = reached_limit
            record(
                "relay_round_limit_reached",
                round_limit=reached_limit,
                rounds_completed=result["rounds_completed"],
            )

            waiter = getattr(
                control,
                "wait_for_round_limit_decision",
                None,
            )
            if callable(waiter):
                decision = waiter(result["rounds_completed"])
            else:
                decision = "finish"

            if decision == "stopped":
                return stopped(
                    "round_limit",
                    result["rounds_completed"],
                )

            if (
                isinstance(decision, str)
                and decision.startswith("reset:")
            ):
                return coordinated_reset(
                    decision.split(":", 1)[1],
                    "round_limit",
                    result["rounds_completed"],
                )

            if decision == "extended":
                new_limit = current_round_limit()
                result["rounds_requested"] = new_limit
                record(
                    "relay_round_limit_extended",
                    previous_round_limit=reached_limit,
                    round_limit=new_limit,
                    round=round_number,
                )
                observed_round_limit = new_limit
                continue

            break

    result["rounds_requested"] = current_round_limit()
    transition(COMPLETE)
    result["status"] = "complete"
    result["latest_a"] = current_a
    result["latest_b"] = current_b
    record(
        "relay_completed",
        rounds_completed=result["rounds_completed"],
        transfers=len(result["transfers"]),
    )
    return result

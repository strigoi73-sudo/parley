"""Deterministic two-ChatGPT relay engine.

This module owns conversation sequencing only. Browser/CDP behavior and
ChatGPT DOM extraction remain in their respective layers.
"""

from .state import (
    COMPLETE,
    ERROR,
    IDLE,
    PREPARE,
    READ_A,
    TRANSFER_A_TO_B,
    TRANSFER_B_TO_A,
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
):
    return {
        "round": round_number,
        "direction": direction,
        "source_tab": source_tab,
        "destination_tab": destination_tab,
        "source_turn_id": source_turn.get("turn_id"),
        "source_turn_index": source_turn.get("turn_index"),
        "source_chars": len(source_turn["text"]),
        "source_text": source_turn["text"],
        "response_turn_id": response_turn.get("turn_id"),
        "response_turn_index": response_turn.get("turn_index"),
        "response_chars": len(response_turn["text"]),
        "response_text": response_turn["text"],
    }


def run_bidirectional_relay(
    tab_a,
    tab_b,
    rounds,
    *,
    read_response,
    send_and_wait,
):
    """Run a deterministic A -> B -> A relay for a fixed number of rounds.

    One round is one full back-and-forth exchange:
        A(n) -> B => B(n)
        B(n) -> A => A(n+1)

    Therefore N rounds always contain exactly 2N successful transfers.
    """
    result = {
        "status": "running",
        "state": IDLE,
        "state_history": [IDLE],
        "tab_a": tab_a,
        "tab_b": tab_b,
        "rounds_requested": rounds,
        "rounds_completed": 0,
        "transfers": [],
    }

    def transition(state):
        result["state"] = state
        result["state_history"].append(state)

    def fail(error, stage, round_number=None, detail=None):
        transition(ERROR)
        result["status"] = "error"
        result["error"] = error
        result["stage"] = stage
        if round_number is not None:
            result["round"] = round_number
        if detail is not None:
            result["detail"] = detail
        return result

    transition(PREPARE)

    if not tab_a or not tab_b:
        return fail("relay_tab_missing", "prepare")

    if tab_a == tab_b:
        return fail("relay_tabs_must_be_distinct", "prepare")

    if not isinstance(rounds, int) or isinstance(rounds, bool) or rounds < 1:
        return fail("relay_rounds_must_be_positive_integer", "prepare")

    transition(READ_A)
    initial_raw = read_response(tab_a)
    current_a, error = _initial_turn(initial_raw)
    if error:
        return fail(error, "read_a", detail=initial_raw)

    for round_number in range(1, rounds + 1):
        transition(TRANSFER_A_TO_B)
        b_raw = send_and_wait(tab_b, current_a["text"])
        current_b, error = _completed_reply(b_raw)
        if error:
            return fail(
                error,
                "a_to_b",
                round_number=round_number,
                detail=b_raw,
            )

        result["transfers"].append(
            _transfer_record(
                round_number,
                "A->B",
                tab_a,
                tab_b,
                current_a,
                current_b,
            )
        )

        transition(TRANSFER_B_TO_A)
        a_raw = send_and_wait(tab_a, current_b["text"])
        next_a, error = _completed_reply(a_raw)
        if error:
            return fail(
                error,
                "b_to_a",
                round_number=round_number,
                detail=a_raw,
            )

        result["transfers"].append(
            _transfer_record(
                round_number,
                "B->A",
                tab_b,
                tab_a,
                current_b,
                next_a,
            )
        )

        current_a = next_a
        result["rounds_completed"] = round_number

    transition(COMPLETE)
    result["status"] = "complete"
    result["latest_a"] = current_a
    result["latest_b"] = current_b
    return result

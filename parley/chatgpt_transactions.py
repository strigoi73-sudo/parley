"""Strict ChatGPT send/wait transaction state machine.

This module owns transaction sequencing after a ChatGPT participant has been
selected. Browser transport, DOM-state extraction, comparison helpers, and the
clock are supplied explicitly by the workflow layer so this state machine can
move independently without creating a circular dependency.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ChatGPTTransactionOperations:
    adapter_for_tab: object
    monotonic: object
    sleep: object
    connect: object
    focus_emulation: object
    state: object
    evaluate: object
    send: object
    has_new_user: object
    has_new_assistant: object
    same_user_turn: object
    normalize_text: object
    page_activity: object
    unsent_submission_js: object


def send_and_wait(
    tab_id,
    text,
    wait_timeout_ms=60000,
    silence_ms=1500,
    adapter=None,
    expected_reply_prefix=None,
    expected_reply_suffix=None,
    should_stop=None,
    pre_state_override=None,
    require_user_text_match=True,
    submission_timeout_ms=10000,
    retry_unsent_submission=False,
    required_attachment_filename=None,
    submission_retry_interval_ms=2000,
    max_submission_attempts=4,
    operations=None,
):
    """Strict ChatGPT send/wait transaction using one target connection.

    Invariants:
    - one target connection from snapshot through completion;
    - submission must be proven by a newer user turn;
    - response must be proven to be a newer assistant turn;
    - only a provably post-submission assistant candidate may satisfy completion;
    - transient candidate identity replacement is allowed while the same
      verified user turn remains current;
    - a newer user turn or ambiguous response state fails closed.
    """
    if not isinstance(operations, ChatGPTTransactionOperations):
        raise TypeError("operations must be ChatGPTTransactionOperations")

    if adapter is None:
        adapter = operations.adapter_for_tab(tab_id)
    if adapter is None or adapter.name != "chatgpt":
        return {
            "error": "chatgpt_adapter_required",
            "response_complete": False,
        }

    started = operations.monotonic()
    deadline = (
        None
        if wait_timeout_ms is None
        else started + (wait_timeout_ms / 1000.0)
    )

    def keep_waiting(until=None):
        if callable(should_stop) and should_stop():
            return False
        return until is None or operations.monotonic() < until

    ws = operations.connect(tab_id, timeout=None)
    if not ws:
        return {
            "error": "cannot_connect_to_tab",
            "stage": "connect",
            "response_complete": False,
        }

    def fail(error, stage, **extra):
        result = {
            "error": error,
            "stage": stage,
            "response_complete": False,
            "response_duration_ms": int(
                (operations.monotonic() - started) * 1000
            ),
        }
        result.update(extra)
        return result

    try:
        focus = operations.focus_emulation(
            tab_id,
            True,
            timeout=None,
            ws=ws,
            should_stop=should_stop,
        )
        if not focus.get("ok"):
            return fail(
                "chatgpt_focus_emulation_failed",
                "focus_emulation",
                detail=focus,
            )

        pre_state = pre_state_override
        if pre_state is None:
            pre_state = operations.state(
                ws,
                adapter,
                should_stop=should_stop,
            )
        if not isinstance(pre_state, dict):
            return fail(
                "chatgpt_snapshot_invalid",
                "snapshot",
            )
        if pre_state.get("error") == "stopped":
            return fail("chatgpt_wait_stopped", "snapshot")
        if not pre_state.get("ok"):
            return fail(
                pre_state.get("error", "chatgpt_snapshot_failed"),
                "snapshot",
                detail=pre_state,
            )

        pre_assistant_count = int(
            pre_state.get("assistant_count", 0) or 0
        )
        pre_user_count = int(pre_state.get("user_count", 0) or 0)

        prepare = operations.evaluate(
            ws,
            adapter.prepare_composer_js,
            should_stop=should_stop,
        )
        if prepare.get("error") == "stopped":
            return fail("chatgpt_wait_stopped", "prepare")
        if not prepare.get("ok"):
            return fail(
                prepare.get("error", "chatgpt_composer_prepare_failed"),
                "prepare",
                detail=prepare,
            )

        insert_result = operations.send(
            ws,
            "Input.insertText",
            {"text": text},
            timeout=None,
            should_stop=should_stop,
        )
        if isinstance(insert_result, dict) and insert_result.get("error") == "stopped":
            return fail("chatgpt_wait_stopped", "insert")
        if (
            not isinstance(insert_result, dict)
            or insert_result.get("error")
            or ("code" in insert_result and "message" in insert_result)
        ):
            return fail(
                "chatgpt_insert_text_failed",
                "insert",
                detail=insert_result,
            )

        # Give React enough time to enable the send button after insertText.
        operations.sleep(0.2)

        click = operations.evaluate(
            ws,
            adapter.click_send_js,
            should_stop=should_stop,
        )
        if click.get("error") == "stopped":
            return fail("chatgpt_wait_stopped", "submit")
        if not click.get("ok"):
            return fail(
                click.get("error", "chatgpt_send_failed"),
                "submit",
                detail=click,
            )

        submission_attempts = 1
        last_unsent_evidence = None
        next_submission_retry = (
            operations.monotonic()
            + (submission_retry_interval_ms / 1000.0)
        )

        # Submission is not considered successful until ChatGPT's DOM proves a
        # newer user turn exists.
        submitted_state = None
        human_reset_requested = False
        last_submission_state = None
        submission_limit = (
            None
            if submission_timeout_ms is None
            else operations.monotonic() + (
                submission_timeout_ms / 1000.0
            )
        )
        if deadline is None:
            submission_deadline = submission_limit
        elif submission_limit is None:
            submission_deadline = deadline
        else:
            submission_deadline = min(
                deadline,
                submission_limit,
            )
        while keep_waiting(submission_deadline):
            state = operations.state(ws, adapter, should_stop=should_stop)
            last_submission_state = state
            if not state.get("ok"):
                return fail(
                    state.get("error", "chatgpt_state_failed"),
                    "verify_submission",
                    detail=state,
                )
            expected_user_text = (
                text
                if require_user_text_match
                else None
            )
            if operations.has_new_user(
                pre_state,
                state,
                expected_user_text,
            ):
                submitted_state = state
                break
            if operations.has_new_user(pre_state, state, "RESET CHAT"):
                submitted_state = state
                human_reset_requested = True
                break

            if (
                retry_unsent_submission
                and submission_attempts < max_submission_attempts
                and operations.monotonic() >= next_submission_retry
            ):
                evidence = operations.evaluate(
                    ws,
                    operations.unsent_submission_js(
                        text,
                        required_attachment_filename,
                    ),
                    should_stop=should_stop,
                )
                last_unsent_evidence = evidence
                if evidence.get("error") == "stopped":
                    return fail(
                        "chatgpt_wait_stopped",
                        "verify_submission",
                        submission_attempts=submission_attempts,
                    )

                safe_to_retry = bool(
                    evidence.get("ok")
                    and evidence.get("exactText")
                    and evidence.get("sendEnabled")
                    and not evidence.get("hasStopButton")
                    and evidence.get("attachmentPresent")
                )
                if safe_to_retry:
                    retry_click = operations.evaluate(
                        ws,
                        adapter.click_send_js,
                        should_stop=should_stop,
                    )
                    if retry_click.get("error") == "stopped":
                        return fail(
                            "chatgpt_wait_stopped",
                            "verify_submission",
                            submission_attempts=submission_attempts,
                        )
                    if retry_click.get("ok"):
                        submission_attempts += 1

                next_submission_retry = (
                    operations.monotonic()
                    + (submission_retry_interval_ms / 1000.0)
                )

            operations.sleep(0.1)

        if submitted_state is None:
            if callable(should_stop) and should_stop():
                return fail(
                    "chatgpt_wait_stopped",
                    "verify_submission",
                    pre_user_count=pre_user_count,
                )
            observed_user = (
                (last_submission_state or {}).get("user") or {}
            )
            pre_user = pre_state.get("user") or {}
            observed_text = operations.normalize_text(
                observed_user.get("text")
            )
            pre_text = operations.normalize_text(
                pre_user.get("text")
            )
            expected_text = operations.normalize_text(text)
            return fail(
                "chatgpt_submission_not_verified",
                "verify_submission",
                pre_user_count=pre_user_count,
                pre_user_turn_id=pre_user.get("turn_id"),
                pre_user_chars=len(pre_text),
                observed_user_count=int(
                    (last_submission_state or {}).get("user_count", 0)
                    or 0
                ),
                observed_user_turn_id=observed_user.get("turn_id"),
                observed_user_chars=len(observed_text),
                expected_user_chars=len(expected_text),
                expected_user_text_match=bool(
                    expected_text and observed_text == expected_text
                ),
                user_text_verification_required=bool(
                    require_user_text_match
                ),
                submission_attempts=submission_attempts,
                last_unsent_evidence=last_unsent_evidence,
                page_activity=operations.page_activity(
                    last_submission_state or {}
                ),
            )

        # Test-protocol mode uses an explicit final-reply marker as positive
        # evidence that ChatGPT has reached the real assistant answer. This
        # deliberately ignores transient "Thinking" surfaces and temporary
        # React rollbacks to the pre-submission assistant turn.
        if expected_reply_prefix:
            expected_prefix = str(expected_reply_prefix).strip()
            expected_suffix = (
                str(expected_reply_suffix).strip()
                if expected_reply_suffix
                else None
            )
            last_text = ""
            last_change = operations.monotonic()
            candidate_replacements = 0
            last_identity = None
            candidate_seen = False

            while keep_waiting(deadline):
                state = operations.state(ws, adapter, should_stop=should_stop)
                if not state.get("ok"):
                    return fail(
                        state.get("error", "chatgpt_state_failed"),
                        "track_marked_response",
                        response_text=last_text,
                        detail=state,
                    )

                if not operations.same_user_turn(submitted_state, state):
                    current_user = state.get("user") or {}
                    current_user_text = operations.normalize_text(
                        current_user.get("text")
                    )
                    if (
                        current_user_text == "RESET CHAT"
                        and operations.has_new_user(
                            submitted_state,
                            state,
                            "RESET CHAT",
                        )
                    ):
                        submitted_state = state
                        human_reset_requested = True
                        candidate_seen = False
                        last_identity = None
                        last_text = ""
                        last_change = operations.monotonic()
                        operations.sleep(0.1)
                        continue

                    submitted_user = submitted_state.get("user") or {}
                    return fail(
                        "chatgpt_user_turn_changed_during_response",
                        "track_marked_response",
                        response_text=last_text,
                        expected_user_turn_id=submitted_user.get("turn_id"),
                        current_user_turn_id=current_user.get("turn_id"),
                    )

                current = state.get("assistant")
                current_text = (
                    (current or {}).get("text") or ""
                ).strip()
                is_reset = current_text == "RESET CHAT"
                has_open_marker = current_text.startswith(expected_prefix)
                has_close_marker = (
                    not expected_suffix
                    or current_text.endswith(expected_suffix)
                )
                is_marked = has_open_marker and has_close_marker

                eligible = bool(
                    current
                    and operations.has_new_assistant(pre_state, state)
                    and (is_marked or is_reset)
                )

                if not eligible:
                    # The latest rendered assistant may temporarily be
                    # "Thinking", disappear, or roll back to the old answer.
                    # When a terminal marker is configured, a partial
                    # reply with only the opening marker is ineligible. In
                    # prefix-only protocol mode, the opening marker plus the
                    # normal stability/streaming checks is sufficient.
                    candidate_seen = False
                    last_identity = None
                    last_text = ""
                    last_change = operations.monotonic()
                    operations.sleep(0.2)
                    continue

                current_count = int(
                    state.get("assistant_count", 0) or 0
                )
                current_identity = (
                    current.get("turn_id"),
                    current.get("turn_index"),
                    current_count,
                )
                now = operations.monotonic()

                if not candidate_seen:
                    candidate_seen = True
                    last_identity = current_identity
                    last_text = current_text
                    last_change = now
                elif current_identity != last_identity:
                    candidate_replacements += 1
                    last_identity = current_identity
                    last_text = current_text
                    last_change = now
                elif current_text != last_text:
                    last_text = current_text
                    last_change = now

                streaming = bool(
                    current.get("hasStreaming")
                    or state.get("hasStopButton")
                )
                stable_ms = int((now - last_change) * 1000)

                if (
                    last_text
                    and not streaming
                    and stable_ms >= silence_ms
                ):
                    return {
                        "pre_msg_count": pre_assistant_count,
                        "pre_user_count": pre_user_count,
                        "post_user_count": int(
                            state.get("user_count", 0) or 0
                        ),
                        "submitted_user_turn_id": (
                            (submitted_state.get("user") or {}).get("turn_id")
                        ),
                        "send_method": click.get(
                            "method",
                            "chatgpt-send-button",
                        ),
                        "sent_chars": len(text),
                        "response_text": last_text,
                        "response_complete": True,
                        "response_turn_id": current.get("turn_id"),
                        "response_turn_index": current.get("turn_index"),
                        "response_source": "chatgpt-strict",
                        "response_candidate_replacements": (
                            candidate_replacements
                        ),
                        "expected_reply_prefix": expected_prefix,
                        "expected_reply_suffix": expected_suffix,
                        "human_reset_requested": human_reset_requested,
                        "submission_attempts": submission_attempts,
                        "focus_emulation": True,
                        "page_activity": operations.page_activity(state),
                        "response_duration_ms": int(
                            (now - started) * 1000
                        ),
                    }

                operations.sleep(0.2)

            if callable(should_stop) and should_stop():
                return fail(
                    "chatgpt_wait_stopped",
                    "track_marked_response",
                    response_text=last_text,
                    pre_msg_count=pre_assistant_count,
                    pre_user_count=pre_user_count,
                )

            return fail(
                "chatgpt_marked_response_timeout",
                "track_marked_response",
                response_text=last_text,
                expected_reply_prefix=expected_prefix,
                expected_reply_suffix=expected_suffix,
                response_candidate_replacements=candidate_replacements,
                pre_msg_count=pre_assistant_count,
                pre_user_count=pre_user_count,
            )

        # Now require a provably newer assistant turn.
        response_state = None
        while keep_waiting(deadline):
            state = operations.state(ws, adapter, should_stop=should_stop)
            if not state.get("ok"):
                return fail(
                    state.get("error", "chatgpt_state_failed"),
                    "wait_for_response",
                    detail=state,
                )
            if operations.has_new_assistant(pre_state, state):
                response_state = state
                break
            operations.sleep(0.2)

        if response_state is None:
            if callable(should_stop) and should_stop():
                return fail(
                    "chatgpt_wait_stopped",
                    "wait_for_response",
                    pre_assistant_count=pre_assistant_count,
                )
            return fail(
                "chatgpt_new_assistant_turn_timeout",
                "wait_for_response",
                pre_assistant_count=pre_assistant_count,
                post_user_count=int(
                    submitted_state.get("user_count", 0) or 0
                ),
            )

        target = response_state.get("assistant") or {}
        target_turn_id = target.get("turn_id")
        target_turn_index = target.get("turn_index")
        target_assistant_count = int(
            response_state.get("assistant_count", 0) or 0
        )
        submitted_user_count = int(
            submitted_state.get("user_count", 0) or 0
        )

        last_text = (target.get("text") or "").strip()
        last_change = operations.monotonic()
        candidate_replacements = 0

        while keep_waiting(deadline):
            state = operations.state(ws, adapter, should_stop=should_stop)
            if not state.get("ok"):
                return fail(
                    state.get("error", "chatgpt_state_failed"),
                    "track_response",
                    response_text=last_text,
                    detail=state,
                )

            current_user_count = int(
                state.get("user_count", 0) or 0
            )
            if not operations.same_user_turn(submitted_state, state):
                submitted_user = submitted_state.get("user") or {}
                current_user = state.get("user") or {}
                return fail(
                    "chatgpt_user_turn_changed_during_response",
                    "track_response",
                    response_text=last_text,
                    expected_user_count=submitted_user_count,
                    current_user_count=current_user_count,
                    expected_user_turn_id=submitted_user.get("turn_id"),
                    current_user_turn_id=current_user.get("turn_id"),
                )

            current = state.get("assistant")
            if not current:
                return fail(
                    "chatgpt_assistant_turn_disappeared",
                    "track_response",
                    response_text=last_text,
                )

            if not operations.has_new_assistant(pre_state, state):
                return fail(
                    "chatgpt_response_candidate_not_new",
                    "track_response",
                    response_text=last_text,
                )

            current_count = int(
                state.get("assistant_count", 0) or 0
            )
            current_turn_id = current.get("turn_id")
            current_turn_index = current.get("turn_index")
            current_text = (current.get("text") or "").strip()
            now = operations.monotonic()

            identity_changed = (
                (
                    target_turn_id
                    and current_turn_id
                    and current_turn_id != target_turn_id
                )
                or (
                    target_turn_index is not None
                    and current_turn_index != target_turn_index
                )
                or current_count != target_assistant_count
            )

            if identity_changed:
                # Current ChatGPT can expose a transient assistant candidate
                # (for example a "Thinking" surface) and then replace it with
                # the actual answer while processing the same submitted user
                # turn. Follow that replacement only while the transaction's
                # verified user-turn boundary remains unchanged.
                target_turn_id = current_turn_id
                target_turn_index = current_turn_index
                target_assistant_count = current_count
                candidate_replacements += 1
                last_text = current_text
                last_change = now
            elif current_text != last_text:
                last_text = current_text
                last_change = now

            streaming = bool(
                current.get("hasStreaming")
                or state.get("hasStopButton")
            )
            stable_ms = int((now - last_change) * 1000)

            if (
                last_text
                and not streaming
                and stable_ms >= silence_ms
            ):
                return {
                    "pre_msg_count": pre_assistant_count,
                    "pre_user_count": pre_user_count,
                    "post_user_count": int(
                        state.get("user_count", 0) or 0
                    ),
                    "submitted_user_turn_id": (
                        (submitted_state.get("user") or {}).get("turn_id")
                    ),
                    "send_method": click.get(
                        "method",
                        "chatgpt-send-button",
                    ),
                    "sent_chars": len(text),
                    "response_text": last_text,
                    "response_complete": True,
                    "response_turn_id": current_turn_id,
                    "response_turn_index": current_turn_index,
                    "response_source": "chatgpt-strict",
                    "response_candidate_replacements": candidate_replacements,
                    "focus_emulation": True,
                    "page_activity": operations.page_activity(state),
                    "response_duration_ms": int(
                        (now - started) * 1000
                    ),
                }

            operations.sleep(0.2)

        if callable(should_stop) and should_stop():
            return fail(
                "chatgpt_wait_stopped",
                "track_response",
                response_text=last_text,
                response_turn_id=target_turn_id,
                response_turn_index=target_turn_index,
            )

        return fail(
            "chatgpt_response_completion_timeout",
            "track_response",
            response_text=last_text,
            response_turn_id=target_turn_id,
            response_turn_index=target_turn_index,
            response_candidate_replacements=candidate_replacements,
            pre_msg_count=pre_assistant_count,
            pre_user_count=pre_user_count,
            post_user_count=int(
                response_state.get("user_count", 0) or 0
            ),
        )

    finally:
        try:
            ws.close()
        except Exception:
            pass

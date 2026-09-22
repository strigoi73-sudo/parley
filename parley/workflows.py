"""
parley.workflows - AI chat workflow layer.

Built on top of `parley.core` (CDP engine) and `parley.adapters` (site
knowledge). Provides the higher-level operations that make Parley useful for
AI automation:

  * read_response(tab)         - extract the latest AI answer
  * robust_send(...)           - reliably type + submit a prompt (handles
                                 Gemini stuck-button reload + input clearing)
  * send_and_wait(tab, text)   - send, then wait for the full NEW response
  * wait_stream(tab)           - wait for an in-flight response to finish
  * bridge(from, to, rounds)   - relay a conversation between two AIs
  * poll(tab)                  - block until new content appears

Every function returns plain dicts/values; printing/formatting is the CLI's job.
"""

import time

from . import core
from .core import cdp_connect, cdp_send, cdp_send_with_retry
from .adapters import detect
from .adapters.js import (
    UNIVERSAL_GET_RESPONSE,
    UNIVERSAL_GET_MSG_COUNT,
    FOCUS_AND_CLEAR_JS,
    SEND_CLICK_JS,
    REFOCUS_JS,
    GEMINI_STUCK_STATE_JS,
    make_mutation_observer_js,
)


_UNKNOWN_TAB_RESPONSE_JS = """
(() => ({
    ok: false,
    error: 'tab_url_unavailable',
    text: '',
    count: 0,
    source: 'fail-closed'
}))()
"""

_UNKNOWN_TAB_COUNT_JS = """
(() => ({
    ok: false,
    error: 'tab_url_unavailable',
    count: 0,
    source: 'fail-closed'
}))()
"""


def _adapter_for_tab(tab_id):
    """Return the registered site adapter, or None if tab identity is unknown."""
    url = core.tab_url(tab_id)
    if not url:
        return None
    return detect(url)


def _response_js(tab_id):
    """Return the site-specific response extractor for this tab.

    ChatGPT supplies a fail-closed extractor. A tab whose URL cannot be
    resolved also fails closed. Other known sites currently retain the
    inherited universal detector.
    """
    adapter = _adapter_for_tab(tab_id)
    if adapter is None:
        return _UNKNOWN_TAB_RESPONSE_JS
    return getattr(adapter, "response_js", UNIVERSAL_GET_RESPONSE)


def _message_count_js(tab_id):
    """Return the site-specific assistant-message counter for this tab."""
    adapter = _adapter_for_tab(tab_id)
    if adapter is None:
        return _UNKNOWN_TAB_COUNT_JS
    return getattr(adapter, "message_count_js", UNIVERSAL_GET_MSG_COUNT)


def read_response(tab_id):
    """Return the latest AI response object {text, source, hasStreaming, ...}."""
    val = core.evaluate(tab_id, _response_js(tab_id))
    return val


def robust_send(ws, tab_id, text):
    """Shared send routine used by send() and send_and_wait().

    Handles: (1) reloading to recover from Gemini's stuck 'Stop response' state,
    (2) clearing accumulated input text, (3) inserting text via CDP, (4) clicking
    send with Enter-key fallback. Returns (result_dict, ws) - ws may be a new
    connection if a reload happened.
    """
    reloaded = False
    # Step 0: Detect Gemini stuck state; reload to reset the Angular UI if needed.
    stuck_result = cdp_send_with_retry(ws, "Runtime.evaluate", {
        "expression": GEMINI_STUCK_STATE_JS,
        "returnByValue": True,
    }, tab_id=tab_id)
    stuck_val = stuck_result.get("result", {}).get("value", {}) if "result" in stuck_result else {}
    if stuck_val.get("stuck"):
        cdp_send_with_retry(ws, "Page.reload", {}, tab_id=tab_id)
        try:
            ws.close()
        except Exception:
            pass
        time.sleep(7)  # Wait for reload + Angular re-init (conversation preserved)
        ws = cdp_connect(tab_id)
        reloaded = True

    # Step 1: Focus input + select all existing content (so insertText replaces it)
    focus_result = cdp_send_with_retry(ws, "Runtime.evaluate", {
        "expression": FOCUS_AND_CLEAR_JS,
        "returnByValue": True,
    }, tab_id=tab_id)
    focus_val = focus_result.get("result", {}).get("value", {}) if "result" in focus_result else {}

    # Capture the CURRENT latest response text (before sending) so the waiter can
    # tell when a genuinely NEW response has arrived (avoids returning stale text).
    prev_text = ""
    prev_result = cdp_send_with_retry(ws, "Runtime.evaluate", {
        "expression": _response_js(tab_id),
        "returnByValue": True,
    }, tab_id=tab_id)
    if "result" in prev_result:
        prev_text = (prev_result["result"].get("value", {}) or {}).get("text", "") or ""

    # Step 2: Insert text via CDP Input.insertText (replaces selection)
    insert_result = cdp_send_with_retry(ws, "Input.insertText", {
        "text": text,
    }, tab_id=tab_id)

    time.sleep(0.4)  # Brief pause for UI (Angular/React) to register text

    # Step 3: Click send button
    click_result = cdp_send_with_retry(ws, "Runtime.evaluate", {
        "expression": SEND_CLICK_JS,
        "returnByValue": True,
    }, tab_id=tab_id)
    click_val = click_result.get("result", {}).get("value", {}) if "result" in click_result else {}
    send_method = click_val.get("method", "unknown")

    if not click_val.get("ok"):
        # Fallback: re-focus then press Enter via CDP key events
        cdp_send_with_retry(ws, "Runtime.evaluate", {
            "expression": REFOCUS_JS,
            "returnByValue": True,
        }, tab_id=tab_id)
        time.sleep(0.1)
        cdp_send_with_retry(ws, "Input.dispatchKeyEvent", {
            "type": "keyDown", "key": "Enter", "code": "Enter",
            "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13,
        }, tab_id=tab_id)
        cdp_send_with_retry(ws, "Input.dispatchKeyEvent", {
            "type": "keyUp", "key": "Enter", "code": "Enter",
            "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13,
        }, tab_id=tab_id)
        send_method = "enter_key_fallback"

    return {
        "reloaded": reloaded,
        "prev_text": prev_text,
        "typed": len(text),
        "focus": focus_val,
        "insert": "ok" if "error" not in insert_result else insert_result,
        "send_method": send_method,
    }, ws


def send(tab_id, text):
    """Type + submit a prompt (no wait). Returns a result dict."""
    ws = cdp_connect(tab_id)
    if not ws:
        return {"error": "cannot connect to tab"}
    try:
        result, ws = robust_send(ws, tab_id, text)
        result["ok"] = True
        return result
    finally:
        try:
            ws.close()
        except Exception:
            pass


def wait_stream(tab_id, timeout_ms=60000, silence_ms=1500):
    """Wait for an AI response to finish streaming using MutationObserver."""
    ws = cdp_connect(tab_id)
    if not ws:
        return {"error": "cannot connect to tab"}
    try:
        count_result = cdp_send(ws, "Runtime.evaluate", {
            "expression": _message_count_js(tab_id),
            "returnByValue": True,
        })
        initial_count = 0
        if "result" in count_result and "value" in count_result["result"]:
            initial_count = count_result["result"]["value"].get("count", 0)

        initial = cdp_send(ws, "Runtime.evaluate", {
            "expression": _response_js(tab_id),
            "returnByValue": True,
        })
        initial_text = ""
        has_streaming = False
        has_stop = False
        initial_count_val = 0
        if "result" in initial and "value" in initial["result"]:
            val = initial["result"]["value"]
            initial_text = val.get("text", "")
            has_streaming = val.get("hasStreaming", False)
            has_stop = val.get("hasStopButton", False)
            initial_count_val = val.get("count", 0)

        if initial_text and not has_streaming and not has_stop:
            return {
                "status": "complete",
                "text": initial_text,
                "msg_count": initial_count_val,
                "duration_ms": 0,
            }

        # initial_msg_count=0 skips Phase 1 count tracking (unreliable on Gemini).
        observer_js = make_mutation_observer_js(timeout_ms, silence_ms, initial_msg_count=0)
        result = cdp_send(ws, "Runtime.evaluate", {
            "expression": observer_js,
            "returnByValue": True,
            "awaitPromise": True,
        }, timeout=timeout_ms // 1000 + 5)

        if "result" in result:
            val = result["result"].get("value", {})
            return {
                "status": "complete" if val.get("done") else "timeout",
                "text": val.get("text", initial_text),
                "duration_ms": val.get("duration", 0),
            }
        return {"status": "error", "text": initial_text, "error": str(result)}
    finally:
        try:
            ws.close()
        except Exception:
            pass


def poll(tab_id, interval_ms=2000, max_iters=60):
    """Poll for new chat content - returns text when it changes and settles."""
    ws = cdp_connect(tab_id)
    if not ws:
        return {"error": "cannot connect to tab"}
    try:
        last_hash = ""
        for _ in range(max_iters):
            result = cdp_send(ws, "Runtime.evaluate", {
                "expression": _response_js(tab_id),
                "returnByValue": True,
            })
            if "result" in result and "value" in result["result"]:
                current = result["result"]["value"]
                current_hash = str(current.get("text", ""))[-200:]
                if current_hash != last_hash and not current.get("hasStreaming") and not current.get("hasStopButton"):
                    return current
                if current.get("hasStreaming") or current.get("hasStopButton"):
                    time.sleep(1)
                    continue
            time.sleep(interval_ms / 1000)
        return {"error": "poll timeout", "last": last_hash}
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _runtime_value(result):
    """Extract Runtime.evaluate returnByValue payload, or None."""
    if not isinstance(result, dict):
        return None
    inner = result.get("result")
    if isinstance(inner, dict):
        return inner.get("value")
    return None


def _chatgpt_eval(ws, expression):
    """Evaluate strict ChatGPT JS on an existing target connection."""
    raw = cdp_send(ws, "Runtime.evaluate", {
        "expression": expression,
        "returnByValue": True,
    })
    value = _runtime_value(raw)
    if not isinstance(value, dict):
        return {
            "ok": False,
            "error": "chatgpt_runtime_value_unavailable",
            "detail": raw,
        }
    return value


def _chatgpt_state(ws, adapter):
    """Read deterministic ChatGPT turn state on the existing connection."""
    state = _chatgpt_eval(ws, adapter.turn_state_js)
    if not state.get("ok"):
        return state
    if "assistant_count" not in state or "user_count" not in state:
        return {
            "ok": False,
            "error": "chatgpt_turn_state_incomplete",
        }
    return state


def _chatgpt_has_new_assistant(pre_state, current_state):
    """Return True only when a newer assistant turn can be proven."""
    current = current_state.get("assistant")
    if not current:
        return False

    pre_count = int(pre_state.get("assistant_count", 0) or 0)
    current_count = int(current_state.get("assistant_count", 0) or 0)
    if current_count > pre_count:
        return True

    previous = pre_state.get("assistant")
    if previous is None and current is not None:
        return True

    if previous and current:
        previous_id = previous.get("turn_id")
        current_id = current.get("turn_id")
        if previous_id and current_id and previous_id != current_id:
            return True

    return False


def _normalize_chatgpt_text(value):
    """Normalize rendered chat text for deterministic equality checks."""
    return " ".join(str(value or "").split())


def _chatgpt_has_new_user(pre_state, current_state, expected_text):
    """Return True only when the submitted user turn can be proven."""
    current = current_state.get("user")
    previous = pre_state.get("user")
    expected = _normalize_chatgpt_text(expected_text)

    if isinstance(current, dict):
        current_text = _normalize_chatgpt_text(current.get("text"))
        if expected and current_text != expected:
            return False

        current_id = current.get("turn_id")
        previous_id = (
            previous.get("turn_id")
            if isinstance(previous, dict)
            else None
        )

        if current_id and previous_id:
            return current_id != previous_id

        if previous is None:
            return True

        previous_text = _normalize_chatgpt_text(previous.get("text"))
        if current_text and current_text != previous_text:
            return True

        pre_count = int(pre_state.get("user_count", 0) or 0)
        current_count = int(current_state.get("user_count", 0) or 0)
        if current_count > pre_count and current_text:
            return True

        return False

    # Backward-compatible fallback for state producers without explicit
    # user-turn metadata. Current ChatGPT strict state does provide it.
    pre_count = int(pre_state.get("user_count", 0) or 0)
    current_count = int(current_state.get("user_count", 0) or 0)
    return current_count > pre_count


def _chatgpt_same_user_turn(expected_state, current_state):
    """Return True when both states refer to the same latest user turn."""
    expected = expected_state.get("user")
    current = current_state.get("user")

    if isinstance(expected, dict) and isinstance(current, dict):
        expected_id = expected.get("turn_id")
        current_id = current.get("turn_id")

        if expected_id and current_id:
            return expected_id == current_id

        expected_text = _normalize_chatgpt_text(expected.get("text"))
        current_text = _normalize_chatgpt_text(current.get("text"))
        if expected_text and current_text:
            return expected_text == current_text

        return False

    if expected is None and current is None:
        return int(expected_state.get("user_count", 0) or 0) == int(
            current_state.get("user_count", 0) or 0
        )

    return False


def _chatgpt_send_and_wait(
    tab_id,
    text,
    wait_timeout_ms=60000,
    silence_ms=1500,
    adapter=None,
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
    if adapter is None:
        adapter = _adapter_for_tab(tab_id)
    if adapter is None or adapter.name != "chatgpt":
        return {
            "error": "chatgpt_adapter_required",
            "response_complete": False,
        }

    started = time.monotonic()
    deadline = started + (wait_timeout_ms / 1000.0)

    ws = cdp_connect(tab_id)
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
                (time.monotonic() - started) * 1000
            ),
        }
        result.update(extra)
        return result

    try:
        pre_state = _chatgpt_state(ws, adapter)
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

        prepare = _chatgpt_eval(ws, adapter.prepare_composer_js)
        if not prepare.get("ok"):
            return fail(
                prepare.get("error", "chatgpt_composer_prepare_failed"),
                "prepare",
                detail=prepare,
            )

        insert_result = cdp_send(ws, "Input.insertText", {"text": text})
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
        time.sleep(0.2)

        click = _chatgpt_eval(ws, adapter.click_send_js)
        if not click.get("ok"):
            return fail(
                click.get("error", "chatgpt_send_failed"),
                "submit",
                detail=click,
            )

        # Submission is not considered successful until ChatGPT's DOM proves a
        # newer user turn exists.
        submitted_state = None
        submission_deadline = min(deadline, time.monotonic() + 10.0)
        while time.monotonic() < submission_deadline:
            state = _chatgpt_state(ws, adapter)
            if not state.get("ok"):
                return fail(
                    state.get("error", "chatgpt_state_failed"),
                    "verify_submission",
                    detail=state,
                )
            if _chatgpt_has_new_user(pre_state, state, text):
                submitted_state = state
                break
            time.sleep(0.1)

        if submitted_state is None:
            return fail(
                "chatgpt_submission_not_verified",
                "verify_submission",
                pre_user_count=pre_user_count,
            )

        # Now require a provably newer assistant turn.
        response_state = None
        while time.monotonic() < deadline:
            state = _chatgpt_state(ws, adapter)
            if not state.get("ok"):
                return fail(
                    state.get("error", "chatgpt_state_failed"),
                    "wait_for_response",
                    detail=state,
                )
            if _chatgpt_has_new_assistant(pre_state, state):
                response_state = state
                break
            time.sleep(0.2)

        if response_state is None:
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
        last_change = time.monotonic()
        candidate_replacements = 0

        while time.monotonic() < deadline:
            state = _chatgpt_state(ws, adapter)
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
            if not _chatgpt_same_user_turn(submitted_state, state):
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

            if not _chatgpt_has_new_assistant(pre_state, state):
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
            now = time.monotonic()

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
                    "response_duration_ms": int(
                        (now - started) * 1000
                    ),
                }

            time.sleep(0.2)

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


def _legacy_send_and_wait(
    tab_id,
    text,
    wait_timeout_ms=60000,
    silence_ms=1500,
):
    """Inherited multi-site send/wait path for non-ChatGPT adapters."""
    result = {}

    ws_pre = cdp_connect(tab_id)
    if not ws_pre:
        return {"error": "cannot connect to tab"}

    pre_count = 0
    try:
        count_result = cdp_send(ws_pre, "Runtime.evaluate", {
            "expression": _message_count_js(tab_id),
            "returnByValue": True,
        })
        if "result" in count_result and "value" in count_result["result"]:
            pre_count = count_result["result"]["value"].get("count", 0)
    finally:
        try:
            ws_pre.close()
        except Exception:
            pass

    result["pre_msg_count"] = pre_count

    ws_send = cdp_connect(tab_id)
    if not ws_send:
        return {"error": "cannot connect for send"}

    prev_text = ""
    try:
        send_info, ws_send = robust_send(ws_send, tab_id, text)
        result["send_method"] = send_info.get("send_method", "unknown")
        result["reloaded"] = send_info.get("reloaded", False)
        prev_text = send_info.get("prev_text", "") or ""
        result["sent_chars"] = len(text)
    finally:
        try:
            ws_send.close()
        except Exception:
            pass

    time.sleep(1)

    ws_wait = cdp_connect(tab_id)
    if not ws_wait:
        return {**result, "error": "cannot connect for wait"}

    try:
        max_wait = wait_timeout_ms // 1000
        for _ in range(max_wait * 2):
            count_result = cdp_send(ws_wait, "Runtime.evaluate", {
                "expression": _message_count_js(tab_id),
                "returnByValue": True,
            })
            current_count = 0
            if "result" in count_result and "value" in count_result["result"]:
                current_count = count_result["result"]["value"].get("count", 0)

            if current_count > pre_count:
                break

            check = cdp_send(ws_wait, "Runtime.evaluate", {
                "expression": _response_js(tab_id),
                "returnByValue": True,
            })
            if "result" in check and "value" in check["result"]:
                val = check["result"]["value"]
                if val.get("hasStreaming") or val.get("hasStopButton"):
                    break
                new_text = val.get("text", "")
                if new_text and new_text.strip() != prev_text.strip():
                    break

            time.sleep(0.5)

        observer_js = make_mutation_observer_js(
            wait_timeout_ms,
            silence_ms,
            initial_msg_count=0,
            prev_text=prev_text,
        )
        wait_result = cdp_send(ws_wait, "Runtime.evaluate", {
            "expression": observer_js,
            "returnByValue": True,
            "awaitPromise": True,
        }, timeout=wait_timeout_ms // 1000 + 5)

        if "result" in wait_result and "value" in wait_result["result"]:
            val = wait_result["result"]["value"]
            result["response_text"] = val.get("text", "")
            result["response_duration_ms"] = val.get("duration", 0)
            result["response_complete"] = val.get("done", False)
            if not result["response_text"]:
                result["note"] = (
                    "empty response "
                    "(target may be rate-limited or not logged in)"
                )
        else:
            final = cdp_send(ws_wait, "Runtime.evaluate", {
                "expression": _response_js(tab_id),
                "returnByValue": True,
            })
            final_text = ""
            if "result" in final and "value" in final["result"]:
                final_text = final["result"]["value"].get("text", "")
            result["response_text"] = final_text
            result["response_complete"] = bool(final_text)
            if not final_text:
                result["note"] = (
                    "empty response "
                    "(target may be rate-limited or not logged in)"
                )
    finally:
        try:
            ws_wait.close()
        except Exception:
            pass

    return result


def send_and_wait(tab_id, text, wait_timeout_ms=60000, silence_ms=1500):
    """Send text and wait for a new completed response.

    ChatGPT uses the strict one-connection transaction path. Other adapters
    retain the inherited behavior until they are deliberately migrated.
    """
    adapter = _adapter_for_tab(tab_id)
    if adapter is None:
        return {
            "error": "tab_url_unavailable",
            "response_complete": False,
        }

    if adapter.name == "chatgpt":
        return _chatgpt_send_and_wait(
            tab_id,
            text,
            wait_timeout_ms=wait_timeout_ms,
            silence_ms=silence_ms,
            adapter=adapter,
        )

    return _legacy_send_and_wait(
        tab_id,
        text,
        wait_timeout_ms=wait_timeout_ms,
        silence_ms=silence_ms,
    )


def _validate_chatgpt_tab(tab_id):
    """Fail-closed validation for a relay participant tab."""
    url = core.tab_url(tab_id)
    if not url:
        return {
            "ok": False,
            "error": "relay_tab_unavailable",
            "tab_id": tab_id,
        }

    adapter = detect(url)
    if adapter.name != "chatgpt":
        return {
            "ok": False,
            "error": "relay_tab_not_chatgpt",
            "tab_id": tab_id,
            "url": url,
        }

    return {
        "ok": True,
        "tab_id": tab_id,
        "url": url,
        "adapter": "chatgpt",
    }


def bridge(
    tab_from,
    tab_to,
    rounds=3,
    *,
    control=None,
    include_text=False,
    event_sink=None,
):
    """Run a guarded bidirectional ChatGPT relay.

    One round is one complete A -> B -> A exchange. Pause/stop control is
    cooperative at safe checkpoints between browser transactions.
    """
    from .relay import run_bidirectional_relay

    return run_bidirectional_relay(
        tab_from,
        tab_to,
        rounds,
        read_response=read_response,
        send_and_wait=send_and_wait,
        validate_tab=_validate_chatgpt_tab,
        control=control,
        include_text=include_text,
        event_sink=event_sink,
    )

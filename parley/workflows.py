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


def read_response(tab_id):
    """Return the latest AI response object {text, source, hasStreaming, ...}."""
    val = core.evaluate(tab_id, UNIVERSAL_GET_RESPONSE)
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
        "expression": UNIVERSAL_GET_RESPONSE,
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
            "expression": UNIVERSAL_GET_MSG_COUNT,
            "returnByValue": True,
        })
        initial_count = 0
        if "result" in count_result and "value" in count_result["result"]:
            initial_count = count_result["result"]["value"].get("count", 0)

        initial = cdp_send(ws, "Runtime.evaluate", {
            "expression": UNIVERSAL_GET_RESPONSE,
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
                "expression": UNIVERSAL_GET_RESPONSE,
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


def send_and_wait(tab_id, text, wait_timeout_ms=60000, silence_ms=1500):
    """Send text to a tab and wait for the full NEW response.

    Keep one CDP attachment for the complete transaction. This avoids needless
    detach/reattach churn in live-browser mode and makes response tracking
    deterministic across both classic and live transports.
    """
    result = {}
    ws = cdp_connect(tab_id)
    if not ws:
        return {"error": "cannot connect to tab"}

    started = time.monotonic()

    try:
        # Snapshot the current assistant-message count before sending.
        pre_count = 0
        count_result = cdp_send(ws, "Runtime.evaluate", {
            "expression": UNIVERSAL_GET_MSG_COUNT,
            "returnByValue": True,
        })
        if "result" in count_result and "value" in count_result["result"]:
            pre_count = count_result["result"]["value"].get("count", 0) or 0

        result["pre_msg_count"] = pre_count

        # Send on the SAME target attachment.
        send_info, ws = robust_send(ws, tab_id, text)
        result["send_method"] = send_info.get("send_method", "unknown")
        result["reloaded"] = send_info.get("reloaded", False)
        result["sent_chars"] = len(text)

        prev_text = send_info.get("prev_text", "") or ""
        deadline = started + (wait_timeout_ms / 1000.0)

        saw_new = False
        last_text = ""
        last_source = ""
        last_change = None
        last_streaming = False

        # Poll the latest assistant turn through CDP. We intentionally avoid a
        # page-wide MutationObserver here: modern ChatGPT pages can mutate
        # unrelated DOM continuously, which makes "silence" a poor completion
        # signal even when the assistant response itself is stable.
        while time.monotonic() < deadline:
            check = cdp_send(ws, "Runtime.evaluate", {
                "expression": UNIVERSAL_GET_RESPONSE,
                "returnByValue": True,
            })

            val = {}
            if "result" in check and "value" in check["result"]:
                val = check["result"].get("value", {}) or {}

            current_text = (val.get("text", "") or "").strip()
            current_count = val.get("count", 0) or 0
            current_source = val.get("source", "") or ""
            has_streaming = bool(val.get("hasStreaming"))
            has_stop = bool(val.get("hasStopButton"))
            is_streaming = has_streaming or has_stop

            if (
                current_count > pre_count
                or (current_text and current_text != prev_text.strip())
                or is_streaming
            ):
                saw_new = True

            if saw_new and current_text:
                if current_text != last_text:
                    last_text = current_text
                    last_source = current_source
                    last_change = time.monotonic()

                last_streaming = is_streaming

                if last_change is not None:
                    stable_ms = int((time.monotonic() - last_change) * 1000)

                    if not is_streaming and stable_ms >= silence_ms:
                        result["response_text"] = last_text
                        result["response_duration_ms"] = int(
                            (time.monotonic() - started) * 1000
                        )
                        result["response_complete"] = True
                        result["response_source"] = last_source
                        return result

                    # Fail-soft for UIs whose stop/streaming indicator lingers
                    # after the text itself has stopped changing.
                    if stable_ms >= 5000:
                        result["response_text"] = last_text
                        result["response_duration_ms"] = int(
                            (time.monotonic() - started) * 1000
                        )
                        result["response_complete"] = True
                        result["response_source"] = last_source
                        result["note"] = "text stable despite streaming indicator"
                        return result

            time.sleep(0.2)

        result["response_text"] = last_text
        result["response_duration_ms"] = int(
            (time.monotonic() - started) * 1000
        )
        result["response_complete"] = False
        if last_source:
            result["response_source"] = last_source

        if last_text:
            result["note"] = (
                "assistant text was detected but did not reach a stable "
                "completion state before timeout"
            )
        else:
            result["note"] = (
                "no assistant response was detected before timeout"
            )
        return result

    finally:
        try:
            ws.close()
        except Exception:
            pass


def bridge(tab_from, tab_to, rounds=3):
    """Relay a conversation: read from tab_from, send to tab_to, wait, repeat."""
    results = []

    for i in range(rounds):
        round_result = {"round": i}

        ws_from = cdp_connect(tab_from)
        if not ws_from:
            round_result["error"] = "cannot connect to source"
            results.append(round_result)
            break

        try:
            initial = cdp_send(ws_from, "Runtime.evaluate", {
                "expression": UNIVERSAL_GET_RESPONSE,
                "returnByValue": True,
            })

            source_text = ""
            has_streaming = False
            has_stop = False

            if "result" in initial and "value" in initial["result"]:
                val = initial["result"]["value"]
                source_text = val.get("text", "")
                has_streaming = val.get("hasStreaming", False)
                has_stop = val.get("hasStopButton", False)

            if has_streaming or has_stop:
                observer_js = make_mutation_observer_js(60000, 1500)
                wait_result = cdp_send(ws_from, "Runtime.evaluate", {
                    "expression": observer_js,
                    "returnByValue": True,
                    "awaitPromise": True,
                }, timeout=65)
                if "result" in wait_result and "value" in wait_result["result"]:
                    val = wait_result["result"]["value"]
                    source_text = val.get("text", source_text)

            if not source_text:
                round_result["error"] = "no text found in source"
                results.append(round_result)
                break

            round_result["source_chars"] = len(source_text)
            round_result["source_preview"] = source_text[:200]
        finally:
            try:
                ws_from.close()
            except Exception:
                pass

        round_result_send = send_and_wait(tab_to, source_text[:8000])
        round_result.update(round_result_send)
        results.append(round_result)

    return results

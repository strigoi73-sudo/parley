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

import json
import time
from pathlib import Path

from . import bootstrap, chatgpt_transactions, core
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
from .relay.engine import (
    TEST_A_REPLY_PREFIX,
    TEST_A_REPLY_SUFFIX,
    TEST_B_REPLY_PREFIX,
    TEST_B_REPLY_SUFFIX,
    RESET_CHAT_COMMAND,
    RELAY_RESPONSE_TIMEOUT_MS,
)


_PARLEY_PROTOCOL_DIR = Path(__file__).resolve().parent / "protocols"
_PARLEY_PROTOCOL_WAIT_TIMEOUT_MS = 120000
_PARLEY_ATTACHMENT_TIMEOUT_MS = 30000
_PARLEY_ATTACHMENT_STABILIZE_SECONDS = 8.0
_FRESH_CHATGPT_URL = "https://chatgpt.com/"
_FRESH_CHAT_READY_TIMEOUT_MS = 30000
_FRESH_CHAT_INITIAL_PROMPT = "INITIAL PROMPT.  DO NOT REPLY."
_FRESH_CHAT_INIT_TIMEOUT_MS = 30000
_FRESH_CHAT_IDLE_STABLE_SECONDS = 1.0
_PARLEY_PROTOCOLS = {
    "A": {
        "filename": "PARLEY_TEST_CHAT_A_PROTOCOL.md",
        "ack": "PARLEY PROTOCOL A RECEIVED",
        "activation": "INITIALIZE PARLEY TEST CHAT A",
        "reply_prefix": TEST_A_REPLY_PREFIX,
        "reply_suffix": TEST_A_REPLY_SUFFIX,
    },
    "B": {
        "filename": "PARLEY_TEST_CHAT_B_PROTOCOL.md",
        "ack": "PARLEY PROTOCOL B RECEIVED",
        "activation": "INITIALIZE PARLEY TEST CHAT B",
        "reply_prefix": TEST_B_REPLY_PREFIX,
        "reply_suffix": TEST_B_REPLY_SUFFIX,
    },
}

_CHATGPT_REVEAL_FILE_INPUT_JS = r"""
(() => {
    const existing = document.querySelector('input#upload-files');
    if (existing) return {ok: true, action: 'existing-input'};

    const visible = (el) => {
        if (!el) return false;
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return (
            rect.width > 0 &&
            rect.height > 0 &&
            style.display !== 'none' &&
            style.visibility !== 'hidden'
        );
    };

    const items = Array.from(
        document.querySelectorAll('button,[role="button"],[role="menuitem"]')
    ).filter(visible);

    const textFor = (el) => [
        el.getAttribute('aria-label') || '',
        el.getAttribute('title') || '',
        el.getAttribute('data-testid') || '',
        el.innerText || '',
        el.textContent || '',
    ].join(' ').trim();

    const fileItem = items.find((el) => {
        const text = textFor(el);
        return /(upload|attach|add).*(file|photo)|(file|photo).*(upload|attach|add)/i.test(text);
    });
    if (fileItem) {
        fileItem.click();
        return {ok: true, action: 'clicked-file-control', label: textFor(fileItem)};
    }

    const plus = items.find((el) => {
        const text = textFor(el);
        return (
            el.getAttribute('data-testid') === 'composer-plus-btn' ||
            /^(add|attach|\+|more)$/i.test(text) ||
            /add.*(photo|file)|attach/i.test(text)
        );
    });
    if (plus) {
        plus.click();
        return {ok: true, action: 'clicked-composer-control', label: textFor(plus)};
    }

    return {ok: false, error: 'chatgpt_file_control_unavailable'};
})()
"""


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


def _chatgpt_page_activity(value):
    """Normalize renderer visibility/focus evidence from strict ChatGPT state."""
    if not isinstance(value, dict):
        return None
    if not any(
        key in value
        for key in ("visibilityState", "hidden", "hasFocus")
    ):
        return None
    return {
        "visibilityState": value.get("visibilityState"),
        "hidden": value.get("hidden"),
        "hasFocus": value.get("hasFocus"),
    }


def _chatgpt_focused_eval(tab_id, expression):
    """Evaluate ChatGPT state while renderer focus emulation is asserted."""
    ws = cdp_connect(tab_id, timeout=None)
    if not ws:
        return {
            "ok": False,
            "error": "cannot_connect_to_tab",
        }
    try:
        focus = core.set_focus_emulation(
            tab_id,
            True,
            timeout=None,
            ws=ws,
        )
        if not focus.get("ok"):
            return {
                "ok": False,
                "error": "chatgpt_focus_emulation_failed",
                "detail": focus,
            }
        value = core.evaluate(
            tab_id,
            expression,
            timeout=None,
            ws=ws,
        )
        if isinstance(value, dict) and value.get("ok"):
            value = dict(value)
            activity = _chatgpt_page_activity(value)
            if activity is not None:
                value["page_activity"] = activity
        return value
    finally:
        try:
            ws.close()
        except Exception:
            pass


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
    adapter = _adapter_for_tab(tab_id)
    if adapter is not None and adapter.name == "chatgpt":
        return _chatgpt_focused_eval(
            tab_id,
            adapter.response_js,
        )
    return core.evaluate(
        tab_id,
        _response_js(tab_id),
        timeout=None,
    )


def read_turn_state(tab_id):
    """Return strict ChatGPT user/assistant turn state for relay checks."""
    adapter = _adapter_for_tab(tab_id)
    if adapter is None:
        return {"ok": False, "error": "tab_url_unavailable"}
    if adapter.name != "chatgpt":
        return {"ok": False, "error": "chatgpt_adapter_required"}
    return _chatgpt_focused_eval(
        tab_id,
        adapter.turn_state_js,
    )


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
        ws = cdp_connect(tab_id, timeout=None)
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
        adapter = _adapter_for_tab(tab_id)
        if adapter is not None and adapter.name == "chatgpt":
            focus = core.set_focus_emulation(
                tab_id,
                True,
                timeout=None,
                ws=ws,
            )
            if not focus.get("ok"):
                return {
                    "error": "chatgpt_focus_emulation_failed",
                    "detail": focus,
                }
        result, ws = robust_send(ws, tab_id, text)
        result["ok"] = True
        if adapter is not None and adapter.name == "chatgpt":
            result["focus_emulation"] = True
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


def _chatgpt_eval(ws, expression, should_stop=None):
    """Evaluate strict ChatGPT JS on an existing target connection."""
    raw = cdp_send(
        ws,
        "Runtime.evaluate",
        {
            "expression": expression,
            "returnByValue": True,
        },
        timeout=None,
        should_stop=should_stop,
    )
    if isinstance(raw, dict) and raw.get("error") == "stopped":
        return {"ok": False, "error": "stopped"}

    value = _runtime_value(raw)
    if not isinstance(value, dict):
        return {
            "ok": False,
            "error": "chatgpt_runtime_value_unavailable",
            "detail": raw,
        }
    return value


def _chatgpt_state(ws, adapter, should_stop=None):
    """Read deterministic ChatGPT turn state on the existing connection."""
    state = _chatgpt_eval(
        ws,
        adapter.turn_state_js,
        should_stop=should_stop,
    )
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

        if current_id and previous_id and current_id != previous_id:
            return True

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
        expected_text = _normalize_chatgpt_text(expected.get("text"))
        current_text = _normalize_chatgpt_text(current.get("text"))
        expected_count = int(expected_state.get("user_count", 0) or 0)
        current_count = int(current_state.get("user_count", 0) or 0)

        if expected_id and current_id and expected_id == current_id:
            return True

        if expected_text and current_text and expected_text == current_text:
            # React may replace the same rendered user turn under a different
            # DOM identity while transient duplicate nodes disappear. A
            # non-increasing count plus identical normalized text is safe to
            # treat as the same submitted turn.
            return current_count <= expected_count

        return False

    if expected is None and current is None:
        return int(expected_state.get("user_count", 0) or 0) == int(
            current_state.get("user_count", 0) or 0
        )

    return False




def _snapshot_chatgpt_state(tab_id, adapter=None, should_stop=None):
    """Capture strict ChatGPT turn state before mutating the composer."""
    if adapter is None:
        adapter = _adapter_for_tab(tab_id)
    if adapter is None or adapter.name != "chatgpt":
        return {
            "ok": False,
            "error": "chatgpt_adapter_required",
        }

    ws = cdp_connect(tab_id, timeout=None)
    if not ws:
        return {
            "ok": False,
            "error": "cannot_connect_to_tab",
        }

    try:
        focus = core.set_focus_emulation(
            tab_id,
            True,
            timeout=None,
            ws=ws,
            should_stop=should_stop,
        )
        if not focus.get("ok"):
            return {
                "ok": False,
                "error": "chatgpt_focus_emulation_failed",
                "detail": focus,
            }
        return _chatgpt_state(
            ws,
            adapter,
            should_stop=should_stop,
        )
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _chatgpt_attachment_ready_js(filename):
    """Return JS that confirms the selected file is represented in the composer."""
    name = json.dumps(str(filename))
    return f"""
(() => {{
    const name = {name};
    const composer = document.querySelector('#prompt-textarea');
    const root = (
        (composer && composer.closest('form')) ||
        (composer && composer.parentElement && composer.parentElement.parentElement) ||
        document.body
    );

    const exactText = Array.from(root.querySelectorAll('*')).some((el) => {{
        if (el.children.length) return false;
        return (el.textContent || '').trim() === name;
    }});

    const namedAttribute = Array.from(
        root.querySelectorAll('[title],[aria-label]')
    ).some((el) => {{
        const title = el.getAttribute('title') || '';
        const aria = el.getAttribute('aria-label') || '';
        return title.includes(name) || aria.includes(name);
    }});

    const uploadInput = document.querySelector('input#upload-files');
    const selected = !!(
        uploadInput &&
        Array.from(uploadInput.files || []).some(
            (file) => file.name === name
        )
    );

    return {{
        ok: exactText || namedAttribute,
        filename: name,
        exactText,
        namedAttribute,
        selected,
    }};
}})()
"""


def attach_chatgpt_file(
    tab_id,
    file_path,
    timeout_ms=_PARLEY_ATTACHMENT_TIMEOUT_MS,
    should_stop=None,
):
    """Attach one local file to the ChatGPT composer and verify its UI chip."""
    adapter = _adapter_for_tab(tab_id)
    if adapter is None or adapter.name != "chatgpt":
        return {"ok": False, "error": "chatgpt_adapter_required"}

    focus = core.set_focus_emulation(
        tab_id,
        True,
        timeout=None,
        should_stop=should_stop,
    )
    if not focus.get("ok"):
        return {
            "ok": False,
            "error": "chatgpt_focus_emulation_failed",
            "stage": "focus_emulation",
            "detail": focus,
        }

    path = Path(file_path).expanduser().resolve()
    if not path.is_file():
        return {
            "ok": False,
            "error": "attachment_file_missing",
            "path": str(path),
        }

    started = time.monotonic()
    deadline = started + (timeout_ms / 1000.0)
    last_detail = None

    while time.monotonic() < deadline:
        if callable(should_stop) and should_stop():
            return {"ok": False, "error": "chatgpt_wait_stopped"}

        set_result = core.set_file_input_files(
            tab_id,
            str(path),
            selector="input#upload-files",
            timeout=5,
            should_stop=should_stop,
        )
        last_detail = set_result
        if set_result.get("ok"):
            break
        if set_result.get("error") == "stopped":
            return {"ok": False, "error": "chatgpt_wait_stopped"}
        if set_result.get("error") != "file_input_not_found":
            return {
                "ok": False,
                "error": set_result.get(
                    "error",
                    "chatgpt_file_attachment_failed",
                ),
                "stage": "select_file",
                "detail": set_result,
            }

        last_detail = core.evaluate(
            tab_id,
            _CHATGPT_REVEAL_FILE_INPUT_JS,
            timeout=5,
        )
        time.sleep(0.2)
    else:
        return {
            "ok": False,
            "error": "chatgpt_file_input_timeout",
            "stage": "locate_file_input",
            "detail": last_detail,
        }

    ready_js = _chatgpt_attachment_ready_js(path.name)
    while time.monotonic() < deadline:
        if callable(should_stop) and should_stop():
            return {"ok": False, "error": "chatgpt_wait_stopped"}

        ready = core.evaluate(tab_id, ready_js, timeout=5)
        last_detail = ready
        if isinstance(ready, dict) and ready.get("ok"):
            return {
                "ok": True,
                "path": str(path),
                "filename": path.name,
                "duration_ms": int(
                    (time.monotonic() - started) * 1000
                ),
                "evidence": ready,
            }
        time.sleep(0.2)

    return {
        "ok": False,
        "error": "chatgpt_attachment_ready_timeout",
        "stage": "verify_attachment",
        "filename": path.name,
        "detail": last_detail,
    }



def _wait_protocol_attachment_stable(
    should_stop=None,
    seconds=_PARLEY_ATTACHMENT_STABILIZE_SECONDS,
):
    """Hold a short interruptible barrier after ChatGPT renders an attachment."""
    deadline = time.monotonic() + max(0.0, float(seconds))
    while time.monotonic() < deadline:
        if callable(should_stop) and should_stop():
            return False
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    return not (callable(should_stop) and should_stop())


def _parley_protocol_active(tab_id, spec):
    """Return True if this conversation already contains a marked protocol reply."""
    prefix = json.dumps(spec["reply_prefix"])
    suffix = json.dumps(spec["reply_suffix"])
    expression = f"""
(() => {{
    const prefix = {prefix};
    const suffix = {suffix};
    const nodes = Array.from(document.querySelectorAll(
        'article[data-turn="assistant"],[data-message-author-role="assistant"]'
    ));
    const seen = nodes.some((node) => {{
        const text = (node.innerText || node.textContent || '').trim();
        return text.startsWith(prefix) && text.endsWith(suffix);
    }});
    return {{ok: true, seen}};
}})()
"""
    result = core.evaluate(tab_id, expression, timeout=5)
    return bool(isinstance(result, dict) and result.get("ok") and result.get("seen"))




def _initiate_fresh_chatgpt_tab(
    tab_id,
    *,
    prompt=_FRESH_CHAT_INITIAL_PROMPT,
    timeout_ms=_FRESH_CHAT_INIT_TIMEOUT_MS,
    idle_stable_seconds=_FRESH_CHAT_IDLE_STABLE_SECONDS,
):
    """Submit the seed prompt and wait for a stable /c/ conversation."""
    send_result = send(tab_id, prompt)
    if not isinstance(send_result, dict) or send_result.get("error"):
        return {
            "ok": False,
            "error": (
                send_result.get("error")
                if isinstance(send_result, dict)
                else "fresh_chat_initial_send_failed"
            ) or "fresh_chat_initial_send_failed",
            "stage": "send_initial_prompt",
            "detail": send_result,
        }

    started = time.monotonic()
    deadline = started + (timeout_ms / 1000.0)
    stable_since = None
    last_state = None
    last_url = ""

    while time.monotonic() < deadline:
        state = read_turn_state(tab_id)
        last_state = state
        last_url = core.tab_url(tab_id)

        if isinstance(state, dict) and state.get("ok"):
            user = state.get("user") or {}
            assistant = state.get("assistant") or {}
            user_matches = (
                _normalize_chatgpt_text(user.get("text"))
                == _normalize_chatgpt_text(prompt)
            )
            conversation_url = "/c/" in str(last_url or "")
            idle = not state.get("hasStopButton") and not assistant.get(
                "hasStreaming"
            )

            if user_matches and conversation_url and idle:
                if stable_since is None:
                    stable_since = time.monotonic()
                elif (
                    time.monotonic() - stable_since
                    >= idle_stable_seconds
                ):
                    return {
                        "ok": True,
                        "tab_id": tab_id,
                        "url": last_url,
                        "prompt": prompt,
                        "page_activity": _chatgpt_page_activity(state),
                        "duration_ms": int(
                            (time.monotonic() - started) * 1000
                        ),
                    }
            else:
                stable_since = None

        time.sleep(0.1)

    return {
        "ok": False,
        "error": "fresh_chat_initialization_timeout",
        "stage": "verify_initial_prompt",
        "tab_id": tab_id,
        "url": last_url,
        "detail": last_state,
    }


def _create_fresh_chatgpt_tab(label):
    """Create one blank ChatGPT tab and return normalized tab metadata."""
    result = core.create_tab(_FRESH_CHATGPT_URL)
    if not isinstance(result, dict) or not result.get("ok"):
        return {
            "ok": False,
            "error": (
                result.get("error")
                if isinstance(result, dict)
                else "fresh_chat_create_failed"
            ) or "fresh_chat_create_failed",
            "stage": "create_fresh_chat",
            "participant": label,
            "detail": result,
        }

    return {
        "ok": True,
        "tab": {
            "id": result["id"],
            "title": f"Fresh ChatGPT {label}",
            "url": result.get("url") or _FRESH_CHATGPT_URL,
        },
    }


def _ready_fresh_chatgpt_tab(
    label,
    tab,
    *,
    ready_timeout_ms,
    progress=None,
):
    """Focus one fresh tab, wait for its composer, and refresh its URL."""
    focus = core.set_focus_emulation(
        tab["id"],
        True,
        timeout=None,
    )
    if not focus.get("ok"):
        return {
            "ok": False,
            "error": "fresh_chat_focus_emulation_failed",
            "stage": "focus_emulation",
            "participant": label,
            "detail": focus,
        }

    if callable(progress):
        progress({
            "label": label,
            "stage": "focus_emulation",
            "status": "complete",
        })

    ready = core.wait_for(
        tab["id"],
        "#prompt-textarea",
        timeout_ms=ready_timeout_ms,
    )
    if not isinstance(ready, dict) or not ready.get("found"):
        return {
            "ok": False,
            "error": (
                ready.get("error")
                if isinstance(ready, dict)
                else "fresh_chat_composer_timeout"
            ) or "fresh_chat_composer_timeout",
            "stage": "wait_fresh_chat",
            "participant": label,
            "detail": ready,
        }

    current_url = core.tab_url(tab["id"])
    if current_url:
        tab["url"] = current_url

    return {
        "ok": True,
        "tab": tab,
    }


def _seed_fresh_chatgpt_tab(label, tab, *, progress=None):
    """Seed one ready ChatGPT tab and wait for a stable conversation URL."""
    if callable(progress):
        progress({
            "label": label,
            "stage": "initial_prompt",
            "status": "starting",
            "prompt": _FRESH_CHAT_INITIAL_PROMPT,
        })

    initiated = _initiate_fresh_chatgpt_tab(tab["id"])
    tab["initialization"] = initiated
    if not initiated.get("ok"):
        return {
            "ok": False,
            "error": initiated.get(
                "error",
                "fresh_chat_initialization_failed",
            ),
            "stage": initiated.get(
                "stage",
                "verify_initial_prompt",
            ),
            "participant": label,
            "detail": initiated,
        }

    tab["url"] = initiated.get("url") or tab["url"]
    if callable(progress):
        progress({
            "label": label,
            "stage": "initial_prompt",
            "status": "complete",
            "url": tab["url"],
            "page_activity": initiated.get("page_activity"),
        })

    return {
        "ok": True,
        "tab": tab,
    }


def create_fresh_chatgpt_pair(
    ready_timeout_ms=_FRESH_CHAT_READY_TIMEOUT_MS,
    progress=None,
):
    """Create, seed, and stabilize two clean ChatGPT conversations."""
    created = {}

    # Preserve the established pair startup barrier:
    # create both tabs -> ready both composers -> seed both conversations.
    for label in ("A", "B"):
        step = _create_fresh_chatgpt_tab(label)
        if not step.get("ok"):
            return {
                "ok": False,
                "error": step.get("error", "fresh_chat_create_failed"),
                "stage": step.get("stage", "create_fresh_chat"),
                "participant": label,
                "detail": step.get("detail"),
                "created": created,
            }
        created[label] = step["tab"]

    for label in ("A", "B"):
        step = _ready_fresh_chatgpt_tab(
            label,
            created[label],
            ready_timeout_ms=ready_timeout_ms,
            progress=progress,
        )
        if not step.get("ok"):
            return {
                "ok": False,
                "error": step.get("error", "fresh_chat_composer_timeout"),
                "stage": step.get("stage", "wait_fresh_chat"),
                "participant": label,
                "detail": step.get("detail"),
                "created": created,
            }

    for label in ("A", "B"):
        step = _seed_fresh_chatgpt_tab(
            label,
            created[label],
            progress=progress,
        )
        if not step.get("ok"):
            return {
                "ok": False,
                "error": step.get(
                    "error",
                    "fresh_chat_initialization_failed",
                ),
                "stage": step.get("stage", "verify_initial_prompt"),
                "participant": label,
                "detail": step.get("detail"),
                "created": created,
            }

    return {
        "ok": True,
        "A": created["A"],
        "B": created["B"],
        "created": created,
    }


def create_fresh_chatgpt_participant(
    label,
    ready_timeout_ms=_FRESH_CHAT_READY_TIMEOUT_MS,
    progress=None,
):
    """Create, seed, and stabilize one clean ChatGPT participant."""
    label = str(label or "").strip().upper()
    if label not in ("A", "B"):
        return {
            "ok": False,
            "error": "invalid_participant_label",
            "stage": "create_fresh_chat",
            "participant": label or None,
        }

    step = _create_fresh_chatgpt_tab(label)
    if not step.get("ok"):
        return {
            "ok": False,
            "error": step.get("error", "fresh_chat_create_failed"),
            "stage": step.get("stage", "create_fresh_chat"),
            "participant": label,
            "detail": step.get("detail"),
        }

    tab = step["tab"]
    tab["source"] = "fresh"

    step = _ready_fresh_chatgpt_tab(
        label,
        tab,
        ready_timeout_ms=ready_timeout_ms,
        progress=progress,
    )
    if not step.get("ok"):
        return {
            "ok": False,
            "error": step.get("error", "fresh_chat_composer_timeout"),
            "stage": step.get("stage", "wait_fresh_chat"),
            "participant": label,
            "detail": step.get("detail"),
            "tab": tab,
        }

    step = _seed_fresh_chatgpt_tab(
        label,
        tab,
        progress=progress,
    )
    if not step.get("ok"):
        return {
            "ok": False,
            "error": step.get(
                "error",
                "fresh_chat_initialization_failed",
            ),
            "stage": step.get("stage", "verify_initial_prompt"),
            "participant": label,
            "detail": step.get("detail"),
            "tab": tab,
        }

    return {
        "ok": True,
        "participant": label,
        "tab": tab,
    }


def initialize_parley_pair(
    tab_a,
    tab_b,
    *,
    wait_timeout_ms=_PARLEY_PROTOCOL_WAIT_TIMEOUT_MS,
    should_stop=None,
    progress=None,
):
    """Provision and activate A/B protocols through the bootstrap layer."""
    operations = bootstrap.ProtocolBootstrapOperations(
        validate_tab=_validate_chatgpt_tab,
        protocol_active=_parley_protocol_active,
        adapter_for_tab=_adapter_for_tab,
        snapshot_state=_snapshot_chatgpt_state,
        attach_file=attach_chatgpt_file,
        wait_attachment_stable=_wait_protocol_attachment_stable,
        send_ack=_chatgpt_send_and_wait,
        send_activation=send_and_wait,
    )
    return bootstrap.initialize_parley_pair(
        tab_a,
        tab_b,
        protocols=_PARLEY_PROTOCOLS,
        protocol_dir=_PARLEY_PROTOCOL_DIR,
        attachment_stabilize_seconds=(
            _PARLEY_ATTACHMENT_STABILIZE_SECONDS
        ),
        operations=operations,
        wait_timeout_ms=wait_timeout_ms,
        should_stop=should_stop,
        progress=progress,
    )


def _emit_session_progress(progress, stage, status, **extra):
    """Emit a normalized session-preparation progress event."""
    if not callable(progress):
        return
    event = {"stage": stage, "status": status}
    event.update(extra)
    progress(event)


def coordinate_parley_reset(
    source_label,
    tab_a,
    tab_b,
    *,
    should_stop=None,
):
    """Propagate an exact RESET CHAT command to the counterpart participant."""
    label = str(source_label or "").strip().upper()
    if label not in ("A", "B"):
        return {
            "ok": False,
            "error": "parley_reset_source_invalid",
            "stage": "reset_propagation",
            "reset_requested": True,
            "reset_by": label or None,
            "reset_propagated": False,
            "restart_point": "A",
            "awaiting_human_restart": True,
        }

    counterpart_label = "B" if label == "A" else "A"
    counterpart_tab = tab_b if label == "A" else tab_a
    expected_prefix = (
        TEST_B_REPLY_PREFIX if label == "A" else TEST_A_REPLY_PREFIX
    )
    expected_suffix = (
        TEST_B_REPLY_SUFFIX if label == "A" else TEST_A_REPLY_SUFFIX
    )

    result = {
        "reset_requested": True,
        "reset_by": label,
        "reset_propagated": False,
        "restart_point": "A",
        "awaiting_human_restart": True,
    }

    try:
        response = send_and_wait(
            counterpart_tab,
            RESET_CHAT_COMMAND,
            wait_timeout_ms=RELAY_RESPONSE_TIMEOUT_MS,
            should_stop=should_stop,
            expected_reply_prefix=expected_prefix,
            expected_reply_suffix=expected_suffix,
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": "parley_reset_propagation_failed",
            "stage": "reset_propagation",
            "detail": {"exception": str(exc)},
            **result,
        }

    text = (
        (response.get("response_text") or "").strip()
        if isinstance(response, dict)
        else ""
    )
    if (
        not isinstance(response, dict)
        or response.get("error")
        or not response.get("response_complete")
        or text != RESET_CHAT_COMMAND
    ):
        error = (
            response.get("error")
            if isinstance(response, dict)
            else None
        )
        return {
            "ok": False,
            "error": (
                error
                if error == "chatgpt_wait_stopped"
                else "parley_reset_propagation_failed"
            ),
            "stage": "reset_propagation",
            "detail": response,
            **result,
        }

    return {
        "ok": True,
        "status": "stopped",
        "stage": "reset",
        "reset_acknowledged_by": counterpart_label,
        "reset_response": response,
        **result,
        "reset_propagated": True,
    }


def _prepare_resolved_parley_session(
    prompt,
    tab_a,
    tab_b,
    *,
    should_stop=None,
    progress=None,
    sources=None,
):
    """Finish startup once concrete A/B browser targets are resolved."""
    context = {
        "A": tab_a,
        "B": tab_b,
    }
    if sources is not None:
        context["sources"] = sources

    _emit_session_progress(progress, "protocols", "starting")
    initialized = initialize_parley_pair(
        tab_a["id"],
        tab_b["id"],
        wait_timeout_ms=None,
        should_stop=should_stop,
        progress=progress,
    )
    if not isinstance(initialized, dict) or not initialized.get("ok"):
        return {
            "ok": False,
            "error": (
                initialized.get("error")
                if isinstance(initialized, dict)
                else "parley_protocol_bootstrap_failed"
            ) or "parley_protocol_bootstrap_failed",
            "stage": (
                initialized.get("stage", "protocols")
                if isinstance(initialized, dict)
                else "protocols"
            ),
            "participant": (
                initialized.get("participant")
                if isinstance(initialized, dict)
                else None
            ),
            "detail": initialized,
            **context,
        }

    _emit_session_progress(progress, "protocols", "complete")
    _emit_session_progress(
        progress,
        "session_prompt",
        "starting",
        label="A",
    )
    initial = send_and_wait(
        tab_a["id"],
        prompt,
        wait_timeout_ms=None,
        expected_reply_prefix=TEST_A_REPLY_PREFIX,
        expected_reply_suffix=TEST_A_REPLY_SUFFIX,
        should_stop=should_stop,
    )
    if (
        not isinstance(initial, dict)
        or initial.get("error")
        or not initial.get("response_complete")
    ):
        return {
            "ok": False,
            "error": (
                initial.get("error")
                if isinstance(initial, dict)
                else "parley_initial_prompt_failed"
            ) or "parley_initial_prompt_failed",
            "stage": "session_prompt",
            "participant": "A",
            "detail": initial,
            **context,
        }

    text = (initial.get("response_text") or "").strip()
    if text == RESET_CHAT_COMMAND:
        _emit_session_progress(
            progress,
            "reset_propagation",
            "starting",
            label="B",
            reset_by="A",
        )
        reset = coordinate_parley_reset(
            "A",
            tab_a["id"],
            tab_b["id"],
            should_stop=should_stop,
        )
        if not reset.get("ok"):
            return {
                **reset,
                "participant": "B",
                "initial_prompt": prompt,
                "initial_response": initial,
                "initial_response_text": text,
                "protocols": initialized,
                **context,
            }

        _emit_session_progress(
            progress,
            "reset_propagation",
            "complete",
            label="B",
            reset_by="A",
            restart_point=reset.get("restart_point"),
        )
        return {
            **reset,
            "initial_prompt": prompt,
            "initial_response": initial,
            "initial_response_text": text,
            "protocols": initialized,
            **context,
        }

    _emit_session_progress(
        progress,
        "session_prompt",
        "complete",
        label="A",
        response_chars=len(text),
    )
    return {
        "ok": True,
        "stage": "ready",
        **context,
        "protocols": initialized,
        "initial_prompt": prompt,
        "initial_response": initial,
        "initial_response_text": text,
    }


def prepare_parley_session(
    initial_prompt,
    participant_specs,
    *,
    should_stop=None,
    progress=None,
):
    """Prepare a Parley session from independently selected A/B sources.

    Each participant spec must declare source="existing" with a concrete tab,
    or source="fresh". Mixed existing/fresh sessions are supported.
    """
    prompt = str(initial_prompt or "").strip()
    if not prompt:
        return {
            "ok": False,
            "error": "parley_initial_prompt_required",
            "stage": "initial_prompt",
        }

    specs = participant_specs if isinstance(participant_specs, dict) else {}
    resolved = {}
    sources = {}

    _emit_session_progress(progress, "participants", "starting")

    for label in ("A", "B"):
        spec = specs.get(label)
        if not isinstance(spec, dict):
            return {
                "ok": False,
                "error": "parley_participant_spec_required",
                "stage": "participants",
                "participant": label,
            }

        source = str(spec.get("source") or "").strip().lower()
        sources[label] = source

        if source == "fresh":
            _emit_session_progress(
                progress,
                "participant",
                "starting",
                label=label,
                source="fresh",
            )
            fresh = create_fresh_chatgpt_participant(
                label,
                progress=progress,
            )
            if not isinstance(fresh, dict) or not fresh.get("ok"):
                return {
                    "ok": False,
                    "error": (
                        fresh.get("error")
                        if isinstance(fresh, dict)
                        else "fresh_chat_create_failed"
                    ) or "fresh_chat_create_failed",
                    "stage": (
                        fresh.get("stage", "participants")
                        if isinstance(fresh, dict)
                        else "participants"
                    ),
                    "participant": label,
                    "detail": fresh,
                    "participants": resolved,
                }
            resolved[label] = dict(fresh["tab"])
            resolved[label]["source"] = "fresh"
            _emit_session_progress(
                progress,
                "participant",
                "complete",
                label=label,
                source="fresh",
                tab_id=resolved[label]["id"],
            )
        elif source == "existing":
            tab = spec.get("tab")
            if not isinstance(tab, dict) or not tab.get("id"):
                return {
                    "ok": False,
                    "error": "parley_existing_tab_required",
                    "stage": "participants",
                    "participant": label,
                }
            resolved[label] = dict(tab)
            resolved[label]["source"] = "existing"
            _emit_session_progress(
                progress,
                "participant",
                "complete",
                label=label,
                source="existing",
                tab_id=resolved[label]["id"],
            )
        else:
            return {
                "ok": False,
                "error": "parley_participant_source_invalid",
                "stage": "participants",
                "participant": label,
                "source": source or None,
            }

        if callable(should_stop) and should_stop():
            return {
                "ok": False,
                "error": "chatgpt_wait_stopped",
                "stage": "participants",
                "participant": label,
                "participants": resolved,
            }

    tab_a = resolved["A"]
    tab_b = resolved["B"]
    if tab_a["id"] == tab_b["id"]:
        return {
            "ok": False,
            "error": "parley_same_tab",
            "stage": "participants",
            "A": tab_a,
            "B": tab_b,
        }

    _emit_session_progress(progress, "participants", "complete")

    return _prepare_resolved_parley_session(
        prompt,
        tab_a,
        tab_b,
        should_stop=should_stop,
        progress=progress,
        sources=sources,
    )


def prepare_fresh_parley_session(
    initial_prompt,
    *,
    should_stop=None,
    progress=None,
):
    """Prepare an all-fresh A/B session while preserving pair startup barriers."""
    prompt = str(initial_prompt or "").strip()
    if not prompt:
        return {
            "ok": False,
            "error": "parley_initial_prompt_required",
            "stage": "initial_prompt",
        }

    _emit_session_progress(progress, "fresh_chats", "starting")
    fresh = create_fresh_chatgpt_pair(progress=progress)
    if not isinstance(fresh, dict) or not fresh.get("ok"):
        return {
            "ok": False,
            "error": (
                fresh.get("error")
                if isinstance(fresh, dict)
                else "fresh_chat_create_failed"
            ) or "fresh_chat_create_failed",
            "stage": (
                fresh.get("stage", "fresh_chats")
                if isinstance(fresh, dict)
                else "fresh_chats"
            ),
            "participant": (
                fresh.get("participant")
                if isinstance(fresh, dict)
                else None
            ),
            "detail": fresh,
        }

    tab_a = fresh["A"]
    tab_b = fresh["B"]
    _emit_session_progress(progress, "fresh_chats", "complete")

    if callable(should_stop) and should_stop():
        return {
            "ok": False,
            "error": "chatgpt_wait_stopped",
            "stage": "fresh_chats",
            "A": tab_a,
            "B": tab_b,
        }

    result = _prepare_resolved_parley_session(
        prompt,
        tab_a,
        tab_b,
        should_stop=should_stop,
        progress=progress,
    )
    if result.get("ok"):
        result["fresh"] = fresh
    return result


def _chatgpt_unsent_submission_js(text, attachment_filename=None):
    """Return JS proving the composer still holds the unsent transaction."""
    expected = json.dumps(_normalize_chatgpt_text(text))
    attachment = json.dumps(
        str(attachment_filename)
        if attachment_filename
        else ""
    )
    return f"""
(() => {{
    const normalize = (value) => String(value || '')
        .replace(/\\s+/g, ' ')
        .trim();
    const expected = {expected};
    const attachmentName = {attachment};
    const composer = document.querySelector('#prompt-textarea');
    const rawText = composer
        ? (
            composer.tagName === 'TEXTAREA'
                ? composer.value
                : (composer.innerText || composer.textContent || '')
        )
        : '';
    const button = document.querySelector(
        'button[data-testid="send-button"]'
    );
    const stop = document.querySelector(
        'button[data-testid="stop-button"],'
        + 'button[aria-label*="Stop"],'
        + 'button[aria-label*="stop"]'
    );

    let attachmentPresent = !attachmentName;
    if (attachmentName) {{
        const root = (
            (composer && composer.closest('form')) ||
            (composer && composer.parentElement &&
                composer.parentElement.parentElement) ||
            document.body
        );
        attachmentPresent = Array.from(
            root.querySelectorAll('*')
        ).some((el) => {{
            if (el.children.length) return false;
            return (el.textContent || '').trim() === attachmentName;
        }}) || Array.from(
            root.querySelectorAll('[title],[aria-label]')
        ).some((el) => {{
            const title = el.getAttribute('title') || '';
            const aria = el.getAttribute('aria-label') || '';
            return (
                title.includes(attachmentName) ||
                aria.includes(attachmentName)
            );
        }});
    }}

    const sendEnabled = !!(
        button &&
        !button.disabled &&
        button.getAttribute('aria-disabled') !== 'true'
    );
    const exactText = normalize(rawText) === expected;
    return {{
        ok: true,
        source: 'chatgpt-unsent-submission',
        exactText,
        sendEnabled,
        hasStopButton: !!stop,
        attachmentPresent,
        composerChars: normalize(rawText).length,
    }};
}})()
"""


def _chatgpt_send_and_wait(
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
):
    """Delegate the strict ChatGPT transaction to its dedicated module."""
    operations = chatgpt_transactions.ChatGPTTransactionOperations(
        adapter_for_tab=_adapter_for_tab,
        monotonic=time.monotonic,
        sleep=time.sleep,
        connect=cdp_connect,
        focus_emulation=core.set_focus_emulation,
        state=_chatgpt_state,
        evaluate=_chatgpt_eval,
        send=cdp_send,
        has_new_user=_chatgpt_has_new_user,
        has_new_assistant=_chatgpt_has_new_assistant,
        same_user_turn=_chatgpt_same_user_turn,
        normalize_text=_normalize_chatgpt_text,
        page_activity=_chatgpt_page_activity,
        unsent_submission_js=_chatgpt_unsent_submission_js,
    )
    return chatgpt_transactions.send_and_wait(
        tab_id,
        text,
        wait_timeout_ms=wait_timeout_ms,
        silence_ms=silence_ms,
        adapter=adapter,
        expected_reply_prefix=expected_reply_prefix,
        expected_reply_suffix=expected_reply_suffix,
        should_stop=should_stop,
        pre_state_override=pre_state_override,
        require_user_text_match=require_user_text_match,
        submission_timeout_ms=submission_timeout_ms,
        retry_unsent_submission=retry_unsent_submission,
        required_attachment_filename=required_attachment_filename,
        submission_retry_interval_ms=submission_retry_interval_ms,
        max_submission_attempts=max_submission_attempts,
        operations=operations,
    )


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


def send_and_wait(
    tab_id,
    text,
    wait_timeout_ms=60000,
    silence_ms=1500,
    expected_reply_prefix=None,
    expected_reply_suffix=None,
    should_stop=None,
):
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
            expected_reply_prefix=expected_reply_prefix,
            expected_reply_suffix=expected_reply_suffix,
            should_stop=should_stop,
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
    initial_context=None,
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
        read_turn_state=read_turn_state,
        send_and_wait=send_and_wait,
        validate_tab=_validate_chatgpt_tab,
        control=control,
        include_text=include_text,
        event_sink=event_sink,
        initial_context=initial_context,
    )

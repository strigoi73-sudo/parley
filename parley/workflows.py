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
from .relay.engine import (
    TEST_A_REPLY_PREFIX,
    TEST_A_REPLY_SUFFIX,
    TEST_B_REPLY_PREFIX,
    TEST_B_REPLY_SUFFIX,
)


_PARLEY_PROTOCOL_DIR = Path(__file__).resolve().parent / "protocols"
_PARLEY_PROTOCOL_WAIT_TIMEOUT_MS = 120000
_PARLEY_ATTACHMENT_TIMEOUT_MS = 30000
_PARLEY_ATTACHMENT_STABILIZE_SECONDS = 3.0
_FRESH_CHATGPT_URL = "https://chatgpt.com/"
_FRESH_CHAT_READY_TIMEOUT_MS = 30000
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
    val = core.evaluate(
        tab_id,
        _response_js(tab_id),
        timeout=None,
    )
    return val


def read_turn_state(tab_id):
    """Return strict ChatGPT user/assistant turn state for relay checks."""
    adapter = _adapter_for_tab(tab_id)
    if adapter is None:
        return {"ok": False, "error": "tab_url_unavailable"}
    if adapter.name != "chatgpt":
        return {"ok": False, "error": "chatgpt_adapter_required"}
    return core.evaluate(
        tab_id,
        adapter.turn_state_js,
        timeout=None,
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



def create_fresh_chatgpt_pair(
    ready_timeout_ms=_FRESH_CHAT_READY_TIMEOUT_MS,
):
    """Create two clean ChatGPT tabs and wait for both composers."""
    created = {}

    for label in ("A", "B"):
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
                "created": created,
            }

        created[label] = {
            "id": result["id"],
            "title": f"Fresh ChatGPT {label}",
            "url": result.get("url") or _FRESH_CHATGPT_URL,
        }

    for label in ("A", "B"):
        tab = created[label]
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
                "created": created,
            }

        current_url = core.tab_url(tab["id"])
        if current_url:
            tab["url"] = current_url

    return {
        "ok": True,
        "A": created["A"],
        "B": created["B"],
        "created": created,
    }


def _emit_protocol_progress(progress, label, stage, status, **extra):
    if not callable(progress):
        return
    event = {"label": label, "stage": stage, "status": status}
    event.update(extra)
    progress(event)


def initialize_parley_pair(
    tab_a,
    tab_b,
    *,
    wait_timeout_ms=_PARLEY_PROTOCOL_WAIT_TIMEOUT_MS,
    should_stop=None,
    progress=None,
):
    """Provision and activate A/B protocols with explicit serial barriers."""
    if tab_a == tab_b:
        return {
            "ok": False,
            "error": "parley_same_tab",
            "stage": "preflight",
            "response_complete": False,
        }

    participants = {}
    ordered = (
        ("A", tab_a, _PARLEY_PROTOCOLS["A"]),
        ("B", tab_b, _PARLEY_PROTOCOLS["B"]),
    )

    for label, tab_id, spec in ordered:
        validation = _validate_chatgpt_tab(tab_id)
        if not validation.get("ok"):
            return {
                "ok": False,
                "error": validation.get(
                    "error",
                    "parley_tab_preflight_failed",
                ),
                "stage": "preflight",
                "participant": label,
                "response_complete": False,
                "detail": validation,
                "participants": participants,
            }

        active = _parley_protocol_active(tab_id, spec)
        participants[label] = {
            "tab_id": tab_id,
            "already_active": active,
        }
        _emit_protocol_progress(
            progress,
            label,
            "preflight",
            "already_active" if active else "pending",
        )

    # Phase 1: A file + ACK, then B file + ACK.
    for label, tab_id, spec in ordered:
        if participants[label]["already_active"]:
            _emit_protocol_progress(
                progress, label, "protocol_ready", "complete", reused=True
            )
            continue

        protocol_path = _PARLEY_PROTOCOL_DIR / spec["filename"]
        adapter = _adapter_for_tab(tab_id)
        pre_attachment_state = _snapshot_chatgpt_state(
            tab_id,
            adapter=adapter,
            should_stop=should_stop,
        )
        if not pre_attachment_state.get("ok"):
            return {
                "ok": False,
                "error": pre_attachment_state.get(
                    "error",
                    "parley_protocol_snapshot_failed",
                ),
                "stage": "protocol_snapshot",
                "participant": label,
                "response_complete": False,
                "detail": pre_attachment_state,
                "participants": participants,
            }

        _emit_protocol_progress(
            progress,
            label,
            "provision",
            "starting",
            filename=spec["filename"],
        )

        attachment = attach_chatgpt_file(
            tab_id,
            protocol_path,
            should_stop=should_stop,
        )
        participants[label]["attachment"] = attachment
        if not attachment.get("ok"):
            return {
                "ok": False,
                "error": attachment.get(
                    "error",
                    "parley_protocol_attachment_failed",
                ),
                "stage": "protocol_attachment",
                "participant": label,
                "response_complete": False,
                "detail": attachment,
                "participants": participants,
            }

        _emit_protocol_progress(
            progress,
            label,
            "attachment_stabilizing",
            "waiting",
            seconds=_PARLEY_ATTACHMENT_STABILIZE_SECONDS,
        )
        if not _wait_protocol_attachment_stable(
            should_stop=should_stop,
        ):
            return {
                "ok": False,
                "error": "chatgpt_wait_stopped",
                "stage": "attachment_stabilizing",
                "participant": label,
                "response_complete": False,
                "participants": participants,
            }

        ack_prompt = (
            "Read the attached Parley protocol file. Do not initialize or "
            "apply the Parley test protocol yet. Reply exactly with the "
            "following text and nothing else:\n\n"
            + spec["ack"]
        )
        _emit_protocol_progress(
            progress, label, "protocol_ack", "waiting"
        )
        ack_result = _chatgpt_send_and_wait(
            tab_id,
            ack_prompt,
            wait_timeout_ms=wait_timeout_ms,
            adapter=adapter,
            should_stop=should_stop,
            pre_state_override=pre_attachment_state,
            require_user_text_match=False,
            submission_timeout_ms=60000,
        )
        participants[label]["provision"] = ack_result
        if (
            not isinstance(ack_result, dict)
            or ack_result.get("error")
            or not ack_result.get("response_complete")
        ):
            return {
                "ok": False,
                "error": (
                    ack_result.get("error")
                    if isinstance(ack_result, dict)
                    else "parley_protocol_ack_failed"
                ) or "parley_protocol_ack_failed",
                "stage": "protocol_ack",
                "participant": label,
                "response_complete": False,
                "detail": ack_result,
                "participants": participants,
            }

        observed = (ack_result.get("response_text") or "").strip()
        if observed != spec["ack"]:
            return {
                "ok": False,
                "error": "parley_protocol_ack_mismatch",
                "stage": "protocol_ack",
                "participant": label,
                "response_complete": False,
                "expected": spec["ack"],
                "observed": observed,
                "participants": participants,
            }

        _emit_protocol_progress(
            progress, label, "protocol_ready", "complete"
        )

    # Global barrier: both protocols are verified before activation begins.
    for label, tab_id, spec in ordered:
        if participants[label]["already_active"]:
            _emit_protocol_progress(
                progress, label, "ready", "complete", reused=True
            )
            continue

        _emit_protocol_progress(
            progress, label, "activation", "starting"
        )
        activation = send_and_wait(
            tab_id,
            spec["activation"],
            wait_timeout_ms=wait_timeout_ms,
            expected_reply_prefix=spec["reply_prefix"],
            expected_reply_suffix=spec["reply_suffix"],
            should_stop=should_stop,
        )
        participants[label]["activation"] = activation
        if (
            not isinstance(activation, dict)
            or activation.get("error")
            or not activation.get("response_complete")
        ):
            return {
                "ok": False,
                "error": (
                    activation.get("error")
                    if isinstance(activation, dict)
                    else "parley_protocol_activation_failed"
                ) or "parley_protocol_activation_failed",
                "stage": "activation",
                "participant": label,
                "response_complete": False,
                "detail": activation,
                "participants": participants,
            }

        _emit_protocol_progress(
            progress,
            label,
            "ready",
            "complete",
            response_text=activation.get("response_text"),
        )

    return {
        "ok": True,
        "response_complete": True,
        "stage": "ready",
        "participants": participants,
    }


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
    deadline = (
        None
        if wait_timeout_ms is None
        else started + (wait_timeout_ms / 1000.0)
    )

    def keep_waiting(until=None):
        if callable(should_stop) and should_stop():
            return False
        return until is None or time.monotonic() < until

    ws = cdp_connect(tab_id, timeout=None)
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
        pre_state = pre_state_override
        if pre_state is None:
            pre_state = _chatgpt_state(
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

        prepare = _chatgpt_eval(
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

        insert_result = cdp_send(
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
        time.sleep(0.2)

        click = _chatgpt_eval(
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

        # Submission is not considered successful until ChatGPT's DOM proves a
        # newer user turn exists.
        submitted_state = None
        human_reset_requested = False
        last_submission_state = None
        submission_limit = (
            None
            if submission_timeout_ms is None
            else time.monotonic() + (
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
            state = _chatgpt_state(ws, adapter, should_stop=should_stop)
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
            if _chatgpt_has_new_user(
                pre_state,
                state,
                expected_user_text,
            ):
                submitted_state = state
                break
            if _chatgpt_has_new_user(pre_state, state, "RESET CHAT"):
                submitted_state = state
                human_reset_requested = True
                break
            time.sleep(0.1)

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
            observed_text = _normalize_chatgpt_text(
                observed_user.get("text")
            )
            pre_text = _normalize_chatgpt_text(
                pre_user.get("text")
            )
            expected_text = _normalize_chatgpt_text(text)
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
            last_change = time.monotonic()
            candidate_replacements = 0
            last_identity = None
            candidate_seen = False

            while keep_waiting(deadline):
                state = _chatgpt_state(ws, adapter, should_stop=should_stop)
                if not state.get("ok"):
                    return fail(
                        state.get("error", "chatgpt_state_failed"),
                        "track_marked_response",
                        response_text=last_text,
                        detail=state,
                    )

                if not _chatgpt_same_user_turn(submitted_state, state):
                    current_user = state.get("user") or {}
                    current_user_text = _normalize_chatgpt_text(
                        current_user.get("text")
                    )
                    if (
                        current_user_text == "RESET CHAT"
                        and _chatgpt_has_new_user(
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
                        last_change = time.monotonic()
                        time.sleep(0.1)
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
                    and _chatgpt_has_new_assistant(pre_state, state)
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
                    last_change = time.monotonic()
                    time.sleep(0.2)
                    continue

                current_count = int(
                    state.get("assistant_count", 0) or 0
                )
                current_identity = (
                    current.get("turn_id"),
                    current.get("turn_index"),
                    current_count,
                )
                now = time.monotonic()

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
                        "response_duration_ms": int(
                            (now - started) * 1000
                        ),
                    }

                time.sleep(0.2)

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
            state = _chatgpt_state(ws, adapter, should_stop=should_stop)
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
        last_change = time.monotonic()
        candidate_replacements = 0

        while keep_waiting(deadline):
            state = _chatgpt_state(ws, adapter, should_stop=should_stop)
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

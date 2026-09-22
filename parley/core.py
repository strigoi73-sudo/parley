"""
parley.core - Pure Chrome DevTools Protocol (CDP) engine.

This layer is completely site-agnostic. It knows nothing about ChatGPT, Gemini
or any AI chat UI. It provides:

  * Two CDP transports:
      - classic: HTTP tab discovery + per-tab WebSocket
      - live: one persistent browser WebSocket + flattened Target sessions
    with automatic reconnection.
  * Generic browser-automation primitives that work on ANY website:
    evaluate JS, read DOM text, extract elements, wait for a selector,
    click, type, navigate, and read cookies.

Build site-specific behaviour on top of this in `parley.adapters` and
`parley.workflows`.
"""

import json
import os
import time
import urllib.request
import urllib.error

import websocket

from .live_browser import (
    LiveTabConnection,
    chrome_user_data_dir,
    live_devtools_endpoint,
    live_target_infos,
)

CDP_HOST = os.environ.get("PARLEY_CDP_HOST", "localhost")
CDP_PORT = int(os.environ.get("PARLEY_CDP_PORT", "9222"))
CDP_HTTP = f"http://{CDP_HOST}:{CDP_PORT}"

# Maximum reconnect attempts for WebSocket connections
MAX_RECONNECT = 3
RECONNECT_DELAY = 1


# ============================================================================
# CONNECTION MODE
# ============================================================================

def connection_mode():
    """Return the configured transport: classic or live."""
    mode = os.environ.get(
        "PARLEY_CONNECTION_MODE",
        "classic",
    ).strip().lower()
    if mode not in ("classic", "live"):
        raise ValueError(
            "PARLEY_CONNECTION_MODE must be 'classic' or 'live' "
            "(got %r)" % mode
        )
    return mode


# ============================================================================
# CDP TRANSPORT
# ============================================================================

def http_get(path):
    try:
        with urllib.request.urlopen(f"{CDP_HTTP}{path}", timeout=5) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}


def get_ws_url(tab_id):
    tabs = http_get("/json/list")
    if isinstance(tabs, dict) and "error" in tabs:
        return None
    for tab in tabs:
        if tab.get("id") == tab_id:
            return tab.get("webSocketDebuggerUrl")
    return None


def cdp_connect(tab_id, retries=MAX_RECONNECT, timeout=10):
    """Connect to a tab using the configured transport."""
    if connection_mode() == "live":
        for attempt in range(retries):
            try:
                return LiveTabConnection(tab_id, timeout=timeout)
            except Exception:
                if attempt < retries - 1:
                    time.sleep(RECONNECT_DELAY)
                else:
                    return None
        return None

    ws_url = get_ws_url(tab_id)
    if not ws_url:
        return None
    for attempt in range(retries):
        try:
            ws = websocket.create_connection(
                ws_url,
                timeout=timeout,
                suppress_origin=True,
            )
            return ws
        except Exception:
            if attempt < retries - 1:
                time.sleep(RECONNECT_DELAY)
                # Refresh WebSocket URL (may have changed)
                ws_url = get_ws_url(tab_id)
                if not ws_url:
                    return None
            else:
                return None
    return None


def cdp_send(
    ws,
    method,
    params=None,
    timeout=10,
    should_stop=None,
):
    """Send one CDP command, optionally without an overall deadline."""
    command = getattr(ws, "command", None)
    if callable(command):
        if callable(should_stop):
            return command(
                method,
                params,
                timeout=timeout,
                should_stop=should_stop,
            )
        return command(method, params, timeout=timeout)

    msg_id = int(time.time() * 1000) % 100000
    msg = {"id": msg_id, "method": method}
    if params:
        msg["params"] = params
    ws.send(json.dumps(msg))
    deadline = None if timeout is None else time.monotonic() + timeout
    poll_timeout = 0.25 if callable(should_stop) else timeout
    ws.settimeout(poll_timeout)

    while deadline is None or time.monotonic() < deadline:
        if callable(should_stop) and should_stop():
            return {"error": "stopped"}

        try:
            resp = json.loads(ws.recv())
            if resp.get("id") == msg_id:
                if "result" in resp:
                    return resp["result"]
                if "error" in resp:
                    return {"error": resp["error"]}
                return {"error": "malformed CDP response"}
        except websocket.WebSocketTimeoutException:
            if deadline is None:
                continue
            break
        except Exception:
            break
    return {"error": "timeout"}


def cdp_send_with_retry(
    ws,
    method,
    params=None,
    timeout=10,
    tab_id=None,
    retries=MAX_RECONNECT,
    should_stop=None,
):
    """Send CDP command with automatic reconnection on failure."""
    for attempt in range(retries):
        try:
            result = cdp_send(
                ws,
                method,
                params,
                timeout,
                should_stop=should_stop,
            )
            if "error" not in result:
                return result
            if result.get("error") == "stopped":
                return result
            # If error and we have retries left, reconnect. Live target
            # handles can reattach in place so callers keep a valid object.
            if attempt < retries - 1 and tab_id:
                reconnect = getattr(ws, "reconnect", None)
                if callable(reconnect):
                    if not reconnect():
                        return {"error": "reconnect failed"}
                else:
                    ws.close()
                    ws = cdp_connect(tab_id, timeout=timeout)
                    if not ws:
                        return {"error": "reconnect failed"}
        except Exception as e:
            if attempt < retries - 1 and tab_id:
                reconnect = getattr(ws, "reconnect", None)
                if callable(reconnect):
                    if not reconnect():
                        return {"error": "reconnect failed"}
                else:
                    try:
                        ws.close()
                    except Exception:
                        pass
                    ws = cdp_connect(tab_id, timeout=timeout)
                    if not ws:
                        return {"error": "reconnect failed"}
            else:
                return {"error": str(e)}
    return {"error": "max retries exceeded"}


def _unwrap(result, default=None):
    """Extract a returnByValue result value from a Runtime.evaluate response."""
    if isinstance(result, dict) and "result" in result:
        val = result["result"]
        if isinstance(val, dict) and "value" in val:
            return val["value"]
    return default


# ============================================================================
# GENERIC BROWSER PRIMITIVES (site-agnostic)
# ============================================================================

def list_tabs():
    """Return a list of open page tabs: [{id, title, url}, ...]."""
    if connection_mode() == "live":
        try:
            targets = live_target_infos()
        except Exception as exc:
            return {"error": str(exc)}

        return [
            {
                "id": target.get("targetId"),
                "title": target.get("title", "")[:80],
                "url": target.get("url", ""),
            }
            for target in targets
            if target.get("type") == "page"
        ]

    tabs = http_get("/json/list")
    if isinstance(tabs, dict) and "error" in tabs:
        return tabs
    result = []
    for tab in tabs:
        if tab.get("type") == "page":  # Only pages, not iframes/workers
            result.append({
                "id": tab.get("id"),
                "title": tab.get("title", "")[:80],
                "url": tab.get("url", ""),
            })
    return result


def tab_url(tab_id):
    """Return the current URL of a tab."""
    tabs = list_tabs()
    if isinstance(tabs, dict) and "error" in tabs:
        return ""
    for tab in tabs:
        if tab.get("id") == tab_id:
            return tab.get("url", "")
    return ""


def evaluate(tab_id, js, await_promise=False, timeout=10, ws=None):
    """Evaluate JavaScript in a tab and return the (by-value) result.

    Opens a short-lived connection unless an existing `ws` is provided.
    """
    own = ws is None
    if own:
        ws = cdp_connect(tab_id, timeout=timeout)
        if not ws:
            return {"error": "cannot connect to tab"}
    try:
        result = cdp_send_with_retry(ws, "Runtime.evaluate", {
            "expression": js,
            "returnByValue": True,
            "awaitPromise": await_promise,
        }, timeout=timeout, tab_id=tab_id)
        val = _unwrap(result, default="__NO_VALUE__")
        if val == "__NO_VALUE__":
            return result
        return val
    finally:
        if own:
            try:
                ws.close()
            except Exception:
                pass


def _js_str(s):
    """Escape a Python string for safe embedding inside a single-quoted JS string."""
    return s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r")


def read_dom(tab_id, selector=None):
    """Return the innerText of `selector` (first match) or the whole body."""
    if selector:
        js = f"""
        (() => {{
            const el = document.querySelector('{_js_str(selector)}');
            if (!el) return {{ error: 'not found: {_js_str(selector)}' }};
            return {{ text: el.innerText, html: el.outerHTML.slice(0, 20000) }};
        }})()
        """
    else:
        js = "(() => ({ text: document.body.innerText, title: document.title, url: location.href }))()"
    return evaluate(tab_id, js)


def extract(tab_id, selector, attr=None):
    """Return a list of text (or attribute values) for all elements matching selector."""
    getter = f"el.getAttribute('{_js_str(attr)}')" if attr else "el.innerText"
    js = f"""
    (() => {{
        const els = document.querySelectorAll('{_js_str(selector)}');
        const out = [];
        for (const el of els) {{ const v = {getter}; if (v != null) out.push(v); }}
        return {{ count: out.length, items: out }};
    }})()
    """
    return evaluate(tab_id, js)


def wait_for(tab_id, selector, timeout_ms=10000, poll_ms=250):
    """Poll until `selector` exists in the DOM (or timeout). Returns {found, waited_ms}."""
    js = f"(() => !!document.querySelector('{_js_str(selector)}'))()"
    ws = cdp_connect(tab_id)
    if not ws:
        return {"error": "cannot connect to tab"}
    start = time.time()
    try:
        while (time.time() - start) * 1000 < timeout_ms:
            found = _unwrap(cdp_send_with_retry(ws, "Runtime.evaluate", {
                "expression": js, "returnByValue": True,
            }, tab_id=tab_id))
            if found:
                return {"found": True, "waited_ms": int((time.time() - start) * 1000)}
            time.sleep(poll_ms / 1000)
        return {"found": False, "waited_ms": int((time.time() - start) * 1000)}
    finally:
        try:
            ws.close()
        except Exception:
            pass


def click(tab_id, selector):
    """Click the first element matching a CSS selector."""
    js = f"""
    (() => {{
        const el = document.querySelector('{_js_str(selector)}');
        if (!el) return {{ error: 'not found: {_js_str(selector)}' }};
        el.click();
        return {{ ok: true, tag: el.tagName }};
    }})()
    """
    return evaluate(tab_id, js)


def navigate(tab_id, url):
    """Navigate a tab to a URL."""
    ws = cdp_connect(tab_id)
    if not ws:
        return {"error": "cannot connect to tab"}
    try:
        return cdp_send_with_retry(ws, "Page.navigate", {"url": url}, tab_id=tab_id)
    finally:
        try:
            ws.close()
        except Exception:
            pass


def get_cookies(tab_id, domain=None):
    """Return cookies for a tab via CDP (includes HttpOnly cookies).

    Uses Network.getAllCookies. Optionally filter by a domain substring.
    WARNING: session cookies are bearer credentials. Never log or commit them.
    """
    ws = cdp_connect(tab_id)
    if not ws:
        return {"error": "cannot connect to tab"}
    try:
        result = cdp_send_with_retry(ws, "Network.getAllCookies", {}, tab_id=tab_id)
        cookies = result.get("cookies", []) if isinstance(result, dict) else []
        if domain:
            cookies = [c for c in cookies if domain in (c.get("domain") or "")]
        return {"count": len(cookies), "cookies": cookies}
    finally:
        try:
            ws.close()
        except Exception:
            pass


def set_cookie(tab_id, name, value, domain, path="/", secure=True, http_only=False):
    """Inject a cookie into a tab via CDP (Network.setCookie)."""
    ws = cdp_connect(tab_id)
    if not ws:
        return {"error": "cannot connect to tab"}
    try:
        return cdp_send_with_retry(ws, "Network.setCookie", {
            "name": name, "value": value, "domain": domain,
            "path": path, "secure": secure, "httpOnly": http_only,
        }, tab_id=tab_id)
    finally:
        try:
            ws.close()
        except Exception:
            pass

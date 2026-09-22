"""
parley.core - Pure Chrome DevTools Protocol (CDP) engine.

This layer is completely site-agnostic. It knows nothing about ChatGPT, Gemini
or any AI chat UI. It provides:

  * A CDP transport with two connection modes:
      - classic: HTTP tab discovery + per-tab WebSocket (default, e.g. :9222)
      - live: attach to an already-running Chrome via DevToolsActivePort and
        browser-level Target sessions (Chrome 144+ live-session debugging)
  * Generic browser-automation primitives that work on ANY website:
    evaluate JS, read DOM text, extract elements, wait for a selector,
    click, type, navigate, and read cookies.

Build site-specific behaviour on top of this in `parley.adapters` and
`parley.workflows`.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

import websocket


CDP_HOST = os.environ.get("PARLEY_CDP_HOST", "localhost")
CDP_PORT = int(os.environ.get("PARLEY_CDP_PORT", "9222"))
CDP_HTTP = f"http://{CDP_HOST}:{CDP_PORT}"

# Maximum reconnect attempts for WebSocket connections.
MAX_RECONNECT = 3
RECONNECT_DELAY = 1


# ============================================================================
# CONNECTION MODE / LIVE CHROME DISCOVERY
# ============================================================================

def connection_mode():
    """Return `classic` or `live` from PARLEY_CONNECTION_MODE."""
    mode = os.environ.get("PARLEY_CONNECTION_MODE", "classic").strip().lower()
    if mode not in ("classic", "live"):
        raise ValueError(
            "PARLEY_CONNECTION_MODE must be 'classic' or 'live' "
            f"(got {mode!r})"
        )
    return mode


def chrome_user_data_dir():
    """Return the Chrome user-data directory used for live attachment.

    Override with PARLEY_CHROME_USER_DATA_DIR. Otherwise use the normal stable
    Chrome user-data location for the current OS.
    """
    override = os.environ.get("PARLEY_CHROME_USER_DATA_DIR")
    if override:
        return Path(os.path.expandvars(os.path.expanduser(override))).resolve()

    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise RuntimeError(
                "LOCALAPPDATA is unavailable; set PARLEY_CHROME_USER_DATA_DIR"
            )
        return Path(local_app_data) / "Google" / "Chrome" / "User Data"

    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Google"
            / "Chrome"
        )

    return Path.home() / ".config" / "google-chrome"


def live_devtools_endpoint(user_data_dir=None):
    """Read Chrome's browser WebSocket endpoint from DevToolsActivePort.

    Chrome writes two non-empty lines:
      <port>
      <browser websocket path>

    Example:
      49321
      /devtools/browser/<uuid>
    """
    root = Path(user_data_dir) if user_data_dir else chrome_user_data_dir()
    port_file = root / "DevToolsActivePort"

    try:
        content = port_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            "Could not find Chrome's DevToolsActivePort at "
            f"{port_file}. In normal Chrome, open "
            "chrome://inspect/#remote-debugging and enable remote debugging. "
            "If Chrome uses a different user-data directory, set "
            "PARLEY_CHROME_USER_DATA_DIR."
        ) from exc

    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if len(lines) < 2:
        raise RuntimeError(f"Invalid DevToolsActivePort contents in {port_file}")

    try:
        port = int(lines[0])
    except ValueError as exc:
        raise RuntimeError(
            f"Invalid DevToolsActivePort port {lines[0]!r} in {port_file}"
        ) from exc

    if port <= 0 or port > 65535:
        raise RuntimeError(
            f"Invalid DevToolsActivePort port {port!r} in {port_file}"
        )

    path = lines[1]
    if not path.startswith("/"):
        path = "/" + path

    return f"ws://127.0.0.1:{port}{path}"


def _open_live_browser_ws(timeout=30):
    """Open the browser-level live-debugging WebSocket.

    Chrome may display a user-approval prompt when a live connection is opened.
    """
    endpoint = live_devtools_endpoint()
    return websocket.create_connection(
        endpoint,
        timeout=timeout,
        suppress_origin=True,
    )


def _next_message_id():
    # A process-local monotonic-enough id is sufficient because every helper
    # opens a dedicated WebSocket and drains its own response stream.
    return int(time.time_ns() % 2_000_000_000)


def _browser_cdp_send(ws, method, params=None, timeout=10, session_id=None):
    """Send one command on a browser-level CDP WebSocket."""
    msg_id = _next_message_id()
    msg = {"id": msg_id, "method": method}
    if params:
        msg["params"] = params
    if session_id:
        msg["sessionId"] = session_id

    ws.send(json.dumps(msg))
    ws.settimeout(timeout)
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            resp = json.loads(ws.recv())
        except websocket.WebSocketTimeoutException:
            break
        except Exception as exc:
            return {"error": str(exc)}

        if resp.get("id") != msg_id:
            continue

        # For flattened target sessions the command response is tagged with the
        # sessionId. Browser-level commands have no sessionId.
        if session_id and resp.get("sessionId") not in (None, session_id):
            continue

        return resp.get("result", resp.get("error", {}))

    return {"error": "timeout"}


def _live_target_infos():
    """Return browser TargetInfo objects from the currently running Chrome."""
    ws = _open_live_browser_ws()
    try:
        result = _browser_cdp_send(ws, "Target.getTargets", timeout=15)
        if "error" in result:
            raise RuntimeError(f"Target.getTargets failed: {result['error']}")
        return result.get("targetInfos", [])
    finally:
        try:
            ws.close()
        except Exception:
            pass


class LiveTabConnection:
    """Tab-like CDP socket backed by a browser-level flattened Target session.

    Existing Parley workflow code expects a socket with send/recv/settimeout/
    close. This adapter keeps that contract intact while routing every command
    through a browser WebSocket using the target's sessionId.
    """

    def __init__(self, tab_id, timeout=30):
        self.tab_id = tab_id
        self._ws = _open_live_browser_ws(timeout=timeout)
        self._closed = False
        self._timeout = timeout

        result = _browser_cdp_send(
            self._ws,
            "Target.attachToTarget",
            {"targetId": tab_id, "flatten": True},
            timeout=timeout,
        )
        session_id = result.get("sessionId") if isinstance(result, dict) else None
        if not session_id:
            try:
                self._ws.close()
            except Exception:
                pass
            raise RuntimeError(
                f"Could not attach to Chrome target {tab_id}: {result}"
            )
        self.session_id = session_id

    def send(self, payload):
        if self._closed:
            raise RuntimeError("live tab connection is closed")
        msg = json.loads(payload)
        msg["sessionId"] = self.session_id
        self._ws.send(json.dumps(msg))

    def recv(self):
        if self._closed:
            raise RuntimeError("live tab connection is closed")

        # Ignore unrelated browser events. This WebSocket owns only one
        # attached Parley target session, so command responses for that session
        # are safe to pass through to the existing cdp_send response matcher.
        while True:
            raw = self._ws.recv()
            msg = json.loads(raw)
            session_id = msg.get("sessionId")
            if session_id not in (None, self.session_id):
                continue
            return json.dumps(msg)

    def settimeout(self, timeout):
        self._timeout = timeout
        self._ws.settimeout(timeout)

    def close(self):
        if self._closed:
            return
        try:
            _browser_cdp_send(
                self._ws,
                "Target.detachFromTarget",
                {"sessionId": self.session_id},
                timeout=2,
            )
        except Exception:
            pass
        self._closed = True
        try:
            self._ws.close()
        except Exception:
            pass


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
    """Return a classic-mode per-tab WebSocket URL."""
    tabs = http_get("/json/list")
    if isinstance(tabs, dict) and "error" in tabs:
        return None
    for tab in tabs:
        if tab.get("id") == tab_id:
            return tab.get("webSocketDebuggerUrl")
    return None


def cdp_connect(tab_id, retries=MAX_RECONNECT):
    """Connect to a tab via the configured transport with automatic retry."""
    if connection_mode() == "live":
        for attempt in range(retries):
            try:
                return LiveTabConnection(tab_id)
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
                timeout=10,
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


def cdp_send(ws, method, params=None, timeout=10):
    """Send CDP command and wait for response with timeout."""
    msg_id = _next_message_id()
    msg = {"id": msg_id, "method": method}
    if params:
        msg["params"] = params
    ws.send(json.dumps(msg))
    ws.settimeout(timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = json.loads(ws.recv())
            if resp.get("id") == msg_id:
                return resp.get("result", resp.get("error", {}))
        except websocket.WebSocketTimeoutException:
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
):
    """Send CDP command with automatic reconnection on failure."""
    for attempt in range(retries):
        try:
            result = cdp_send(ws, method, params, timeout)
            if "error" not in result:
                return result
            # If error and we have retries left, reconnect
            if attempt < retries - 1 and tab_id:
                ws.close()
                ws = cdp_connect(tab_id)
                if not ws:
                    return {"error": "reconnect failed"}
        except Exception as e:
            if attempt < retries - 1 and tab_id:
                try:
                    ws.close()
                except Exception:
                    pass
                ws = cdp_connect(tab_id)
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
            targets = _live_target_infos()
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
        ws = cdp_connect(tab_id)
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

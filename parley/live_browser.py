"""Production live-Chrome CDP transport.

Attaches to an already-running normal Chrome session through
DevToolsActivePort. One browser-level WebSocket is kept for the lifetime of
the Parley process; flattened Target sessions are attached/detached over that
single connection.

All browser-socket command/response transactions are serialized. This avoids
multiple target sessions competing to consume responses from one shared
WebSocket while still avoiding repeated Chrome remote-debugging approval
prompts.
"""

import atexit
import json
import os
import sys
import threading
import time
from pathlib import Path

import websocket


def chrome_user_data_dir():
    """Return Chrome's user-data directory for the current OS."""
    override = os.environ.get("PARLEY_CHROME_USER_DATA_DIR")
    if override:
        return Path(
            os.path.expandvars(os.path.expanduser(override))
        ).resolve()

    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise RuntimeError(
                "LOCALAPPDATA is unavailable; set "
                "PARLEY_CHROME_USER_DATA_DIR"
            )
        return (
            Path(local_app_data)
            / "Google"
            / "Chrome"
            / "User Data"
        )

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
    """Read Chrome's browser WebSocket endpoint from DevToolsActivePort."""
    root = (
        Path(user_data_dir)
        if user_data_dir is not None
        else chrome_user_data_dir()
    )
    port_file = root / "DevToolsActivePort"

    try:
        content = port_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            "Could not find Chrome's DevToolsActivePort at %s. "
            "In Chrome, open chrome://inspect/#remote-debugging and "
            "enable remote debugging. If Chrome uses a different "
            "user-data directory, set PARLEY_CHROME_USER_DATA_DIR."
            % port_file
        ) from exc

    lines = [
        line.strip()
        for line in content.splitlines()
        if line.strip()
    ]
    if len(lines) < 2:
        raise RuntimeError(
            "Invalid DevToolsActivePort contents in %s" % port_file
        )

    try:
        port = int(lines[0])
    except ValueError as exc:
        raise RuntimeError(
            "Invalid DevToolsActivePort port %r in %s"
            % (lines[0], port_file)
        ) from exc

    if port <= 0 or port > 65535:
        raise RuntimeError(
            "Invalid DevToolsActivePort port %r in %s"
            % (port, port_file)
        )

    path = lines[1]
    if not path.startswith("/"):
        path = "/" + path

    return "ws://127.0.0.1:%d%s" % (port, path)


class LiveBrowserManager:
    """Own one browser-level CDP WebSocket and serialize all commands."""

    def __init__(self, endpoint_fn=live_devtools_endpoint):
        self._endpoint_fn = endpoint_fn
        self._lock = threading.RLock()
        self._ws = None
        self._next_id = 1

    def _connected_locked(self):
        if self._ws is None:
            return False
        try:
            return bool(getattr(self._ws, "connected", True))
        except Exception:
            return False

    def _close_locked(self):
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def close(self):
        with self._lock:
            self._close_locked()

    def _ensure_connected_locked(self, timeout=30):
        if self._connected_locked():
            try:
                self._ws.settimeout(timeout)
            except Exception:
                self._close_locked()

        if self._ws is None:
            endpoint = self._endpoint_fn()
            self._ws = websocket.create_connection(
                endpoint,
                timeout=timeout,
                suppress_origin=True,
            )

        return self._ws

    def _new_id_locked(self):
        msg_id = self._next_id
        self._next_id += 1
        return msg_id

    def command(
        self,
        method,
        params=None,
        *,
        timeout=10,
        session_id=None,
    ):
        """Send one CDP command and return its result/error payload."""
        with self._lock:
            try:
                ws = self._ensure_connected_locked(timeout=timeout)
                msg_id = self._new_id_locked()
                message = {
                    "id": msg_id,
                    "method": method,
                }
                if params:
                    message["params"] = params
                if session_id:
                    message["sessionId"] = session_id

                ws.send(json.dumps(message))
                ws.settimeout(timeout)

                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    try:
                        response = json.loads(ws.recv())
                    except websocket.WebSocketTimeoutException:
                        break

                    if response.get("id") != msg_id:
                        # Browser/target events are intentionally ignored here.
                        # Transactions are serialized, so there is no other
                        # Parley command response that needs routing.
                        continue

                    if (
                        session_id
                        and response.get("sessionId")
                        not in (None, session_id)
                    ):
                        continue

                    if "result" in response:
                        return response["result"]
                    if "error" in response:
                        return {"error": response["error"]}
                    return {"error": "malformed CDP response"}

                return {"error": "timeout"}

            except Exception as exc:
                self._close_locked()
                return {"error": str(exc)}

    def target_infos(self):
        result = self.command(
            "Target.getTargets",
            timeout=15,
        )
        if isinstance(result, dict) and result.get("error"):
            raise RuntimeError(
                "Target.getTargets failed: %s"
                % result.get("error")
            )
        return result.get("targetInfos", [])

    def attach(self, target_id, timeout=30):
        result = self.command(
            "Target.attachToTarget",
            {
                "targetId": target_id,
                "flatten": True,
            },
            timeout=timeout,
        )
        session_id = (
            result.get("sessionId")
            if isinstance(result, dict)
            else None
        )
        if not session_id:
            raise RuntimeError(
                "Could not attach to Chrome target %s: %r"
                % (target_id, result)
            )
        return session_id

    def detach(self, session_id):
        if not session_id:
            return
        self.command(
            "Target.detachFromTarget",
            {"sessionId": session_id},
            timeout=2,
        )


_MANAGER = LiveBrowserManager()


def close_live_browser_manager():
    """Close the current process-wide live browser connection."""
    _MANAGER.close()


atexit.register(close_live_browser_manager)


def live_browser_manager():
    return _MANAGER


def reset_live_browser_manager(manager=None):
    """Testing/recovery helper: replace the process-wide manager."""
    global _MANAGER
    try:
        _MANAGER.close()
    except Exception:
        pass
    _MANAGER = manager or LiveBrowserManager()
    return _MANAGER


def live_target_infos():
    return _MANAGER.target_infos()


class LiveTabConnection:
    """Flattened target-session handle over the shared browser manager."""

    def __init__(self, tab_id, timeout=30, manager=None):
        self.tab_id = tab_id
        self._manager = manager or _MANAGER
        self._closed = False
        self._timeout = timeout
        self.session_id = self._manager.attach(
            tab_id,
            timeout=timeout,
        )

    def command(self, method, params=None, timeout=10):
        if self._closed:
            return {"error": "live tab connection is closed"}
        return self._manager.command(
            method,
            params,
            timeout=timeout,
            session_id=self.session_id,
        )

    def settimeout(self, timeout):
        self._timeout = timeout

    def reconnect(self):
        """Reconnect the browser if needed and reattach this target in place."""
        if self._closed:
            return False
        old_session = self.session_id
        try:
            self._manager.detach(old_session)
        except Exception:
            pass
        try:
            self.session_id = self._manager.attach(
                self.tab_id,
                timeout=self._timeout,
            )
            return True
        except Exception:
            return False

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._manager.detach(self.session_id)
        except Exception:
            pass

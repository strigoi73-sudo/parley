"""Production live-Chrome CDP transport.

Attaches to an already-running normal Chrome session through
DevToolsActivePort. One browser-level WebSocket is kept for the lifetime of
the Parley process; flattened Target sessions are attached/detached over that
single connection.

Writes are serialized; one receive loop routes replies to waiting commands.
A slow target therefore cannot block commands for another target, and callers
never compete to consume replies from the shared WebSocket.
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
    """Own one browser WebSocket with one reader and independent waiters."""

    def __init__(self, endpoint_fn=live_devtools_endpoint):
        self._endpoint_fn = endpoint_fn
        self._lock = threading.RLock()
        self._work = threading.Condition(self._lock)
        self._ws = None
        self._next_id = 1
        self._pending = {}

    def _connected_locked(self):
        if self._ws is None:
            return False
        try:
            return bool(getattr(self._ws, "connected", True))
        except Exception:
            return False

    def _close_locked(self, error="live browser connection closed"):
        ws = self._ws
        self._ws = None
        for pending in self._pending.values():
            pending["response"] = {"error": error}
            pending["done"].set()
        self._pending.clear()
        self._work.notify_all()
        if ws is not None:
            try:
                # close() normally reads the close handshake itself. Never
                # let it compete with our sole reader, even during shutdown.
                ws.close(timeout=0)
            except Exception:
                pass

    def close(self):
        with self._lock:
            self._close_locked()

    def _ensure_connected_locked(self, timeout=None):
        if self._ws is not None and not self._connected_locked():
            self._close_locked()

        if self._ws is None:
            endpoint = self._endpoint_fn()
            self._ws = websocket.create_connection(
                endpoint,
                timeout=timeout,
                suppress_origin=True,
            )
            # A socket poll is only an opportunity to observe close/cancel;
            # it is never a command's failure deadline.
            self._ws.settimeout(0.25)
            threading.Thread(
                target=self._receive,
                args=(self._ws,),
                name="parley-cdp-receive",
                daemon=True,
            ).start()

        return self._ws

    def _receive(self, ws):
        """The only recv consumer for this socket generation."""
        while True:
            with self._work:
                while self._ws is ws and not self._pending:
                    self._work.wait()
                if self._ws is not ws:
                    return
            try:
                response = json.loads(ws.recv())
                if not isinstance(response, dict):
                    raise ValueError("malformed CDP response")
            except websocket.WebSocketTimeoutException:
                continue
            except Exception as exc:
                with self._lock:
                    if self._ws is ws:
                        self._close_locked(str(exc))
                return

            with self._lock:
                if self._ws is not ws:
                    return
                pending = self._pending.get(response.get("id"))
                if pending is None:
                    # Events and late replies to stopped/expired commands.
                    continue
                if response.get("sessionId") not in (None, pending["session_id"]):
                    continue
                if "result" in response:
                    result = response["result"]
                else:
                    result = {"error": response.get("error", "malformed CDP response")}
                self._pending.pop(response["id"])
                pending["response"] = result
                pending["done"].set()

    def _new_id_locked(self):
        msg_id = self._next_id
        self._next_id += 1
        return msg_id

    def command(
        self,
        method,
        params=None,
        *,
        timeout=None,
        session_id=None,
        should_stop=None,
    ):
        """Send one CDP command and return its result/error payload."""
        if callable(should_stop) and should_stop():
            return {"error": "stopped"}
        pending = {"done": threading.Event(), "session_id": session_id}
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

                self._pending[msg_id] = pending
                ws.send(json.dumps(message))
                self._work.notify()
            except Exception as exc:
                self._close_locked(str(exc))
                return {"error": str(exc)}

        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            while True:
                if callable(should_stop) and should_stop():
                    return {"error": "stopped"}
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return {"error": "timeout"}
                wait = remaining
                if callable(should_stop):
                    wait = 0.25 if remaining is None else min(0.25, remaining)
                if pending["done"].wait(wait):
                    return pending["response"]
        finally:
            with self._lock:
                self._pending.pop(msg_id, None)

    def target_infos(self):
        result = self.command(
            "Target.getTargets",
            timeout=None,
        )
        if isinstance(result, dict) and result.get("error"):
            raise RuntimeError(
                "Target.getTargets failed: %s"
                % result.get("error")
            )
        return result.get("targetInfos", [])

    def attach(self, target_id, timeout=None):
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
            timeout=None,
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

    def __init__(self, tab_id, timeout=None, manager=None):
        self.tab_id = tab_id
        self._manager = manager or _MANAGER
        self._closed = False
        self._timeout = timeout
        self.session_id = self._manager.attach(
            tab_id,
            timeout=timeout,
        )

    def command(
        self,
        method,
        params=None,
        timeout=None,
        should_stop=None,
    ):
        if self._closed:
            return {"error": "live tab connection is closed"}
        return self._manager.command(
            method,
            params,
            timeout=timeout,
            session_id=self.session_id,
            should_stop=should_stop,
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

"""Thread-safe relay pause/resume/stop control."""

import threading
import time


RUNNING = "running"
PAUSED = "paused"
STOPPED = "stopped"


class RelayControl:
    """Mutable relay control suitable for a future CLI/UI thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._state = RUNNING

    @property
    def state(self):
        with self._lock:
            return self._state

    def pause(self):
        with self._lock:
            if self._state != STOPPED:
                self._state = PAUSED

    def resume(self):
        with self._lock:
            if self._state != STOPPED:
                self._state = RUNNING

    def stop(self):
        with self._lock:
            self._state = STOPPED

    def wait_until_runnable(self, poll_seconds=0.1, sleep=time.sleep):
        """Block while paused; return False once stopped."""
        while True:
            state = self.state
            if state == STOPPED:
                return False
            if state == RUNNING:
                return True
            sleep(poll_seconds)

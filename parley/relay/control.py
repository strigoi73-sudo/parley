"""Thread-safe relay pause/resume/stop control."""

import threading
import time


RUNNING = "running"
PAUSED = "paused"
STOPPED = "stopped"


class RelayControl:
    """Mutable relay control suitable for a future CLI/UI thread."""

    def __init__(self, round_limit=None):
        self._lock = threading.Lock()
        self._state = RUNNING
        self._round_limit = round_limit

    @property
    def state(self):
        with self._lock:
            return self._state

    @property
    def round_limit(self):
        with self._lock:
            return self._round_limit

    def set_round_limit(self, round_limit):
        if (
            not isinstance(round_limit, int)
            or isinstance(round_limit, bool)
            or round_limit < 1
        ):
            raise ValueError("round limit must be a positive integer")
        with self._lock:
            self._round_limit = round_limit
            return self._round_limit

    def extend_round_limit(self, new_total):
        if (
            not isinstance(new_total, int)
            or isinstance(new_total, bool)
            or new_total < 1
        ):
            raise ValueError("round limit must be a positive integer")
        with self._lock:
            current = self._round_limit
            if current is None:
                self._round_limit = new_total
                return self._round_limit
            if new_total <= current:
                raise ValueError(
                    "new round limit must be greater than current limit"
                )
            self._round_limit = new_total
            return self._round_limit

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

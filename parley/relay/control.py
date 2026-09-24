"""Thread-safe relay pause/resume/stop and round-limit control."""

import threading
import time


RUNNING = "running"
PAUSED = "paused"
STOPPED = "stopped"


class RelayControl:
    """Mutable relay control suitable for a future CLI/UI thread."""

    def __init__(self, round_limit=None, confirm_round_limit=False):
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._state = RUNNING
        self._round_limit = round_limit
        self._confirm_round_limit = bool(confirm_round_limit)
        self._awaiting_round_extension = False
        self._finish_at_round_limit = False
        self._reset_request = None

    @property
    def state(self):
        with self._lock:
            return self._state

    @property
    def round_limit(self):
        with self._lock:
            return self._round_limit

    @property
    def awaiting_round_extension(self):
        with self._lock:
            return self._awaiting_round_extension

    @property
    def confirm_round_limit(self):
        with self._lock:
            return self._confirm_round_limit

    def set_round_limit(self, round_limit):
        if (
            not isinstance(round_limit, int)
            or isinstance(round_limit, bool)
            or round_limit < 1
        ):
            raise ValueError("round limit must be a positive integer")
        with self._condition:
            self._round_limit = round_limit
            self._condition.notify_all()
            return self._round_limit

    def extend_round_limit(self, new_total):
        if (
            not isinstance(new_total, int)
            or isinstance(new_total, bool)
            or new_total < 1
        ):
            raise ValueError("round limit must be a positive integer")
        with self._condition:
            current = self._round_limit
            if current is None:
                self._round_limit = new_total
                self._condition.notify_all()
                return self._round_limit
            if new_total <= current:
                raise ValueError(
                    "new round limit must be greater than current limit"
                )
            self._round_limit = new_total
            self._finish_at_round_limit = False
            self._awaiting_round_extension = False
            self._condition.notify_all()
            return self._round_limit

    def request_reset(self, label):
        """Wake a parked relay so the engine can coordinate RESET CHAT."""
        label = str(label or "").strip().upper()
        if label not in ("A", "B"):
            raise ValueError("reset label must be A or B")
        with self._condition:
            self._reset_request = label
            self._condition.notify_all()
            return label

    def finish_at_round_limit(self):
        """Confirm that the relay should end at its current round ceiling."""
        with self._condition:
            self._finish_at_round_limit = True
            self._condition.notify_all()

    def wait_for_round_limit_decision(self, completed_rounds):
        """Wait for extension or explicit completion at a reached limit.

        Interactive RelaySession controls opt into this behavior. Direct engine
        users retain the historical behavior of completing immediately at the
        configured limit.
        """
        with self._condition:
            if not self._confirm_round_limit:
                return "finish"

            self._awaiting_round_extension = True
            self._finish_at_round_limit = False
            self._condition.notify_all()

            while True:
                if self._state == STOPPED:
                    self._awaiting_round_extension = False
                    return "stopped"

                if self._reset_request:
                    label = self._reset_request
                    self._reset_request = None
                    self._awaiting_round_extension = False
                    return "reset:%s" % label

                if (
                    self._round_limit is not None
                    and self._round_limit > completed_rounds
                ):
                    self._awaiting_round_extension = False
                    return "extended"

                if self._finish_at_round_limit:
                    self._finish_at_round_limit = False
                    self._awaiting_round_extension = False
                    return "finish"

                # Human round-limit decisions have no deadline.
                # Wake only when extend/finish/stop changes the condition.
                self._condition.wait()

    def pause(self):
        with self._condition:
            if self._state != STOPPED:
                self._state = PAUSED
            self._condition.notify_all()

    def resume(self):
        with self._condition:
            if self._state != STOPPED:
                self._state = RUNNING
            self._condition.notify_all()

    def stop(self):
        with self._condition:
            self._state = STOPPED
            self._condition.notify_all()

    def wait_until_runnable(self, poll_seconds=0.1, sleep=time.sleep):
        """Block while paused; return False once stopped."""
        while True:
            state = self.state
            if state == STOPPED:
                return False
            if state == RUNNING:
                return True
            sleep(poll_seconds)

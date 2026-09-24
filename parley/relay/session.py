"""In-process relay session used by the CLI and future UI."""

import threading

from .control import RelayControl


class RelaySession:
    """Run one relay in a worker thread with observable state and controls."""

    def __init__(
        self,
        bridge,
        tab_a,
        tab_b,
        rounds,
        *,
        include_text=False,
        initial_context=None,
    ):
        self.bridge = bridge
        self.tab_a = tab_a
        self.tab_b = tab_b
        self.rounds = rounds
        self.include_text = include_text
        self.initial_context = initial_context
        self.control = RelayControl(
            round_limit=rounds,
            confirm_round_limit=True,
        )

        self._lock = threading.Lock()
        self._thread = None
        self._state = "IDLE"
        self._events = []
        self._transfers = []
        self._rounds_completed = 0
        self._result = None
        self._exception = None

    def _on_event(self, event):
        with self._lock:
            self._events.append(dict(event))
            if event.get("event") == "state_changed":
                self._state = event.get("state", self._state)
            elif event.get("event") == "transfer_completed":
                item = dict(event)
                self._transfers.append(item)
                if item.get("direction") == "B->A":
                    self._rounds_completed = max(
                        self._rounds_completed,
                        int(item.get("round", 0) or 0),
                    )

    def _run(self):
        try:
            result = self.bridge(
                self.tab_a,
                self.tab_b,
                rounds=self.rounds,
                control=self.control,
                include_text=self.include_text,
                event_sink=self._on_event,
                initial_context=self.initial_context,
            )
            with self._lock:
                self._result = result
                if isinstance(result, dict) and result.get("state"):
                    self._state = result["state"]
        except Exception as exc:
            with self._lock:
                self._exception = exc
                self._state = "ERROR"

    def start(self):
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("relay session already started")
            self._thread = threading.Thread(
                target=self._run,
                name="parley-relay",
                daemon=False,
            )
            thread = self._thread
        thread.start()
        return self

    def pause(self):
        self.control.pause()

    def resume(self):
        self.control.resume()

    def stop(self):
        self.control.stop()

    def extend_rounds(self, new_total):
        return self.control.extend_round_limit(new_total)

    def request_reset(self, label):
        return self.control.request_reset(label)

    def finish_at_round_limit(self):
        self.control.finish_at_round_limit()

    def is_alive(self):
        with self._lock:
            thread = self._thread
        return bool(thread and thread.is_alive())

    def join(self, timeout=None):
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self.result

    @property
    def result(self):
        with self._lock:
            return self._result

    @property
    def exception(self):
        with self._lock:
            return self._exception

    def events(self):
        """Return a snapshot of relay events for UI/observer catch-up."""
        with self._lock:
            return [dict(item) for item in self._events]

    def transfers(self):
        """Return a snapshot of completed transfers for UI transcripts."""
        with self._lock:
            return [dict(item) for item in self._transfers]

    def status(self):
        with self._lock:
            result = self._result
            transfers = list(self._transfers)
            state = self._state
            exception = self._exception
            rounds_completed = self._rounds_completed

        last_transfer = transfers[-1] if transfers else None
        status = "running" if self.is_alive() else "idle"

        if isinstance(result, dict):
            status = result.get("status", status)
            rounds_completed = result.get("rounds_completed", 0)

        if exception is not None:
            status = "error"

        return {
            "status": status,
            "state": state,
            "control": self.control.state,
            "tab_a": self.tab_a,
            "tab_b": self.tab_b,
            "rounds_requested": (
                self.control.round_limit
                if self.control.round_limit is not None
                else self.rounds
            ),
            "rounds_completed": rounds_completed,
            "awaiting_extension": self.control.awaiting_round_extension,
            "transfers_completed": len(transfers),
            "last_transfer": last_transfer,
            "error": str(exception) if exception else (
                result.get("error")
                if isinstance(result, dict)
                else None
            ),
        }

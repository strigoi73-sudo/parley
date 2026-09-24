"""Non-Tkinter controller for the supported Parley desktop workflow."""

from dataclasses import dataclass
import threading
import time

from . import workflows
from .relay import RelaySession


@dataclass(frozen=True)
class DesktopControllerOperations:
    prepare_session: object
    bridge: object
    session_factory: object
    monotonic: object


def default_operations():
    return DesktopControllerOperations(
        prepare_session=workflows.prepare_parley_session,
        bridge=workflows.bridge,
        session_factory=RelaySession,
        monotonic=time.monotonic,
    )


class DesktopController:
    """Own desktop startup/session lifecycle without owning presentation."""

    def __init__(self, operations=None):
        self.operations = operations or default_operations()
        self.operation_stop = threading.Event()
        self.startup_thread = None
        self.session = None
        self.user_stop_requested = False
        self.started_at = None
        self.tabs = None
        self._seen_transfers = 0
        self._seen_events = 0
        self._session_done_seen = False

    @property
    def startup_alive(self):
        thread = self.startup_thread
        return bool(thread and thread.is_alive())

    @property
    def session_alive(self):
        session = self.session
        return bool(session and session.is_alive())

    @property
    def busy(self):
        return self.startup_alive or self.session_alive

    @property
    def session_done_seen(self):
        return self._session_done_seen

    @property
    def has_session(self):
        return self.session is not None

    def status(self):
        if self.session is None:
            return {}
        return self.session.status()

    def start(self, prompt, participant_specs, rounds, *, progress, finished):
        if self.busy:
            raise RuntimeError("desktop controller is already busy")

        self.operation_stop.clear()
        self.user_stop_requested = False
        self.started_at = self.operations.monotonic()
        self.tabs = None
        self.session = None
        self._seen_transfers = 0
        self._seen_events = 0
        self._session_done_seen = False

        def worker():
            try:
                result = self.operations.prepare_session(
                    prompt,
                    participant_specs,
                    should_stop=self.operation_stop.is_set,
                    progress=progress,
                )
                if (
                    isinstance(result, dict)
                    and result.get("ok")
                    and not result.get("reset_requested")
                ):
                    tab_a = result["A"]
                    tab_b = result["B"]
                    self.tabs = {"A": tab_a, "B": tab_b}
                    self.session = self.operations.session_factory(
                        self.operations.bridge,
                        tab_a["id"],
                        tab_b["id"],
                        rounds,
                        include_text=True,
                        initial_context=prompt,
                    )
                    self.session.start()
                elif (
                    isinstance(result, dict)
                    and result.get("ok")
                    and result.get("reset_requested")
                ):
                    self.tabs = {
                        "A": result["A"],
                        "B": result["B"],
                    }
            except Exception as exc:
                result = {
                    "ok": False,
                    "error": "desktop_startup_exception",
                    "stage": "desktop_startup",
                    "detail": str(exc),
                }
            finished(result, prompt, rounds)

        self.startup_thread = threading.Thread(
            target=worker,
            name="parley-desktop-startup",
            daemon=False,
        )
        self.startup_thread.start()
        return self.startup_thread

    def pause(self):
        if not self.session_alive:
            return False
        self.session.pause()
        return True

    def resume(self):
        if not self.session_alive:
            return False
        self.session.resume()
        return True

    def extend_rounds(self, new_total):
        if not self.session_alive:
            raise RuntimeError("relay session is not active")
        return self.session.extend_rounds(new_total)

    def finish_at_round_limit(self):
        if not self.session_alive:
            return False
        self.session.finish_at_round_limit()
        return True

    def stop(self):
        if self.startup_alive:
            self.user_stop_requested = True
            self.operation_stop.set()
            return "startup"

        if self.session_alive:
            self.user_stop_requested = True
            self.session.stop()
            return "session"

        return None

    def reset(self):
        if self.busy:
            return False

        self.session = None
        self.tabs = None
        self._seen_transfers = 0
        self._seen_events = 0
        self._session_done_seen = False
        self.user_stop_requested = False
        self.operation_stop.clear()
        self.started_at = None
        self.startup_thread = None
        return True

    def elapsed_seconds(self):
        if self.started_at is None:
            return 0
        return max(
            0,
            int(self.operations.monotonic() - self.started_at),
        )

    def poll(self):
        session = self.session
        if session is None:
            return None

        status = session.status()
        transfers = session.transfers()
        new_transfers = transfers[self._seen_transfers:]
        self._seen_transfers = len(transfers)

        events = session.events()
        new_events = events[self._seen_events:]
        self._seen_events = len(events)

        finished = (
            not session.is_alive()
            and not self._session_done_seen
        )
        if finished:
            self._session_done_seen = True

        return {
            "status": status,
            "transfers": [dict(item) for item in new_transfers],
            "events": [dict(item) for item in new_events],
            "finished": finished,
            "result": session.result if finished else None,
            "exception": session.exception if finished else None,
        }

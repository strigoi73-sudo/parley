"""Structured relay audit events."""

import time


def audit_event(event, **fields):
    item = {
        "timestamp": time.time(),
        "event": event,
    }
    item.update(fields)
    return item

"""Duplicate-turn protection for the relay engine."""

import hashlib


def normalize_text(text):
    """Normalize only transport-irrelevant whitespace for fingerprinting."""
    return " ".join((text or "").split())


def turn_text_hash(turn):
    """Return a SHA-256 hash of normalized turn text."""
    normalized = normalize_text(turn.get("text", ""))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def turn_fingerprint(source_tab, destination_tab, turn):
    """Return a stable fingerprint for one source-turn delivery."""
    text_hash = turn_text_hash(turn)

    identity = turn.get("turn_id")
    if identity:
        turn_key = "id:" + str(identity)
    else:
        turn_key = "index:" + str(turn.get("turn_index"))

    raw = "|".join([
        str(source_tab),
        str(destination_tab),
        turn_key,
        text_hash,
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class DuplicateGuard:
    """Track source-turn deliveries for one relay run."""

    def __init__(self):
        self._seen = set()

    def claim(self, source_tab, destination_tab, turn):
        fingerprint = turn_fingerprint(
            source_tab,
            destination_tab,
            turn,
        )
        if fingerprint in self._seen:
            return False, fingerprint
        self._seen.add(fingerprint)
        return True, fingerprint

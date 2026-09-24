"""Shared participant discovery rules for Parley presentation layers."""

from urllib.parse import urlparse


CHATGPT_HOSTS = frozenset({
    "chatgpt.com",
    "www.chatgpt.com",
    "chat.openai.com",
})


def is_eligible_chatgpt_tab(tab):
    """Return True when a browser target is an addressable ChatGPT tab."""
    if not isinstance(tab, dict) or not tab.get("id"):
        return False

    try:
        host = (urlparse(tab.get("url") or "").hostname or "").lower()
    except (TypeError, ValueError):
        return False

    return host in CHATGPT_HOSTS


def eligible_chatgpt_tabs(tabs):
    """Return copies of eligible ChatGPT targets from an iterable of tabs."""
    return [
        dict(tab)
        for tab in (tabs or [])
        if is_eligible_chatgpt_tab(tab)
    ]

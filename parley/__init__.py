"""Parley - a lightweight CDP browser automation framework with first-class AI workflow support.

Layers:
    core       - site-agnostic Chrome DevTools Protocol engine (DOM, JS, input, nav, cookies)
    adapters   - per-site knowledge (ChatGPT, Gemini, Claude, Grok, Generic)
    workflows  - AI workflows built on top (send, wait, bridge, poll)
"""

from parley_version import __version__

from . import core
from . import workflows
from . import adapters
from . import relay

__all__ = ["core", "workflows", "adapters", "relay", "__version__"]

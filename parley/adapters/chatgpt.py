"""ChatGPT (chatgpt.com / chat.openai.com) site adapter."""

from .base import SiteAdapter
from .chatgpt_strict import CHATGPT_GET_MSG_COUNT_JS, CHATGPT_GET_RESPONSE_JS


class ChatGPTAdapter(SiteAdapter):
    name = "chatgpt"
    url_patterns = ("chatgpt.com", "chat.openai.com")
    response_selectors = (
        'article[data-turn="assistant"]',
        '[data-message-author-role="assistant"]',
    )
    input_selectors = ("#prompt-textarea", '[contenteditable="true"]')
    send_button_selectors = ('button[data-testid="send-button"]',)
    needs_reload_recovery = False

    # ChatGPT is fail-closed: workflows must use these site-specific scripts
    # rather than the universal response detector's generic page-text fallback.
    response_js = CHATGPT_GET_RESPONSE_JS
    message_count_js = CHATGPT_GET_MSG_COUNT_JS

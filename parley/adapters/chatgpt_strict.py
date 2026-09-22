"""Strict ChatGPT DOM extraction.

This module intentionally has no generic page-text fallback.
"""

CHATGPT_GET_RESPONSE_JS = """
(() => {
  const host = (location.hostname || '').toLowerCase();
  const isChatGPT = host === 'chatgpt.com' || host === 'www.chatgpt.com' || host === 'chat.openai.com';
  if (!isChatGPT) return {ok:false,error:'not_chatgpt_page',text:'',count:0,source:'chatgpt-strict'};

  const articles = Array.from(document.querySelectorAll('article[data-turn="assistant"]'));
  const roles = Array.from(document.querySelectorAll('[data-message-author-role="assistant"]'));

  let turn = null;
  let role = null;
  let count = 0;
  let selector = '';

  if (articles.length) {
    turn = articles[articles.length - 1];
    role = turn.querySelector('[data-message-author-role="assistant"]');
    count = articles.length;
    selector = 'article[data-turn="assistant"]';
  } else if (roles.length) {
    role = roles[roles.length - 1];
    turn = role.closest('article') || role;
    count = roles.length;
    selector = '[data-message-author-role="assistant"]';
  } else {
    return {ok:false,error:'chatgpt_assistant_turn_not_found',text:'',count:0,source:'chatgpt-strict'};
  }

  let content = null;
  if (role) {
    content = role.querySelector('.markdown') || role.querySelector('.markdown-new-styling') || role;
  }
  if (!content && turn) {
    content = turn.querySelector('[data-message-author-role="assistant"] .markdown') ||
              turn.querySelector('.markdown') ||
              turn.querySelector('.markdown-new-styling') ||
              turn.querySelector('[data-message-author-role="assistant"]');
  }

  const text = content ? (content.innerText || content.textContent || '').trim() : '';
  if (!text) {
    return {ok:false,error:'chatgpt_message_body_not_found',text:'',count:count,source:'chatgpt-strict',selector:selector};
  }

  const turnId =
    (turn && (turn.getAttribute('data-message-id') || turn.getAttribute('data-turn-id') || turn.id)) ||
    (role && (role.getAttribute('data-message-id') || role.id)) ||
    null;

  const stop = document.querySelector('button[data-testid="stop-button"], button[aria-label*="Stop"], button[aria-label*="stop"]');
  const streaming = !!(
    (turn && turn.querySelector('[data-is-streaming="true"]')) ||
    (role && role.querySelector('[data-is-streaming="true"]'))
  );

  return {
    ok:true,
    text:text,
    count:count,
    turn_index:count - 1,
    turn_id:turnId,
    selector:selector,
    source:'chatgpt-strict',
    isLatest:true,
    hasStreaming:streaming,
    hasStopButton:!!stop
  };
})()
"""

CHATGPT_GET_MSG_COUNT_JS = """
(() => {
  const host = (location.hostname || '').toLowerCase();
  const isChatGPT = host === 'chatgpt.com' || host === 'www.chatgpt.com' || host === 'chat.openai.com';
  if (!isChatGPT) return {ok:false,error:'not_chatgpt_page',count:0,source:'chatgpt-strict'};

  const articles = document.querySelectorAll('article[data-turn="assistant"]');
  if (articles.length) {
    return {ok:true,count:articles.length,source:'chatgpt-strict',selector:'article[data-turn="assistant"]'};
  }

  const roles = document.querySelectorAll('[data-message-author-role="assistant"]');
  return {ok:true,count:roles.length,source:'chatgpt-strict',selector:'[data-message-author-role="assistant"]'};
})()
"""


# Transaction state for reliable ChatGPT send/wait. Unlike
# CHATGPT_GET_RESPONSE_JS, this is valid on a blank conversation: absence of an
# assistant turn is represented as assistant: null, not as an extraction error.
CHATGPT_GET_TURN_STATE_JS = """
(() => {
  const host = (location.hostname || '').toLowerCase();
  const isChatGPT = host === 'chatgpt.com' || host === 'www.chatgpt.com' || host === 'chat.openai.com';
  if (!isChatGPT) return {ok:false,error:'not_chatgpt_page',source:'chatgpt-strict'};

  const assistantArticles = Array.from(document.querySelectorAll('article[data-turn="assistant"]'));
  const assistantRoles = Array.from(document.querySelectorAll('[data-message-author-role="assistant"]'));
  const userArticles = Array.from(document.querySelectorAll('article[data-turn="user"]'));
  const userRoles = Array.from(document.querySelectorAll('[data-message-author-role="user"]'));

  const assistantCount = assistantArticles.length || assistantRoles.length;
  const userCount = userArticles.length || userRoles.length;

  let turn = null;
  let role = null;
  let selector = '';

  if (assistantArticles.length) {
    turn = assistantArticles[assistantArticles.length - 1];
    role = turn.querySelector('[data-message-author-role="assistant"]');
    selector = 'article[data-turn="assistant"]';
  } else if (assistantRoles.length) {
    role = assistantRoles[assistantRoles.length - 1];
    turn = role.closest('article') || role;
    selector = '[data-message-author-role="assistant"]';
  }

  let assistant = null;
  if (turn || role) {
    let content = null;
    if (role) {
      content = role.querySelector('.markdown') || role.querySelector('.markdown-new-styling') || role;
    }
    if (!content && turn) {
      content = turn.querySelector('[data-message-author-role="assistant"] .markdown') ||
                turn.querySelector('.markdown') ||
                turn.querySelector('.markdown-new-styling') ||
                turn.querySelector('[data-message-author-role="assistant"]');
    }

    const text = content ? (content.innerText || content.textContent || '').trim() : '';
    const turnId =
      (turn && (turn.getAttribute('data-message-id') || turn.getAttribute('data-turn-id') || turn.id)) ||
      (role && (role.getAttribute('data-message-id') || role.id)) ||
      null;

    assistant = {
      text:text,
      turn_id:turnId,
      turn_index:assistantCount - 1,
      selector:selector,
      hasStreaming:!!(
        (turn && turn.querySelector('[data-is-streaming="true"]')) ||
        (role && role.querySelector('[data-is-streaming="true"]'))
      )
    };
  }

  const stop = document.querySelector('button[data-testid="stop-button"], button[aria-label*="Stop"], button[aria-label*="stop"]');

  return {
    ok:true,
    source:'chatgpt-strict',
    assistant_count:assistantCount,
    user_count:userCount,
    assistant:assistant,
    hasStopButton:!!stop
  };
})()
"""


CHATGPT_PREPARE_COMPOSER_JS = """
(() => {
  const host = (location.hostname || '').toLowerCase();
  const isChatGPT = host === 'chatgpt.com' || host === 'www.chatgpt.com' || host === 'chat.openai.com';
  if (!isChatGPT) return {ok:false,error:'not_chatgpt_page',source:'chatgpt-strict'};

  const input = document.querySelector('#prompt-textarea');
  if (!input) {
    return {ok:false,error:'chatgpt_composer_not_found',source:'chatgpt-strict'};
  }

  input.focus();

  if (input.tagName === 'TEXTAREA') {
    input.select();
  } else if (input.isContentEditable || input.getAttribute('contenteditable') === 'true') {
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(input);
    selection.removeAllRanges();
    selection.addRange(range);
  } else {
    return {ok:false,error:'chatgpt_composer_not_editable',source:'chatgpt-strict'};
  }

  return {
    ok:true,
    source:'chatgpt-strict',
    tag:input.tagName,
    contenteditable:!!input.isContentEditable
  };
})()
"""


CHATGPT_CLICK_SEND_JS = """
(() => {
  const host = (location.hostname || '').toLowerCase();
  const isChatGPT = host === 'chatgpt.com' || host === 'www.chatgpt.com' || host === 'chat.openai.com';
  if (!isChatGPT) return {ok:false,error:'not_chatgpt_page',source:'chatgpt-strict'};

  const button = document.querySelector('button[data-testid="send-button"]');
  if (!button) {
    return {ok:false,error:'chatgpt_send_button_not_found',source:'chatgpt-strict'};
  }
  if (button.disabled) {
    return {ok:false,error:'chatgpt_send_button_disabled',source:'chatgpt-strict'};
  }

  button.click();
  return {ok:true,source:'chatgpt-strict',method:'chatgpt-send-button'};
})()
"""

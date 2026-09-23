"""Strict ChatGPT DOM extraction.

This module intentionally has no generic page-text fallback.
"""

CHATGPT_GET_RESPONSE_JS = """
(() => {
  const host = (location.hostname || '').toLowerCase();
  const isChatGPT = host === 'chatgpt.com' || host === 'www.chatgpt.com' || host === 'chat.openai.com';
  if (!isChatGPT) return {ok:false,error:'not_chatgpt_page',text:'',count:0,source:'chatgpt-strict'};


function collectTurns(roleName) {
  const articleSelector = 'article[data-turn="' + roleName + '"]';
  const roleSelector = '[data-message-author-role="' + roleName + '"]';
  const nodes = [];
  const seen = new Set();

  for (const article of document.querySelectorAll(articleSelector)) {
    nodes.push(article);
    seen.add(article);
  }

  for (const role of document.querySelectorAll(roleSelector)) {
    const candidate =
      role.closest(articleSelector) ||
      role;
    if (!seen.has(candidate)) {
      nodes.push(candidate);
      seen.add(candidate);
    }
  }

  nodes.sort((a, b) => {
    if (a === b) return 0;
    const pos = a.compareDocumentPosition(b);
    if (pos & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
    if (pos & Node.DOCUMENT_POSITION_PRECEDING) return 1;
    return 0;
  });

  return {
    nodes: nodes,
    articleSelector: articleSelector,
    roleSelector: roleSelector
  };
}

  const turns = collectTurns('assistant');
  const count = turns.nodes.length;
  if (!count) {
    return {ok:false,error:'chatgpt_assistant_turn_not_found',text:'',count:0,source:'chatgpt-strict'};
  }

  const turn = turns.nodes[count - 1];
  const role = turn.matches(turns.roleSelector)
    ? turn
    : turn.querySelector(turns.roleSelector);

  let content = null;
  if (role) {
    content = role.querySelector('.markdown') ||
              role.querySelector('.markdown-new-styling') ||
              role;
  }
  if (!content && turn) {
    content = turn.querySelector(turns.roleSelector + ' .markdown') ||
              turn.querySelector('.markdown') ||
              turn.querySelector('.markdown-new-styling') ||
              turn.querySelector(turns.roleSelector);
  }

  const text = content ? (content.innerText || content.textContent || '').trim() : '';
  if (!text) {
    return {ok:false,error:'chatgpt_message_body_not_found',text:'',count:count,source:'chatgpt-strict',selector:'explicit-assistant-turns'};
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
    selector:'explicit-assistant-turns',
    source:'chatgpt-strict',
    isLatest:true,
    hasStreaming:streaming,
    hasStopButton:!!stop,
    visibilityState:document.visibilityState,
    hidden:!!document.hidden,
    hasFocus:document.hasFocus()
  };
})()
"""


CHATGPT_GET_MSG_COUNT_JS = """
(() => {
  const host = (location.hostname || '').toLowerCase();
  const isChatGPT = host === 'chatgpt.com' || host === 'www.chatgpt.com' || host === 'chat.openai.com';
  if (!isChatGPT) return {ok:false,error:'not_chatgpt_page',count:0,source:'chatgpt-strict'};


function collectTurns(roleName) {
  const articleSelector = 'article[data-turn="' + roleName + '"]';
  const roleSelector = '[data-message-author-role="' + roleName + '"]';
  const nodes = [];
  const seen = new Set();

  for (const article of document.querySelectorAll(articleSelector)) {
    nodes.push(article);
    seen.add(article);
  }

  for (const role of document.querySelectorAll(roleSelector)) {
    const candidate =
      role.closest(articleSelector) ||
      role;
    if (!seen.has(candidate)) {
      nodes.push(candidate);
      seen.add(candidate);
    }
  }

  nodes.sort((a, b) => {
    if (a === b) return 0;
    const pos = a.compareDocumentPosition(b);
    if (pos & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
    if (pos & Node.DOCUMENT_POSITION_PRECEDING) return 1;
    return 0;
  });

  return {
    nodes: nodes,
    articleSelector: articleSelector,
    roleSelector: roleSelector
  };
}

  const turns = collectTurns('assistant');
  return {
    ok:true,
    count:turns.nodes.length,
    source:'chatgpt-strict',
    selector:'explicit-assistant-turns'
  };
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


function collectTurns(roleName) {
  const articleSelector = 'article[data-turn="' + roleName + '"]';
  const roleSelector = '[data-message-author-role="' + roleName + '"]';
  const nodes = [];
  const seen = new Set();

  for (const article of document.querySelectorAll(articleSelector)) {
    nodes.push(article);
    seen.add(article);
  }

  for (const role of document.querySelectorAll(roleSelector)) {
    const candidate =
      role.closest(articleSelector) ||
      role;
    if (!seen.has(candidate)) {
      nodes.push(candidate);
      seen.add(candidate);
    }
  }

  nodes.sort((a, b) => {
    if (a === b) return 0;
    const pos = a.compareDocumentPosition(b);
    if (pos & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
    if (pos & Node.DOCUMENT_POSITION_PRECEDING) return 1;
    return 0;
  });

  return {
    nodes: nodes,
    articleSelector: articleSelector,
    roleSelector: roleSelector
  };
}

  function latestTurn(turns, roleName) {
    const count = turns.nodes.length;
    if (!count) return null;

    const turn = turns.nodes[count - 1];
    const role = turn.matches(turns.roleSelector)
      ? turn
      : turn.querySelector(turns.roleSelector);

    let content = null;
    if (roleName === 'assistant') {
      if (role) {
        content = role.querySelector('.markdown') ||
                  role.querySelector('.markdown-new-styling') ||
                  role;
      }
      if (!content && turn) {
        content = turn.querySelector(turns.roleSelector + ' .markdown') ||
                  turn.querySelector('.markdown') ||
                  turn.querySelector('.markdown-new-styling') ||
                  turn.querySelector(turns.roleSelector);
      }
    } else {
      if (role) {
        content = role.querySelector('.whitespace-pre-wrap') ||
                  role.querySelector('[class*="whitespace-pre-wrap"]') ||
                  role.querySelector('[class*="break-words"]') ||
                  role;
      }
      if (!content && turn) {
        content = turn.querySelector(
                    turns.roleSelector + ' .whitespace-pre-wrap'
                  ) ||
                  turn.querySelector('[class*="whitespace-pre-wrap"]') ||
                  turn.querySelector('[class*="break-words"]') ||
                  turn.querySelector(turns.roleSelector) ||
                  turn;
      }
    }

    const text = content
      ? (content.innerText || content.textContent || '').trim()
      : '';

    const turnId =
      (turn && (
        turn.getAttribute('data-message-id') ||
        turn.getAttribute('data-turn-id') ||
        turn.id
      )) ||
      (role && (
        role.getAttribute('data-message-id') ||
        role.id
      )) ||
      null;

    return {
      text:text,
      turn_id:turnId,
      turn_index:count - 1,
      selector:'explicit-' + roleName + '-turns',
      hasStreaming: roleName === 'assistant' ? !!(
        (turn && turn.querySelector('[data-is-streaming="true"]')) ||
        (role && role.querySelector('[data-is-streaming="true"]'))
      ) : false
    };
  }

  const assistantTurns = collectTurns('assistant');
  const userTurns = collectTurns('user');
  const assistant = latestTurn(assistantTurns, 'assistant');
  const user = latestTurn(userTurns, 'user');

  const stop = document.querySelector(
    'button[data-testid="stop-button"], button[aria-label*="Stop"], button[aria-label*="stop"]'
  );

  return {
    ok:true,
    source:'chatgpt-strict',
    assistant_count:assistantTurns.nodes.length,
    user_count:userTurns.nodes.length,
    assistant:assistant,
    user:user,
    hasStopButton:!!stop,
    visibilityState:document.visibilityState,
    hidden:!!document.hidden,
    hasFocus:document.hasFocus()
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

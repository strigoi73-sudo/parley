# Parley Architecture

This document describes the current architecture on `main`. It is the source of truth for responsibility boundaries; historical planning documents under `docs/history/` describe how the implementation evolved and may no longer match the code.

## System purpose

Parley's actively developed production path is a deterministic two-ChatGPT relay running against a user's existing Chrome session.

The runtime itself contains no language model. It performs browser control, participant preparation, strict turn extraction, and ordered message transfer.

The repository also retains inherited generic CDP commands, site adapters, MCP integration, and an opencode plugin. Those capabilities remain available but are secondary to the current two-ChatGPT relay acceptance target.

## Layer overview

```text
Desktop UI / CLI
        |
        v
    workflows.py
        |
        +-------------------+
        |                   |
        v                   v
  relay subsystem       site adapters
        |                   |
        +---------+---------+
                  |
                  v
               core.py
                  |
          +-------+--------+
          |                |
          v                v
 live_browser.py      classic CDP
          |
          v
  normal signed-in Chrome
```

The important design rule is that each layer owns one kind of decision.

## `parley/live_browser.py` — live Chrome transport

The production live transport attaches to an already-running Chrome session through Chrome's `DevToolsActivePort` file.

It owns:

- discovery of the browser-level DevTools endpoint;
- one persistent browser WebSocket per Parley process;
- serialized command writes;
- a single receive loop;
- routing command replies to independent waiters;
- flattened target-session attachment over the shared connection;
- safe cleanup and connection-loss handling.

It does **not** interpret ChatGPT turns or decide relay order.

## `parley/core.py` — generic browser operations

The core layer provides site-agnostic CDP operations and transport dispatch.

It is the bridge between higher-level workflows and either:

- the production live-Chrome transport; or
- the inherited classic localhost CDP transport.

Generic operations include tab discovery, JavaScript evaluation, DOM access, navigation, input, waits, and cookie operations.

Site-specific selectors and A/B relay semantics do not belong here.

## `parley/adapters/` — site knowledge

Adapters contain site-specific behavior.

The critical production adapter is `chatgpt_strict.py`. It intentionally recognizes explicit ChatGPT user/assistant turn structures and has **no generic page-text fallback**.

For the production relay, failure to identify the expected ChatGPT turn is an error. Sidebar text, navigation text, or an arbitrary large visible container must never be accepted as a substitute assistant reply.

Other inherited adapters remain in the package for generic/multi-site use.

## `parley/workflows.py` — orchestration

The workflow layer coordinates browser primitives and site-specific behavior into higher-level operations.

Current responsibilities include:

- reading and validating participant tabs;
- ChatGPT send-and-wait transactions;
- tracking newly submitted user turns and corresponding assistant replies;
- fresh ChatGPT participant creation;
- existing/fresh participant preparation;
- protocol-file upload and activation;
- barriered A/B initialization;
- lower-level inherited bridge and site workflows.

The workflow layer prepares reliable operations for the relay engine; it does not own the relay state machine itself.

## `parley/relay/` — deterministic relay behavior

The relay subsystem owns A/B conversation sequencing and relay safety.

### `engine.py`

Owns:

- deterministic A → B → A sequencing;
- round accounting;
- validation of completed relay turns;
- transfer records;
- protocol reply-marker checks where required.

### `session.py`

Owns the running relay session/worker lifecycle exposed to the CLI and desktop app.

### `control.py`

Owns cooperative pause, resume, and stop state.

Pause/stop occur at safe checkpoints around browser transactions; an already in-flight browser transaction is allowed to finish or fail before a stop prevents subsequent sends.

### `dedupe.py`

Prevents the same source turn from being relayed more than once to the same destination.

### `audit.py`

Produces structured events for diagnosis without requiring full message text.

### `state.py`

Defines the explicit relay states used by the engine and UI.

## `parley/app.py` — desktop presentation

The Tkinter desktop application owns presentation and user interaction.

It handles:

- participant source selection;
- existing-tab selection;
- initial prompt and round configuration;
- startup/status display;
- transcript presentation;
- pause/extend/finish/stop controls;
- diagnostics display.

It should not duplicate workflow or relay-engine logic.

## `parley/cli.py` — command-line presentation

The CLI exposes both:

- human-facing production commands such as `chats`, `app`, and `relay`; and
- inherited lower-level browser automation commands.

The production human-facing commands use live mode by default unless explicitly overridden.

## Participant preparation

A and B are selected independently.

Each participant can be:

- an eligible existing ChatGPT tab; or
- a fresh ChatGPT conversation created by Parley.

The four supported combinations are therefore:

```text
existing A / existing B
existing A / fresh B
fresh A    / existing B
fresh A    / fresh B
```

The browser target is the live execution binding. Titles and conversation URLs are useful metadata but should not silently replace the selected live target if that target disappears.

## Protocol bootstrap

The canonical runtime protocol files are:

```text
parley/protocols/PARLEY_TEST_CHAT_A_PROTOCOL.md
parley/protocols/PARLEY_TEST_CHAT_B_PROTOCOL.md
```

For structured Parley test conversations, initialization is barriered:

```text
A protocol file -> A acknowledgement
B protocol file -> B acknowledgement
A activation
B activation
initial prompt to A
relay
```

The exact protocol text is loaded from `parley/protocols/`. No second documentation copy should be maintained.

## Fail-closed invariants

The production relay should stop rather than guess when it cannot establish required state.

Examples include:

- the target is no longer an approved ChatGPT page;
- a unique/expected turn cannot be identified;
- the newly submitted user turn cannot be tracked reliably;
- assistant completion cannot be established;
- a duplicate source turn would be transferred;
- the browser/CDP connection fails in an unsafe or ambiguous way.

## Data and credential boundaries

Parley operates against authenticated browser sessions.

The repository must not persist:

- browser cookies;
- auth headers;
- session tokens;
- private conversation contents used during live testing.

Tests should use synthetic or sanitized data. Full transfer text should remain opt-in where auditing is involved.

## Current vs historical documentation

Use these documents for current behavior:

- `README.md`
- `CONTRIBUTING.md`
- `docs/architecture.md`
- `docs/development.md`

Use `docs/history/` only to understand prior design stages and decisions.

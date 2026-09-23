# ChatGPT-to-ChatGPT Relay Development Plan

Status: **Active planning baseline**  
Repository: `strigoi73-sudo/parley`  
Purpose: Turn the current Parley fork into a reliable two-ChatGPT relay using the user's existing Chrome session and ChatGPT login.

---

## 1. Goal

Build a deterministic local workflow in which two existing ChatGPT browser tabs can exchange completed assistant messages with each other:

```text
ChatGPT A  ->  ChatGPT B
ChatGPT A  <-  ChatGPT B
ChatGPT A  ->  ChatGPT B
ChatGPT A  <-  ChatGPT B
...
```

The runtime should not contain an AI model of its own. It should act only as a browser-control and message-relay layer.

The initial product target is **ChatGPT-to-ChatGPT only**. Gemini, Claude, Grok, and generic-site support are not priorities until ChatGPT-to-ChatGPT relay is reliable.

---

## 2. Verified Current State

### 2.1 Fork and local setup

- Upstream repository: `Satyajeet-04/parley`
- Fork: `strigoi73-sudo/parley`
- Local clone path used during development: `C:\Projects\parley`
- Python virtual environment: `.venv`
- Existing Parley transport is raw Chrome DevTools Protocol (CDP) over WebSocket using `websocket-client`.

### 2.2 Existing Chrome-session attachment has been proven

A live-browser transport was developed and manually tested against the user's normal Chrome profile.

Verified behavior:

- Chrome remote debugging can be enabled through `chrome://inspect/#remote-debugging`.
- Chrome writes a valid `DevToolsActivePort` file under the normal user-data directory.
- Parley can attach to the already-running Chrome session.
- Parley can enumerate existing normal-browser tabs.
- Parley can see an already-signed-in ChatGPT tab.
- Parley can insert and submit a prompt in a newly opened ChatGPT tab.
- A corrected send/wait path successfully detected the resulting ChatGPT response.

Important UX finding:

- Chrome prompts with **"Allow remote debugging?"** for incoming debugging clients.
- Repeatedly creating independent browser-level CDP connections causes repeated consent prompts.
- The live transport therefore needs to maintain **one persistent browser-level connection per Parley process/session**, with tab target sessions attached through that connection.

### 2.3 Original upstream bridge behavior has been tested and is defective

The original repository's bridge behavior was tested without changing `workflows.bridge()`.

Observed result:

1. Original ChatGPT response extraction failed against the current ChatGPT DOM.
2. Its generic fallback captured a large page-level text block including sidebar/history/interface text.
3. That page dump was sent to the second ChatGPT tab.
4. The second ChatGPT responded to the page dump.
5. On the next bridge round, the first ChatGPT's page dump was sent to the second tab again.
6. The second ChatGPT's response was never relayed back to the first tab.

Therefore the inherited repository does **not** currently provide the documented bidirectional ChatGPT-to-ChatGPT conversation behavior.

### 2.4 Two separate defects are confirmed

#### Defect A: ChatGPT response extraction

The current universal/generic extraction strategy is unsafe for modern ChatGPT.

When ChatGPT-specific selectors fail, Parley may fall back to selecting a large visible text block. This can relay unrelated UI text such as navigation, project names, chat history, and other page content.

For ChatGPT relay, this behavior must be removed. Failure to identify an assistant turn must stop the relay instead of falling back to unrelated page content.

#### Defect B: Bridge directionality

The inherited `workflows.bridge(tab_from, tab_to, rounds)` implementation does not alternate directions.

Its effective behavior is:

```text
read A -> send to B -> wait for B
read A -> send to B -> wait for B
read A -> send to B -> wait for B
...
```

The response returned by B is logged but never sent back to A.

---

## 3. Branches and Their Purpose

These branches should be preserved until the replacement design is stable.

### `main`

Baseline fork of upstream Parley. Treat as the inherited starting point.

### `test/upstream-bridge-live`

Purpose: empirical baseline testing of the original bridge logic while using the user's existing Chrome session.

Rules:

- Preserve inherited `workflows.bridge()` behavior.
- Preserve inherited ChatGPT extraction behavior unless a test harness requires otherwise.
- Live-browser transport changes are allowed only to make testing possible.
- This branch exists as evidence of how the inherited implementation behaves.

### `feature/live-browser-attach`

Experimental branch used to prove live Chrome attachment and corrected send/wait behavior.

It contains useful implementation work, but it should **not** automatically be merged wholesale. Reuse or redesign its ideas deliberately when building the new architecture.

### `docs/chatgpt-relay-plan`

This planning branch/document.

---

## 4. Design Principles

The new implementation should follow these rules.

### 4.1 Deterministic over heuristic

Do not guess which text belongs to an assistant message.

If the expected ChatGPT turn cannot be identified with confidence, stop and report an error.

### 4.2 Track turns, not page text

A relay unit should be an identifiable assistant turn, not an arbitrary text string scraped from the page.

Useful turn metadata may include:

- target/tab ID
- assistant-turn index
- DOM turn identifier if available
- normalized text
- content hash
- observed completion state
- timestamp

### 4.3 One browser connection per runtime session

Maintain one approved browser-level CDP WebSocket.

Attach A and B as target sessions over that single browser connection.

Avoid opening and closing a browser-level debugging connection for every operation.

### 4.4 One tab attachment per transaction where practical

A single send/wait operation should use one stable target attachment for:

```text
snapshot state
-> submit prompt
-> detect new assistant turn
-> wait for completion
-> return exact response
```

### 4.5 Fail closed

The relay should stop rather than guess when:

- the ChatGPT DOM does not match expected structures;
- the target tab is no longer ChatGPT;
- no unique composer is available;
- a new assistant turn cannot be distinguished from an old one;
- response completion cannot be established;
- the Chrome/CDP session is lost;
- a duplicate turn would be relayed;
- the configured timeout expires.

### 4.6 No hidden model/runtime intelligence

The relay itself should be deterministic software.

It should not use an additional AI model to decide what messages mean, whether to relay them, or how to interpret the DOM.

---

## 5. Target Architecture

Recommended separation:

```text
parley/
    browser/
        cdp.py
        chrome_live.py

    sites/
        chatgpt.py

    relay/
        engine.py
        state.py
        dedupe.py

    cli.py
```

Exact filenames may change, but responsibilities should remain separated.

### Browser layer

Responsibilities:

- discover Chrome live-debugging endpoint;
- hold one browser-level CDP connection;
- list page targets;
- attach/detach target sessions;
- send CDP commands;
- recover from target/session loss when safe.

It must know nothing about ChatGPT messages.

### ChatGPT adapter

Responsibilities:

- verify a target is a ChatGPT page;
- locate the composer;
- submit text;
- enumerate assistant turns;
- identify the latest assistant turn;
- distinguish old versus new turns;
- detect generation in progress;
- detect completion;
- extract only the assistant message body.

It must not implement A/B relay logic.

### Relay engine

Responsibilities:

- manage A and B;
- track which turn was last relayed;
- perform ordered A -> B -> A exchanges;
- enforce round limits;
- pause/resume/stop;
- stop on ambiguous state;
- emit an audit trail.

It must not know ChatGPT CSS selectors.

---

## 6. ChatGPT Turn Extraction

This is the first application-level repair.

### Required behavior

The adapter should recognize only explicit ChatGPT assistant-turn structures.

Candidate current structures include:

```text
[data-message-author-role="assistant"]
article[data-turn="assistant"]
```

Specific selectors must be verified against the user's current ChatGPT DOM before being treated as authoritative.

The adapter should extract the message content from within the assistant turn itself.

### Prohibited behavior

For ChatGPT relay, do **not** fall back to:

- longest visible `div`;
- full `document.body.innerText`;
- generic large text containers;
- sidebar/history/navigation text.

If no assistant turn is recognized:

```text
ERROR: ChatGPT assistant turn could not be identified
```

and stop.

---

## 7. Reliable `send_and_wait()`

Define one reliable ChatGPT operation before implementing the relay.

Conceptual contract:

```python
reply = chatgpt.send_and_wait(tab, prompt)
```

It must:

1. snapshot the current assistant-turn state;
2. focus the correct ChatGPT composer;
3. submit the exact prompt;
4. verify submission occurred;
5. wait for evidence of a **new assistant turn**;
6. track that specific new turn while it streams;
7. wait until that turn is complete/stable;
8. return only that turn's text and identity.

Suggested return structure:

```python
{
    "turn_id": "...",
    "turn_index": 5,
    "text": "...",
    "complete": True,
    "source": "chatgpt",
}
```

A completed old assistant message must never satisfy a wait for a new response.

---

## 8. Bidirectional Relay Semantics

Replace the inherited bridge semantics with explicit bidirectional semantics.

Define **one round as one full back-and-forth exchange**.

Given initial assistant response `A1`:

```text
Round 1
    A1 -> B
    B produces B1
    B1 -> A
    A produces A2

Round 2
    A2 -> B
    B produces B2
    B2 -> A
    A produces A3
```

Therefore:

```text
N rounds = 2N relay transfers
```

Conceptually:

```python
response_a = A.latest_completed_response()

for round_no in range(rounds):
    response_b = B.send_and_wait(response_a.text)
    response_a = A.send_and_wait(response_b.text)
```

The real implementation must include turn identity, duplicate prevention, timeouts, and cancellation.

---

## 9. Duplicate Prevention

Every assistant turn relayed should have a stable fingerprint.

At minimum:

```text
tab identity
+ turn identity/index
+ normalized-text hash
```

Before sending:

```text
if this exact source turn has already been relayed to this destination:
    stop or skip according to explicit policy
```

Initial policy should be **stop and report**, not silently skip.

This prevents the inherited behavior in which the same A response is repeatedly injected into B.

---

## 10. Relay State Machine

Use an explicit state machine instead of a loose loop.

Suggested states:

```text
IDLE
PREPARE
WAIT_A
SEND_A_TO_B
WAIT_B
SEND_B_TO_A
PAUSED
COMPLETE
ERROR
STOPPED
```

Possible flow:

```text
IDLE
  |
  v
PREPARE
  |
  v
WAIT_A
  |
  v
SEND_A_TO_B
  |
  v
WAIT_B
  |
  v
SEND_B_TO_A
  |
  +---- next round ----> WAIT_A
  |
  +---- limit reached -> COMPLETE
```

Pause/stop should be checked between operations and during waits.

---

## 11. Controls

Minimum runtime controls:

- choose ChatGPT tab A;
- choose ChatGPT tab B;
- start;
- pause;
- resume;
- stop;
- configure maximum rounds;
- view current relay state;
- view current round;
- view last message transferred;
- view errors.

Do not build a large GUI until the engine is proven from the CLI.

---

## 12. Audit Trail

Record enough information to diagnose every transfer without logging credentials.

For each transfer:

```text
timestamp
round number
direction (A->B or B->A)
source tab
source turn identity
source text hash
character count
destination tab
submission result
destination response turn identity
completion result
duration
error, if any
```

Do **not** log cookies, auth headers, or other bearer credentials.

Full message text may be optionally logged for development, but should be configurable.

---

## 13. Testing Strategy

### Verification policy

Verification is **selective and milestone-based**.

- Do not use GitHub Actions for this project.
- Keep verification scripts and tests in the repository so they are reusable and survive interruptions.
- Do not run a full verification cycle after every minor edit.
- Run targeted checks when a risky subsystem changes, and run a broader local verification when a development phase is ready to be accepted or merged.
- Prefer one deliberate verification at the end of a phase over repeated low-value checks during implementation.
### 13.1 Unit tests

Tests should prove:

- ChatGPT extraction returns only the assistant message body.
- Sidebar/navigation text is never accepted as an assistant message.
- Missing ChatGPT selectors produce an error rather than generic fallback.
- `send_and_wait()` waits for a new turn rather than returning an old one.
- response streaming does not count as complete too early.
- stable completed text is returned exactly once.
- duplicate source turns are rejected.
- one bridge round produces exactly two transfers.
- two bridge rounds produce exactly four transfers.
- transfer order is exactly:
  `A1 -> B, B1 -> A, A2 -> B, B2 -> A`.
- a timeout stops the relay.
- an ambiguous DOM stops the relay.
- losing A or B stops the relay safely.
- pause prevents new sends.
- stop prevents all future sends.

### 13.2 Recorded DOM fixtures

Where practical, capture sanitized HTML fragments representing:

- blank ChatGPT conversation;
- completed user + assistant exchange;
- streaming assistant turn;
- multiple completed turns.

Use these fixtures to test selectors without repeatedly hitting the live website.

Do not store private chat content in committed fixtures.

### 13.3 Live smoke tests

Keep a small manual/live test that:

1. attaches to normal Chrome;
2. opens two throwaway ChatGPT tabs;
3. seeds A;
4. performs one full A -> B -> A round;
5. verifies both tabs received exactly one relay message;
6. stops.

Live tests should be explicit/manual because they interact with real ChatGPT sessions.

---

## 14. Development Phases

### Phase 0 — Preserve evidence

Status: **Complete**

- Fork upstream.
- Preserve inherited baseline on `main`.
- Preserve original bridge experiment on `test/upstream-bridge-live`.
- Document observed failures.

### Phase 1 — Browser session layer

Status: **Production transport verified; second live smoke exposed transient user-turn count/identity churn; fix implemented, pending local re-verification and smoke rerun**

Deliverables:

- one persistent browser-level live CDP connection;
- tab enumeration;
- serialized flattened target sessions that remain stable for each browser transaction;
- minimal reconnect behavior;
- reduced Chrome approval prompts;
- tests for target/session handling.

Acceptance:

- normal signed-in Chrome session is used;
- no second Chrome profile/login is required;
- ordinary operation uses one initial Chrome approval prompt per runtime session.

### Phase 2 — Strict ChatGPT adapter

Status: **Complete and locally verified (10 tests passed on 2026-09-22)**

Deliverables:

- ChatGPT page validation;
- strict assistant-turn enumeration;
- exact message-body extraction;
- composer detection and submission;
- no generic fallback.

Acceptance:

- the page dump observed in the baseline experiment is impossible by design.

### Phase 3 — Reliable ChatGPT `send_and_wait()`

Status: **Complete and locally verified (milestone verification passed on 2026-09-22)**

Deliverables:

- pre-send turn snapshot;
- submission verification;
- new-turn detection;
- streaming/completion tracking;
- exact completed response return.

Acceptance:

- repeated tests correctly distinguish old and new assistant responses.

### Phase 4 — Bidirectional relay engine

Status: **Complete and locally verified (milestone verification passed on 2026-09-22)**

Deliverables:

- explicit A/B relay state machine;
- full-round semantics;
- round counter;
- deterministic transfer order.

Acceptance:

- for 2 rounds, test sequence is exactly:
  `A1 -> B, B1 -> A, A2 -> B, B2 -> A`.

### Phase 5 — Safety and resilience

Status: **Complete and locally verified (milestone verification passed on 2026-09-22)**

Control semantics: pause/stop are cooperative at safe checkpoints between browser transactions. An in-flight `send_and_wait()` is allowed to finish or fail; stop then prevents any subsequent relay send.

Deliverables:

- duplicate guard;
- timeouts;
- cancellation;
- pause/resume;
- tab/navigation validation;
- connection failure handling;
- structured audit log.

### Phase 6 — CLI usability

Status: **Complete and locally verified (milestone verification passed on 2026-09-22)**

Deliverables:

- list/select ChatGPT tabs;
- start relay;
- pause/resume/stop;
- set rounds;
- display state/transcript summary.

### Phase 7 — Desktop UI

Status: **Legacy UI removed on 2026-09-23; redesign pending.**

The original Tkinter desktop console and its launcher/tests were removed after
the fresh-chat production workflow became the authoritative operating model.
The next UI should be designed from a blank slate around that proven lifecycle
rather than preserving the old tab-selection/Initialize-Both interface.

---

## 15. Explicit Non-Goals for the Initial Build

Do not prioritize:

- autonomous agent role systems;
- agent personalities;
- multi-agent orchestration beyond A and B;
- model routing;
- API-based LLM calls;
- more than two active relay participants;
- Gemini/Claude/Grok parity;
- enterprise deployment;
- cloud hosting;
- browser-profile copying;
- bypassing Chrome's remote-debugging consent controls.

---

## 16. Immediate Next Step

Phase 1 production live-browser integration is implemented and the local transport milestone passed. The first live relay attempt proved A → B submission but exposed current ChatGPT replacing a transient `Thinking` assistant turn with the final answer under a new turn identity; that assistant-candidate replacement fix passed local verification. The second live attempt then showed ChatGPT transiently changing the rendered user-turn count from 3 to 2 while B was generating. The strict state now exposes the latest explicit user turn, and transaction tracking follows that submitted user turn by identity/text while tolerating non-increasing count/DOM-ID churn for the same normalized message. A genuinely different user message still fails closed. Re-run `scripts/verify-live-browser-production.ps1`, then rerun `scripts/parley-live-relay-v1.ps1`.

---

## 17. Interruption / Handoff Note

If work resumes in a future session, start here:

1. Read this document.
2. Treat `main` as the inherited baseline.
3. Treat `test/upstream-bridge-live` as the preserved empirical baseline showing the original bridge defects.
4. Do not assume `feature/live-browser-attach` should be merged wholesale; it is an experimental source of proven ideas.
5. Production live-browser transport verification has passed. Run the first real end-to-end browser relay using `scripts/parley-live-relay-v1.ps1`. If that succeeds, Phase 1 is complete and the project can proceed to Phase 7.
6. Before changing architecture, verify whether a newer planning document or merged implementation has replaced this one.


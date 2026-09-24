# Parley

Parley is a local browser-automation and message-relay tool built on the Chrome DevTools Protocol (CDP). Its current production focus is a **deterministic two-ChatGPT relay** that uses the Chrome session you are already signed into.

Parley does not run an LLM of its own. It attaches to Chrome, identifies the two ChatGPT participants, provisions the conversation-local A/B protocols when requested, relays completed assistant turns, and stops rather than guessing when browser or conversation state is ambiguous.

This repository is a fork of [Satyajeet-04/parley](https://github.com/Satyajeet-04/parley). The fork retains the inherited generic CDP utilities and multi-site adapters, but the actively developed product path is the two-ChatGPT relay described here.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## Current capabilities

The current `main` branch includes:

- a desktop app for configuring and running two-participant ChatGPT relays;
- live attachment to a normal, signed-in Chrome session through `DevToolsActivePort`;
- independent participant selection for A and B:
  - existing / existing;
  - existing / fresh;
  - fresh / existing;
  - fresh / fresh;
- barriered protocol provisioning and activation for structured Parley test conversations;
- strict ChatGPT turn extraction with no generic page-text fallback;
- verified send-and-wait behavior that tracks the newly submitted user turn and corresponding assistant response;
- deterministic A → B → A relay sequencing;
- duplicate prevention, pause/resume/stop controls, audit events, and fail-closed error handling;
- a small CLI for launching the desktop app, listing eligible ChatGPT tabs, and lower-level browser/debug automation;
- inherited MCP, opencode, generic CDP, and non-ChatGPT adapter functionality.

The primary architecture and developer workflow are documented in:

- [Architecture](docs/architecture.md)
- [Development](docs/development.md)
- [Release policy](docs/releases.md)
- [Changelog](CHANGELOG.md)
- [Contributing](CONTRIBUTING.md)

The original relay implementation plan is retained only as a historical record under [docs/history](docs/history/CHATGPT-RELAY-PLAN.md).

## Quick start

### 1. Clone this fork

```powershell
git clone https://github.com/strigoi73-sudo/parley.git
cd parley
```

### 2. Create a virtual environment and install the runtime dependency

Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
```

macOS/Linux:

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -e .
```

Parley requires Python 3.11 or newer. Project metadata and runtime dependencies are declared in `pyproject.toml`; `requirements.txt` is retained as a compatibility mirror for existing workflows.

An editable install also provides the `parley` console command:

```powershell
parley chats
parley app
```

The root `parley.py` shim remains supported for existing scripts.

### 3. Enable Chrome remote debugging

For the primary live relay workflow, Parley attaches to your normal Chrome profile rather than launching a separate browser.

In Chrome, open:

```text
chrome://inspect/#remote-debugging
```

Enable remote debugging. Chrome may ask you to approve the Parley connection.

If Chrome uses a nonstandard user-data directory, set:

```text
PARLEY_CHROME_USER_DATA_DIR
```

### 4. Start the desktop app

On Windows:

```powershell
.\scripts\start-parley.ps1
```

or directly:

```powershell
.\.venv\Scripts\python.exe .\parley.py app
```

Choose each participant independently as a fresh conversation or an eligible existing ChatGPT tab, enter the initial prompt and round count, then start the relay.

## CLI utilities

The desktop app is the only supported end-user relay interface.

The CLI remains available for launching the app, listing eligible ChatGPT tabs, and lower-level development/debug operations:

```powershell
.\.venv\Scripts\python.exe .\parley.py app
.\.venv\Scripts\python.exe .\parley.py chats
.\.venv\Scripts\python.exe .\parley.py list
```

The former interactive `parley relay` command and `scripts/parley-live-relay.ps1` launcher are retired. The low-level `bridge` command remains available for development/debug use; it is not the supported production relay UI.

## Architecture

The current runtime is organized around explicit responsibility boundaries:

```text
parley/
├── app.py                 desktop presentation
├── cli.py                 desktop launcher and developer utilities
├── core.py                generic CDP operations and transport dispatch
├── live_browser.py        persistent live-Chrome transport
├── participants.py        shared ChatGPT target eligibility
├── bootstrap.py           barriered A/B protocol orchestration
├── adapters/
│   ├── chatgpt_strict.py  fail-closed ChatGPT turn extraction
│   ├── chatgpt.py         inherited ChatGPT adapter behavior
│   ├── gemini.py
│   ├── claude.py
│   ├── grok.py
│   ├── generic.py
│   └── js.py
├── protocols/             canonical runtime A/B protocol files
├── relay/
│   ├── engine.py          deterministic relay sequencing
│   ├── session.py         session/worker lifecycle
│   ├── control.py         pause/resume/stop
│   ├── dedupe.py          duplicate-turn protection
│   ├── audit.py           structured audit events
│   └── state.py           relay states
└── workflows.py           browser/chat orchestration and participant bootstrap
```

See [docs/architecture.md](docs/architecture.md) for the detailed responsibility model.

## Secondary / inherited browser automation

The low-level CLI remains useful outside the production two-ChatGPT workflow. Examples include:

```powershell
python .\parley.py list
python .\parley.py read-dom <TAB_ID> ".main-content"
python .\parley.py extract <TAB_ID> "a" href
python .\parley.py click <TAB_ID> "button[type='submit']"
python .\parley.py navigate <TAB_ID> "https://example.com"
python .\parley.py eval <TAB_ID> "document.title"
python .\parley.py cookies <TAB_ID> example.com
```

The inherited `bridge`, MCP server, opencode plugin, and non-ChatGPT adapters remain in the repository, but they are not the acceptance target for the current production relay work.

### Classic transport

The inherited localhost CDP transport remains available:

```bash
./scripts/start-browser.sh
python3 parley.py --classic list
```

Use `--live` or `--classic` to override transport selection where supported.

## Verification

The repository contains a `unittest` regression suite under `tests/`.

Windows:

```powershell
.\scripts\verify.ps1
```

The verification script uses the repository `.venv`, compiles the Python sources, and runs the full `unittest` suite.

Live Chrome behavior is verified separately because it operates against a real signed-in browser session.

See [docs/development.md](docs/development.md) for the complete development and verification workflow.

## Protocol files

The canonical protocol files used by the running application are:

```text
parley/protocols/PARLEY_TEST_CHAT_A_PROTOCOL.md
parley/protocols/PARLEY_TEST_CHAT_B_PROTOCOL.md
```

Do not maintain duplicate protocol definitions elsewhere in the repository. Changes to protocol semantics should update the runtime files and their tests together.

## Security and responsible use

Parley can control authenticated browser tabs and can read cookies through CDP. Treat browser access and cookie values as credentials.

- Do not commit cookies, auth headers, session tokens, or private conversation content.
- Use Parley only with accounts, sessions, and data you are authorized to automate.
- Respect site terms, rate limits, privacy obligations, and applicable law.
- Prefer sanitized fixtures when adding tests.

See [SECURITY.md](SECURITY.md) for vulnerability-reporting guidance and supported-version policy.

## Project history and upstream attribution

Parley began as a fork of [Satyajeet-04/parley](https://github.com/Satyajeet-04/parley). The original repository provided the generic CDP foundation, site adapters, CLI/MCP integrations, and the MIT-licensed base from which this fork developed.

The current fork has since added a substantial ChatGPT-to-ChatGPT relay architecture, live Chrome transport, strict turn tracking, relay safety controls, protocol bootstrap, desktop UI, and participant selection.

The Git history and merged pull requests preserve that development record. Historical planning material is kept under `docs/history/` and should not be treated as current architecture or roadmap documentation.

## License

Parley is distributed under the [MIT License](LICENSE). The original copyright and license notice are preserved from the upstream project.

# Parley Development

This document defines the normal development workflow for the current repository.

## Canonical branch

`main` is the canonical current product branch.

Start ordinary work from an up-to-date `main`:

```powershell
git switch main
git pull origin main
git switch -c feature/short-description
```

Do not normally stack new unrelated work on an unmerged feature branch.

After a PR is merged, delete the feature branch unless there is a concrete reason to keep it.

## Environment setup

### Windows PowerShell

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### macOS/Linux

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -e .
```

Parley requires Python 3.11 or newer. Runtime dependency and package metadata are defined in `pyproject.toml`; `requirements.txt` is retained only as a compatibility mirror.

The editable install exposes the same CLI through the `parley` command while keeping source edits immediately active.

## Local verification

Always run commands through the repository virtual environment so tests do not accidentally use a different system Python.

### Windows

```powershell
.\.venv\Scripts\python.exe -m compileall -q .\parley .\parley.py .\parley_mcp.py .\tests
.\.venv\Scripts\python.exe -m unittest discover -s .\tests -v
```

### macOS/Linux

```bash
./.venv/bin/python -m compileall -q ./parley ./parley.py ./parley_mcp.py ./tests
./.venv/bin/python -m unittest discover -s ./tests -v
```

The full suite is the pre-PR regression gate. Add focused tests for changed behavior rather than relying only on existing coverage.

## Live Chrome verification

Unit tests do not replace a live browser smoke test for changes involving:

- `DevToolsActivePort` discovery;
- Chrome target creation/attachment;
- current ChatGPT DOM behavior;
- file upload/protocol bootstrap;
- participant selection;
- real send-and-wait behavior.

Live testing should use throwaway or non-sensitive conversation content.

Do not commit captured private chat text, cookies, or browser credentials.

## Remote debugging

For the production workflow, enable remote debugging in normal Chrome at:

```text
chrome://inspect/#remote-debugging
```

If Chrome uses a custom user-data directory, set `PARLEY_CHROME_USER_DATA_DIR`.

The live transport expects the browser's `DevToolsActivePort` data and maintains one browser-level WebSocket for the Parley process.

## Development boundaries

Before changing behavior, identify which layer owns it.

- Browser transport issue → `live_browser.py`
- Generic CDP primitive → `core.py`
- Site DOM knowledge → `adapters/`
- ChatGPT strict extraction → `adapters/chatgpt_strict.py`
- Higher-level browser/chat operation → `workflows.py`
- A/B sequencing, dedupe, control, or audit → `relay/`
- CLI presentation → `cli.py`
- Desktop presentation → `app.py`

See [architecture.md](architecture.md) before moving responsibilities across layers.

## Protocol changes

The only canonical protocol files are:

```text
parley/protocols/PARLEY_TEST_CHAT_A_PROTOCOL.md
parley/protocols/PARLEY_TEST_CHAT_B_PROTOCOL.md
```

If protocol text or markers change:

1. update the runtime protocol file;
2. update any relay constants/validation that depend on it;
3. update protocol bootstrap tests;
4. run the full suite;
5. perform a live structured relay smoke test when the change affects actual bootstrap behavior.

Do not create a second protocol copy under `docs/`.

## Documentation policy

Current documentation should describe the current `main` branch.

Historical plans and handoff notes belong under `docs/history/` and should be clearly labeled as superseded.

When an implementation milestone is completed, update current docs rather than leaving an old plan marked "active."

## Pull-request discipline

A PR should be reviewable as one coherent change.

Before opening it:

1. update from `main` if necessary;
2. run compile checks;
3. run the full regression suite;
4. run any required live smoke test;
5. update current documentation;
6. summarize verification in the PR body.

Avoid force-pushing `main` or treating long-lived feature branches as the new product baseline.

## Repository maintenance

Repository-wide tooling changes such as packaging metadata, CI, branch protection, issue templates, and release/version policy should be made as explicit maintenance changes rather than smuggled into unrelated runtime work.

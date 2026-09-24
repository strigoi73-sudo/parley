# Contributing to Parley

Thanks for contributing to Parley.

The current development target is a reliable, local two-ChatGPT relay using the user's existing Chrome session. The repository also retains inherited generic CDP utilities and adapters, but changes should not weaken the safety and determinism of the production ChatGPT relay.

Python package metadata is defined in `pyproject.toml`. Development installs should normally use `pip install -e .` so the `parley` console entry point and package-data behavior are exercised locally.

Before making a substantial change, read:

- [Architecture](docs/architecture.md)
- [Development workflow](docs/development.md)

## Start from current main

New work should normally begin from an up-to-date `main`:

```powershell
git switch main
git pull origin main
git switch -c feature/short-description
```

Do not use an old feature branch as the base for unrelated work. When the change is complete, open a pull request back to `main` and remove the feature branch after merge.

## Set up the development environment

Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

macOS/Linux:

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -e .
```

## Where changes belong

Use the existing responsibility boundaries rather than putting new behavior into whichever file is convenient.

- `parley/live_browser.py` — live Chrome/CDP transport.
- `parley/core.py` — generic browser primitives and transport dispatch.
- `parley/adapters/` — site-specific DOM knowledge.
- `parley/adapters/chatgpt_strict.py` — strict ChatGPT turn extraction.
- `parley/workflows.py` — higher-level browser/chat orchestration, participant preparation, and protocol bootstrap.
- `parley/relay/` — deterministic A/B sequencing, dedupe, control, state, and audit behavior.
- `parley/app.py` — supported desktop relay presentation/controller.
- `parley/cli.py` — desktop launcher plus lower-level developer/debug commands; do not recreate a second production relay controller here.
- `parley/protocols/` — canonical runtime Parley A/B protocol files.
- `tests/` — regression coverage.

See [docs/architecture.md](docs/architecture.md) for the detailed boundary rules.

## Core engineering rules

### Fail closed for ChatGPT relay behavior

Do not fall back to page-wide text, the largest visible container, or other heuristic scraping when strict ChatGPT turn identity cannot be established.

Ambiguous state should produce a structured error rather than an invented success.

### Preserve deterministic relay semantics

The runtime relay is deterministic software. Do not add model calls that interpret message meaning, decide whether a turn "looks right," or choose relay behavior semantically.

### Keep browser transport separate from relay logic

The browser layer should not decide A/B conversation semantics. The relay engine should not know ChatGPT selectors.

### Treat credentials as secrets

Never commit or log cookies, authentication headers, browser credentials, or private conversation contents.

### Avoid unnecessary dependencies

Parley currently has one external runtime dependency, `websocket-client`. Add dependencies only when they provide a clear maintenance or correctness benefit.

## Testing

For ordinary changes, run the complete local regression suite before opening a PR. Parley intentionally relies on local verification rather than GitHub Actions because hosted Actions usage is limited.

Windows:

```powershell
.\scripts\verify.ps1
```

macOS/Linux:

```bash
./.venv/bin/python -m compileall -q ./parley ./parley.py ./parley_mcp.py ./tests
./.venv/bin/python -m unittest discover -s ./tests -v
```

Add focused tests for the behavior you change. A bug fix should normally include a regression test that fails without the fix.

Changes involving real Chrome behavior may also require a manual live smoke test. Describe that verification in the PR.

## Pull requests

A useful PR should state:

- what problem it solves;
- which architectural layer it changes;
- important failure/safety behavior;
- tests added or changed;
- the exact local verification performed;
- any manual Chrome smoke test that remains outstanding.

Keep PRs focused. Avoid mixing repository cleanup, behavior changes, and large refactors unless they are inseparable.

## Documentation

When behavior changes, update the current documentation at the same time.

Current documentation lives in:

- `README.md`
- `CONTRIBUTING.md`
- `docs/architecture.md`
- `docs/development.md`

Files under `docs/history/` are historical records. Do not edit them to make them describe the present.

The runtime A/B protocols live only under `parley/protocols/`; do not create documentation copies that can drift from the files actually loaded by Parley.

## Bug reports

GitHub Issues are not currently enabled for this fork. Issue forms are already stored under `.github/ISSUE_TEMPLATE/` and will become available when the repository issue tracker is enabled.

Until then, use pull requests for fixes you can reproduce and propose. Do not disclose credentials or sensitive browser/session data in a public PR. Security vulnerabilities should follow [SECURITY.md](SECURITY.md) rather than a public issue or PR.

## Code of conduct

Be respectful, specific, and constructive. Critique code and design decisions rather than contributors.

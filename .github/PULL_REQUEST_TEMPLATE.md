## Summary

Describe what this PR changes and why.

## Scope

Which architectural layer(s) does this change affect?

- [ ] Browser transport (`parley/live_browser.py`)
- [ ] Generic CDP core (`parley/core.py`)
- [ ] Site adapter / strict ChatGPT extraction
- [ ] Workflows / participant bootstrap
- [ ] Relay engine / session / controls / dedupe / audit
- [ ] CLI
- [ ] Desktop app
- [ ] Protocol files
- [ ] Packaging / repository maintenance
- [ ] Documentation only

## Verification

Record the checks actually performed.

- [ ] Compile check
- [ ] Full local `unittest` suite
- [ ] Focused regression tests
- [ ] Live Chrome smoke test (when browser behavior changed)
- [ ] Not applicable — documentation/repository-only change

### Results

Paste the concise result, such as test count and outcome.

## Safety / compatibility

- [ ] ChatGPT relay behavior still fails closed on ambiguous state
- [ ] No credentials, cookies, tokens, or private chat content were committed
- [ ] Existing compatibility paths were preserved or the break is documented
- [ ] Current documentation was updated where behavior changed

## Notes

Call out remaining limitations, manual checks, or follow-up work.

# Release and Version Policy

Parley uses Semantic Versioning for formal releases.

## Version source

The canonical version is defined once in:

```text
parley_version.py
```

`pyproject.toml`, the `parley` package, and the MCP server derive their version from that module. Do not add additional hard-coded version strings.

Release tags use the form:

```text
vMAJOR.MINOR.PATCH
```

For example, package version `1.1.0` corresponds to tag `v1.1.0`.

## What counts as a release

A version is considered formally released only when all of the following are true:

1. the release change has been merged into `main`;
2. the exact `main` commit has been tagged `vMAJOR.MINOR.PATCH`;
3. a GitHub Release has been published from that tag.

A version string present only in source code is not, by itself, a release.

## Semantic Versioning

Use:

- **PATCH** for backward-compatible fixes and maintenance changes;
- **MINOR** for backward-compatible functionality;
- **MAJOR** for incompatible public behavior, interface, or workflow changes.

Use judgment for internal refactors that do not affect supported behavior; they normally do not require a version bump by themselves.

## Release procedure

Parley does not use GitHub Actions. Release verification is local and explicit.

1. Start a short-lived release branch from current `main`.
2. Update `parley_version.py` to the intended version.
3. Move completed entries from `[Unreleased]` into a dated version section in `CHANGELOG.md`.
4. Run:

   ```powershell
   .\scripts\verify.ps1
   ```

5. Perform a live Chrome smoke test when the release contains browser-, ChatGPT-, protocol-, or relay-runtime changes.
6. Open and merge the release PR into protected `main`.
7. Tag the exact merge commit with an annotated tag `vMAJOR.MINOR.PATCH`.
8. Push the tag.
9. Publish a GitHub Release from that tag using the corresponding changelog section as the release notes.

Do not move or reuse a published release tag. If a released version is wrong, fix forward with a new version.

## Development between releases

`main` may temporarily retain the latest released version number while unreleased work accumulates. The `[Unreleased]` section in `CHANGELOG.md` distinguishes that development state from the tagged release.

Do not bump the version for every merged PR. Bump it deliberately as part of release preparation.

## Package publishing

No automated PyPI publishing pipeline is currently configured. A GitHub Release and tag define the project's formal release state unless a separate package-publishing policy is added later.

## Fork history

This repository is a fork of `Satyajeet-04/parley`. Upstream history and any upstream version semantics remain part of the Git history, but release tags in this fork describe releases of this maintained fork.

The first formal fork release is `v1.1.0`. The source tree already contained `1.1.0` before the fork established tags/releases; the first release formalizes that existing number rather than reconstructing fictional earlier releases.

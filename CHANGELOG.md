# Changelog

All notable changes to the maintained Parley fork are recorded here.

The project uses Semantic Versioning for formal releases. The Git history remains the authoritative record for development before the first formal fork release.

## [Unreleased]

### Changed

- centralized ChatGPT participant eligibility rules so the CLI and desktop app use the same approved-host and target-ID checks;
- deduplicated fresh ChatGPT participant creation into shared create, composer-ready, and seed/stabilization phases while preserving the pair startup barrier;
- unified general and all-fresh session startup after participant resolution so protocol bootstrap and the initial A transaction have one implementation.

## [1.1.0] - 2026-09-24

First formal release baseline for the maintained `strigoi73-sudo/parley` fork.

### Added

- deterministic two-ChatGPT relay sequencing;
- live attachment to an existing signed-in Chrome session;
- independent existing/fresh participant selection for A and B;
- barriered A/B protocol provisioning and activation;
- strict ChatGPT turn extraction and send-and-wait tracking;
- pause, resume, stop, deduplication, audit, and fail-closed relay controls;
- desktop relay configuration UI;
- standard Python packaging metadata and the `parley` console entry point;
- current architecture, development, security, and contributor documentation;
- repository issue/PR templates and maintenance conventions.

### Changed

- normalized `main` as the canonical product branch;
- consolidated verification scripts into `scripts/verify.ps1`;
- renamed the production relay launcher to `scripts/parley-live-relay.ps1`;
- established local compile/test verification as the repository gate instead of GitHub Actions;
- established formal release/version policy for future changes.

### Historical note

The source tree already contained the string `1.1.0` before this fork had any GitHub tag or Release. This entry formalizes that existing number as the first release baseline for this fork; it does not claim that earlier fork commits were separately released as version 1.1.0.

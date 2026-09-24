# Security Policy

## Supported versions

Until Parley establishes a formal release/tag policy, security fixes are made against the current `main` branch.

Older commits, historical branches, and superseded development snapshots should not be assumed to receive security fixes.

## Reporting a vulnerability

Please do **not** disclose security vulnerabilities, credentials, session tokens, cookies, private conversation content, or exploit details in a public issue or pull request.

If GitHub shows a private vulnerability-reporting option for this repository, use that channel. If private vulnerability reporting is unavailable, contact the maintainer privately using a contact method published on the maintainer's GitHub profile or repository metadata.

A useful report should include:

- the affected Parley component;
- the conditions required to reproduce the problem;
- the security impact;
- minimal reproduction steps;
- whether authenticated Chrome state, cookies, or private chat data are involved;
- any mitigation you have already identified.

Do not include live credentials or reusable authentication material. Redact secrets and use synthetic data wherever possible.

## Security-sensitive areas

Parley controls authenticated browser sessions through Chrome DevTools Protocol. Changes involving the following areas deserve particular care:

- cookie and session access;
- Chrome remote-debugging attachment;
- arbitrary JavaScript evaluation;
- file upload and protocol provisioning;
- navigation and target selection;
- logging/audit behavior;
- handling of private conversation content.

Parley is intended to operate only on accounts, sessions, and data the user is authorized to control.

# Security policy

## Reporting a vulnerability

Report suspected vulnerabilities privately through the repository's security advisory form (Security tab, then "Report a vulnerability"), or by email to Phexara Limited. Do not open a public issue, and never include candidate data, API keys or knowledge base content in a report.

Please include the affected version (`GET /health/live`), the endpoint or command involved, and the steps to reproduce. We aim to acknowledge reports within two working days and to agree a fix and disclosure timeline within ten.

## Supported versions

Only the latest minor release receives security fixes.

## What this service protects

The threat model, the controls in place and the residual risks are documented in [docs/security.md](docs/security.md). In short:

* candidate messages are redacted for personal data before any model call or audit write;
* untrusted text is fenced so it cannot instruct the model, and every output is verified claim by claim before release;
* platform and admin access use separate keys compared in constant time;
* production refuses to start with unsafe settings;
* the audit trail is retained only for the configured period (`v2e audit purge`).

## Handling secrets

Secrets belong in a secret manager, never in the repository, images or logs. The pre commit hooks and CI run a secret scanner. If a key is exposed, rotate it immediately: deploy the new key alongside the old one in `V2E_API_KEYS` or `V2E_ADMIN_API_KEYS`, switch the platform, then remove the old key. Rotate an exposed OpenRouter key in the OpenRouter dashboard.

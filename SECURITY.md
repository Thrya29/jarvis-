# Security

JARVIS can operate your computer, so its security model is part of the product, not an afterthought.

## Reporting a vulnerability

Please do **not** open a public issue. Use GitHub's
[private vulnerability reporting](https://github.com/Thrya29/jarvis-/security/advisories/new).

## Threat model

| Threat | Mitigation |
|---|---|
| Another machine on the network drives JARVIS | API binds to loopback only; non-loopback `server.host` is rejected at config load |
| A malicious web page calls the local API (CSRF / DNS rebinding) | Bearer token on every non-health route; `Host` header allow-list (`127.0.0.1`, `localhost`); WebSocket requires token in first frame within 5 s |
| Another local user reads the token or keys | Token in per-user `%LOCALAPPDATA%`; API keys in Windows Credential Manager (DPAPI-encrypted, per-user) |
| Secrets leak through logs | All log and audit output passes through a redaction filter |
| Prompt injection from screen/web/file content (M1+) | Observed content is passed to the model as untrusted data, never as instructions; risky tools require user confirmation regardless of what the model asks for |
| Agent takes a destructive action by mistake (M1+) | Per-tool risk tiers; confirm-before-act for delete/send/install/purchase; file tools restricted to `safety.allowed_roots`; code runs in a constrained subprocess with timeouts |
| Runaway agent (M2+) | Global kill hotkey; every action is written to the append-only audit log |

## Out of scope

JARVIS runs with your user's privileges. It is not a sandbox against malware already running as you.

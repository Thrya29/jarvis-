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
| Prompt injection from screen/web/file content | File, web and command output is wrapped in `<untrusted_content>` and the system prompt forbids following it; approvals are enforced by the tool registry, not by the model, so injected text cannot skip them |
| Data exfiltration via web requests | `fetch_url` / `open_url` require approval by default; requests to loopback, private and link-local addresses are refused (re-checked on every redirect), so web content can't reach the JARVIS API or the LAN |
| Agent takes a destructive action by mistake | Risk tiers per call (`READ` < `WRITE` < `EXECUTE` < `DESTRUCTIVE` < `EXTERNAL`); overwrite and delete **always** ask regardless of config; deletes go to the Recycle Bin; moves and copies never overwrite; file tools confined to `safety.allowed_roots` with symlinks/junctions resolved first; JARVIS's own config/data/log folders are always denied |
| Secrets leak to generated code | `run_shell` / `run_python` children get an environment with `ANTHROPIC_*`, `JARVIS_*`, cloud and GitHub tokens removed; no stdin, no window, hard timeout that kills the whole process tree |
| Email sent without consent | There is no send capability: `draft_email` only opens a draft for the user |
| Runaway agent | Global kill hotkey (Ctrl+Alt+J, `RegisterHotKey`) cancels the running task from any app; held keys and mouse buttons are released when a task ends; every action is written to the append-only audit log |
| Screen control used without the user knowing | Seeing the screen or operating it needs consent once per task (or an explicit `desktop.screen_control = "allow"`); `deny` removes the screen tools entirely |
| Agent views or operates credentials | Windows matching `desktop.blocked_windows` (password managers, Windows Security) can't be screenshotted, inspected or operated; typing into a focused password field is refused; UI trees mask password values; typed text is recorded in the audit log only as a length |
| Instructions hidden in on-screen content | Screen text and UI trees are marked untrusted; approvals are enforced by the registry, not the model |

## Known limitations

- A host that passes the public-address check could re-resolve to a private address between the check and the connection (DNS rebinding). Approval of web requests is the primary control.
- Approved shell commands and scripts run with your user's privileges. Review them before approving.
- Once screen control is granted for a task, clicks and keystrokes aren't approved one by one. JARVIS can do anything you could do with the mouse and keyboard in unprotected windows. Watch the task, and use the kill switch if needed.
- Windows blocks input to elevated (administrator) windows from a non-elevated JARVIS. This is intentional; run tasks that need admin rights yourself.

## Out of scope

JARVIS runs with your user's privileges. It is not a sandbox against malware already running as you.

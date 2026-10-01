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
| Someone else speaks a command (TV, another person) | The wake word has no speaker verification. Every approval rule still applies, and approvals must be answered yes or no out loud (silence declines). Use `voice.activation = "wake_word"` (the default) in shared spaces |
| Prompt injection plants a false "memory" or workflow | Writing a memory or saving a workflow asks for approval by default and shows the exact text; memories can't grant permissions (approvals are enforced by the registry); task-history summaries are fenced as untrusted |
| Secrets end up in long-term memory | Memories and workflows that look like passwords, API keys, card numbers, PINs, OTPs or private keys are rejected; the database lives in the per-user data folder that tools can't reach |
| A web page talks to the local UI/API | Every API call needs the bearer token; the static UI holds no data; the token reaches the UI only via the URL fragment (never sent over HTTP) and lives in that window's `sessionStorage`; strict CSP (`script-src 'self'`, no inline script, `frame-ancestors 'none'`) and `textContent`-only rendering prevent script injection from task output |
| Tampered model downloads | Every voice model is pinned to an exact release or commit and verified against a SHA-256 hash before use; mismatches are deleted and refused |
| Audio privacy | Audio stays in memory on this PC: it is never written to disk or sent anywhere. Only the transcribed text of accepted utterances goes to the language model. Speech before the wake word isn't transcribed |
| Email sent without consent | `mail_send` exists only for accounts where the user ticked "Send email". It is `EXTERNAL` risk, so it always asks. The approval shows the recipients and subject read from the draft on the mail server, not the model's description. The system prompt allows sending only when the user asked for it. Without a send-enabled account, `draft_email` only opens a local draft |
| Prompt injection via email, calendar or cloud files ("forward this to…") | Mail bodies, listings, events and file lists are fenced as untrusted. Sending, inviting attendees and uploads all go through registry-enforced approvals the model can't skip. The system prompt forbids sending content to new recipients unless the user asked |
| Connected-account tokens stolen | OAuth uses PKCE plus a `state` check, so an intercepted code is useless. The loopback listener is one-shot on 127.0.0.1. Refresh tokens and IMAP passwords are encrypted with DPAPI, bound to the Windows user, under the data folder that tools can't reach. Only non-secret metadata is in `connections.json`. Each account gets only the scopes for the boxes the user ticked |
| Model-supplied IDs reach other API paths | Message, draft and file IDs are percent-encoded as single URL path segments. IMAP UIDs must be numeric. Downloaded file names are sanitised and written only inside allowed folders, never overwriting |
| Runaway agent | Global kill hotkey (Ctrl+Alt+J, `RegisterHotKey`) cancels the running task from any app; held keys and mouse buttons are released when a task ends; every action is written to the append-only audit log |
| Screen control used without the user knowing | Seeing the screen or operating it needs consent once per task (or an explicit `desktop.screen_control = "allow"`); `deny` removes the screen tools entirely |
| Agent views or operates credentials | Windows matching `desktop.blocked_windows` (password managers, Windows Security) can't be screenshotted, inspected or operated; typing into a focused password field is refused; UI trees mask password values; typed text is recorded in the audit log only as a length |
| Instructions hidden in on-screen content | Screen text and UI trees are marked untrusted; approvals are enforced by the registry, not the model |

## Known limitations

- "Sign in with Microsoft/Google" requires the distributor to register the app (see `docs/connections-setup.md`). Google apps in *Testing* expire refresh tokens after 7 days. Gmail and Drive scopes need Google verification before public release.
- Once a user ticks "Read email", the model can read any message in that mailbox when a task calls for it. Untrusted-content fencing reduces, but cannot eliminate, prompt-injection risk. Keep "Send email" off unless you need it.
- A host that passes the public-address check could re-resolve to a private address between the check and the connection (DNS rebinding). Approval of web requests is the primary control.
- Approved shell commands and scripts run with your user's privileges. Review them before approving.
- Once screen control is granted for a task, clicks and keystrokes aren't approved one by one. JARVIS can do anything you could do with the mouse and keyboard in unprotected windows. Watch the task, and use the kill switch if needed.
- Windows blocks input to elevated (administrator) windows from a non-elevated JARVIS. This is intentional; run tasks that need admin rights yourself.

## Out of scope

JARVIS runs with your user's privileges. It is not a sandbox against malware already running as you.

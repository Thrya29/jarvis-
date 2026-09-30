# Changelog

## 1.0.0 — 2026-09-30

First release: an AI control layer for Windows that you can talk to.

- **Desktop app**: a window (Edge app mode) and a tray icon.
  - First-run setup: model choice and API key check, plus the voice model download.
  - The live plan, activity feed, and approval and question dialogs.
  - History with resume, memory and workflows.
  - A voice toggle and Stop.
- **Installer**: per-user, no admin rights.
  - Start-menu and optional desktop shortcuts, optional start-at-sign-in (to the tray) and
    PATH entry.
  - Clean uninstall that keeps your data.
- **Agent** (M1):
  - Plan → act → verify with Claude Opus 5.5 (default) or offline Ollama.
  - Files, Excel and Word, email drafts, PowerShell and Python, web tools.
- **Screen control** (M2):
  - Windows UI Automation tools, plus Claude's computer-use toolset (screenshots, mouse,
    keyboard).
  - Consent per task, protected windows, password-field protection, multi-monitor
    handling.
- **Full-duplex voice** (M3): all local.
  - "Hey Jarvis" wake word, echo cancellation, barge-in, spoken approvals.
  - Whisper speech recognition and the Piper voice.
- **Memory** (M4): preferences and facts, saved parameterised workflows, a task journal
  with resume.
- **Safety**:
  - Risk tiers. Delete and overwrite always ask.
  - Allowed-folder confinement, and untrusted-content fencing.
  - Secrets are scrubbed from child processes and never stored in memory.
  - Loopback-only, token-authenticated API with a strict CSP UI.
  - Global kill switch **Ctrl+Alt+J**, and an audit log of every action.

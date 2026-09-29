# Architecture

JARVIS is a single Windows process (the **daemon**) that hosts every subsystem, plus thin clients
(tray/overlay UI, CLI) that talk to it over a loopback WebSocket API.

```
┌──────────────────────────── jarvis.exe run (asyncio) ────────────────────────────┐
│                                                                                  │
│  Voice I/O (M3)             Agent core (M1)              Effectors (M1–M2)       │
│  mic → AEC → VAD ─┐         Planner: goal → task DAG     Files (allowed roots)   │
│  wake word        ├─ STT ─▶ Executor: observe→act→verify Shell / Python sandbox  │
│  barge-in ◀───────┘         Interrupt bus                Browser (Playwright)    │
│  TTS (streamed) ◀────────── Memory (M4)                  Office (xlsx/docx)      │
│                             LLM provider: Claude|Ollama  Mouse/keyboard, UIA     │
│                                                                                  │
│  Perception (M2): UI Automation tree · screenshots · OCR · vision grounding      │
│  Safety: risk tiers · confirmations · kill hotkey · audit log                    │
│  Foundation (M0): config · logging · secrets · instance lock · local API         │
└────────────────────── ws://127.0.0.1:8765/v1/ws  (bearer token) ─────────────────┘
```

## Source layout

| Path | Responsibility |
|---|---|
| `src/jarvis/cli.py` | `jarvis run / doctor / config / secret / version` |
| `src/jarvis/core/config.py` | Typed settings: defaults → `config.toml` → `JARVIS_*` env |
| `src/jarvis/core/paths.py` | Per-user app directories; `JARVIS_HOME` override |
| `src/jarvis/core/logging_setup.py` | JSON logs, rotation, secret redaction |
| `src/jarvis/core/secrets.py` | Windows Credential Manager via `keyring` |
| `src/jarvis/core/instance.py` | Single-instance lock, local API token |
| `src/jarvis/core/audit.py` | Append-only JSONL record of actions |
| `src/jarvis/server/app.py` | FastAPI app: `/health`, `/v1/status`, `/v1/ws` |
| `src/jarvis/server/daemon.py` | Wires the above together and runs uvicorn |
| `packaging/` | PyInstaller spec, Inno Setup installer script |

## Key decisions

- **One process, asyncio.** Voice barge-in must cancel in-flight LLM calls and tool actions within
  ~200 ms; a single event loop with structured cancellation makes that tractable. CPU-heavy work
  (STT, TTS, OCR) runs in worker threads/processes.
- **Accessibility tree before pixels.** Windows UI Automation gives exact, named controls; vision is
  a fallback for apps that don't expose them. This is faster and far more reliable than
  screenshot-only control.
- **Pluggable LLM providers.** A single provider interface with Claude (default; best planning
  and vision) and Ollama (offline). Selected by `llm.provider`.
- **Loopback + token API.** The UI is a separate client so it can crash or restart without taking
  down an in-progress task, and so future clients (mobile companion, scripts) reuse the same API.
- **Per-user install, no admin.** Installer writes to `%LOCALAPPDATA%\Programs\Jarvis`.

## WebSocket protocol (v1)

1. Client connects to `ws://127.0.0.1:8765/v1/ws`.
2. First frame within 5 s: `{"type": "auth", "token": "<token>"}` else close code `4401`.
3. Server replies `{"type": "ready", "version": "..."}`.
4. `{"type": "ping"}` → `{"type": "pong"}`. Task, event and approval messages arrive in M1.

The token is stored at `<data dir>/api-token` and created on first run.

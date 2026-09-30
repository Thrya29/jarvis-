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
| `src/jarvis/llm/` | Provider-neutral interface; `anthropic_provider.py` (Claude: streaming, adaptive thinking, refusal fallback), `ollama_provider.py` |
| `src/jarvis/agent/loop.py` | The agent loop: turns, tool dispatch, retries, limits, cancellation |
| `src/jarvis/agent/prompts.py` | Cache-stable system prompt; per-goal context goes in the user turn |
| `src/jarvis/agent/factory.py` | Builds an agent (guard, registry, tools, provider) from settings |
| `src/jarvis/safety/policy.py` | Risk tiers, `needs_approval`, `PathGuard`, `Approver` protocol |
| `src/jarvis/tools/` | Tool contract + registry pipeline, and the file, Office, email, execution and web tools |
| `src/jarvis/server/session.py` | Per-client agent session over WebSocket; approvals round-trip to the client |
| `src/jarvis/interfaces/console.py` | Terminal renderer and approver for `jarvis do` / `jarvis chat` |
| `src/jarvis/desktop/winapi.py` | ctypes: DPI awareness, `SendInput` mouse/keyboard, window geometry |
| `src/jarvis/desktop/screen.py` | Monitor capture (mss), downscaling, screenshot-to-screen coordinate mapping, zoom |
| `src/jarvis/desktop/computer.py` | Executes the 17 members of Claude's `computer_toolset_20260801` |
| `src/jarvis/desktop/uia.py` | UI Automation service on a dedicated COM thread: windows, control trees, patterns, app launch |
| `src/jarvis/desktop/session.py` | Per-task screen-control consent and protected-window checks |
| `src/jarvis/desktop/killswitch.py` | Global kill hotkey on its own Win32 message loop |
| `src/jarvis/tools/desktop.py` | Model-agnostic desktop tools built on the UIA service |
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
4. Client → server:
   - `{"type": "task.start", "goal": "..."}` starts a task (one at a time per machine).
   - `{"type": "task.cancel"}` cancels it.
   - `{"type": "approval.response", "id": "...", "approved": true|false, "note": "..."}`
   - `{"type": "ask.response", "id": "...", "answer": "..."}`
   - `{"type": "ping"}` → `{"type": "pong"}`
5. Server → client events: `task.started`, `plan.updated`, `assistant.text`, `tool.started`,
   `tool.finished`, `approval.request` (`risk`, `summary`, `details`), `ask.request`,
   `task.finished` (`status`: completed | failed | cancelled | refused | limit_reached),
   `error`. Unanswered approvals are declined after 5 minutes, or at once if the client
   disconnects.

## Tool call pipeline

Every tool call goes through `ToolRegistry.execute` in this order:
1. Validate the input (pydantic).
2. Compute the risk tier (it can depend on the arguments, e.g. overwrite).
3. Enforce `PathGuard`, then ask for approval if the tier requires it.
4. Write to the audit log.
5. Run with a timeout.
6. Wrap untrusted output in a fence, truncate it, and write the audit log again.

Failures come back to the model as error results; they never crash the task.

## Screen control

- Claude gets the `computer_toolset_20260801` entry (no beta header). Its calls are `tool_use`
  blocks named after the member (`left_click`, `type`, ...) with `toolset_name: "computer"`,
  often several per turn. JARVIS runs them in order. After the first failure it answers the
  rest with the exact halt text, and every result echoes `toolset_name`.
- Screenshots are downscaled to `desktop.max_screenshot_edge` (default 1366 px, hard cap
  2000 px). Coordinates are mapped back to physical pixels. The process is per-monitor-v2
  DPI aware, so screenshots, `SendInput` and UI Automation rectangles agree.
- History stays append-only; old screenshots are removed server-side by context editing
  (`clear_tool_uses_20250919`, cleared in large batches). This never invalidates thinking
  blocks.
- Every model gets the UI Automation tools. Text-only local models can operate standard apps
  through them; they don't get pixel control.

## Voice pipeline

```
mic 16 kHz ─► WebRTC APM (AEC + NS + HPF) ─┬─► openWakeWord ("hey jarvis", 80 ms steps)
   ▲ speaker reference (22.05→16 kHz)       └─► Silero VAD (32 ms) ─► Segmenter
   │                                              speech_start / pause / resume / utterance
Player ◄── Piper TTS (sentence by sentence)          │
   ▲                                                  ▼
   └────────────── VoiceAssistant (asyncio) ◄── faster-whisper base.en (int8, CPU)
                          │  ▲
                 goals ───┘  └── agent events (spoken), approvals (asked aloud)
```

- `voice/audio.py`: PortAudio streams, the playback buffer (instant pause, resume or clear),
  the AEC reference path and the front-end thread.
- `voice/vad.py`: the Silero VAD wrapper and the segmenter state machine. It fires
  `speech_start` after 160 ms of speech, a speculative `pause` after 250 ms of silence,
  `resume` if speech continues, and `utterance` after 700 ms of silence.
- `voice/wake.py`: openWakeWord's mel → embedding → classifier pipeline on onnxruntime,
  with scores matching the reference package.
- `voice/engine.py`: the conversation state machine (see its module docstring).
- `voice/models.py`: pinned, hash-verified model downloads into the cache directory.

## Agent loop invariants

- The conversation is append-only. Claude's assistant content, including thinking blocks, is
  replayed unchanged, which keeps thinking valid and the prompt cache warm.
- Every `tool_use` gets exactly one `tool_result`, in one message, even when a task is
  cancelled, times out or fails midway. The session stays usable for follow-up goals.
- Tool calls cut off by `max_tokens` are never executed.
- Retryable model errors (rate limits, 5xx, network, unparseable streamed tool input) are
  retried twice with backoff; others end the task with a clear message.

The token is stored at `<data dir>/api-token` and created on first run.

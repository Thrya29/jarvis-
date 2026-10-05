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
   - `{"type": "task.resume", "task_id": "..."}` resumes an interrupted, cancelled,
     failed or limit-reached task from the journal.
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

## Desktop app

```
jarvisw.exe app ─┬─ uvicorn (asyncio) ── FastAPI: /v1/* REST, /v1/ws, / + /ui/* static UI
                 │        └─ Hub: provider (lazy), store, voice task, broadcast to all clients
                 ├─ tray thread (pystray): Open · Voice · Stop · Quit
                 └─ opens msedge --app=http://127.0.0.1:8765/#token=… (own profile)
```

- **The UI** is plain HTML/CSS/JS in `src/jarvis/ui`, with no build step and no external
  resources.
  - It renders everything with `textContent`, never `innerHTML`.
  - It runs under a strict CSP: `script-src 'self'`, no inline scripts, no framing.
- **The token** is passed in the URL fragment, so it never reaches the server or its logs.
  The UI keeps it in `sessionStorage` and removes it from the address bar.
- **REST endpoints**:
  - `GET /v1/status` (with setup state)
  - `GET /v1/tasks`
  - `GET` and `DELETE /v1/memories`, `/v1/workflows`
  - `POST /v1/setup/api-key` (validated against the API before it's stored)
  - `POST /v1/setup/provider`, `POST /v1/setup/voice-models` (progress over the WebSocket)
  - `POST /v1/voice`, `POST /v1/stop`
- **Agents are created lazily** per WebSocket session and rebuilt when `Hub.generation`
  changes, for example after a new API key or model. So the app can start before setup is
  done.
- **Broadcasting.** The hub sends voice-initiated task events, voice state and setup progress
  to every connected client.

## Memory, workflows and the task journal

`memory/store.py` keeps one SQLite database (WAL mode) with three parts:
- `memories` with an FTS5 index (Porter stemming, BM25 ranking).
- `workflows`: named instructions with `{parameter}` placeholders.
- `tasks`: goal, status, plan snapshot, summary, and the owning process id.

How it's used:
- Each goal carries a `<memory>` block with all preferences plus the best keyword matches,
  capped by `memory.context_items`. It goes in the user turn, so the cached system prompt
  never changes.
- The agent journals task start, plan updates and finish. Journal errors are logged, never
  fatal.
- At startup, `running` tasks whose process is no longer alive become `interrupted`. Tasks
  owned by another live JARVIS process are left alone.
- Resuming starts a new task linked by `resumed_from`. Its goal restates the original
  request, the last plan and the outcome, and asks the model to check the current state
  before repeating any step.

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

## Desktop UI (v2.2)

`frontend/` is a React 19 + Vite + Tailwind 4 app built into `src/jarvis/ui/` (committed;
CI checks it matches a fresh build). Structure:

- `state.tsx`: a single reducer turns daemon WebSocket events into UI state (chat, streaming
  message, plan, activity, approvals/questions, voice, connections, documents, map focus).
- `lib/`: the REST client (token from the URL fragment, kept in `sessionStorage`) and the
  WebSocket (auth in the first frame, backoff reconnect).
- `views/`: Chat, Maps, Connections, the setup Wizard, and overlay dialogs (native
  `<dialog>`).
- `maps/style.ts`: the HUD basemap style on OpenFreeMap vector tiles, plus geometry
  helpers (range rings, curved arcs, a code-drawn SDF plane icon, so no sprites are
  needed).

Rendering is text-only. A test bans `dangerouslySetInnerHTML`, `.innerHTML`,
`insertAdjacentHTML`, `eval` and `new Function` in the UI source.

## Floating overlay (v2.3)

- `overlay/` (Rust, Tauri 2) creates one transparent, undecorated, always-on-top,
  skip-taskbar, click-through window. It loads `/ui/overlay.html#token=…` from the
  daemon (validated to be loopback), places itself at the top-right of the primary
  monitor, and registers Ctrl+Alt+O (toggle click-through, then
  `window.dispatchEvent('jarvis-overlay')` via `eval`) and Ctrl+Alt+H (hide/show). A
  thread waits on the parent process handle and exits with it.
- `app/overlay.py` (`Overlay`) starts and stops it. The hub runs it exactly when
  `features.hud.enabled`, in desktop mode only.
- `server/session.py` (`SessionRegistry`): observer sessions (auth frame
  `"observe": true`) receive mirrored task events from every session.
  `approval.response`/`ask.response` from any session resolve the pending request
  wherever it lives, and `approval.resolved` is broadcast so other windows close the
  prompt.
- `frontend/overlay.html` → `src/overlay/` is a separate Vite entry. It shares React and
  the socket client, but not MapLibre.

## Live maps (v2.2)

`maps/service.py` (`MapService`, shared per process) serves the three panels and the
agent's map tools, with per-source caches:

- sky: 60 s;
- weather: 10 min;
- agenda: 5 min;
- network: 4 s.

The sources:

- `maps/sky.py`: the OpenSky `states/all` bounding-box query, parsed into aircraft with
  distance and emergency squawks.
- `maps/network.py`: a psutil connection snapshot, grouped by (remote IP, PID), with
  flags. `GeoDB` downloads DB-IP's country-lite MMDB (this month's, or last month's
  early in a month), verifies it and looks up locally. `country_centroids.json` (Google
  DSPL, CC BY 4.0) places countries on the map.
- `maps/weather.py`: Open-Meteo geocoding and forecast.

The UI polls only the panel on screen. The `map_show` tool emits a `map.focus` event that
switches the window to that panel and flies there.

## Connections (v2.1)

- `connect/oauth.py`: authorization code + PKCE with a one-shot loopback listener
  (`http://localhost:<random port>`), `state` check, refresh and friendly errors.
- `connect/secure_store.py`: DPAPI-encrypted JSON blobs per account.
- `connect/accounts.py`: `ConnectionManager` with the account registry
  (`connections.json`, non-secret), the capability → scope map, `pick()` (named or first
  capable account), token refresh under a per-account lock, and `open()`, which yields a
  service bound to a fresh HTTP client.
- `connect/services.py`: `MicrosoftService` (Graph) and `GoogleService` (Gmail /
  Calendar / Drive REST).
- `connect/imap.py`: `ImapService` (imaplib/smtplib in a worker thread, short-lived TLS
  connections). All three expose the same mail interface.
- `tools/connected.py`: eight tools. Connected tools are registered only while at least one
  account exists, and the system prompt lists each account with its allowed actions.
  Connecting or disconnecting bumps the hub generation, so sessions rebuild their agents.
- Sign-in runs as a background task in the hub. The result is broadcast as
  `connections.changed` / `connections.error`, so the HTTP request returns immediately.
- `ToolRegistry` calls `Tool.preview()` before an approval, so prompts show authoritative
  details (e.g. a draft's real recipients).

## Ask my documents (v2.1)

`knowledge/index.py` stores chunks (1,200 characters, 150 overlap), float16 BGE-small
vectors and an FTS5 table in `documents.db`. Search fuses vector and keyword rankings
with reciprocal-rank fusion. The hub re-syncs every 30 minutes (incremental, by size and
mtime); `search_documents` also catches up within a 45-second budget if the index is stale.

## Agent loop invariants

- The conversation is append-only. Claude's assistant content, including thinking blocks, is
  replayed unchanged, which keeps thinking valid and the prompt cache warm.
- Every `tool_use` gets exactly one `tool_result`, in one message, even when a task is
  cancelled, times out or fails midway. The session stays usable for follow-up goals.
- Tool calls cut off by `max_tokens` are never executed.
- Retryable model errors (rate limits, 5xx, network, unparseable streamed tool input) are
  retried twice with backoff; others end the task with a clear message.

The token is stored at `<data dir>/api-token` and created on first run.

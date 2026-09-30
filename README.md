# JARVIS

An AI control layer for Windows. Give it a goal in plain language — by voice or text — and it
plans the steps, operates your apps, files, browser and terminal, watches the screen to verify
each step, and reports back. Full-duplex voice means you can interrupt it mid-sentence.

> **Status:** V1 in development. **M0 (foundation)**, **M1 (agent core)**, **M2 (screen
> control)**, **M3 (full-duplex voice)** and **M4 (memory and workflows)** are complete. Talk to JARVIS, interrupt it
> mid-sentence, and change your instruction on the fly. It plans and works through files,
> Office documents, email drafts, the shell, the web and any app on screen, under a safety
> layer, and tells you what it did. See the [roadmap](#roadmap).

## Requirements

- Windows 10 (22H2) or Windows 11, x64
- 8 GB RAM minimum (16 GB recommended for local speech models + Ollama)
- One LLM backend:
  - **Anthropic API key** (default, recommended — best planning and screen understanding), or
  - **[Ollama](https://ollama.com)** running locally (fully offline; text tasks only on CPU-class hardware)

## Install

### From a release (end users)

Download `JarvisSetup-<version>.exe` from the
[Releases](https://github.com/Thrya29/jarvis-/releases) page and run it. No admin rights needed.

### From source (developers)

```powershell
python -m pip install --user uv
git clone https://github.com/Thrya29/jarvis-.git
cd jarvis-
uv sync
uv run jarvis doctor
```

## First run

```powershell
jarvis config init                          # writes %LOCALAPPDATA%\Jarvis\Jarvis\config.toml
jarvis secret set ANTHROPIC_API_KEY         # stored in Windows Credential Manager, never on disk
jarvis doctor                               # verifies everything is ready
jarvis chat                                 # talk to JARVIS in the terminal
```

Or give a single goal:

```powershell
jarvis do "Organise the PDFs in Documents\Project X into folders by client, write a summary of each into a Word document, build an Excel tracker of them, and draft an email to priya@example.com with both attached"
```

`jarvis run` starts the background daemon (`127.0.0.1:8765`) that the desktop UI and voice
front-ends (M3, M5) connect to.

## What JARVIS can do (M1)

| Area | Tools | Asks first? |
|---|---|---|
| Planning | `update_plan`, `ask_user` | - |
| Files | `list_dir`, `read_file` (text, Word, Excel, PDF), `search_files` | No |
| | `write_file`, `make_dir`, `move_path`, `copy_path` (never overwrite) | No (configurable) |
| | Overwriting a file, `delete_path` (to the Recycle Bin) | **Always** |
| Office | `create_excel` (formatted workbook), `create_word_document` | No; overwrite always asks |
| Email | `draft_email` opens a draft in Outlook or your mail app. JARVIS never sends. | No |
| Code | `run_shell` (PowerShell), `run_python` | Yes (configurable) |
| Web | `fetch_url` (public pages only), `open_url` | Yes (configurable) |

File tools only work inside `safety.allowed_roots` (default: Documents, Desktop, Downloads).

## Memory, workflows and task history (M4)

- **Memory.** Tell JARVIS "remember that I prefer reports as PDF" and it saves a note. Your
  preferences and the notes relevant to each request are shown to the model with every
  goal. Saving or changing a memory asks you first (`memory.confirm_writes`).
  Passwords, keys, card numbers and codes are refused outright.
- **Workflows.** After a task goes well, say "save this as a workflow called weekly report".
  Later: "run my weekly report workflow for the Falcon folder". Workflows can take
  parameters, and you approve the saved instructions when they're created.
- **Task history and resume.** Every task is journaled with its plan and outcome. If JARVIS
  is closed or crashes mid-task, the task is marked *interrupted*, and you can pick it up
  again. JARVIS checks what's already done before continuing.

```powershell
jarvis memory list | search <words> | forget <id> | clear
jarvis workflows [list] | show <name> | run <name> key=value ... | delete <name>
jarvis tasks [list] | resume <task-id>
```

Everything is stored in one SQLite file (`%LOCALAPPDATA%\Jarvis\Jarvis\jarvis.db`) that
JARVIS's own tools can't read or modify. Search uses SQLite full-text search (BM25 with
stemming). For a personal store of hundreds of notes it's accurate, and it needs no extra
model.

## Voice (M3)

```powershell
jarvis voice setup      # one-time: downloads and verifies ~130 MB of speech models
jarvis voice say "Hello, I am Jarvis."   # checks your speakers
jarvis voice            # hands-free session: say "Hey Jarvis, ..."
```

- **Full duplex.** The microphone stays on while JARVIS talks. WebRTC echo cancellation
  removes JARVIS's own voice, so you can talk over it:
  - Start speaking and it **pauses within ~160 ms**.
  - Say "okay" or "uh-huh" and it carries on.
  - Say "stop" and it stops.
  - Say something new ("actually, make it a spreadsheet") and it drops what it was doing and
    changes course in the same conversation.
- **Wake word.** Say "Hey Jarvis". While you're in a conversation (JARVIS is working,
  speaking, or has just finished) you don't need to repeat it. Set `voice.activation` to
  `always` in a quiet room to skip the wake word entirely.
- **Spoken approvals.** Risky actions are asked out loud ("Move old.txt to the Recycle Bin.
  Should I go ahead?"). Answer yes or no. If you don't answer, the action is declined.
- **Local speech.** Speech recognition (faster-whisper `base.en`), the voice (Piper), wake
  word, VAD and echo cancellation all run on your CPU. Audio never leaves the PC or touches
  the disk; only the transcribed text goes to the language model.
- **Latency on a 4-core laptop CPU:** about 0.7 s of silence ends your turn. Transcription
  then takes ~1–1.5 s, sped up by starting it speculatively during the pause, and speech
  synthesis runs ~10× faster than real time.
- **Headphones or speakers.** Echo cancellation makes speakers work. If it's unavailable,
  JARVIS falls back to half-duplex and you should use headphones.
- **Run with the daemon.** Set `voice.enabled = true` to start voice with `jarvis run`. It
  shares the one-task-at-a-time lock with other clients, and Ctrl+Alt+J silences it.

| Setting | Default | |
|---|---|---|
| `voice.activation` | `wake_word` | or `always` |
| `voice.stt_model` | `base.en` | `small.en` is more accurate but ~3× slower |
| `voice.tts_engine` / `tts_voice` | `piper` / `lessac` | voice `amy`, or engine `sapi` (Windows voices, no download) |
| `voice.barge_in` | `true` | talking over JARVIS interrupts it |
| `voice.follow_up_s` | `8` | seconds to keep listening after JARVIS speaks |
| `voice.input_device` / `output_device` | system default | see `jarvis voice devices` |

## Screen control (M2)

JARVIS can see the screen and operate any app:

| Tools | How it works | Models |
|---|---|---|
| `launch_app`, `list_windows`, `focus_window` | Starts apps from the Start menu; finds and brings windows forward | All |
| `inspect_window`, `click_element`, `set_element_text` | Reads an app's real controls through Windows UI Automation and acts on them by id | All |
| Computer-use toolset: `screenshot`, clicks, drag, `type`, `key`, `scroll`, `zoom`, ... | Looks at screenshots and drives the mouse and keyboard | Claude |

- **Consent per task.** The first screen action in a task asks: *"Let JARVIS see your screen
  and control the mouse and keyboard for this task?"* Set `desktop.screen_control` to `allow`
  to stop asking, or `deny` to turn screen control off.
- **Kill switch.** Press **Ctrl+Alt+J** anywhere to stop JARVIS immediately (`safety.kill_hotkey`).
- **Protected windows.** JARVIS won't view or operate password managers or Windows Security
  (`desktop.blocked_windows`). It refuses to type into password fields.
- **Multiple monitors.** JARVIS works on one monitor (`desktop.monitor`, default: primary).
  Windows it focuses are moved onto that monitor.
- Screenshots are sent to the model provider. They are never written to disk.

The default model is Claude Opus 5.5 (`llm.anthropic.model`), with `effort = "high"`.
Anthropic's server-side refusal fallback is enabled (`llm.anthropic.server_fallback`).

To go fully offline, install Ollama, `ollama pull qwen2.5:3b`, and set in `config.toml`:

```toml
[llm]
provider = "ollama"
```

Small local models handle simple file tasks but are much weaker at long multi-step work.

## Configuration

Settings are resolved in this order (highest wins):

1. Environment variables: `JARVIS_<SECTION>__<KEY>`, e.g. `JARVIS_LLM__PROVIDER=ollama`
2. `config.toml` (`jarvis config path` prints its location)
3. Built-in defaults (`jarvis config show` prints the effective config)

Set `JARVIS_HOME` to relocate all config, data and logs into one folder (portable installs).

| Location | Contents |
|---|---|
| `%LOCALAPPDATA%\Jarvis\Jarvis\config.toml` | Configuration |
| `%LOCALAPPDATA%\Jarvis\Jarvis\Logs\jarvis.log` | Structured JSON logs, rotated |
| `%LOCALAPPDATA%\Jarvis\Jarvis\Logs\audit.jsonl` | Append-only record of every action taken |
| Windows Credential Manager → `jarvis` | API keys |

## Security model (summary)

- The control API binds to **loopback only**; non-loopback hosts are rejected at config load.
- Every API call needs a per-install bearer token; `Host` header allow-listing blocks DNS rebinding.
- Secrets live in Windows Credential Manager and are redacted from all logs.
- Deleting or overwriting always requires confirmation; running code and web requests do by default.
- File tools are confined to allowed folders; JARVIS's own config, token and logs are unreachable.
- Content from files, web pages and command output is fenced as untrusted data for the model.
- Child processes get an environment with API keys and tokens removed.
- A global kill hotkey (`Ctrl+Alt+J`) halts all activity (M2+).

See [SECURITY.md](SECURITY.md) and [docs/architecture.md](docs/architecture.md).

## Roadmap

| Milestone | Scope | Status |
|---|---|---|
| M0 | Foundation: config, logging, secrets, API, CLI, CI, installer | ✅ |
| M1 | Agent core (plan → act → verify), Claude + Ollama providers, file/shell/Office/email/web tools, safety layer | ✅ |
| M2 | Screen perception (UI Automation + screenshots), mouse/keyboard/app control, kill switch | ✅ |
| M3 | Full-duplex voice: echo cancellation, VAD, barge-in, local STT/TTS, wake word, spoken approvals | ✅ |
| M4 | Long-term memory, reusable workflows, task history and resume | ✅ |
| M5 | Tray/overlay UI, first-run wizard, V1 release | ⏳ |

## Development

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv run pyinstaller packaging/jarvis.spec --noconfirm   # builds dist\jarvis\jarvis.exe
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

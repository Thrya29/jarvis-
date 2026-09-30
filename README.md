# JARVIS

An AI control layer for Windows. Give it a goal in plain language — by voice or text — and it
plans the steps, operates your apps, files, browser and terminal, watches the screen to verify
each step, and reports back. Full-duplex voice means you can interrupt it mid-sentence.

> **Status:** V1 in development. **M0 (foundation)** and **M1 (agent core)** are complete:
> JARVIS takes a goal, plans it, works through files, Office documents, email drafts, the shell
> and the web under a safety layer, and reports back. Screen control and voice are next. See
> the [roadmap](#roadmap).

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
| M2 | Screen perception (UI Automation + vision + OCR), mouse/keyboard/app and browser control | ⏳ |
| M3 | Full-duplex voice: AEC, VAD, barge-in, streaming STT/TTS, wake word | ⏳ |
| M4 | Long-term memory, reusable workflows, task resume | ⏳ |
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

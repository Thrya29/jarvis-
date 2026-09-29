# JARVIS

An AI control layer for Windows. Give it a goal in plain language — by voice or text — and it
plans the steps, operates your apps, files, browser and terminal, watches the screen to verify
each step, and reports back. Full-duplex voice means you can interrupt it mid-sentence.

> **Status:** V1 in development. Milestone **M0 (foundation)** is complete: config, logging,
> secrets, local control API, CLI, CI and the installer pipeline. See the [roadmap](#roadmap).

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
jarvis run                                  # starts the daemon on 127.0.0.1:8765
```

To go fully offline, install Ollama, `ollama pull qwen2.5:3b`, and set in `config.toml`:

```toml
[llm]
provider = "ollama"
```

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
- Risky actions (delete, send, install, pay) will always require explicit confirmation (M1+).
- A global kill hotkey (`Ctrl+Alt+J`) halts all activity (M2+).

See [SECURITY.md](SECURITY.md) and [docs/architecture.md](docs/architecture.md).

## Roadmap

| Milestone | Scope | Status |
|---|---|---|
| M0 | Foundation: config, logging, secrets, API, CLI, CI, installer | ✅ |
| M1 | Agent core (plan → act → verify), Claude + Ollama providers, file/shell/Office/browser tools, safety layer | ⏳ |
| M2 | Screen perception (UI Automation + vision + OCR), mouse/keyboard/app control | ⏳ |
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

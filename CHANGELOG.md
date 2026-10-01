# Changelog

## 2.2.0 — 2026-10-01

A new interface, and live maps.

- **New HUD-style interface**, rebuilt in React + Vite + Tailwind:
  - a left rail with Command, Maps, Connections and Settings;
  - an animated core that shows whether JARVIS is standing by, listening, working or
    replying;
  - self-hosted fonts.
  - Every feature of the old window carries over: the setup wizard, streaming replies,
    sources, plan and activity, approvals, history/memory/workflows, Connections and
    voice controls.
  - Still rendered as text only (no `innerHTML`), under the same strict CSP. A test bans
    markup-injection APIs in the UI source.
- **Live maps** (MapLibre GL on OpenFreeMap vector tiles, custom dark style):
  - **Live sky**: aircraft near you from the OpenSky Network, coloured by altitude.
    Emergency squawks are highlighted; click a plane for details.
  - **Network security map**: the outside connections of this PC by program and country.
    It flags unusual ports and unknown programs, and lists ports open to the network.
    Locations come from DB-IP's country database, looked up locally.
  - **Situation**: Open-Meteo weather, a 12-hour rain chart, a 3-day outlook, and today's
    calendar events on the map.
  - **"Fly to a place"** search.
- **Map tools** for the agent:
  - `map_show` opens a panel, centred on a place if asked;
  - `flights_nearby`, `network_activity` and `weather`.
- **Settings**:
  - home city (geocoded when saved);
  - Live maps with per-panel tick boxes and a sky range;
  - later features' version badges updated (overlay v2.3, protocols v2.4, phone v2.5,
    helpers v2.6, smart home and webcam v2.7).
- **Security**:
  - The CSP adds `worker-src 'self'` (MapLibre's worker is a same-origin file, not a
    `blob:`) and the tile host.
  - All other map data is fetched by the daemon.
  - The geolocation database is verified before use.
- **Build and CI**:
  - the UI is built from `frontend/` into `src/jarvis/ui/`;
  - CI rebuilds it and fails if the committed build is stale;
  - the frozen-build smoke test now covers the network map.

## 2.1.0 — 2026-10-01

Your accounts, the web and your documents.

- **Connections**: each user connects their own accounts and ticks what JARVIS may do.
  - **Microsoft 365 / Outlook** (Microsoft Graph) and **Google** (Gmail, Calendar,
    Drive): sign in on the provider's page (OAuth 2.0 with PKCE and a one-time local
    redirect). Only the scopes for the ticked options are requested.
  - **IMAP/SMTP** for other providers, using an app password.
  - Tokens and passwords are DPAPI-encrypted per Windows user. Disconnecting deletes them
    (and revokes Google's grant).
- **New tools**: `mail_search`, `mail_read`, `mail_draft`, `mail_send`,
  `calendar_events`, `calendar_create`, `files_search`, `file_download`.
  - Sending always asks first and shows the draft's real recipients from the server.
  - Creating an event with attendees asks first; downloads never overwrite and stay in
    allowed folders.
  - Mail, events and file listings are fenced as untrusted content.
- **Web research with sources**: Claude's server-side web search and fetch. Sources are
  shown as links under the answer, and search fees are counted in the daily budget.
- **Ask my documents**: a local hybrid (meaning + keyword) index of chosen folders,
  using BGE-small (int8 ONNX, SHA-256 pinned). Incremental re-indexing runs every 30
  minutes; the status and a "Re-index now" button are in Connections.
- Approval prompts can now show live details fetched before asking (`Tool.preview`).
- Setup guide for app registration: `docs/connections-setup.md`.

## 2.0.0 — 2026-09-30

Personalisation and a faster, more natural conversation.

- **Setup wizard and Settings**, per user, in seven steps:
  - About you, Personality, AI model, Voice, Features, Budget, Review.
  - Every choice is saved to that user's `config.toml` and applied immediately.
- **Personality**: four styles and a form of address (sir / ma'am / name / nickname /
  none). Optional warnings before unwise actions.
- **Streaming replies**: text appears as it's written, and voice starts speaking at the
  first complete sentence.
- **Progress updates**: the model's notes between tool calls are shown in the window, and
  spoken if you choose. They're requested with `thinking.display = "updates"`; on
  Claude Opus 5.5 these notes were previously invisible.
- **Quick replies**: conversational turns are answered by Claude Haiku 4.5. The fast
  model hands anything that needs action to the full agent.
- **Voices**: British male (Alan), British female (Jenny), American male (Ryan), American
  female (Lessac), with a preview button. All are pinned and SHA-256 verified.
- **Budget**: estimated daily API spend with a hard cap, shown in the top bar.
- **Future features** (web research, documents, email and calendar, protocols, HUD, phone,
  helpers, smart home, webcam) can be chosen now. They switch on as versions 2.1–2.6
  ship.

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

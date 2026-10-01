import { useCallback, useEffect, useMemo, useReducer, useRef, type ReactNode } from "react";
import { coreLabel, coreState, Reactor } from "./components/Reactor";
import { api, errorText, post, TOKEN } from "./lib/api";
import { JarvisSocket } from "./lib/socket";
import type { JsonEvent, SettingsView, Status } from "./lib/types";
import { AppContext, initialState, reducer, useApp, type View } from "./state";
import { ChatView } from "./views/Chat";
import { ConnectionsView } from "./views/Connections";
import { MapsView } from "./views/Maps";
import { ApprovalDialog, AskDialog, Toast } from "./views/Overlays";
import { Wizard } from "./views/Wizard";

function Icon({ d }: { d: string }) {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={d} />
    </svg>
  );
}

const NAV: { view: View; label: string; icon: string }[] = [
  { view: "chat", label: "Command", icon: "M4 5h16v11H8l-4 4z" },
  { view: "maps", label: "Maps", icon: "M12 21s-7-6.2-7-12a7 7 0 0 1 14 0c0 5.8-7 12-7 12zM12 11.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z" },
  { view: "connections", label: "Connections", icon: "M9 15l6-6M10.5 6.5l1.8-1.8a4 4 0 0 1 5.7 5.7L16.2 12M7.8 12l-1.8 1.8a4 4 0 0 0 5.7 5.7l1.8-1.8" },
];

function NavRail() {
  const { state, dispatch } = useApp();
  const button = (active: boolean) =>
    `flex w-full cursor-pointer flex-col items-center gap-0.5 border-0 border-l-2 bg-transparent py-3 font-[family-name:var(--font-hud)] text-[11px] font-semibold tracking-wider uppercase ${active ? "border-cyan text-cyan" : "border-transparent text-muted hover:text-ice"}`;
  return (
    <nav className="flex w-20 flex-none flex-col items-center border-r border-line bg-deep/70" aria-label="Main">
      {NAV.map((n) => (
        <button key={n.view} type="button" className={button(state.view === n.view)} aria-current={state.view === n.view ? "page" : undefined} onClick={() => dispatch({ type: "view", view: n.view })}>
          <Icon d={n.icon} />
          {n.label}
        </button>
      ))}
      <span className="flex-1" />
      <button type="button" className={button(state.wizard.open)} onClick={() => dispatch({ type: "wizard", open: true, step: 1 })}>
        <Icon d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />
        Settings
      </button>
    </nav>
  );
}

function Clock() {
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    const tick = () => {
      if (ref.current) ref.current.textContent = new Date().toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    };
    tick();
    const t = window.setInterval(tick, 10_000);
    return () => window.clearInterval(t);
  }, []);
  return <span ref={ref} className="font-mono text-sm text-ice" />;
}

function TopBar() {
  const { state, dispatch, send, toast } = useApp();
  const setup = state.status?.setup;
  const core = coreState(state.connected, state.busy, state.streamingId !== null, state.voiceState);
  const spend = setup?.spend_today_usd;
  const cap = setup?.daily_cap_usd;

  const toggleVoice = async () => {
    const turnOn = state.voiceState === "off";
    if (turnOn && setup && !setup.voice_models_ready) {
      dispatch({ type: "wizard", open: true, step: 4 });
      return toast("Tick hands-free voice and finish setup to download the voice models.");
    }
    dispatch({ type: "voiceState", state: turnOn ? "starting" : "off" });
    try {
      await post("/v1/voice", { enabled: turnOn });
    } catch (err) {
      toast(errorText(err));
      dispatch({ type: "refresh", status: true });
    }
  };
  const stop = async () => {
    send({ type: "task.cancel" });
    try {
      await post("/v1/stop");
    } catch (err) {
      toast(errorText(err));
    }
  };

  const voiceLabel: Record<string, string> = { off: "Voice off", starting: "Voice starting…", listening: "Listening" };
  return (
    <header className="flex flex-none items-center gap-3 border-b border-line bg-deep/80 px-3 py-2">
      <div className="flex items-center gap-2">
        <Reactor state={core} size={34} />
        <div className="leading-tight">
          <div className="font-[family-name:var(--font-hud)] text-lg font-bold tracking-[0.3em]">JARVIS</div>
          <div className="hud-label">{coreLabel(core)}</div>
        </div>
      </div>
      <div className="ml-2 flex min-w-0 flex-1 flex-wrap items-center gap-1.5">
        <span className={`pill ${state.connected ? "pill-ok" : "pill-warn"}`} title="Connection to the JARVIS service">
          <span className="dot" />
          {state.connected ? "Online" : "Offline"}
        </span>
        {setup?.model ? (
          <span className="pill hidden md:inline-flex" title="Language model">
            {setup.provider}: {setup.model}
          </span>
        ) : null}
        <button type="button" className={`pill cursor-pointer ${state.voiceState === "listening" ? "pill-live" : ""}`} title="Hands-free voice (say “Hey Jarvis”)" onClick={() => void toggleVoice()}>
          {voiceLabel[state.voiceState] || `Voice: ${state.voiceState}`}
        </button>
        {spend !== undefined ? (
          <span className={`pill hidden sm:inline-flex ${cap && spend >= cap * 0.8 ? "pill-warn" : ""}`} title="Estimated API spend today">
            {cap ? `$${spend.toFixed(2)} / $${cap.toFixed(2)}` : `$${spend.toFixed(2)}`} today
          </span>
        ) : null}
      </div>
      <Clock />
      {state.busy ? (
        <button type="button" className="btn btn-danger" title="Stop everything (also Ctrl+Alt+J)" onClick={() => void stop()}>
          Stop
        </button>
      ) : null}
    </header>
  );
}

function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-full flex-col">
      <TopBar />
      <div className="flex min-h-0 flex-1">
        <NavRail />
        <main className="flex min-w-0 flex-1 flex-col">{children}</main>
      </div>
    </div>
  );
}

export function App() {
  const [state, dispatch] = useReducer(reducer, initialState);
  const socket = useRef<JarvisSocket | null>(null);

  const toast = useCallback((text: string) => dispatch({ type: "toast", text }), []);
  const send = useCallback((msg: JsonEvent) => {
    const ok = socket.current?.send(msg) ?? false;
    if (!ok) dispatch({ type: "toast", text: "Not connected to JARVIS yet - retrying…" });
    return ok;
  }, []);
  const busyRef = useRef(false);
  busyRef.current = state.busy;
  const submitGoal = useCallback(
    (text: string) => {
      const goal = text.trim();
      if (!goal || busyRef.current) return false;
      if (!send({ type: "task.start", goal })) return false;
      dispatch({ type: "say", kind: "user", text: goal });
      dispatch({ type: "view", view: "chat" });
      return true;
    },
    [send],
  );

  useEffect(() => {
    if (!TOKEN) {
      dispatch({ type: "say", kind: "error", text: "Open JARVIS from the Start menu or the tray icon." });
      return;
    }
    const s = new JarvisSocket(
      (event) => dispatch({ type: "event", event }),
      (connected, unauthorised) => {
        dispatch({ type: "socket", connected, unauthorised });
        if (unauthorised)
          dispatch({ type: "say", kind: "error", text: "This window isn't authorised. Open JARVIS from the Start menu or tray icon." });
      },
    );
    socket.current = s;
    s.connect();
    return () => s.close();
  }, []);

  // Status (model, voice, spend) after connecting and after each task.
  useEffect(() => {
    if (!state.refreshStatus) return;
    api<Status>("/v1/status")
      .then((status) => {
        dispatch({ type: "status", status });
        const setup = status.setup;
        if (setup && (!setup.llm_ready || !setup.onboarded)) dispatch({ type: "wizard", open: true });
      })
      .catch((err) => toast(`Status: ${errorText(err)}`));
    api<SettingsView>("/v1/settings")
      .then((cfg) => dispatch({ type: "greeting", text: cfg.profile.name ? cfg.profile.name.split(" ")[0] : "" }))
      .catch(() => {});
  }, [state.refreshStatus, toast]);

  const value = useMemo(() => ({ state, dispatch, send, submitGoal, toast }), [state, send, submitGoal, toast]);

  return (
    <AppContext.Provider value={value}>
      <Shell>
        {state.view === "chat" && <ChatView />}
        {state.view === "maps" && <MapsView />}
        {state.view === "connections" && <ConnectionsView />}
      </Shell>
      {state.wizard.open ? <Wizard /> : null}
      <ApprovalDialog />
      <AskDialog />
      <Toast />
    </AppContext.Provider>
  );
}

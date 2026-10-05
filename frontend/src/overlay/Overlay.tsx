import { useEffect, useReducer, useRef, useState } from "react";
import { coreLabel, coreState, Reactor } from "../components/Reactor";
import { api, post, TOKEN } from "../lib/api";
import { JarvisSocket } from "../lib/socket";
import type { ApprovalRequest, JsonEvent, PlanStep, Status } from "../lib/types";

const IDLE_DIM_MS = 15_000;
const RISK_TEXT: Record<string, string> = {
  read: "Reads information",
  write: "Makes changes",
  execute: "Runs code or controls the screen",
  destructive: "Deletes or overwrites",
  external: "Leaves this computer",
};
const DONE_TEXT: Record<string, string> = {
  completed: "Done",
  cancelled: "Stopped",
  failed: "Failed",
  refused: "Declined",
  limit_reached: "Stopped at a limit",
  interrupted: "Interrupted",
};

interface OState {
  connected: boolean;
  busy: boolean;
  streaming: boolean;
  voice: string;
  goal: string;
  plan: PlanStep[];
  reply: string;
  note: string;
  result: string;
  approvals: ApprovalRequest[];
  questions: number;
  lastActivity: number;
}

const initial: OState = {
  connected: false,
  busy: false,
  streaming: false,
  voice: "off",
  goal: "",
  plan: [],
  reply: "",
  note: "",
  result: "",
  approvals: [],
  questions: 0,
  lastActivity: Date.now(),
};

type Action =
  | { type: "event"; e: JsonEvent }
  | { type: "socket"; connected: boolean }
  | { type: "status"; status: Status };

function reduce(s: OState, a: Action): OState {
  if (a.type === "socket") return { ...s, connected: a.connected };
  if (a.type === "status") return { ...s, busy: a.status.busy, voice: a.status.setup?.voice_state || s.voice };
  const e = a.e;
  const touched = { ...s, lastActivity: Date.now() };
  switch (e.type) {
    case "ready":
      return { ...s, busy: Boolean(e.busy) };
    case "task.started":
      return { ...touched, busy: true, goal: e.goal || "", plan: [], reply: "", note: "", result: "" };
    case "plan.updated":
      return { ...touched, plan: e.steps || [] };
    case "assistant.delta":
      return { ...touched, streaming: true, reply: (s.streaming ? s.reply : "") + e.text };
    case "assistant.discard":
      return { ...touched, streaming: false, reply: "" };
    case "assistant.text":
      return { ...touched, streaming: false, reply: e.text };
    case "assistant.progress":
      return { ...touched, streaming: false, note: e.text };
    case "tool.started":
      return { ...touched, streaming: false, note: `… ${e.tool}` };
    case "task.finished":
      return {
        ...touched,
        busy: false,
        streaming: false,
        note: "",
        result: DONE_TEXT[e.status] || e.status,
        approvals: [],
        questions: 0,
      };
    case "approval.request":
      return { ...touched, approvals: [...s.approvals, e as unknown as ApprovalRequest] };
    case "approval.resolved":
      return { ...touched, approvals: s.approvals.filter((r) => r.id !== e.id) };
    case "ask.request":
      return { ...touched, questions: s.questions + 1 };
    case "voice.state":
      return { ...touched, voice: e.state };
    default:
      return s;
  }
}

export function Overlay() {
  const [s, dispatch] = useReducer(reduce, initial);
  const [interactive, setInteractive] = useState(false);
  const [now, setNow] = useState(Date.now());
  const socket = useRef<JarvisSocket | null>(null);

  useEffect(() => {
    if (!TOKEN) return;
    const sock = new JarvisSocket(
      (e) => dispatch({ type: "event", e }),
      (connected) => {
        dispatch({ type: "socket", connected });
        if (connected) api<Status>("/v1/status").then((status) => dispatch({ type: "status", status })).catch(() => {});
      },
      true,
    );
    socket.current = sock;
    sock.connect();
    return () => sock.close();
  }, []);

  // The native overlay tells the page when Ctrl+Alt+O makes it clickable.
  useEffect(() => {
    const onToggle = (ev: Event) => setInteractive(Boolean((ev as CustomEvent).detail?.interactive));
    window.addEventListener("jarvis-overlay", onToggle);
    return () => window.removeEventListener("jarvis-overlay", onToggle);
  }, []);

  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 2000);
    return () => window.clearInterval(t);
  }, []);

  const approval = s.approvals[0];
  const core = coreState(s.connected, s.busy, s.streaming, s.voice);
  const done = s.plan.filter((p) => p.status === "done").length;
  const current = s.plan.find((p) => p.status === "in_progress");
  const dim = !interactive && !approval && !s.busy && now - s.lastActivity > IDLE_DIM_MS;

  const answer = (approved: boolean) => {
    if (!approval) return;
    socket.current?.send({ type: "approval.response", id: approval.id, approved, note: "" });
  };
  const openMain = () => void post("/v1/window/show").catch(() => {});

  return (
    <div className="h-screen w-screen p-2">
      <div
        className={`overlay-panel hud-panel flex h-full flex-col gap-1.5 overflow-hidden px-3 py-2.5 ${dim ? "dim" : ""} ${interactive ? "interactive" : ""}`}
      >
        <div className="flex items-center gap-2.5">
          <Reactor state={core} size={44} />
          <div className="min-w-0 flex-1 leading-tight">
            <div className="font-[family-name:var(--font-hud)] text-base font-bold tracking-[0.3em]">JARVIS</div>
            <div className="hud-label">
              {coreLabel(core)}
              {s.voice === "listening" ? " · voice on" : ""}
            </div>
          </div>
          {s.plan.length ? (
            <span className="pill pill-live">
              {done}/{s.plan.length}
            </span>
          ) : s.result ? (
            <span className="pill">{s.result}</span>
          ) : null}
        </div>

        {s.plan.length ? (
          <div className="h-1 w-full overflow-hidden rounded-sm bg-line">
            <div className="h-full bg-cyan transition-[width] duration-500" style={{ width: `${(100 * done) / s.plan.length}%` }} />
          </div>
        ) : null}

        {approval ? (
          <div className="rounded border border-amber/60 bg-amber/10 px-2.5 py-1.5">
            <p className="hud-label m-0 text-amber">Approval needed · {RISK_TEXT[approval.risk] || approval.risk}</p>
            <p className="clamp-3 m-0 text-sm">{approval.summary}</p>
            {interactive ? (
              <div className="mt-1.5 flex gap-2">
                <button className="btn btn-sm" type="button" onClick={() => answer(false)}>
                  Don't allow
                </button>
                <button className="btn btn-sm btn-primary" type="button" onClick={() => answer(true)}>
                  Allow
                </button>
              </div>
            ) : (
              <p className="hud-label m-0 mt-1">Ctrl+Alt+O to answer here</p>
            )}
          </div>
        ) : (
          <>
            {s.goal ? <p className="m-0 truncate text-sm text-muted">▸ {s.goal}</p> : null}
            {current ? <p className="m-0 truncate text-sm text-cyan">{current.title}</p> : null}
            {s.reply ? (
              <p className="clamp-3 m-0 text-sm">{s.reply.length > 400 ? `…${s.reply.slice(-400)}` : s.reply}</p>
            ) : s.note ? (
              <p className="clamp-3 m-0 text-sm text-muted italic">{s.note}</p>
            ) : !s.goal ? (
              <p className="m-0 text-sm text-muted">{s.connected ? "Say “Hey Jarvis”, or give me a goal." : "Waiting for JARVIS…"}</p>
            ) : null}
          </>
        )}

        {s.questions > 0 && !approval ? (
          <p className="m-0 text-sm text-amber">JARVIS has a question — open the main window to answer.</p>
        ) : null}

        <div className="mt-auto flex items-center gap-2">
          {interactive ? (
            <>
              <button className="btn btn-sm" type="button" onClick={openMain}>
                Open JARVIS
              </button>
              <span className="hud-label ml-auto">Ctrl+Alt+O lock · Ctrl+Alt+H hide</span>
            </>
          ) : (
            <span className="hud-label ml-auto opacity-70">Ctrl+Alt+O interact</span>
          )}
        </div>
      </div>
    </div>
  );
}

// App state: one reducer, fed by daemon events and UI actions.

import { createContext, useContext, type Dispatch } from "react";
import type {
  ActivityItem,
  ApprovalRequest,
  AskRequest,
  ChatMessage,
  ConnectionsStatus,
  DocumentsStatus,
  JsonEvent,
  MapFocus,
  PlanStep,
  Status,
} from "./lib/types";

export type View = "chat" | "maps" | "connections";

export interface VoiceSetup {
  text: string;
  ok?: boolean;
  progress: number | null; // percent while downloading
  readyAt: number; // bumps when models finish installing
}

export interface State {
  connected: boolean;
  unauthorised: boolean;
  busy: boolean;
  status: Status | null;
  messages: ChatMessage[];
  streamingId: number | null;
  plan: PlanStep[];
  activity: ActivityItem[];
  approvals: ApprovalRequest[];
  asks: AskRequest[];
  voiceState: string;
  toast: { id: number; text: string } | null;
  view: View;
  wizard: { open: boolean; step: number };
  mapFocus: MapFocus | null;
  connections: ConnectionsStatus | null;
  connWaiting: boolean;
  connResult: { text: string; ok: boolean } | null;
  docs: DocumentsStatus | null;
  voiceSetup: VoiceSetup;
  refreshLists: number;
  refreshStatus: number;
  greeting: string;
}

export const initialState: State = {
  connected: false,
  unauthorised: false,
  busy: false,
  status: null,
  messages: [],
  streamingId: null,
  plan: [],
  activity: [],
  approvals: [],
  asks: [],
  voiceState: "off",
  toast: null,
  view: "chat",
  wizard: { open: false, step: 1 },
  mapFocus: null,
  connections: null,
  connWaiting: false,
  connResult: null,
  docs: null,
  voiceSetup: { text: "", progress: null, readyAt: 0 },
  refreshLists: 0,
  refreshStatus: 0,
  greeting: "",
};

export type Action =
  | { type: "event"; event: JsonEvent }
  | { type: "socket"; connected: boolean; unauthorised: boolean }
  | { type: "status"; status: Status }
  | { type: "say"; kind: ChatMessage["kind"]; text: string; label?: string }
  | { type: "toast"; text: string }
  | { type: "toast.clear"; id: number }
  | { type: "view"; view: View }
  | { type: "wizard"; open: boolean; step?: number }
  | { type: "approval.done"; id: string; summary: string; approved: boolean }
  | { type: "ask.done"; id: string; answer: string }
  | { type: "connections"; status: ConnectionsStatus; result?: { text: string; ok: boolean } | null }
  | { type: "conn.result"; text: string; ok: boolean; waiting?: boolean }
  | { type: "docs"; docs: DocumentsStatus }
  | { type: "voiceSetup"; patch: Partial<VoiceSetup> }
  | { type: "voiceState"; state: string }
  | { type: "refresh"; lists?: boolean; status?: boolean }
  | { type: "greeting"; text: string };

let nextId = 1;
const id = () => nextId++;
const MAX_ACTIVITY = 60;

function addActivity(s: State, text: string, failed = false): State {
  return { ...s, activity: [{ id: id(), text, failed }, ...s.activity].slice(0, MAX_ACTIVITY) };
}

function addMessage(s: State, msg: Omit<ChatMessage, "id">): State {
  return { ...s, messages: [...s.messages, { id: id(), ...msg }].slice(-400) };
}

const FINISH_NOTES: Record<string, (summary: string) => string> = {
  cancelled: () => "Stopped.",
  failed: (summary) => `Failed: ${summary}`,
  refused: (summary) => summary,
  limit_reached: (summary) => summary,
};

function applyEvent(s: State, e: JsonEvent): State {
  const voice = e.source === "voice";
  switch (e.type) {
    case "ready":
      return {
        ...s,
        busy: Boolean(e.busy),
        refreshLists: s.refreshLists + 1,
        refreshStatus: s.refreshStatus + 1,
      };
    case "task.started": {
      const next = { ...s, busy: true, streamingId: null, plan: [] };
      return voice ? addMessage(next, { kind: "user", text: e.goal, label: "🎙 said" }) : next;
    }
    case "plan.updated":
      return { ...s, plan: e.steps || [] };
    case "assistant.delta": {
      if (s.streamingId === null) {
        const msgId = id();
        return {
          ...s,
          streamingId: msgId,
          messages: [
            ...s.messages,
            { id: msgId, kind: "assistant", text: e.text, label: voice ? "🔊 JARVIS" : undefined },
          ],
        };
      }
      return {
        ...s,
        messages: s.messages.map((m) => (m.id === s.streamingId ? { ...m, text: m.text + e.text } : m)),
      };
    }
    case "assistant.discard":
      return {
        ...s,
        messages: s.messages.filter((m) => m.id !== s.streamingId),
        streamingId: null,
      };
    case "assistant.text": {
      const sources = Array.isArray(e.sources) ? e.sources : undefined;
      if (e.streamed && s.streamingId !== null) {
        return {
          ...s,
          streamingId: null,
          messages: s.messages.map((m) => (m.id === s.streamingId ? { ...m, text: e.text, sources } : m)),
        };
      }
      return addMessage(
        { ...s, streamingId: null },
        { kind: "assistant", text: e.text, label: voice ? "🔊 JARVIS" : undefined, sources },
      );
    }
    case "assistant.progress":
      return addMessage({ ...s, streamingId: null }, { kind: "progress", text: e.text });
    case "tool.started":
      return addActivity({ ...s, streamingId: null }, `… ${e.tool}`);
    case "tool.finished":
      return e.ok ? s : addActivity(s, `✖ ${e.tool}: ${e.preview}`, true);
    case "task.finished": {
      let next: State = {
        ...s,
        busy: false,
        streamingId: null,
        refreshLists: s.refreshLists + 1,
        refreshStatus: s.refreshStatus + 1,
      };
      const note = FINISH_NOTES[e.status];
      if (note) next = addMessage(next, { kind: "system", text: note(e.summary || "") });
      if (e.tool_calls) {
        const u = e.usage || {};
        next = addActivity(
          next,
          `■ ${e.status} - ${e.tool_calls} tool calls, ${(u.input_tokens || 0).toLocaleString()} in / ${(u.output_tokens || 0).toLocaleString()} out tokens`,
        );
      }
      return next;
    }
    case "approval.request":
      return { ...s, approvals: [...s.approvals, e as unknown as ApprovalRequest] };
    case "ask.request":
      return { ...s, asks: [...s.asks, e as unknown as AskRequest] };
    case "voice.state":
      return { ...s, voiceState: e.state };
    case "voice.log":
      return typeof e.text === "string" && e.text.startsWith("you (voice)>")
        ? addActivity(s, `🎙 ${e.text.slice(12).trim()}`)
        : s;
    case "setup.progress": {
      const pct = e.total ? Math.floor((100 * e.done) / e.total) : 0;
      return {
        ...s,
        voiceSetup: {
          ...s.voiceSetup,
          progress: pct,
          ok: undefined,
          text: e.file ? `Downloading ${e.file}: ${pct}%` : "Preparing speech recognition…",
        },
      };
    }
    case "setup.voice_ready":
      return {
        ...s,
        voiceSetup: { text: "✔ Voice models installed.", ok: true, progress: null, readyAt: Date.now() },
        refreshStatus: s.refreshStatus + 1,
      };
    case "setup.error":
      return { ...s, voiceSetup: { ...s.voiceSetup, text: e.error, ok: false, progress: null } };
    case "connections.changed": {
      const { type: _t, ...status } = e;
      return {
        ...s,
        connections: status as unknown as ConnectionsStatus,
        connWaiting: false,
        connResult: s.connWaiting ? { text: "Connected. JARVIS can now use this account.", ok: true } : s.connResult,
        refreshStatus: s.refreshStatus + 1,
      };
    }
    case "connections.error":
      return { ...s, connWaiting: false, connResult: { text: e.error, ok: false } };
    case "documents.progress": {
      const { type: _t, ...docs } = e;
      return { ...s, docs: { ...(s.docs || {}), enabled: true, ...docs } as DocumentsStatus };
    }
    case "map.focus":
      return {
        ...s,
        view: "maps",
        mapFocus: { view: e.view, lat: e.lat, lon: e.lon, label: e.label, at: Date.now() },
      };
    case "error": {
      const next = addMessage(s, { kind: "error", text: e.error });
      return e.setup_required ? { ...next, wizard: { open: true, step: 3 } } : next;
    }
    default:
      return s;
  }
}

export function reducer(s: State, a: Action): State {
  switch (a.type) {
    case "event":
      return applyEvent(s, a.event);
    case "socket":
      return { ...s, connected: a.connected, unauthorised: a.unauthorised };
    case "status":
      return { ...s, status: a.status, voiceState: a.status.setup?.voice_state || s.voiceState };
    case "say":
      return addMessage(s, { kind: a.kind, text: a.text, label: a.label });
    case "toast":
      return { ...s, toast: { id: id(), text: a.text } };
    case "toast.clear":
      return s.toast?.id === a.id ? { ...s, toast: null } : s;
    case "view":
      return { ...s, view: a.view };
    case "wizard":
      return { ...s, wizard: { open: a.open, step: a.step ?? s.wizard.step } };
    case "approval.done":
      return addActivity(
        { ...s, approvals: s.approvals.filter((r) => r.id !== a.id) },
        `${a.approved ? "✔ allowed" : "✖ declined"}: ${a.summary}`,
        !a.approved,
      );
    case "ask.done": {
      const next = { ...s, asks: s.asks.filter((r) => r.id !== a.id) };
      return a.answer ? addMessage(next, { kind: "user", text: a.answer }) : next;
    }
    case "connections":
      return {
        ...s,
        connections: a.status,
        connWaiting: Boolean(a.status.signing_in),
        connResult: a.result === undefined ? s.connResult : a.result,
      };
    case "conn.result":
      return { ...s, connResult: { text: a.text, ok: a.ok }, connWaiting: a.waiting ?? s.connWaiting };
    case "docs":
      return { ...s, docs: a.docs };
    case "voiceSetup":
      return { ...s, voiceSetup: { ...s.voiceSetup, ...a.patch } };
    case "voiceState":
      return { ...s, voiceState: a.state };
    case "refresh":
      return {
        ...s,
        refreshLists: s.refreshLists + (a.lists ? 1 : 0),
        refreshStatus: s.refreshStatus + (a.status ? 1 : 0),
      };
    case "greeting":
      return { ...s, greeting: a.text };
  }
}

export interface AppContextValue {
  state: State;
  dispatch: Dispatch<Action>;
  send: (msg: JsonEvent) => boolean;
  submitGoal: (text: string) => boolean;
  toast: (text: string) => void;
}

export const AppContext = createContext<AppContextValue | null>(null);

export function useApp(): AppContextValue {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error("useApp outside AppContext");
  return ctx;
}

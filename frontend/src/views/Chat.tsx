import { useCallback, useEffect, useRef, useState } from "react";
import { Dialog } from "../components/Dialog";
import { coreLabel, coreState, Reactor } from "../components/Reactor";
import { api, del, errorText } from "../lib/api";
import type { ChatMessage, Memory, Source, Task, Workflow } from "../lib/types";
import { useApp } from "../state";

const EXAMPLES = [
  "Organise the PDFs on my Desktop into folders by topic",
  "What's on my calendar today, and will it rain?",
  "Show me the planes flying overhead",
];

const MARKS: Record<string, string> = { done: "✔", in_progress: "▶", failed: "✖", skipped: "–", pending: "○" };
const MARK_COLOR: Record<string, string> = {
  done: "text-ok",
  in_progress: "text-cyan",
  failed: "text-danger",
  skipped: "text-muted",
  pending: "text-muted",
};

function Sources({ sources }: { sources: Source[] }) {
  const safe = sources
    .slice(0, 12)
    .map((s) => {
      try {
        const url = new URL(s.url);
        return url.protocol === "https:" || url.protocol === "http:" ? { url, title: s.title } : null;
      } catch {
        return null;
      }
    })
    .filter((x): x is { url: URL; title: string } => x !== null);
  if (!safe.length) return null;
  return (
    <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs">
      <span className="hud-label">Sources</span>
      {safe.map(({ url, title }) => (
        <a
          key={url.href}
          href={url.href}
          title={url.href}
          target="_blank"
          rel="noopener noreferrer"
          className="max-w-[32ch] truncate rounded-sm border border-line px-2 py-px text-cyan no-underline hover:border-cyan"
        >
          {title || url.hostname}
        </a>
      ))}
    </div>
  );
}

function Message({ m }: { m: ChatMessage }) {
  return (
    <div className={`msg msg-${m.kind}`}>
      {m.label ? <span className="hud-label mb-0.5 block">{m.label}</span> : null}
      <span>{m.text}</span>
      {m.sources?.length ? <Sources sources={m.sources} /> : null}
    </div>
  );
}

function Welcome() {
  const { state, submitGoal } = useApp();
  const core = coreState(state.connected, state.busy, false, state.voiceState);
  return (
    <div className="m-auto flex max-w-xl flex-col items-center text-center">
      <Reactor state={core} size={150} />
      <p className="hud-label mt-3">{coreLabel(core)}</p>
      <h1 className="mt-1 font-[family-name:var(--font-hud)] text-3xl font-semibold tracking-wide text-ice">
        What can I do for you{state.greeting ? `, ${state.greeting}` : ""}?
      </h1>
      <p className="hint mt-1">
        Give me a goal in plain language. I plan it, work through your files, apps and the web, and ask before
        anything risky.
      </p>
      <div className="mt-4 flex w-full flex-col gap-2">
        {EXAMPLES.map((ex) => (
          <button
            key={ex}
            type="button"
            className="hud-panel cursor-pointer px-3 py-2.5 text-left text-ice hover:border-cyan"
            onClick={() => submitGoal(ex)}
          >
            {ex}
          </button>
        ))}
      </div>
    </div>
  );
}

function Composer() {
  const { state, submitGoal } = useApp();
  const [goal, setGoal] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    const t = ref.current;
    if (!t) return;
    t.style.height = "auto";
    t.style.height = `${Math.min(160, t.scrollHeight)}px`;
  }, [goal]);
  const submit = () => {
    if (submitGoal(goal)) setGoal("");
  };
  return (
    <form
      className="flex gap-2 border-t border-line bg-deep/80 px-[clamp(12px,4vw,48px)] pt-3 pb-4"
      autoComplete="off"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <textarea
        ref={ref}
        rows={1}
        value={goal}
        aria-label="Goal"
        className="max-h-40 min-h-10 flex-1 resize-none"
        placeholder="Tell JARVIS what to do…  (Enter to send, Shift+Enter for a new line)"
        onChange={(e) => setGoal(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            submit();
          }
        }}
      />
      <button className="btn btn-primary min-w-24" type="submit" disabled={state.busy}>
        {state.busy ? "Working…" : "Send"}
      </button>
    </form>
  );
}

function PlanPanel() {
  const { state } = useApp();
  return (
    <aside className="hidden w-72 flex-none flex-col gap-3 overflow-auto border-l border-line p-3 xl:flex" aria-label="Plan and activity">
      <h2 className="hud-title">Plan</h2>
      <ol className="m-0 flex list-none flex-col gap-1.5 p-0">
        {state.plan.length ? (
          state.plan.map((s, i) => (
            <li key={i} className={`flex items-start gap-2 ${s.status === "skipped" ? "text-muted line-through" : ""}`}>
              <span className={`w-4 flex-none text-center ${MARK_COLOR[s.status || "pending"]}`}>
                {MARKS[s.status || "pending"]}
              </span>
              <span>{s.title}</span>
            </li>
          ))
        ) : (
          <li className="text-muted">{state.busy ? "Planning…" : "No task running."}</li>
        )}
      </ol>
      <h2 className="hud-title mt-2">Activity</h2>
      <ul className="m-0 flex list-none flex-col gap-1 p-0 font-mono text-xs">
        {state.activity.map((a) => (
          <li key={a.id} className={a.failed ? "text-danger" : "text-muted"} style={{ overflowWrap: "anywhere" }}>
            {a.text}
          </li>
        ))}
      </ul>
    </aside>
  );
}

const when = (iso: string) =>
  iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "";

const STATUS_COLOR: Record<string, string> = {
  completed: "text-ok",
  failed: "text-danger",
  refused: "text-danger",
  interrupted: "text-amber",
  cancelled: "text-amber",
  limit_reached: "text-amber",
};

type Tab = "history" | "memory" | "workflows";

function SideLists() {
  const { state, send, dispatch, submitGoal, toast } = useApp();
  const [tab, setTab] = useState<Tab>("history");
  const [tasks, setTasks] = useState<Task[]>([]);
  const [memories, setMemories] = useState<Memory[]>([]);
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [params, setParams] = useState<{ w: Workflow; values: Record<string, string> } | null>(null);

  const load = useCallback(async () => {
    try {
      const [t, m, w] = await Promise.all([
        api<Task[]>("/v1/tasks?limit=40"),
        api<Memory[]>("/v1/memories"),
        api<Workflow[]>("/v1/workflows"),
      ]);
      setTasks(t);
      setMemories(m);
      setWorkflows(w);
    } catch (err) {
      console.warn("could not load lists", err);
    }
  }, []);
  useEffect(() => {
    if (state.connected) void load();
  }, [state.refreshLists, state.connected, load]);

  const resume = (t: Task) => {
    if (state.busy) return toast("Wait for the current task to finish.");
    if (send({ type: "task.resume", task_id: t.id }))
      dispatch({ type: "say", kind: "user", text: `Resume: ${t.goal.split("\n")[0]}` });
  };
  const forget = async (m: Memory) => {
    if (!confirm(`Forget “${m.content}”?`)) return;
    try {
      await del(`/v1/memories/${m.id}`);
      void load();
    } catch (err) {
      toast(errorText(err));
    }
  };
  const removeWorkflow = async (w: Workflow) => {
    if (!confirm(`Delete workflow “${w.name}”?`)) return;
    try {
      await del(`/v1/workflows/${encodeURIComponent(w.name)}`);
      void load();
    } catch (err) {
      toast(errorText(err));
    }
  };
  const run = (w: Workflow) => {
    if (state.busy) return toast("Wait for the current task to finish.");
    if (!w.parameters.length) return void submitGoal(`Run my saved workflow "${w.name}".`);
    setParams({ w, values: Object.fromEntries(w.parameters.map((p) => [p, ""])) });
  };

  const empty = (text: string) => <li className="p-1 text-muted">{text}</li>;
  const item = "rounded border border-line bg-panel-2/60 p-2";

  return (
    <aside className="hidden w-64 flex-none flex-col border-r border-line lg:flex" aria-label="History, memory and workflows">
      <nav className="flex border-b border-line" role="tablist">
        {(["history", "memory", "workflows"] as Tab[]).map((t) => (
          <button
            key={t}
            role="tab"
            aria-selected={tab === t}
            className={`flex-1 cursor-pointer border-0 border-b-2 bg-transparent py-2.5 font-[family-name:var(--font-hud)] text-sm font-semibold capitalize tracking-wider ${tab === t ? "border-cyan text-ice" : "border-transparent text-muted"}`}
            onClick={() => setTab(t)}
          >
            {t}
          </button>
        ))}
      </nav>
      <ul className="m-0 flex flex-1 list-none flex-col gap-2 overflow-auto p-2">
        {tab === "history" &&
          (tasks.length
            ? tasks.map((t) => (
                <li key={t.id} className={item}>
                  <span>{t.goal.split("\n")[0].slice(0, 140)}</span>
                  <span className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted">
                    <span className={STATUS_COLOR[t.status] || ""}>{t.status}</span>
                    <span>{when(t.started_at)}</span>
                    {t.resumable ? (
                      <button className="btn btn-sm" type="button" onClick={() => resume(t)}>
                        Resume
                      </button>
                    ) : null}
                  </span>
                </li>
              ))
            : empty("Tasks you give JARVIS appear here."))}
        {tab === "memory" &&
          (memories.length
            ? memories.map((m) => (
                <li key={m.id} className={item}>
                  <span>{m.content}</span>
                  <span className="mt-1 flex items-center gap-2 text-xs text-muted">
                    <span>{m.kind}</span>
                    <button className="btn btn-sm" type="button" onClick={() => void forget(m)}>
                      Forget
                    </button>
                  </span>
                </li>
              ))
            : empty("Say “remember that…” and JARVIS will keep it here."))}
        {tab === "workflows" &&
          (workflows.length
            ? workflows.map((w) => (
                <li key={w.name} className={item}>
                  <strong className="block">{w.name}</strong>
                  <span>{w.description}</span>
                  <span className="mt-1 flex items-center gap-2 text-xs text-muted">
                    <button className="btn btn-sm" type="button" onClick={() => run(w)}>
                      Run
                    </button>
                    <button className="btn btn-sm" type="button" onClick={() => void removeWorkflow(w)}>
                      Delete
                    </button>
                    <span>run {w.run_count}×</span>
                  </span>
                </li>
              ))
            : empty("After a task, say “save this as a workflow called …”."))}
      </ul>

      <Dialog open={params !== null} onClose={() => setParams(null)} labelledBy="paramsTitle">
        {params ? (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              const args = params.w.parameters.map((p) => `${p}="${params.values[p]}"`).join(", ");
              submitGoal(`Run my saved workflow "${params.w.name}" with ${args}.`);
              setParams(null);
            }}
          >
            <h2 id="paramsTitle" className="hud-title mb-2 text-base">
              Run “{params.w.name}”
            </h2>
            {params.w.parameters.map((p) => (
              <label key={p} className="field">
                {p}
                <input
                  type="text"
                  value={params.values[p]}
                  onChange={(e) => setParams({ ...params, values: { ...params.values, [p]: e.target.value } })}
                />
              </label>
            ))}
            <div className="mt-4 flex justify-end gap-2">
              <button className="btn" type="button" onClick={() => setParams(null)}>
                Cancel
              </button>
              <button className="btn btn-primary" type="submit">
                Run
              </button>
            </div>
          </form>
        ) : null}
      </Dialog>
    </aside>
  );
}

export function ChatView() {
  const { state } = useApp();
  const logRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [state.messages]);
  return (
    <div className="flex min-h-0 flex-1">
      <SideLists />
      <section className="flex min-w-0 flex-1 flex-col" aria-label="Conversation">
        <div ref={logRef} className="flex flex-1 flex-col gap-3 overflow-auto px-[clamp(12px,4vw,48px)] py-6" aria-live="polite">
          {state.messages.length ? state.messages.map((m) => <Message key={m.id} m={m} />) : <Welcome />}
        </div>
        <Composer />
      </section>
      <PlanPanel />
    </div>
  );
}

import { useEffect, useState } from "react";
import { Dialog } from "../components/Dialog";
import { useApp } from "../state";

const RISK_TEXT: Record<string, string> = {
  read: "Reads information",
  write: "Makes changes",
  execute: "Runs code, uses the network or controls the screen",
  destructive: "Deletes or overwrites",
  external: "Leaves this computer",
};
const RISK_COLOR: Record<string, string> = {
  read: "text-cyan",
  write: "text-cyan",
  execute: "text-amber",
  destructive: "text-danger",
  external: "text-danger",
};

function notify(title: string, body: string) {
  if (document.hasFocus() || !("Notification" in window)) return;
  if (Notification.permission === "granted") new Notification(title, { body });
}

export function ApprovalDialog() {
  const { state, send, dispatch } = useApp();
  const req = state.approvals[0];
  const [note, setNote] = useState("");
  useEffect(() => {
    setNote("");
    if (req) notify("JARVIS needs your approval", req.summary);
  }, [req?.id]);

  const answer = (approved: boolean) => {
    if (!req) return;
    send({ type: "approval.response", id: req.id, approved, note });
    dispatch({ type: "approval.done", id: req.id, summary: req.summary, approved });
  };

  return (
    <Dialog open={Boolean(req)} onClose={() => answer(false)} labelledBy="approvalTitle">
      {req ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            answer(true);
          }}
        >
          <p className={`hud-label m-0 font-bold ${RISK_COLOR[req.risk] || ""}`}>{RISK_TEXT[req.risk] || req.risk}</p>
          <h2 id="approvalTitle" className="mt-1 mb-2 font-[family-name:var(--font-hud)] text-xl font-semibold">
            Allow this?
          </h2>
          <p className="mb-2">{req.summary}</p>
          {req.details ? (
            <pre className="max-h-56 overflow-auto rounded border border-line bg-void p-2 font-mono text-xs whitespace-pre-wrap">
              {req.details}
            </pre>
          ) : null}
          <input
            type="text"
            className="mt-2 w-full"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="Optional note (e.g. why not, or what to do instead)"
            aria-label="Note"
          />
          <div className="mt-4 flex justify-end gap-2">
            <button className="btn" type="button" onClick={() => answer(false)}>
              Don't allow
            </button>
            <button className="btn btn-primary" type="submit" autoFocus>
              Allow
            </button>
          </div>
        </form>
      ) : null}
    </Dialog>
  );
}

export function AskDialog() {
  const { state, send, dispatch } = useApp();
  const req = state.asks[0];
  const [answer, setAnswer] = useState("");
  useEffect(() => {
    setAnswer("");
    if (req) notify("JARVIS has a question", req.question);
  }, [req?.id]);

  const reply = (text: string) => {
    if (!req) return;
    send({ type: "ask.response", id: req.id, answer: text });
    dispatch({ type: "ask.done", id: req.id, answer: text });
  };

  return (
    <Dialog open={Boolean(req)} onClose={() => reply("")} labelledBy="askTitle">
      {req ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            reply(answer);
          }}
        >
          <h2 id="askTitle" className="hud-title mb-2 text-base">
            JARVIS asks
          </h2>
          <p className="mb-2">{req.question}</p>
          <input
            type="text"
            className="w-full"
            value={answer}
            autoFocus
            onChange={(e) => setAnswer(e.target.value)}
            aria-label="Your answer"
          />
          <div className="mt-4 flex justify-end gap-2">
            <button className="btn" type="button" onClick={() => reply("")}>
              Skip
            </button>
            <button className="btn btn-primary" type="submit">
              Answer
            </button>
          </div>
        </form>
      ) : null}
    </Dialog>
  );
}

export function Toast() {
  const { state, dispatch } = useApp();
  const t = state.toast;
  useEffect(() => {
    if (!t) return;
    const timer = window.setTimeout(() => dispatch({ type: "toast.clear", id: t.id }), 6000);
    return () => window.clearTimeout(timer);
  }, [t, dispatch]);
  if (!t) return null;
  return (
    <div
      role="alert"
      className="fixed bottom-24 left-1/2 z-50 max-w-[min(560px,calc(100vw-32px))] -translate-x-1/2 rounded border border-danger bg-deep px-4 py-2 shadow-[0_0_20px_rgb(255_59_92/0.3)]"
    >
      {t.text}
    </div>
  );
}

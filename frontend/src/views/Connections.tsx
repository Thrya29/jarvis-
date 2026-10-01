import { useEffect, useState } from "react";
import { api, del, errorText, post, put } from "../lib/api";
import type { Account, ConnectionsStatus, DocumentsStatus, SettingsView } from "../lib/types";
import { useApp } from "../state";

const DEFAULT_CAPS = ["mail_read", "mail_draft", "calendar_read"];
const PROVIDER_NAMES: Record<string, string> = { microsoft: "Microsoft", google: "Google", imap: "Email (IMAP)" };

function Caps({ caps, labels, ticked, onChange }: {
  caps: string[]; labels: Record<string, string>; ticked: string[]; onChange: (t: string[]) => void;
}) {
  return (
    <div className="my-2">
      {caps.map((c) => (
        <label key={c} className="choice my-0.5 text-sm">
          <input
            type="checkbox"
            checked={ticked.includes(c)}
            onChange={(e) => onChange(e.target.checked ? [...ticked, c] : ticked.filter((x) => x !== c))}
          />
          <span>{labels[c] || c}</span>
        </label>
      ))}
    </div>
  );
}

function docsText(d: DocumentsStatus | null): string {
  if (!d) return "";
  if (!d.enabled) return "“Ask my documents” is off. Turn it on in Settings → Features.";
  if (d.state === "downloading") return d.total ? `Downloading the search model… ${Math.round((100 * (d.done || 0)) / d.total)}%` : "Downloading the search model…";
  if (d.state === "indexing") return d.total ? `Indexing your files… ${d.done} of ${d.total}` : "Looking for new or changed files…";
  if (d.state === "error") return `Indexing failed: ${d.error}`;
  if (d.files !== undefined) return `${d.files} files indexed${d.failed ? ` (${d.failed} couldn't be read)` : ""}. JARVIS re-checks for changes every 30 minutes.`;
  return d.model_ready === false ? "The search model will download shortly." : "Ready.";
}

export function ConnectionsView() {
  const { state, dispatch, toast } = useApp();
  const status = state.connections;
  const [caps, setCaps] = useState<Record<string, string[]>>({ microsoft: DEFAULT_CAPS, google: DEFAULT_CAPS, imap: ["mail_read", "mail_draft"] });
  const [apps, setApps] = useState({ microsoft_client_id: "", microsoft_tenant: "common", google_client_id: "", google_client_secret: "" });
  const [imap, setImap] = useState({ email: "", username: "", password: "", imap_host: "", imap_port: 993, smtp_host: "", smtp_port: 465 });
  const [imapBusy, setImapBusy] = useState(false);

  useEffect(() => {
    Promise.all([api<ConnectionsStatus>("/v1/connections"), api<SettingsView>("/v1/settings"), api<DocumentsStatus>("/v1/documents")])
      .then(([c, s, d]) => {
        dispatch({ type: "connections", status: c, result: null });
        dispatch({ type: "docs", docs: d });
        setApps(s.connections);
      })
      .catch((err) => toast(errorText(err)));
  }, [dispatch, toast]);

  const result = (text: string, ok: boolean, waiting?: boolean) => dispatch({ type: "conn.result", text, ok, waiting });

  async function signIn(provider: string) {
    const ticked = caps[provider];
    if (!ticked.length) return result("Tick at least one thing JARVIS may do.", false);
    try {
      dispatch({ type: "connections", status: await post<ConnectionsStatus>(`/v1/connections/${provider}/connect`, { capabilities: ticked }), result: null });
    } catch (err) {
      result(errorText(err), false);
    }
  }
  async function cancel() {
    try {
      await post("/v1/connections/cancel");
    } catch (err) {
      toast(errorText(err));
    }
    result("Sign-in cancelled.", false, false);
  }
  async function connectImap() {
    setImapBusy(true);
    result("Checking the mail server…", true);
    try {
      const c = await post<ConnectionsStatus>("/v1/connections/imap", { ...imap, capabilities: caps.imap });
      dispatch({ type: "connections", status: c, result: { text: `Connected ${imap.email}.`, ok: true } });
      setImap({ ...imap, password: "" });
    } catch (err) {
      result(errorText(err), false);
    } finally {
      setImapBusy(false);
    }
  }
  async function disconnect(a: Account) {
    if (!confirm(`Disconnect ${a.email}? JARVIS deletes its access on this PC.`)) return;
    try {
      const c = await del<ConnectionsStatus>(`/v1/connections/${encodeURIComponent(a.id)}`);
      dispatch({ type: "connections", status: c, result: { text: `Disconnected ${a.email}.`, ok: true } });
    } catch (err) {
      result(errorText(err), false);
    }
  }
  async function saveApps() {
    try {
      await put("/v1/settings", { connections: { ...apps, microsoft_tenant: apps.microsoft_tenant.trim() || "common" } });
      dispatch({ type: "connections", status: await api<ConnectionsStatus>("/v1/connections"), result: { text: "Saved.", ok: true } });
    } catch (err) {
      result(errorText(err), false);
    }
  }
  async function reindex() {
    try {
      await post("/v1/documents/sync");
    } catch (err) {
      toast(errorText(err));
    }
  }

  if (!status) return <div className="m-auto text-muted">Loading…</div>;
  const card = "hud-panel p-4";
  const providers: [string, string, string, string][] = [
    ["microsoft", "Microsoft 365 / Outlook", "Outlook mail, calendar and OneDrive / SharePoint files. Work or personal account.", "Sign in with Microsoft"],
    ["google", "Google", "Gmail, Google Calendar and Google Drive.", "Sign in with Google"],
  ];

  return (
    <div className="flex-1 overflow-auto px-[clamp(12px,4vw,48px)] py-6">
      <div className="mx-auto max-w-4xl">
        <h1 className="hud-title text-lg">Connections</h1>
        <p className="hint mt-1">
          Connect only what you want, and tick only what JARVIS may do. You sign in on Microsoft's or Google's own page,
          so JARVIS never sees your password. Access tokens stay encrypted on this PC. Sending email and inviting people
          always asks you first.
        </p>

        <section className="mt-4 flex flex-col gap-2" aria-label="Connected accounts">
          {status.accounts.length ? (
            status.accounts.map((a) => (
              <div key={a.id} className="hud-panel flex items-center gap-3 px-4 py-2.5">
                <span className="dot text-ok" />
                <span className="min-w-0 flex-1" style={{ overflowWrap: "anywhere" }}>
                  {a.email}
                  <span className="block text-xs text-muted">
                    {PROVIDER_NAMES[a.provider] || a.provider} · {a.capabilities.map((c) => status.capabilities[c] || c).join(", ")}
                  </span>
                </span>
                <button className="btn btn-sm" type="button" onClick={() => void disconnect(a)}>Disconnect</button>
              </div>
            ))
          ) : (
            <p className="hint">No accounts connected yet.</p>
          )}
        </section>

        {state.connWaiting ? (
          <div className="mt-3 flex items-center gap-3 text-cyan">
            <span className="dot pulse" />
            <span>Finish signing in in your browser…</span>
            <button className="btn btn-sm" type="button" onClick={() => void cancel()}>Cancel</button>
          </div>
        ) : null}
        {state.connResult ? (
          <p className={`mt-2 ${state.connResult.ok ? "result-ok" : "result-bad"}`} role="status">{state.connResult.text}</p>
        ) : null}

        <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-2">
          {providers.map(([p, title, blurb, button]) => {
            const ready = status.apps[p as "microsoft" | "google"];
            return (
              <section key={p} className={card}>
                <h2 className="m-0 font-[family-name:var(--font-hud)] text-lg font-semibold">{title}</h2>
                <p className="hint m-0">{blurb}</p>
                <Caps caps={status.provider_capabilities[p] || []} labels={status.capabilities} ticked={caps[p]} onChange={(t) => setCaps({ ...caps, [p]: t })} />
                {!ready ? <p className="hint">Sign-in isn't set up in this copy of JARVIS yet — see Advanced below.</p> : null}
                <button className="btn btn-primary" type="button" disabled={!ready || state.connWaiting} onClick={() => void signIn(p)}>
                  {button}
                </button>
              </section>
            );
          })}
        </div>

        <details className="mt-4 border-t border-line pt-3">
          <summary className="cursor-pointer font-semibold">Other email (Zoho, company mail server…) via IMAP</summary>
          <p className="hint">Use an <em>app password</em> from your email provider, not your normal password.</p>
          <div className="grid grid-cols-1 gap-x-3 sm:grid-cols-2">
            <label className="field">Email address<input type="email" autoComplete="off" value={imap.email} onChange={(e) => setImap({ ...imap, email: e.target.value })} /></label>
            <label className="field">Username (if different)<input type="text" autoComplete="off" value={imap.username} onChange={(e) => setImap({ ...imap, username: e.target.value })} /></label>
            <label className="field">App password<input type="password" autoComplete="new-password" value={imap.password} onChange={(e) => setImap({ ...imap, password: e.target.value })} /></label>
            <label className="field">Incoming (IMAP) server<input type="text" placeholder="imap.example.com" value={imap.imap_host} onChange={(e) => setImap({ ...imap, imap_host: e.target.value })} /></label>
            <label className="field">IMAP port<input type="number" min={1} max={65535} value={imap.imap_port} onChange={(e) => setImap({ ...imap, imap_port: Number(e.target.value) })} /></label>
            <label className="field">Outgoing (SMTP) server<input type="text" placeholder="smtp.example.com (optional)" value={imap.smtp_host} onChange={(e) => setImap({ ...imap, smtp_host: e.target.value })} /></label>
            <label className="field">SMTP port<input type="number" min={1} max={65535} value={imap.smtp_port} onChange={(e) => setImap({ ...imap, smtp_port: Number(e.target.value) })} /></label>
          </div>
          <Caps caps={status.provider_capabilities.imap || []} labels={status.capabilities} ticked={caps.imap} onChange={(t) => setCaps({ ...caps, imap: t })} />
          <button className="btn btn-primary" type="button" disabled={imapBusy} onClick={() => void connectImap()}>Connect</button>
        </details>

        <details className="mt-4 border-t border-line pt-3">
          <summary className="cursor-pointer font-semibold">Advanced: app registration</summary>
          <p className="hint">
            “Sign in with Microsoft/Google” needs this copy of JARVIS to be registered with them once (free). Your IT team
            or whoever set up JARVIS fills these in — see <code>docs/connections-setup.md</code>. These are app IDs, not passwords.
          </p>
          <div className="grid grid-cols-1 gap-x-3 sm:grid-cols-2">
            <label className="field">Microsoft application (client) ID<input type="text" spellCheck={false} value={apps.microsoft_client_id} onChange={(e) => setApps({ ...apps, microsoft_client_id: e.target.value })} /></label>
            <label className="field">Microsoft tenant<input type="text" spellCheck={false} placeholder="common" value={apps.microsoft_tenant} onChange={(e) => setApps({ ...apps, microsoft_tenant: e.target.value })} /></label>
            <label className="field">Google client ID<input type="text" spellCheck={false} value={apps.google_client_id} onChange={(e) => setApps({ ...apps, google_client_id: e.target.value })} /></label>
            <label className="field">Google client secret<input type="text" spellCheck={false} value={apps.google_client_secret} onChange={(e) => setApps({ ...apps, google_client_secret: e.target.value })} /></label>
          </div>
          <button className="btn" type="button" onClick={() => void saveApps()}>Save</button>
        </details>

        <section className="mt-4 border-t border-line pt-3">
          <h2 className="m-0 font-[family-name:var(--font-hud)] text-lg font-semibold">Your documents</h2>
          <p className="hint">{docsText(state.docs)}</p>
          {state.docs?.enabled ? <button className="btn btn-sm" type="button" onClick={() => void reindex()}>Re-index now</button> : null}
        </section>
      </div>
    </div>
  );
}

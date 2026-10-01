import { useEffect, useState, type ReactNode } from "react";
import { api, errorText, post, put } from "../lib/api";
import type { SettingsView } from "../lib/types";
import { useApp } from "../state";

const STEPS = ["About you", "Personality", "AI model", "Voice", "Features", "Budget", "Review"];
const VOICE_LABELS: Record<string, string> = {
  british_male: "British male",
  british_female: "British female",
  american_male: "American male",
  american_female: "American female",
};

type Form = SettingsView & { voiceOn: boolean; docsFolders: string };

function Radio({ name, value, checked, onChange, children }: {
  name: string; value: string; checked: boolean; onChange: (v: string) => void; children: ReactNode;
}) {
  return (
    <label className="choice">
      <input type="radio" name={name} value={value} checked={checked} onChange={() => onChange(value)} />
      <span>{children}</span>
    </label>
  );
}

function Check({ checked, onChange, children }: { checked: boolean; onChange: (v: boolean) => void; children: ReactNode }) {
  return (
    <label className="choice">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span>{children}</span>
    </label>
  );
}

function Result({ text, ok }: { text: string; ok?: boolean }) {
  return (
    <p className={`mt-1.5 min-h-6 ${ok === true ? "result-ok" : ok === false ? "result-bad" : ""}`} role="status">
      {text}
    </p>
  );
}

export function Wizard() {
  const { state, dispatch, toast } = useApp();
  const setup = state.status?.setup;
  const [form, setForm] = useState<Form | null>(null);
  const [step, setStep] = useState(state.wizard.step);
  const [model, setModel] = useState({ provider: "anthropic", key: "", ollama: "qwen2.5:3b", text: "", ok: undefined as boolean | undefined, busy: false });
  const [llmReady, setLlmReady] = useState(false);
  const [finish, setFinish] = useState({ text: "", ok: undefined as boolean | undefined, busy: false, pendingVoice: false });

  // Load settings whenever the wizard opens.
  useEffect(() => {
    let alive = true;
    api<SettingsView>("/v1/settings")
      .then((cfg) => {
        if (!alive) return;
        const s = state.status?.setup;
        setForm({
          ...cfg,
          voiceOn: Boolean(s?.voice_enabled),
          docsFolders: (cfg.features.documents.folders.length ? cfg.features.documents.folders : s?.allowed_roots || []).join("\n"),
        });
        setModel((m) => ({
          ...m,
          provider: s?.provider || "anthropic",
          text: s?.llm_ready ? (s.provider === "anthropic" ? "✔ Claude is set up." : "✔ Ollama is selected.") : "",
          ok: s?.llm_ready ? true : undefined,
        }));
        setLlmReady(Boolean(s?.llm_ready));
      })
      .catch((err) => toast(`Settings: ${errorText(err)}`));
    setStep(state.wizard.step);
    return () => {
      alive = false;
    };
  }, []);

  // Voice models finished downloading after "Finish".
  useEffect(() => {
    if (!state.voiceSetup.readyAt || !finish.pendingVoice) return;
    setFinish((f) => ({ ...f, pendingVoice: false }));
    post("/v1/voice", { enabled: true })
      .catch((err) => toast(errorText(err)))
      .finally(close);
  }, [state.voiceSetup.readyAt]);

  if (!form) return null;
  const f = form.features;
  const set = (patch: Partial<Form>) => setForm({ ...form, ...patch });
  const setProfile = (patch: Partial<Form["profile"]>) => set({ profile: { ...form.profile, ...patch } });
  const setPersona = (patch: Partial<Form["persona"]>) => set({ persona: { ...form.persona, ...patch } });
  const setFeatures = (patch: Partial<Form["features"]>) => set({ features: { ...f, ...patch } });

  function changes() {
    const folders = form!.docsFolders.split("\n").map((x) => x.trim()).filter(Boolean);
    return {
      profile: {
        name: form!.profile.name.trim(),
        address: form!.profile.address || "none",
        nickname: form!.profile.nickname.trim(),
        location: form!.profile.location.trim(),
      },
      persona: form!.persona,
      voice: { tts_voice: form!.voice.tts_voice, tts_speed: Number(form!.voice.tts_speed) },
      features: { ...form!.features, documents: { enabled: f.documents.enabled, folders } },
      budget: { daily_usd: Number(form!.budget.daily_usd || 0), background: form!.budget.background || "fast" },
    };
  }

  function problem(): string | null {
    const p = form!.profile;
    if (step === 1 && p.address === "nickname" && !p.nickname.trim()) return "Enter a nickname, or pick another option.";
    if (step === 1 && p.address === "name" && !p.name.trim()) return "Enter your name, or pick another option.";
    if (step === 3 && !llmReady) return "Save and check an AI model first.";
    if (step === 5 && f.smart_home.enabled && !f.smart_home.url.trim()) return "Enter your Home Assistant address, or untick smart home.";
    if (step === 5 && f.maps.enabled && !p.location.trim()) return "Live maps need your city — add it under About you.";
    return null;
  }

  async function saveModel() {
    setModel((m) => ({ ...m, text: "Checking…", ok: undefined, busy: true }));
    try {
      if (model.provider === "anthropic") {
        const key = model.key.trim();
        if (!key && setup?.api_key_set) await post("/v1/setup/provider", { provider: "anthropic" });
        else await post("/v1/setup/api-key", { key });
        setModel((m) => ({ ...m, key: "", text: "✔ Claude is set up.", ok: true, busy: false }));
      } else {
        await post("/v1/setup/provider", { provider: "ollama", ollama_model: model.ollama.trim() });
        setModel((m) => ({ ...m, text: "✔ Ollama is reachable.", ok: true, busy: false }));
      }
      setLlmReady(true);
      dispatch({ type: "refresh", status: true });
    } catch (err) {
      setModel((m) => ({ ...m, text: errorText(err), ok: false, busy: false }));
    }
  }

  async function preview(id: string) {
    dispatch({ type: "voiceSetup", patch: { text: `Preparing ${VOICE_LABELS[id] || id}… (first time downloads ~63 MB)`, ok: undefined } });
    try {
      await post("/v1/voice/preview", { voice: id, speed: Number(form!.voice.tts_speed) });
      dispatch({ type: "voiceSetup", patch: { text: `Played ${VOICE_LABELS[id] || id}.`, progress: null } });
    } catch (err) {
      dispatch({ type: "voiceSetup", patch: { text: errorText(err), ok: false } });
    }
  }

  function close() {
    dispatch({ type: "wizard", open: false });
    if ("Notification" in window && Notification.permission === "default") void Notification.requestPermission();
    dispatch({ type: "refresh", status: true, lists: true });
  }

  async function finishWizard() {
    setFinish({ text: "Saving…", ok: undefined, busy: true, pendingVoice: false });
    try {
      const body = changes();
      await put("/v1/settings", { ...body, profile: { ...body.profile, onboarded: true } });
      const status = await api<{ setup: { voice_models_ready: boolean; voice_enabled: boolean } }>("/v1/status");
      if (form!.voiceOn && !status.setup.voice_models_ready) {
        setFinish({ text: "Downloading voice models…", ok: undefined, busy: true, pendingVoice: true });
        await post("/v1/setup/voice-models");
        return; // continues when setup.voice_ready arrives
      }
      if (form!.voiceOn !== Boolean(status.setup.voice_enabled)) await post("/v1/voice", { enabled: form!.voiceOn });
      close();
    } catch (err) {
      setFinish({ text: errorText(err), ok: false, busy: false, pendingVoice: false });
    }
  }

  const next = () => {
    const p = problem();
    if (p) return toast(p);
    if (step < STEPS.length) return setStep(step + 1);
    void finishWizard();
  };

  const addressText: Record<string, string> = {
    sir: "Sir", maam: "Ma'am", name: "First name", nickname: `“${form.profile.nickname}”`, none: "No title",
  };
  const featureNames = Object.entries({
    "Web research": f.web_research,
    Documents: f.documents.enabled,
    "Email & calendar": f.email.enabled,
    "Live maps": f.maps.enabled,
    Protocols: f.protocols.enabled,
    HUD: f.hud.enabled,
    Phone: f.phone.enabled,
    Helpers: f.helpers,
    "Smart home": f.smart_home.enabled,
    Webcam: f.webcam,
  }).filter(([, v]) => v).map(([k]) => k);

  const canClose = Boolean(setup?.onboarded && setup?.llm_ready);
  const vs = state.voiceSetup;
  const featureRow = "border-t border-line py-1.5";

  return (
    <section className="fixed inset-0 z-40 overflow-auto bg-void/95 px-4 py-8" aria-label="Setup">
      <div className="hud-panel mx-auto max-w-2xl px-7 py-6">
        <div className="flex items-center justify-between gap-3">
          <h1 className="m-0 font-[family-name:var(--font-hud)] text-2xl font-bold tracking-[0.12em] uppercase">Set up JARVIS</h1>
          <ol className="m-0 flex list-none gap-1.5 p-0" aria-hidden="true">
            {STEPS.map((_, i) => (
              <li
                key={i}
                className={`h-2 w-6 rounded-sm ${i + 1 < step ? "bg-cyan-dim" : i + 1 === step ? "bg-cyan shadow-[0_0_8px_var(--color-cyan)]" : "bg-line"}`}
              />
            ))}
          </ol>
        </div>
        <p className="hud-label mt-1">
          Step {step} of {STEPS.length} · {STEPS[step - 1]}
        </p>

        {step === 1 && (
          <div>
            <label className="field">
              Your name
              <input type="text" maxLength={60} autoComplete="name" placeholder="e.g. Priya Sharma" value={form.profile.name} onChange={(e) => setProfile({ name: e.target.value })} />
            </label>
            <fieldset className="my-3 border-0 p-0">
              <legend className="mb-1 font-semibold">How should JARVIS address you?</legend>
              {[["sir", "Sir"], ["maam", "Ma'am"], ["name", "By my first name"]].map(([v, l]) => (
                <Radio key={v} name="address" value={v} checked={form.profile.address === v} onChange={(x) => setProfile({ address: x })}>{l}</Radio>
              ))}
              <label className="choice">
                <input type="radio" name="address" value="nickname" checked={form.profile.address === "nickname"} onChange={() => setProfile({ address: "nickname" })} />
                <span>A nickname:</span>
                <input type="text" maxLength={40} className="ml-1 w-44 py-0.5" aria-label="Nickname" value={form.profile.nickname} onFocus={() => setProfile({ address: "nickname" })} onChange={(e) => setProfile({ nickname: e.target.value, address: "nickname" })} />
              </label>
              <Radio name="address" value="none" checked={form.profile.address === "none"} onChange={(x) => setProfile({ address: x })}>No title or name</Radio>
            </fieldset>
            <label className="field">
              Your city (for weather and the live maps — optional)
              <input type="text" maxLength={100} placeholder="e.g. Bengaluru, India" value={form.profile.location} onChange={(e) => setProfile({ location: e.target.value })} />
            </label>
            <p className="hint">Only the city's coordinates are used, to fetch weather and nearby flights.</p>
          </div>
        )}

        {step === 2 && (
          <div>
            <fieldset className="my-3 border-0 p-0">
              <legend className="mb-1 font-semibold">Style</legend>
              {[
                ["jarvis", "JARVIS", "calm, precise, quietly witty"],
                ["professional", "Professional", "courteous and to the point"],
                ["friendly", "Friendly", "warm and encouraging"],
                ["minimal", "Minimal", "results only, no small talk"],
              ].map(([v, t, d]) => (
                <Radio key={v} name="style" value={v} checked={form.persona.style === v} onChange={(x) => setPersona({ style: x })}>
                  <strong>{t}</strong> — {d}
                </Radio>
              ))}
            </fieldset>
            <fieldset className="my-3 border-0 p-0">
              <legend className="mb-1 font-semibold">Behaviour</legend>
              <Check checked={form.persona.pushback} onChange={(v) => setPersona({ pushback: v })}>Warn me before I do something unwise</Check>
              <Check checked={form.persona.progress_updates} onChange={(v) => setPersona({ progress_updates: v })}>Give short spoken progress updates during long tasks</Check>
              <Check checked={form.persona.quick_replies} onChange={(v) => setPersona({ quick_replies: v })}>Answer quick questions instantly with a faster model</Check>
            </fieldset>
          </div>
        )}

        {step === 3 && (
          <div>
            <Radio name="provider" value="anthropic" checked={model.provider === "anthropic"} onChange={(v) => setModel({ ...model, provider: v })}>
              <strong>Claude</strong> (recommended) — best at planning and operating your screen. Needs an Anthropic API key.
            </Radio>
            <Radio name="provider" value="ollama" checked={model.provider === "ollama"} onChange={(v) => setModel({ ...model, provider: v })}>
              <strong>Ollama</strong> — fully offline, runs on this PC. Much weaker at long tasks; no screen vision.
            </Radio>
            {model.provider === "anthropic" ? (
              <div className="my-2">
                <input className="w-full" type="password" placeholder={setup?.api_key_set ? "Key saved — leave empty to keep it" : "sk-ant-…"} autoComplete="off" spellCheck={false} aria-label="Anthropic API key" value={model.key} onChange={(e) => setModel({ ...model, key: e.target.value })} />
                <p className="hint">Stored in Windows Credential Manager, never in a file. Get one at console.anthropic.com → API Keys.</p>
              </div>
            ) : (
              <div className="my-2">
                <input className="w-full" type="text" aria-label="Ollama model" value={model.ollama} onChange={(e) => setModel({ ...model, ollama: e.target.value })} />
                <p className="hint">Install Ollama and run <code>ollama pull qwen2.5:3b</code> first.</p>
              </div>
            )}
            <button className="btn btn-primary" type="button" disabled={model.busy} onClick={() => void saveModel()}>Save and check</button>
            <Result text={model.text} ok={model.ok} />
          </div>
        )}

        {step === 4 && (
          <div>
            <p className="mb-1 font-semibold">JARVIS's voice</p>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              {form.voices.map((id) => (
                <div key={id} className={`flex items-center gap-2 rounded border px-3 py-2.5 ${form.voice.tts_voice === id ? "border-cyan bg-cyan/10" : "border-line bg-panel-2/50"}`}>
                  <label className="flex flex-1 cursor-pointer items-center gap-2">
                    <input type="radio" name="voice" checked={form.voice.tts_voice === id} onChange={() => set({ voice: { ...form.voice, tts_voice: id } })} />
                    <span>{VOICE_LABELS[id] || id}</span>
                  </label>
                  <button className="btn btn-sm" type="button" onClick={() => void preview(id)}>▶ Preview</button>
                </div>
              ))}
            </div>
            <label className="field">
              Speaking speed {Number(form.voice.tts_speed).toFixed(2)}×
              <input type="range" min={0.8} max={1.3} step={0.05} value={form.voice.tts_speed} onChange={(e) => set({ voice: { ...form.voice, tts_speed: Number(e.target.value) } })} />
            </label>
            <Check checked={form.voiceOn} onChange={(v) => set({ voiceOn: v })}>
              Hands-free: listen for “Hey Jarvis” (voice models are downloaded when you finish, ~130 MB)
            </Check>
            <Result text={vs.text} ok={vs.ok} />
            {vs.progress !== null ? <progress className="w-full" max={100} value={vs.progress} /> : null}
          </div>
        )}

        {step === 5 && (
          <div>
            <p className="hint">Tick what you want. Features marked with a version arrive in that update — your choices are saved now and switch on when it's installed.</p>
            <div className="flex flex-col">
              <div className={featureRow}>
                <Check checked={f.web_research} onChange={(v) => setFeatures({ web_research: v })}>
                  <strong>Web search &amp; research</strong> with sources (about $0.01 per search)
                </Check>
              </div>
              <div className={featureRow}>
                <Check checked={f.documents.enabled} onChange={(v) => setFeatures({ documents: { ...f.documents, enabled: v } })}>
                  <strong>Ask my documents</strong> — search and answer from your files (indexed on this PC; ~35 MB model)
                </Check>
                {f.documents.enabled && (
                  <label className="field ml-7">
                    Folders to index (one per line)
                    <textarea rows={3} spellCheck={false} value={form.docsFolders} onChange={(e) => set({ docsFolders: e.target.value })} />
                  </label>
                )}
              </div>
              <div className={featureRow}>
                <Check checked={f.email.enabled} onChange={(v) => setFeatures({ email: { ...f.email, enabled: v } })}>
                  <strong>Email, calendar &amp; cloud files</strong> — connect accounts on the <em>Connections</em> page
                </Check>
                {f.email.enabled && f.email.account === "work" && (
                  <p className="ml-7 text-sm text-amber">Check with your IT team before connecting a work account to an AI service.</p>
                )}
              </div>
              <div className={featureRow}>
                <Check checked={f.maps.enabled} onChange={(v) => setFeatures({ maps: { ...f.maps, enabled: v } })}>
                  <strong>Live maps</strong> — planes overhead, who your PC talks to, weather &amp; today's agenda
                </Check>
                {f.maps.enabled && (
                  <div className="ml-7">
                    <Check checked={f.maps.sky} onChange={(v) => setFeatures({ maps: { ...f.maps, sky: v } })}>Live sky — aircraft near you (OpenSky Network)</Check>
                    {f.maps.sky && (
                      <label className="field ml-7">
                        Range: {f.maps.sky_radius_km} km
                        <input type="range" min={20} max={400} step={10} value={f.maps.sky_radius_km} onChange={(e) => setFeatures({ maps: { ...f.maps, sky_radius_km: Number(e.target.value) } })} />
                      </label>
                    )}
                    <Check checked={f.maps.network} onChange={(v) => setFeatures({ maps: { ...f.maps, network: v } })}>Network security map — programs connected to the internet, by country (looked up on this PC)</Check>
                    <Check checked={f.maps.situation} onChange={(v) => setFeatures({ maps: { ...f.maps, situation: v } })}>Situation — weather and today's calendar</Check>
                  </div>
                )}
              </div>
              <div className={featureRow}>
                <Check checked={f.protocols.enabled} onChange={(v) => setFeatures({ protocols: { ...f.protocols, enabled: v } })}>
                  <strong>Protocols &amp; daily briefing</strong> — scheduled and triggered workflows <span className="badge">v2.4</span>
                </Check>
                {f.protocols.enabled && (
                  <label className="field ml-7">
                    Morning briefing at
                    <input type="time" value={f.protocols.briefing_time || ""} onChange={(e) => setFeatures({ protocols: { ...f.protocols, briefing_time: e.target.value || null } })} />
                  </label>
                )}
              </div>
              <div className={featureRow}>
                <Check checked={f.hud.enabled} onChange={(v) => setFeatures({ hud: { ...f.hud, enabled: v } })}>
                  <strong>Floating overlay</strong> — a see-through, always-on-top status panel in the corner of your screen. Clicks pass through it; press Ctrl+Alt+O to approve from it, Ctrl+Alt+H to hide it
                </Check>
              </div>
              <div className={featureRow}>
                <Check checked={f.phone.enabled} onChange={(v) => setFeatures({ phone: { ...f.phone, enabled: v } })}>
                  <strong>Phone companion</strong> — talk to JARVIS and approve actions from your phone <span className="badge">v2.5</span>
                </Check>
                {f.phone.enabled && (
                  <div className="ml-7">
                    <Radio name="phone" value="android" checked={f.phone.platform === "android"} onChange={(v) => setFeatures({ phone: { ...f.phone, platform: v } })}>Android</Radio>
                    <Radio name="phone" value="iphone" checked={f.phone.platform === "iphone"} onChange={(v) => setFeatures({ phone: { ...f.phone, platform: v } })}>iPhone</Radio>
                  </div>
                )}
              </div>
              <div className={featureRow}>
                <Check checked={f.helpers} onChange={(v) => setFeatures({ helpers: v })}>
                  <strong>Parallel helpers</strong> — split big research tasks across several agents <span className="badge">v2.6</span>
                </Check>
              </div>
              <div className={featureRow}>
                <Check checked={f.smart_home.enabled} onChange={(v) => setFeatures({ smart_home: { ...f.smart_home, enabled: v } })}>
                  <strong>Smart home</strong> via Home Assistant <span className="badge">v2.7</span>
                </Check>
                {f.smart_home.enabled && (
                  <label className="field ml-7">
                    Home Assistant address
                    <input type="url" placeholder="http://homeassistant.local:8123" value={f.smart_home.url} onChange={(e) => setFeatures({ smart_home: { ...f.smart_home, url: e.target.value } })} />
                  </label>
                )}
              </div>
              <div className={featureRow}>
                <Check checked={f.webcam} onChange={(v) => setFeatures({ webcam: v })}>
                  <strong>Webcam vision</strong> — “what am I holding?” (off unless you tick it) <span className="badge">v2.7</span>
                </Check>
              </div>
            </div>
          </div>
        )}

        {step === 6 && (
          <div>
            <label className="field">
              Daily API spending limit (USD, 0 = no limit)
              <input type="number" min={0} max={1000} step={0.5} value={form.budget.daily_usd} onChange={(e) => set({ budget: { ...form.budget, daily_usd: Number(e.target.value) } })} />
            </label>
            <p className="hint">An estimate from token usage; JARVIS pauses until midnight when it's reached. Set a hard limit in the Anthropic console too.</p>
            <fieldset className="my-3 border-0 p-0">
              <legend className="mb-1 font-semibold">Background work (briefings, triggers, helpers) uses</legend>
              <Radio name="background" value="fast" checked={form.budget.background === "fast"} onChange={(v) => set({ budget: { ...form.budget, background: v } })}>The faster, cheaper model (recommended)</Radio>
              <Radio name="background" value="main" checked={form.budget.background === "main"} onChange={(v) => set({ budget: { ...form.budget, background: v } })}>The main model</Radio>
            </fieldset>
          </div>
        )}

        {step === 7 && (
          <div>
            <dl className="mt-3 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1.5">
              {[
                ["Name", form.profile.name || "—"],
                ["Address me as", addressText[form.profile.address] || "—"],
                ["City", form.profile.location || "—"],
                ["Personality", form.persona.style],
                ["Model", setup?.model || "—"],
                ["Voice", `${VOICE_LABELS[form.voice.tts_voice] || form.voice.tts_voice}, ${Number(form.voice.tts_speed).toFixed(2)}×${form.voiceOn ? ", hands-free on" : ""}`],
                ["Features", featureNames.length ? featureNames.join(", ") : "Core only"],
                ["Daily limit", form.budget.daily_usd ? `$${Number(form.budget.daily_usd).toFixed(2)}` : "No limit"],
              ].map(([k, v]) => (
                <div key={k} className="contents">
                  <dt className="hud-label pt-0.5">{k}</dt>
                  <dd className="m-0" style={{ overflowWrap: "anywhere" }}>{v}</dd>
                </div>
              ))}
            </dl>
            <p className="hint mt-4">
              JARVIS only touches files in: {(setup?.allowed_roots || []).join(", ")}. It asks before deleting, overwriting,
              running commands, web requests and anything on screen. Press <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>J</kbd> to stop it instantly.
            </p>
            <Result text={finish.pendingVoice && vs.text ? vs.text : finish.text} ok={finish.ok} />
          </div>
        )}

        <div className="mt-6 flex items-center gap-2">
          <button className="btn" type="button" disabled={step === 1} onClick={() => setStep(step - 1)}>Back</button>
          <span className="flex-1" />
          {canClose ? (
            <button className="btn btn-ghost" type="button" onClick={close}>Close</button>
          ) : null}
          <button className="btn btn-primary" type="button" disabled={finish.busy} onClick={next}>
            {step === STEPS.length ? "Finish" : "Next"}
          </button>
        </div>
      </div>
    </section>
  );
}

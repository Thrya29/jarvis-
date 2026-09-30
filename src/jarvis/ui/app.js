// JARVIS desktop UI. Talks to the local daemon: REST for data/settings, WebSocket for
// tasks. Every piece of data is rendered with textContent - never innerHTML - so
// nothing a task produces (file names, web text, model output) can inject markup.
"use strict";

(() => {
  // ------------------------------------------------------------------ token
  // Delivered in the URL fragment (never sent to the server or logged), then removed
  // from the address bar and kept for this window only.
  const fromHash = new URLSearchParams(location.hash.slice(1)).get("token");
  if (fromHash) {
    sessionStorage.setItem("jarvis-token", fromHash);
    history.replaceState(null, "", location.pathname);
  }
  const TOKEN = sessionStorage.getItem("jarvis-token") || "";

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const radio = (name) => document.querySelector(`input[name="${name}"]:checked`)?.value;
  const setRadio = (name, value) => {
    for (const r of document.querySelectorAll(`input[name="${name}"]`)) r.checked = r.value === value;
  };

  const state = { ws: null, retry: 0, busy: false, status: null, settings: null, streaming: null };

  // ------------------------------------------------------------------ REST
  async function api(path, options = {}) {
    const res = await fetch(path, {
      ...options,
      headers: {
        Authorization: `Bearer ${TOKEN}`,
        ...(options.body ? { "Content-Type": "application/json" } : {}),
      },
    });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const body = await res.json();
        detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      } catch { /* not JSON */ }
      throw new Error(detail);
    }
    return res.status === 204 ? null : res.json();
  }
  const post = (path, body) => api(path, { method: "POST", body: body ? JSON.stringify(body) : undefined });

  // ------------------------------------------------------------------ toast
  let toastTimer = 0;
  function toast(message) {
    const t = $("toast");
    t.textContent = message;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 6000);
  }

  // ------------------------------------------------------------------ chat log
  function addMessage(kind, text, source) {
    $("welcome").hidden = true;
    const m = el("div", `msg msg-${kind}`);
    if (source) m.append(el("span", "source", source));
    const body = el("span", "body", text);
    m.append(body);
    $("log").append(m);
    m.scrollIntoView({ block: "end" });
    return m;
  }

  function streamDelta(text, voice) {
    if (!state.streaming) state.streaming = addMessage("assistant", "", voice ? "🔊 JARVIS" : undefined);
    const body = state.streaming.querySelector(".body");
    body.textContent += text;
    state.streaming.scrollIntoView({ block: "end" });
  }

  function finishStream(fullText) {
    if (state.streaming) {
      state.streaming.querySelector(".body").textContent = fullText;
      state.streaming = null;
      return true;
    }
    return false;
  }

  // ------------------------------------------------------------------ plan + activity
  const MARKS = { done: "✔", in_progress: "▶", failed: "✖", skipped: "–", pending: "○" };

  function renderPlan(steps) {
    const list = $("planList");
    list.replaceChildren();
    if (!steps || !steps.length) {
      list.append(el("li", "empty", state.busy ? "Planning…" : "No task running."));
      return;
    }
    for (const s of steps) {
      const li = el("li", `step-${s.status || "pending"}`);
      li.append(el("span", "mark", MARKS[s.status] || "○"), el("span", "", s.title));
      list.append(li);
    }
  }

  function activity(text, failed) {
    const list = $("activity");
    list.prepend(el("li", failed ? "fail" : "", text));
    while (list.children.length > 60) list.lastChild.remove();
  }

  function setBusy(busy) {
    state.busy = busy;
    $("stopBtn").hidden = !busy;
    $("sendBtn").disabled = busy;
    $("sendBtn").textContent = busy ? "Working…" : "Send";
  }

  // ------------------------------------------------------------------ websocket
  function connect() {
    const ws = new WebSocket(`ws://${location.host}/v1/ws`);
    state.ws = ws;
    ws.onopen = () => ws.send(JSON.stringify({ type: "auth", token: TOKEN }));
    ws.onmessage = (e) => {
      let msg;
      try { msg = JSON.parse(e.data); } catch { return; }
      handle(msg);
    };
    ws.onclose = (e) => {
      setConn(false);
      if (e.code === 4401) {
        addMessage("error", "This window isn't authorised. Open JARVIS from the Start menu or tray icon.");
        return;
      }
      setTimeout(connect, Math.min(10000, 500 * 2 ** state.retry++));
    };
  }

  function send(msg) {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify(msg));
      return true;
    }
    toast("Not connected to JARVIS yet - retrying…");
    return false;
  }

  function setConn(ok) {
    const c = $("conn");
    c.textContent = ok ? "connected" : "offline";
    c.className = `pill ${ok ? "pill-ok" : "pill-warn"}`;
  }

  function handle(msg) {
    const voice = msg.source === "voice";
    switch (msg.type) {
      case "ready":
        state.retry = 0;
        setConn(true);
        setBusy(Boolean(msg.busy));
        refreshStatus();
        refreshLists();
        break;
      case "task.started":
        setBusy(true);
        state.streaming = null;
        renderPlan([]);
        if (voice) addMessage("user", msg.goal, "🎙 said");
        break;
      case "plan.updated":
        renderPlan(msg.steps);
        break;
      case "assistant.delta":
        streamDelta(msg.text, voice);
        break;
      case "assistant.discard":
        if (state.streaming) { state.streaming.remove(); state.streaming = null; }
        break;
      case "assistant.text":
        if (!(msg.streamed && finishStream(msg.text))) {
          addMessage("assistant", msg.text, voice ? "🔊 JARVIS" : undefined);
        }
        break;
      case "assistant.progress":
        state.streaming = null;
        addMessage("progress", msg.text);
        break;
      case "tool.started":
        state.streaming = null;
        activity(`… ${msg.tool}`);
        break;
      case "tool.finished":
        if (!msg.ok) activity(`✖ ${msg.tool}: ${msg.preview}`, true);
        break;
      case "task.finished":
        setBusy(false);
        state.streaming = null;
        onFinished(msg);
        refreshLists();
        refreshStatus();
        break;
      case "approval.request":
        showApproval(msg);
        break;
      case "ask.request":
        showAsk(msg);
        break;
      case "voice.state":
        renderVoice(msg.state);
        break;
      case "voice.log":
        if (msg.text.startsWith("you (voice)>")) activity(`🎙 ${msg.text.slice(12).trim()}`);
        break;
      case "setup.progress":
        onVoiceProgress(msg);
        break;
      case "setup.voice_ready":
        onVoiceReady();
        break;
      case "setup.error":
        $("voiceResult").textContent = msg.error;
        $("voiceResult").className = "result bad";
        break;
      case "error":
        if (msg.setup_required) openWizard(3);
        addMessage("error", msg.error);
        break;
      default:
        break;
    }
  }

  function onFinished(msg) {
    const notes = {
      cancelled: "Stopped.",
      failed: `Failed: ${msg.summary}`,
      refused: msg.summary,
      limit_reached: msg.summary,
    };
    if (notes[msg.status]) addMessage("system", notes[msg.status]);
    if (msg.tool_calls) {
      const u = msg.usage || {};
      activity(`■ ${msg.status} - ${msg.tool_calls} tool calls, ${(u.input_tokens || 0).toLocaleString()} in / ${(u.output_tokens || 0).toLocaleString()} out tokens`);
    }
  }

  // ------------------------------------------------------------------ approvals
  const RISK_TEXT = {
    read: "Reads information",
    write: "Makes changes",
    execute: "Runs code, uses the network or controls the screen",
    destructive: "Deletes or overwrites",
    external: "Leaves this computer",
  };

  function showApproval(req) {
    const dlg = $("approvalDlg");
    $("approvalRisk").textContent = RISK_TEXT[req.risk] || req.risk;
    $("approvalRisk").className = `risk risk-${req.risk}`;
    $("approvalSummary").textContent = req.summary;
    $("approvalDetails").textContent = req.details || "";
    $("approvalDetails").hidden = !req.details;
    $("approvalNote").value = "";
    dlg.onclose = () => {
      const approved = dlg.returnValue === "allow";
      send({ type: "approval.response", id: req.id, approved, note: $("approvalNote").value });
      activity(`${approved ? "✔ allowed" : "✖ declined"}: ${req.summary}`, !approved);
    };
    dlg.showModal();
    notify("JARVIS needs your approval", req.summary);
  }

  function showAsk(req) {
    const dlg = $("askDlg");
    $("askQuestion").textContent = req.question;
    $("askAnswer").value = "";
    dlg.onclose = () => {
      const answer = dlg.returnValue === "answer" ? $("askAnswer").value : "";
      send({ type: "ask.response", id: req.id, answer });
      if (answer) addMessage("user", answer);
    };
    dlg.showModal();
    $("askAnswer").focus();
    notify("JARVIS has a question", req.question);
  }

  function notify(title, body) {
    if (document.hasFocus() || !("Notification" in window)) return;
    if (Notification.permission === "granted") new Notification(title, { body });
  }

  // ------------------------------------------------------------------ composer
  function submitGoal(text) {
    const goal = text.trim();
    if (!goal || state.busy) return;
    if (send({ type: "task.start", goal })) {
      addMessage("user", goal);
      $("goal").value = "";
      autosize();
    }
  }

  function autosize() {
    const t = $("goal");
    t.style.height = "auto";
    t.style.height = `${Math.min(160, t.scrollHeight)}px`;
  }

  // ------------------------------------------------------------------ side lists
  const empty = (list, text) => list.replaceChildren(el("li", "empty", text));
  const when = (iso) => (iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "");

  async function refreshLists() {
    try {
      const [tasks, memories, workflows] = await Promise.all([
        api("/v1/tasks?limit=40"), api("/v1/memories"), api("/v1/workflows"),
      ]);
      renderTasks(tasks);
      renderMemories(memories);
      renderWorkflows(workflows);
    } catch (err) {
      console.warn("could not load lists", err);
    }
  }

  function smallButton(label, onClick) {
    const b = el("button", "btn btn-small", label);
    b.type = "button";
    b.onclick = onClick;
    return b;
  }

  function renderTasks(tasks) {
    const list = $("historyList");
    if (!tasks.length) return empty(list, "Tasks you give JARVIS appear here.");
    list.replaceChildren();
    for (const t of tasks) {
      const li = el("li");
      li.append(el("span", "", t.goal.split("\n")[0].slice(0, 140)));
      const meta = el("span", "meta");
      meta.append(el("span", `status-${t.status}`, t.status), el("span", "", when(t.started_at)));
      li.append(meta);
      if (t.resumable) {
        const row = el("div", "row-actions");
        row.append(smallButton("Resume", () => {
          if (state.busy) return toast("Wait for the current task to finish.");
          if (send({ type: "task.resume", task_id: t.id })) addMessage("user", `Resume: ${t.goal.split("\n")[0]}`);
        }));
        li.append(row);
      }
      list.append(li);
    }
  }

  function renderMemories(memories) {
    const list = $("memoryList");
    if (!memories.length) return empty(list, "Say “remember that…” and JARVIS will keep it here.");
    list.replaceChildren();
    for (const m of memories) {
      const li = el("li");
      li.append(el("span", "", m.content));
      const meta = el("span", "meta");
      meta.append(el("span", "", m.kind), smallButton("Forget", async () => {
        if (!confirm(`Forget “${m.content}”?`)) return;
        try { await api(`/v1/memories/${m.id}`, { method: "DELETE" }); refreshLists(); }
        catch (err) { toast(err.message); }
      }));
      li.append(meta);
      list.append(li);
    }
  }

  function renderWorkflows(workflows) {
    const list = $("workflowList");
    if (!workflows.length) return empty(list, "After a task, say “save this as a workflow called …”.");
    list.replaceChildren();
    for (const w of workflows) {
      const li = el("li");
      li.append(el("strong", "", w.name), el("span", "", w.description));
      const row = el("div", "row-actions");
      row.append(
        smallButton("Run", () => runWorkflow(w)),
        smallButton("Delete", async () => {
          if (!confirm(`Delete workflow “${w.name}”?`)) return;
          try { await api(`/v1/workflows/${encodeURIComponent(w.name)}`, { method: "DELETE" }); refreshLists(); }
          catch (err) { toast(err.message); }
        }),
      );
      li.append(row, el("span", "meta", `run ${w.run_count}×`));
      list.append(li);
    }
  }

  function runWorkflow(w) {
    if (state.busy) return toast("Wait for the current task to finish.");
    if (!w.parameters.length) return submitGoal(`Run my saved workflow "${w.name}".`);
    const dlg = $("paramsDlg");
    const fields = $("paramsFields");
    fields.replaceChildren();
    $("paramsTitle").textContent = `Run “${w.name}”`;
    const inputs = {};
    for (const p of w.parameters) {
      const label = el("label", "field", p);
      const input = el("input");
      input.type = "text";
      inputs[p] = input;
      label.append(input);
      fields.append(label);
    }
    dlg.onclose = () => {
      if (dlg.returnValue !== "run") return;
      const args = w.parameters.map((p) => `${p}="${inputs[p].value}"`).join(", ");
      submitGoal(`Run my saved workflow "${w.name}" with ${args}.`);
    };
    dlg.showModal();
  }

  // ------------------------------------------------------------------ status + voice
  async function refreshStatus() {
    try {
      const s = await api("/v1/status");
      state.status = s;
      const setup = s.setup || {};
      $("model").textContent = setup.model ? `${setup.provider}: ${setup.model}` : s.llm_provider;
      renderVoice(setup.voice_state || "off");
      renderSpend(setup.spend_today_usd, setup.daily_cap_usd);
      if (setup && (!setup.llm_ready || !setup.onboarded) && $("setup").hidden) openWizard(1);
      return s;
    } catch (err) {
      toast(`Status: ${err.message}`);
      return null;
    }
  }

  function renderSpend(today, cap) {
    const p = $("spend");
    if (today === undefined) { p.hidden = true; return; }
    p.hidden = false;
    p.textContent = cap ? `$${today.toFixed(2)} / $${cap.toFixed(2)} today` : `$${today.toFixed(2)} today`;
    p.className = `pill ${cap && today >= cap * 0.8 ? "pill-warn" : "pill-muted"}`;
  }

  function renderVoice(voiceState) {
    const b = $("voiceToggle");
    const labels = { off: "Voice: off", starting: "Voice: starting…", listening: "Voice: listening" };
    b.textContent = labels[voiceState] || `Voice: ${voiceState}`;
    b.className = `pill pill-button ${voiceState === "listening" ? "pill-live" : ""}`;
    b.dataset.state = voiceState;
  }

  async function toggleVoice() {
    const s = state.status && state.status.setup;
    const turnOn = $("voiceToggle").dataset.state === "off";
    if (turnOn && s && !s.voice_models_ready) {
      openWizard(4);
      return toast("Tick hands-free voice and finish setup to download the voice models.");
    }
    try {
      renderVoice(turnOn ? "starting" : "off");
      await post("/v1/voice", { enabled: turnOn });
    } catch (err) {
      toast(err.message);
      refreshStatus();
    }
  }

  // ------------------------------------------------------------------ setup wizard
  const STEPS = 7;
  const VOICE_LABELS = {
    british_male: "British male",
    british_female: "British female",
    american_male: "American male",
    american_female: "American female",
  };
  const wiz = { step: 1, llmReady: false, voiceReady: false };

  async function openWizard(step = 1) {
    try {
      state.settings = await api("/v1/settings");
    } catch (err) {
      return toast(`Settings: ${err.message}`);
    }
    const s = (state.status && state.status.setup) || {};
    wiz.llmReady = Boolean(s.llm_ready);
    wiz.voiceReady = Boolean(s.voice_models_ready);
    fillWizard(state.settings, s);
    $("setup").hidden = false;
    $("wizClose").hidden = !(s.onboarded && s.llm_ready);
    showStep(step);
  }

  function fillWizard(cfg, s) {
    const p = cfg.profile;
    $("pName").value = p.name;
    $("pNick").value = p.nickname;
    setRadio("address", p.address);

    setRadio("style", cfg.persona.style);
    $("pushback").checked = cfg.persona.pushback;
    $("progressUpdates").checked = cfg.persona.progress_updates;
    $("quickReplies").checked = cfg.persona.quick_replies;

    setRadio("provider", s.provider || "anthropic");
    showProviderFields();
    if (s.llm_ready) {
      $("modelResult").textContent = s.provider === "anthropic" ? "✔ Claude is set up." : "✔ Ollama is selected.";
      $("modelResult").className = "result ok";
    }

    const grid = $("voiceGrid");
    grid.replaceChildren();
    for (const id of cfg.voices) {
      const card = el("div", "voice-card");
      const label = el("label");
      const input = el("input");
      input.type = "radio";
      input.name = "voice";
      input.value = id;
      input.checked = id === cfg.voice.tts_voice;
      label.append(input, el("span", "", VOICE_LABELS[id] || id));
      card.append(label, smallButton("▶ Preview", () => previewVoice(id)));
      grid.append(card);
    }
    $("speed").value = cfg.voice.tts_speed;
    $("speedLabel").textContent = `${Number(cfg.voice.tts_speed).toFixed(2)}×`;
    $("voiceOn").checked = Boolean(s.voice_enabled);

    const f = cfg.features;
    $("fWeb").checked = f.web_research;
    $("fDocs").checked = f.documents.enabled;
    $("fDocsFolders").value = (f.documents.folders.length ? f.documents.folders : s.allowed_roots || []).join("\n");
    $("fEmail").checked = f.email.enabled;
    setRadio("emailProvider", f.email.provider);
    setRadio("emailAccount", f.email.account);
    $("fProtocols").checked = f.protocols.enabled;
    $("fBriefing").value = f.protocols.briefing_time || "";
    $("fHud").checked = f.hud.enabled;
    $("fDashboard").checked = f.hud.dashboard;
    $("fPhone").checked = f.phone.enabled;
    setRadio("phone", f.phone.platform);
    $("fHelpers").checked = f.helpers;
    $("fHome").checked = f.smart_home.enabled;
    $("fHomeUrl").value = f.smart_home.url;
    $("fWebcam").checked = f.webcam;
    for (const box of document.querySelectorAll("[data-toggles]")) $(box.dataset.toggles).hidden = !box.checked;
    $("workWarning").hidden = radio("emailAccount") !== "work";

    $("bCap").value = cfg.budget.daily_usd;
    setRadio("background", cfg.budget.background);
    $("roots").textContent = (s.allowed_roots || []).join(", ");
  }

  function collect() {
    const folders = $("fDocsFolders").value.split("\n").map((x) => x.trim()).filter(Boolean);
    return {
      profile: {
        name: $("pName").value.trim(),
        address: radio("address") || "none",
        nickname: $("pNick").value.trim(),
      },
      persona: {
        style: radio("style") || "jarvis",
        pushback: $("pushback").checked,
        progress_updates: $("progressUpdates").checked,
        quick_replies: $("quickReplies").checked,
      },
      voice: { tts_voice: radio("voice"), tts_speed: Number($("speed").value) },
      features: {
        web_research: $("fWeb").checked,
        documents: { enabled: $("fDocs").checked, folders },
        email: { enabled: $("fEmail").checked, provider: radio("emailProvider"), account: radio("emailAccount") },
        protocols: { enabled: $("fProtocols").checked, briefing_time: $("fBriefing").value || null },
        hud: { enabled: $("fHud").checked, dashboard: $("fDashboard").checked },
        phone: { enabled: $("fPhone").checked, platform: radio("phone") },
        smart_home: { enabled: $("fHome").checked, url: $("fHomeUrl").value.trim() },
        webcam: $("fWebcam").checked,
        helpers: $("fHelpers").checked,
      },
      budget: { daily_usd: Number($("bCap").value || 0), background: radio("background") || "fast" },
    };
  }

  function showStep(n) {
    wiz.step = Math.max(1, Math.min(STEPS, n));
    for (const s of document.querySelectorAll(".step")) s.hidden = Number(s.dataset.step) !== wiz.step;
    const dots = $("dots");
    dots.replaceChildren();
    for (let i = 1; i <= STEPS; i++) dots.append(el("li", i < wiz.step ? "done" : i === wiz.step ? "current" : ""));
    $("wizBack").disabled = wiz.step === 1;
    $("wizNext").textContent = wiz.step === STEPS ? "Finish" : "Next";
    if (wiz.step === STEPS) renderReview();
  }

  function validateStep() {
    if (wiz.step === 1 && radio("address") === "nickname" && !$("pNick").value.trim()) return "Enter a nickname, or pick another option.";
    if (wiz.step === 1 && radio("address") === "name" && !$("pName").value.trim()) return "Enter your name, or pick another option.";
    if (wiz.step === 3 && !wiz.llmReady) return "Save and check an AI model first.";
    if (wiz.step === 5 && $("fHome").checked && !$("fHomeUrl").value.trim()) return "Enter your Home Assistant address, or untick smart home.";
    return null;
  }

  function renderReview() {
    const c = collect();
    const addressText = { sir: "Sir", maam: "Ma'am", name: "First name", nickname: `“${c.profile.nickname}”`, none: "No title" };
    const on = Object.entries({
      "Web research": c.features.web_research,
      "Documents": c.features.documents.enabled,
      "Email & calendar": c.features.email.enabled,
      "Protocols": c.features.protocols.enabled,
      "HUD": c.features.hud.enabled,
      "Phone": c.features.phone.enabled,
      "Helpers": c.features.helpers,
      "Smart home": c.features.smart_home.enabled,
      "Webcam": c.features.webcam,
    }).filter(([, v]) => v).map(([k]) => k);
    const rows = [
      ["Name", c.profile.name || "—"],
      ["Address me as", addressText[c.profile.address]],
      ["Personality", c.persona.style],
      ["Model", state.status?.setup?.model || "—"],
      ["Voice", `${VOICE_LABELS[c.voice.tts_voice] || c.voice.tts_voice}, ${c.voice.tts_speed.toFixed(2)}×${$("voiceOn").checked ? ", hands-free on" : ""}`],
      ["Features", on.length ? on.join(", ") : "Core only"],
      ["Daily limit", c.budget.daily_usd ? `$${c.budget.daily_usd.toFixed(2)}` : "No limit"],
    ];
    const dl = $("review");
    dl.replaceChildren();
    for (const [k, v] of rows) dl.append(el("dt", "", k), el("dd", "", v));
  }

  async function next() {
    const problem = validateStep();
    if (problem) return toast(problem);
    if (wiz.step < STEPS) return showStep(wiz.step + 1);
    await finishWizard();
  }

  async function finishWizard() {
    const out = $("finishResult");
    out.className = "result";
    out.textContent = "Saving…";
    $("wizNext").disabled = true;
    try {
      const changes = collect();
      changes.profile.onboarded = true;
      await api("/v1/settings", { method: "PUT", body: JSON.stringify(changes) });
      const wantVoice = $("voiceOn").checked;
      const status = await refreshStatus();
      const s = (status && status.setup) || {};
      if (wantVoice && !s.voice_models_ready) {
        out.textContent = "Downloading voice models…";
        $("voiceProgress").hidden = false;
        wiz.pendingVoice = true;
        await post("/v1/setup/voice-models");
        return; // finished in onVoiceReady
      }
      if (wantVoice !== Boolean(s.voice_enabled)) await post("/v1/voice", { enabled: wantVoice });
      closeWizard();
    } catch (err) {
      out.textContent = err.message;
      out.className = "result bad";
    } finally {
      $("wizNext").disabled = false;
    }
  }

  function closeWizard() {
    $("setup").hidden = true;
    if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
    refreshStatus();
    refreshLists();
    api("/v1/settings").then((cfg) => {
      state.settings = cfg;
      const who = cfg.profile.name ? `, ${cfg.profile.name.split(" ")[0]}` : "";
      $("welcomeTitle").textContent = `What can I do for you${who}?`;
    }).catch(() => {});
    $("goal").focus();
  }

  function showProviderFields() {
    const p = radio("provider") || "anthropic";
    $("claudeFields").hidden = p !== "anthropic";
    $("ollamaFields").hidden = p !== "ollama";
  }

  async function saveModel() {
    const p = radio("provider");
    const out = $("modelResult");
    out.className = "result";
    out.textContent = "Checking…";
    $("saveModel").disabled = true;
    try {
      if (p === "anthropic") {
        const key = $("apiKey").value.trim();
        if (!key && state.status?.setup?.api_key_set) await post("/v1/setup/provider", { provider: "anthropic" });
        else await post("/v1/setup/api-key", { key });
        $("apiKey").value = "";
        out.textContent = "✔ Claude is set up.";
      } else {
        await post("/v1/setup/provider", { provider: "ollama", ollama_model: $("ollamaModel").value.trim() });
        out.textContent = "✔ Ollama is reachable.";
      }
      out.className = "result ok";
      wiz.llmReady = true;
      await refreshStatus();
    } catch (err) {
      out.textContent = err.message;
      out.className = "result bad";
    } finally {
      $("saveModel").disabled = false;
    }
  }

  async function previewVoice(id) {
    const out = $("voiceResult");
    out.className = "result";
    out.textContent = `Preparing ${VOICE_LABELS[id] || id}… (first time downloads ~63 MB)`;
    try {
      await post("/v1/voice/preview", { voice: id, speed: Number($("speed").value) });
      out.textContent = `Played ${VOICE_LABELS[id] || id}.`;
      $("voiceProgress").hidden = true;
    } catch (err) {
      out.textContent = err.message;
      out.className = "result bad";
    }
  }

  function onVoiceProgress(p) {
    const pct = p.total ? Math.floor((100 * p.done) / p.total) : 0;
    $("voiceProgress").hidden = false;
    $("voiceProgress").value = pct;
    const text = p.file ? `Downloading ${p.file}: ${pct}%` : "Preparing speech recognition…";
    $("voiceResult").textContent = text;
    if (wiz.pendingVoice) $("finishResult").textContent = text;
  }

  async function onVoiceReady() {
    $("voiceProgress").hidden = true;
    $("voiceResult").textContent = "✔ Voice models installed.";
    $("voiceResult").className = "result ok";
    if (wiz.pendingVoice) {
      wiz.pendingVoice = false;
      try { await post("/v1/voice", { enabled: true }); } catch (err) { toast(err.message); }
      closeWizard();
    }
  }

  // ------------------------------------------------------------------ wiring
  function wire() {
    $("composer").addEventListener("submit", (e) => { e.preventDefault(); submitGoal($("goal").value); });
    $("goal").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submitGoal($("goal").value); }
    });
    $("goal").addEventListener("input", autosize);
    for (const b of document.querySelectorAll(".example")) b.onclick = () => submitGoal(b.textContent);
    $("stopBtn").onclick = async () => {
      send({ type: "task.cancel" });
      try { await post("/v1/stop"); } catch (err) { toast(err.message); }
    };
    $("voiceToggle").onclick = toggleVoice;
    $("setupBtn").onclick = () => openWizard(1);
    for (const r of document.querySelectorAll('input[name="provider"]')) r.onchange = showProviderFields;
    for (const r of document.querySelectorAll('input[name="emailAccount"]')) {
      r.onchange = () => { $("workWarning").hidden = radio("emailAccount") !== "work"; };
    }
    for (const box of document.querySelectorAll("[data-toggles]")) {
      box.onchange = () => { $(box.dataset.toggles).hidden = !box.checked; };
    }
    $("pNick").onfocus = () => setRadio("address", "nickname");
    $("speed").oninput = () => { $("speedLabel").textContent = `${Number($("speed").value).toFixed(2)}×`; };
    $("saveModel").onclick = saveModel;
    $("wizBack").onclick = () => showStep(wiz.step - 1);
    $("wizNext").onclick = next;
    $("wizClose").onclick = () => { $("setup").hidden = true; };
    for (const tab of document.querySelectorAll(".tab")) {
      tab.onclick = () => {
        for (const t of document.querySelectorAll(".tab")) {
          const on = t === tab;
          t.classList.toggle("active", on);
          t.setAttribute("aria-selected", String(on));
          $(`tab-${t.dataset.tab}`).hidden = !on;
        }
      };
    }
  }

  wire();
  if (!TOKEN) {
    addMessage("error", "Open JARVIS from the Start menu or the tray icon.");
    setConn(false);
  } else {
    connect();
  }
})();

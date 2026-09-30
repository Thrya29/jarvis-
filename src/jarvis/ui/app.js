// JARVIS desktop UI. Talks to the local daemon: REST for data, WebSocket for tasks.
// Every piece of data is rendered with textContent - never innerHTML - so nothing a
// task produces (file names, web text, model output) can inject markup or script.
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

  const state = {
    ws: null,
    retry: 0,
    busy: false,
    status: null,
    lastUserGoal: "",
  };

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
      try { detail = (await res.json()).detail || detail; } catch { /* not JSON */ }
      throw new Error(detail);
    }
    return res.status === 204 ? null : res.json();
  }

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
  function hideWelcome() { $("welcome").hidden = true; }

  function addMessage(kind, text, source) {
    hideWelcome();
    const m = el("div", `msg msg-${kind}`);
    if (source) m.append(el("span", "source", source));
    m.append(document.createTextNode(text));
    $("log").append(m);
    m.scrollIntoView({ block: "end" });
    return m;
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
    const li = el("li", failed ? "fail" : "", text);
    const list = $("activity");
    list.prepend(li);
    while (list.children.length > 60) list.lastChild.remove();
  }

  // ------------------------------------------------------------------ busy state
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
      const delay = Math.min(10000, 500 * 2 ** state.retry++);
      setTimeout(connect, delay);
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
        renderPlan([]);
        if (voice) addMessage("user", msg.goal, "🎙 said");
        break;
      case "plan.updated":
        renderPlan(msg.steps);
        break;
      case "assistant.text":
        addMessage("assistant", msg.text, voice ? "🔊 JARVIS" : undefined);
        break;
      case "tool.started":
        activity(`… ${msg.tool}`);
        break;
      case "tool.finished":
        if (!msg.ok) activity(`✖ ${msg.tool}: ${msg.preview}`, true);
        break;
      case "task.finished":
        setBusy(false);
        onFinished(msg);
        refreshLists();
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
        $("getVoice").disabled = false;
        break;
      case "error":
        if (msg.setup_required) openSetup();
        addMessage("error", msg.error);
        break;
      default:
        break;
    }
  }

  function onFinished(msg) {
    const labels = {
      completed: null,
      cancelled: "Stopped.",
      failed: `Failed: ${msg.summary}`,
      refused: msg.summary,
      limit_reached: msg.summary,
    };
    const note = labels[msg.status];
    if (note) addMessage("system", note);
    const u = msg.usage || {};
    activity(`■ ${msg.status} - ${msg.tool_calls} tool calls, ${(u.input_tokens || 0).toLocaleString()} in / ${(u.output_tokens || 0).toLocaleString()} out tokens`);
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
      state.lastUserGoal = goal;
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
  function empty(list, text) {
    list.replaceChildren(el("li", "empty", text));
  }

  function when(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  }

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
        const b = el("button", "btn btn-small", "Resume");
        b.type = "button";
        b.onclick = () => {
          if (state.busy) return toast("Wait for the current task to finish.");
          if (send({ type: "task.resume", task_id: t.id })) addMessage("user", `Resume: ${t.goal.split("\n")[0]}`);
        };
        const row = el("div", "row-actions");
        row.append(b);
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
      meta.append(el("span", "", m.kind));
      const del = el("button", "btn btn-small", "Forget");
      del.type = "button";
      del.onclick = async () => {
        if (!confirm(`Forget “${m.content}”?`)) return;
        try { await api(`/v1/memories/${m.id}`, { method: "DELETE" }); refreshLists(); }
        catch (err) { toast(err.message); }
      };
      meta.append(del);
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
      const run = el("button", "btn btn-small", "Run");
      run.type = "button";
      run.onclick = () => runWorkflow(w);
      const del = el("button", "btn btn-small", "Delete");
      del.type = "button";
      del.onclick = async () => {
        if (!confirm(`Delete workflow “${w.name}”?`)) return;
        try { await api(`/v1/workflows/${encodeURIComponent(w.name)}`, { method: "DELETE" }); refreshLists(); }
        catch (err) { toast(err.message); }
      };
      row.append(run, del);
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
      const label = el("label", "", p);
      const input = el("input");
      input.type = "text";
      input.required = true;
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
      if (setup && !setup.llm_ready) openSetup();
      return s;
    } catch (err) {
      toast(`Status: ${err.message}`);
      return null;
    }
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
      openSetup();
      return toast("Download the voice models first (step 2).");
    }
    try {
      renderVoice(turnOn ? "starting" : "off");
      await api("/v1/voice", { method: "POST", body: JSON.stringify({ enabled: turnOn }) });
    } catch (err) {
      toast(err.message);
      refreshStatus();
    }
  }

  // ------------------------------------------------------------------ setup wizard
  function openSetup() {
    const s = (state.status && state.status.setup) || {};
    $("setup").hidden = false;
    $("roots").textContent = (s.allowed_roots || []).join(", ") || "your Documents and Desktop";
    const provider = s.provider || "anthropic";
    for (const r of document.querySelectorAll('input[name="provider"]')) r.checked = r.value === provider;
    showProviderFields();
    if (s.llm_ready) {
      $("modelResult").textContent = s.provider === "anthropic" ? "✔ Claude is set up." : "✔ Ollama is selected.";
      $("modelResult").className = "result ok";
    }
    if (s.voice_models_ready) onVoiceReady();
    $("voiceOnSetup").checked = Boolean(s.voice_enabled);
    $("finishSetup").disabled = !s.llm_ready;
  }

  function showProviderFields() {
    const p = document.querySelector('input[name="provider"]:checked').value;
    $("claudeFields").hidden = p !== "anthropic";
    $("ollamaFields").hidden = p !== "ollama";
  }

  async function saveModel() {
    const p = document.querySelector('input[name="provider"]:checked').value;
    const out = $("modelResult");
    out.className = "result";
    out.textContent = "Checking…";
    $("saveModel").disabled = true;
    try {
      if (p === "anthropic") {
        const key = $("apiKey").value.trim();
        if (!key && state.status?.setup?.api_key_set) {
          await api("/v1/setup/provider", { method: "POST", body: JSON.stringify({ provider: "anthropic" }) });
        } else {
          await api("/v1/setup/api-key", { method: "POST", body: JSON.stringify({ key }) });
        }
        $("apiKey").value = "";
        out.textContent = "✔ Claude is set up.";
      } else {
        await api("/v1/setup/provider", {
          method: "POST",
          body: JSON.stringify({ provider: "ollama", ollama_model: $("ollamaModel").value.trim() }),
        });
        out.textContent = "✔ Ollama is reachable.";
      }
      out.className = "result ok";
      $("finishSetup").disabled = false;
      await refreshStatus();
    } catch (err) {
      out.textContent = err.message;
      out.className = "result bad";
    } finally {
      $("saveModel").disabled = false;
    }
  }

  async function getVoice() {
    $("getVoice").disabled = true;
    $("voiceProgress").hidden = false;
    $("voiceResult").className = "result";
    $("voiceResult").textContent = "Starting download…";
    try { await api("/v1/setup/voice-models", { method: "POST" }); }
    catch (err) { $("voiceResult").textContent = err.message; $("getVoice").disabled = false; }
  }

  function onVoiceProgress(p) {
    const pct = p.total ? Math.floor((100 * p.done) / p.total) : 0;
    $("voiceProgress").hidden = false;
    $("voiceProgress").value = pct;
    $("voiceResult").textContent = p.file ? `${p.file}: ${pct}%` : "Preparing speech recognition…";
  }

  function onVoiceReady() {
    $("voiceProgress").hidden = true;
    $("getVoice").disabled = true;
    $("getVoice").textContent = "Voice models installed";
    $("voiceResult").textContent = "✔ Voice is ready.";
    $("voiceResult").className = "result ok";
    if (state.status && state.status.setup) state.status.setup.voice_models_ready = true;
  }

  async function finishSetup() {
    const s = state.status && state.status.setup;
    const wantVoice = $("voiceOnSetup").checked;
    if (wantVoice && s && !s.voice_models_ready) return toast("Download the voice models first, or untick voice.");
    try {
      if (s && wantVoice !== Boolean(s.voice_enabled)) {
        await api("/v1/voice", { method: "POST", body: JSON.stringify({ enabled: wantVoice }) });
      }
    } catch (err) { toast(err.message); }
    $("setup").hidden = true;
    if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
    refreshStatus();
    $("goal").focus();
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
      try { await api("/v1/stop", { method: "POST" }); } catch (err) { toast(err.message); }
    };
    $("voiceToggle").onclick = toggleVoice;
    $("setupBtn").onclick = openSetup;
    for (const r of document.querySelectorAll('input[name="provider"]')) r.onchange = showProviderFields;
    $("saveModel").onclick = saveModel;
    $("getVoice").onclick = getVoice;
    $("finishSetup").onclick = finishSetup;
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

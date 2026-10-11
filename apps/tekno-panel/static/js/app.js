/* TEKNO CONTROL front-end: login, live WebSocket, six screens. Vanilla JS, no build step. */
(function () {
  "use strict";
  const LANES = ["Plan", "Generate", "Stems", "Sound Swap", "Edit", "Filter", "Variation", "Mix", "Polish", "Master", "Score", "Review", "Done"];
  const AGENT_COLORS = { "grok bot": "#39ff14", "mix judge": "#ffb020", "claude": "#ff8a5c", "hermes": "#c77dff" };
  const $ = s => document.querySelector(s);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const haptic = (ms = 8) => {
    try { if (navigator.vibrate && (!navigator.userActivation || navigator.userActivation.hasBeenActive)) navigator.vibrate(ms); } catch (e) {}
  };
  const fmtT = s => !isFinite(s) ? "0:00" : `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
  const ago = ts => {
    if (!ts) return "";
    const t = typeof ts === "number" ? ts : Date.parse(ts) / 1000;
    const d = Date.now() / 1000 - t;
    if (d < 60) return `${Math.max(0, Math.round(d))}s ago`;
    if (d < 3600) return `${Math.round(d / 60)}m ago`;
    if (d < 86400) return `${Math.round(d / 3600)}h ago`;
    return `${Math.round(d / 86400)}d ago`;
  };
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} },
  };

  // Wide screen + mouse (not the installed phone app) -> desktop control room. ?view=phone keeps this UI.
  const view = new URLSearchParams(location.search).get("view") || store.get("tekno-view", "");
  if (view !== "phone" && matchMedia("(min-width: 1100px) and (pointer: fine)").matches &&
      !matchMedia("(display-mode: standalone)").matches && !navigator.standalone) {
    location.replace("/desktop" + location.search);
    return;
  }
  if (view === "phone") store.set("tekno-view", "phone");

  const S = {
    snap: null, screen: store.get("tekno-screen", "pipeline"), demo: false, laneFilter: null,
    prevLanes: {}, listenCard: null, scoreCard: null, thread: null, follow: true, nowUrl: null,
    knownAudio: new Set(), seenMsgs: 0, gpuHist: [], ws: null, retry: 0, wsOpen: false,
  };

  function toast(msg, bad) {
    const t = $("#toast"); t.textContent = msg; t.classList.toggle("bad", !!bad); t.classList.remove("hidden");
    clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), 2600);
  }

  // ---------------- login ----------------
  let pin = "";
  function buildKeypad() {
    const kp = $("#keypad"); kp.innerHTML = "";
    ["1", "2", "3", "4", "5", "6", "7", "8", "9", "⌫", "0", "OK"].forEach(k => {
      const b = document.createElement("button"); b.textContent = k; if (k === "⌫" || k === "OK") b.className = "fn";
      b.addEventListener("click", () => {
        haptic(6);
        if (k === "⌫") pin = pin.slice(0, -1);
        else if (k === "OK") return submitPin();
        else if (pin.length < 12) pin += k;
        drawPin();
        if (pin.length === 6) setTimeout(submitPin, 120);
      });
      kp.appendChild(b);
    });
    document.addEventListener("keydown", e => {
      if ($("#login").classList.contains("hidden")) return;
      if (/^\d$/.test(e.key)) { pin += e.key; drawPin(); if (pin.length === 6) submitPin(); }
      if (e.key === "Backspace") { pin = pin.slice(0, -1); drawPin(); }
      if (e.key === "Enter") submitPin();
    });
    drawPin();
  }
  function drawPin() { $("#pin").innerHTML = Array.from({ length: Math.max(6, pin.length) }, (_, i) => `<i class="${i < pin.length ? "on" : ""}"></i>`).join(""); }
  async function submitPin() {
    if (!pin) return;
    const r = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ passcode: pin }) });
    const j = await r.json().catch(() => ({}));
    if (r.ok) { haptic([10, 40, 10]); $("#login-err").textContent = ""; pin = ""; boot(); }
    else { haptic([30, 30, 30]); $("#login-err").textContent = j.error || "Wrong passcode"; pin = ""; drawPin(); const p = $("#pin"); p.classList.remove("shake"); void p.offsetWidth; p.classList.add("shake"); }
  }

  // ---------------- navigation ----------------
  function show(screen) {
    S.screen = screen; store.set("tekno-screen", screen);
    if (screen === "health") V3.studio.start($("#studio-m"), { single: true }); else V3.studio.stop();  // frames only while looked at
    document.querySelectorAll(".screen").forEach(el => el.classList.toggle("active", el.id === "s-" + screen));
    document.querySelectorAll("#tabbar button").forEach(b => b.classList.toggle("active", b.dataset.screen === screen));
    if (screen === "bots") bots.start(); else bots.stop();
    if (screen === "chat") { $("#chat-dot").classList.add("hidden"); S.seenMsgs = (S.snap?.chat?.messages || []).length; }
    window.scrollTo({ top: 0 });
    if (S.snap) render(S.snap);
  }

  // ---------------- live connection ----------------
  function connect() {
    if (S.ws) { S.ws.onclose = null; S.ws.close(); }
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws${S.demo ? "?demo=1" : ""}`);
    S.ws = ws;
    ws.onopen = () => { S.retry = 0; S.wsOpen = true; $("#live-dot").classList.add("on"); };
    ws.onmessage = e => { try { render(JSON.parse(e.data)); } catch (err) { console.error(err); } };
    ws.onclose = ev => {
      S.wsOpen = false; $("#live-dot").classList.remove("on"); stamp();
      if (ev.code === 4401) return showLogin();
      const wait = Math.min(10000, 500 * 2 ** S.retry++);
      setTimeout(connect, wait);
      fetch(`/api/state${S.demo ? "?demo=1" : ""}`).then(r => r.ok ? r.json() : null).then(j => j && render(j)).catch(() => {});
    };
  }

  // ---------------- render ----------------
  function render(snap) {
    S.snap = snap;
    const chip = $("#mode-chip");
    chip.textContent = snap.mode === "demo" ? "DEMO" : "LIVE"; chip.classList.toggle("demo", snap.mode === "demo");
    stamp();
    const errs = Object.entries(snap.sources || {}).filter(([k, v]) => v && v.ok === false && k !== "mail").map(([k, v]) => `${k}: ${v.error}`);
    const ban = $("#banner"); ban.textContent = errs.join(" · "); ban.classList.toggle("hidden", !errs.length);
    renderPipeline(snap);
    bots.update(snap.lanes || {});
    if (S.screen === "listen") renderListen(snap); else trackNewAudio(snap);
    if (S.screen === "scores") renderScores(snap);
    pushGpu(snap);
    if (S.screen === "health") renderHealth(snap);
    renderChat(snap);
    renderBellM(snap);
    const ag = $("#agents-m"), agHtml = V3.agentsHtml(snap.agents);
    if (ag.dataset.html !== agHtml) { ag.dataset.html = agHtml; ag.innerHTML = agHtml; }
    ag.classList.toggle("hidden", !(snap.agents || []).length);
    const m = snap.machine || {};
    $("#paused-banner").classList.toggle("hidden", !m.paused);
    if (m.paused) $("#paused-banner").textContent = `⏸ Machine paused: ${m.reason || ""}. New ACE jobs won't start.`;
  }

  // ---------------- alerts (top bar) ----------------
  function renderBellM(snap) {
    const al = snap.alerts || [], b = $("#bell-m");
    b.textContent = al.length ? String(al.length) : "";
    b.classList.toggle("hidden", !al.length);
    b.classList.toggle("crit", al.some(a => a.level === "crit"));
  }
  function alertsSheet() {
    const al = S.snap?.alerts || [];
    haptic(8);
    openSheet(`<h2>Alerts</h2><p class="muted">Live problems, worst first. They clear by themselves when fixed.</p>` +
      (al.length ? al.map(a => `<div class="al-row ${esc(a.level)}"><i></i><div><b>${esc(a.title)}</b>${a.detail ? `<div class="d">${esc(a.detail)}</div>` : ""}<div class="d">${esc(ago(a.since))}</div></div></div>`).join("")
        : `<p>All clear.</p>`));
  }

  // ---------------- activity feed (Chat tab) ----------------
  const KIND_M = { run: "run", kanban: "board", render: "render", ace: "ACE", service: "service", mail: "mail", alert: "alert", action: "you" };
  function renderActivityM(snap) {
    const list = snap.activity || [];
    const box = $("#act-m"), key = list.map(e => e.id).join("|");
    if (box.dataset.key === key) return;
    box.dataset.key = key;
    let day = "", html = "";
    for (const e of list) {
      const d = new Date(e.ts * 1000).toDateString();
      if (d !== day) { day = d; html += `<div class="tl-day">${esc(d === new Date().toDateString() ? "Today" : new Date(e.ts * 1000).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" }))}</div>`; }
      html += `<div class="tl-row ${esc(e.level)}"><time>${esc(Live.hm(e.ts))}</time><div class="tl-text"><span class="k">${esc(KIND_M[e.kind] || e.kind)}</span>${e.card ? `<b>${esc(e.card)}</b> · ` : ""}${esc(e.text)}</div></div>`;
    }
    box.innerHTML = html || `<p class="muted">Nothing recorded yet.</p>`;
  }

  // Time of the last snapshot the machine pushed; turns amber when it stops ticking.
  function stamp() {
    const el = $("#upd"); if (!el || !S.snap || !S.snap.ts) return;
    const ts = S.snap.ts, age = Math.max(0, Date.now() / 1000 - ts);
    const t = new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
    const stale = !S.wsOpen || age > 10;
    el.textContent = stale ? `${t} · ${ago(ts)}` : t;
    el.classList.toggle("stale", stale);
    el.title = `Last update from the machine: ${new Date(ts * 1000).toLocaleString()}`;
  }

  function laneIdx(l) { const i = LANES.indexOf(l); return i < 0 ? 0 : i; }

  function renderPipeline(snap) {
    const trs = snap.tracks || [];
    const counts = {}; const hot = {};
    trs.forEach(t => { counts[t.lane] = (counts[t.lane] || 0) + 1; if (t.state === "working") hot[t.lane] = 1; });
    $("#lane-strip").innerHTML = `<button class="lane-pill ${!S.laneFilter ? "sel" : ""}" data-lane="">All <b>${trs.length}</b></button>` +
      LANES.map(l => `<button class="lane-pill ${counts[l] ? "" : "zero"} ${S.laneFilter === l ? "sel" : ""} ${hot[l] ? "hot" : ""}" data-lane="${esc(l)}">${esc(l)} <b>${counts[l] || 0}</b></button>`).join("");
    const box = $("#track-cards");
    const shown = trs.filter(t => !S.laneFilter || t.lane === S.laneFilter);
    if (!shown.length) { box.innerHTML = `<div class="empty glass">${trs.length ? "No tracks in this lane." : "No tracks on the board yet.<br><small>Cards appear here the moment Hermes creates them.</small>"}</div>`; return; }
    const existing = new Map([...box.querySelectorAll(".card")].map(el => [el.dataset.slug, el]));
    const frag = document.createDocumentFragment();
    for (const t of shown) {
      let el = existing.get(t.slug);
      if (!el) { el = document.createElement("article"); el.className = "card glass"; el.dataset.slug = t.slug; }
      existing.delete(t.slug);
      const moved = S.prevLanes[t.slug] && S.prevLanes[t.slug] !== t.lane;
      S.prevLanes[t.slug] = t.lane;
      const key = JSON.stringify([t.lane, t.state, t.progress, t.loops, t.last_sendback, t.files_lane, t.review_task_id,
        t.live?.script, t.live?.pid, t.board_behind, (t.runs || []).length, t.plan?.bpm, t.current_task?.last_error, t.brief]);
      if (el.dataset.key !== key) {
        el.dataset.key = key;
        el.className = `card glass tappable state-${t.state}`;
        el.innerHTML = cardHtml(t);
        if (moved) { el.classList.add("moved"); haptic(5); }
      }
      frag.appendChild(el);
    }
    existing.forEach(el => el.remove());
    box.replaceChildren(frag);
  }

  function cardHtml(t) {
    const li = laneIdx(t.lane);
    const backs = new Set((t.sendbacks || []).map(s => s.to));
    const rail = LANES.map((l, i) => `<i class="${i < li ? "past" : ""} ${i === li ? "cur" : ""} ${backs.has(l) && i !== li ? "back" : ""}" title="${esc(l)}"></i>`).join("");
    const sb = t.last_sendback;
    const stateLabel = { working: "working", failed: "failed", review: "review", done: "done", queued: "queued", offboard: "files only" }[t.state] || t.state;
    const filesNote = t.board_behind ? Live.behindNote(t) : t.files_lane && t.files_lane !== t.lane && t.state !== "done" ? `<div class="files-note">On disk it already reached <b>${esc(t.files_lane)}</b> (board says ${esc(t.lane)}).</div>` : "";
    const err = t.state === "failed" && t.current_task?.last_error && !(sb && sb.task_id === t.current_task.id) ? `<div class="sendback"><b>Error:</b> ${esc(t.current_task.last_error)}</div>` : "";
    const review = t.lane === "Review" && t.review_task_id ? `<div class="review-actions">
        <button class="btn approve" data-act="approve" data-task="${esc(t.review_task_id)}" data-slug="${esc(t.slug)}">Approve</button>
        <button class="btn reject" data-act="reject" data-task="${esc(t.review_task_id)}" data-slug="${esc(t.slug)}">Reject + note</button></div>` : "";
    return `<div class="card-top"><div><div class="slug">${esc(t.slug)}</div>
        <div class="lane-name">lane <b>${esc(t.lane)}</b>${t.current_task?.assignee ? ` · ${esc(t.current_task.assignee)}` : ""}</div>${Live.planLine(t)}${V3.chips(t)}</div>
        <div class="pills"><span class="pill ${esc(t.state)}">${esc(stateLabel)}</span>${t.loops ? `<span class="pill loop">↺ ${Number(t.loops)}</span>` : ""}</div></div>
      <div class="rail">${rail}</div>
      <div class="rail-labels"><span>Plan</span><span>Mix</span><span>Done</span></div>
      <div class="progress"><div class="bar"><i style="width:${Math.max(2, t.progress)}%"></i></div><span class="pct">${Number(t.progress)}%</span></div>
      ${Live.liveLine(t)}${Live.sendbackHtml(sb)}
      ${err}${filesNote}${review}`;
  }

  // ---------------- track details (tap a card) ----------------
  function trackSheet(slug) {
    const t = (S.snap?.tracks || []).find(x => x.slug === slug);
    if (!t) return;
    haptic(8);
    const sc = (S.snap.scores || []).find(x => x.card === slug);
    const msgs = (S.snap.chat?.messages || []).filter(m => m.thread === `card-${slug}`);
    const renders = (S.snap.audio || []).filter(a => a.card === slug && a.kind === "render");
    const backs = t.sendbacks || [];
    openSheet(`<h2>${esc(t.slug)}</h2>${Control.trackButtons(t, S.snap)}<div style="margin-top:10px"><span class="pill ${esc(t.state)}">${esc(t.state)}</span> <span class="muted">lane <b style="color:var(--text)">${esc(t.lane)}</b>${t.board_lane && t.board_lane !== t.lane ? ` · board: ${esc(t.board_lane)}` : ""}${t.loops ? ` · <span style="color:var(--amber)">${Live.loops(t.loops)}${t.loops_src === "disk" ? " (from run logs)" : ""}</span>` : ""}</span></div>
      ${Live.liveLine(t)}${Live.behindNote(t)}
      ${t.lane === "Review" && t.review_task_id ? `<div class="review-actions"><button class="btn approve" data-act="approve" data-task="${esc(t.review_task_id)}" data-slug="${esc(t.slug)}">Approve</button>
        <button class="btn reject" data-act="reject" data-task="${esc(t.review_task_id)}" data-slug="${esc(t.slug)}">Reject + note</button></div>` : ""}
      ${renders.length ? `<button class="toggle lv-play" data-play="${esc(renders[0].url)}">▶ Play newest render · ${esc(renders[0].name)} · ${esc(renders[0].stage)} · ${ago(renders[0].mtime)}</button>` : ""}
      <h3>Plan</h3>${Live.planHtml(t.plan)}
      <h3>Runs on disk <small>${(t.runs || []).length}${t.attempts ? ` · ${t.attempts} archived attempts` : ""}</small></h3>${Live.runsHtml(t.runs)}
      ${t.current_task?.last_error ? `<h3>Board note <small>${esc(t.current_task.title)}</small></h3><div class="log">${esc(t.current_task.last_error)}</div>` : ""}
      ${backs.length ? `<h3>Routing</h3>${backs.map(b => Live.sendbackHtml(b)).join("")}` : ""}
      ${sc ? `<h3>Score <small>${esc(sc.verdict)} · ${ago(sc.mtime)}</small></h3><div class="badges">${Live.badgesHtml(sc.badges)}</div>` : ""}
      ${t.handoff?.length ? `<h3>Handoff</h3>${Live.handoffHtml(t.handoff)}` : ""}
      ${Object.keys(t.reports || {}).length ? `<h3>Lane reports</h3>${Live.reportsHtml(t.reports)}` : ""}
      <h3>Thread <small>card-${esc(slug)}</small></h3>${Live.threadMsgsHtml(msgs, agentColor) || `<div class="muted">No messages in this thread yet.</div>`}`);
  }

  // ---------------- review actions ----------------
  function openReview(act, task, slug) {
    haptic(10);
    const body = act === "approve"
      ? `<h2>Approve ${esc(slug)}?</h2><p class="muted">Writes an APPROVED comment on kanban task ${esc(task)}. Optional note:</p>
         <textarea id="rv-note" maxlength="1000" placeholder="e.g. acid rolls nicely, ship it"></textarea>
         <button class="btn approve" style="width:100%" id="rv-go">Approve</button>`
      : `<h2>Reject ${esc(slug)}</h2><p class="muted">Writes a REJECTED comment with your note on kanban task ${esc(task)}.</p>
         <textarea id="rv-note" maxlength="1000" placeholder="What's wrong? e.g. kick too thin, bass muddy at 80 Hz"></textarea>
         <button class="btn reject" style="width:100%" id="rv-go">Reject with note</button>`;
    openSheet(body);
    $("#rv-go").addEventListener("click", async () => {
      const note = $("#rv-note").value.trim();
      if (act === "reject" && !note) { toast("Add a note first", true); haptic([20, 20, 20]); return; }
      $("#rv-go").disabled = true;
      try {
        const r = await fetch(`/api/review/${encodeURIComponent(task)}`, {
          method: "POST", headers: { "Content-Type": "application/json", "x-tekno": "1" },
          body: JSON.stringify({ decision: act, note, demo: S.snap?.mode === "demo" }),
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(j.detail || r.statusText);
        haptic([10, 50, 20]); closeSheet();
        toast(j.demo ? "Demo: comment not written" : act === "approve" ? "Approved ✔ comment written" : "Rejected, note written");
      } catch (e) { toast(String(e.message || e), true); $("#rv-go").disabled = false; }
    });
  }

  // ---------------- sheet ----------------
  function openSheet(html) {
    $("#sheet-body").innerHTML = html; $("#sheet-backdrop").classList.remove("hidden");
    requestAnimationFrame(() => $("#sheet").classList.add("open"));
  }
  function closeSheet() { $("#sheet").classList.remove("open"); $("#sheet-backdrop").classList.add("hidden"); }

  function botSheet(lane, L) {
    const col = { working: "var(--acid)", failed: "var(--red)", waiting: "var(--uv-hot)", idle: "var(--muted)" }[L.state] || "var(--muted)";
    const log = (L.log || []).map(x => `${x.at ? new Date(x.at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : ""}  ${x.text}`).join("\n");
    openSheet(`<h2>${esc(lane)} bot</h2><span class="state-tag" style="color:${col}">${esc(L.state === "idle" ? "sleeping" : L.state)}</span>
      <h3>Current task</h3><p>${L.task ? `<span class="mono">${esc(L.task.title)}</span><br><small class="muted">${esc(L.task.id || "")} ${L.task.assignee ? "· " + esc(L.task.assignee) : ""}${L.task.via ? ` · seen via ${esc(L.task.via)}` : ""}${L.task.since ? ` · ${Live.dur(Date.now() / 1000 - L.task.since)}` : ""}</small>${L.task.error ? `<br><span class="warn-text">${esc(L.task.error)}</span>` : ""}` : `<span class="muted">No card in this lane right now.</span>`}</p>
      ${L.handoff_to ? `<p class="muted">Last hand-off → <b style="color:var(--acid)">${esc(L.handoff_to)}</b> ${ago(L.handoff_at)}</p>` : ""}
      <h3>Last log lines</h3><div class="log">${esc(log || "nothing logged yet")}</div>`);
  }

  // ---------------- listen ----------------
  function trackNewAudio(snap) {
    const list = snap.audio || [];
    const fresh = list.filter(a => !S.knownAudio.has(a.url));
    list.forEach(a => S.knownAudio.add(a.url));
    if (S.follow && fresh.length && S.audioInit) {
      const newest = fresh.filter(a => a.kind === "render").sort((x, y) => y.mtime - x.mtime)[0];
      if (newest && newest.url !== S.nowUrl) { loadTake(newest); toast(`New take: ${newest.card} · ${newest.name}`); }
    }
    S.audioInit = true;
  }

  function loadTake(a) {
    S.nowUrl = a.url;
    player.load(a.url, a.name);
    $("#now-title").textContent = `${a.card} · ${a.name}`;
    $("#now-sub").textContent = `${a.stage} ${a.kind} · ${ago(a.mtime)}`;
    document.querySelectorAll("#audio-list .row").forEach(r => r.classList.toggle("playing", r.dataset.url === a.url));
  }

  function renderListen(snap) {
    trackNewAudio(snap);
    const list = snap.audio || [];
    if (snap.reference) player.loadRef(snap.reference.url);
    $("#ab-b").textContent = snap.reference ? `B · ${snap.reference.name || "reference"}` : "B · (no ref)";
    $("#ab-b").disabled = !snap.reference;
    const cards = [...new Set(list.map(a => a.card))];
    if (!S.nowUrl && list.length) { const first = list.find(a => a.kind === "render") || list[0]; loadTake(first); }
    const cardKey = cards.join("|") + "/" + S.listenCard;
    if ($("#listen-cards").dataset.key !== cardKey) {
      $("#listen-cards").dataset.key = cardKey;
      $("#listen-cards").innerHTML = `<button class="toggle" aria-pressed="${!S.listenCard}" data-card="">All</button>` +
        cards.map(c => `<button class="toggle" aria-pressed="${S.listenCard === c}" data-card="${esc(c)}">${esc(c)}</button>`).join("");
    }
    const shown = list.filter(a => !S.listenCard || a.card === S.listenCard);
    const newest = list.length ? Math.max(...list.map(a => a.mtime)) : 0;
    const key = shown.map(a => a.url + a.mtime).join("|") + S.nowUrl;
    const box = $("#audio-list");
    if (box.dataset.key === key) return;
    box.dataset.key = key;
    box.innerHTML = shown.length ? shown.map(a => `<div class="row ${a.url === S.nowUrl ? "playing" : ""}" data-url="${esc(a.url)}">
        <div class="ico ${a.kind === "stem" ? "stem" : ""}">${a.kind === "stem" ? "S" : "▶"}</div>
        <div class="meta"><div class="name">${esc(a.name)}</div><div class="sub">${esc(a.card)} · ${esc(a.stage)} · ${ago(a.mtime)}${a.size ? " · " + (a.size / 1e6).toFixed(1) + " MB" : ""}</div></div>
        ${a.mtime === newest ? `<span class="new-badge">NEW</span>` : ""}</div>`).join("")
      : `<div class="empty glass">No renders yet. They show up the second a lane writes one.</div>`;
  }

  // ---------------- scores ----------------
  function renderScores(snap) {
    const sc = snap.scores || [];
    if (!sc.length) {
      $("#verdict-box").innerHTML = `<div class="muted">No score.json yet. score_card.py writes one per card in the Score lane.</div>`;
      $("#badges").innerHTML = ""; $("#ref-box").innerHTML = ""; $("#hist-note").textContent = "";
      ["#ch-lufs", "#ch-tp", "#ch-knock", "#ch-judge"].forEach(id => Charts.lines($(id), []));
      $("#score-cards").innerHTML = ""; return;
    }
    const cards = sc.map(s => s.card);
    const cur = sc.find(s => s.card === S.scoreCard) || sc[sc.length - 1];
    const key = cards.join("|") + cur.card;
    if ($("#score-cards").dataset.key !== key) {
      $("#score-cards").dataset.key = key;
      $("#score-cards").innerHTML = [...sc].reverse().map(s => `<button class="toggle" aria-pressed="${s.card === cur.card}" data-score="${esc(s.card)}">${s.pass ? "●" : "○"} ${esc(s.card)}</button>`).join("");
    }
    const go = cur.pass || cur.verdict === "GO";
    $("#verdict-box").innerHTML = `<div><div class="mono">${esc(cur.card)}</div><div class="muted" style="font-size:12px">${ago(cur.mtime)}${cur.duration_s ? ` · ${fmtT(cur.duration_s)}` : ""}</div>
      ${cur.hard_fails?.length ? `<div class="hard">✖ ${cur.hard_fails.map(esc).join("<br>✖ ")}</div>` : ""}</div>
      <div class="verdict ${go ? "go" : "nogo"}">${esc(cur.verdict || (go ? "GO" : "NO-GO"))}</div>`;
    $("#badges").innerHTML = Live.badgesHtml(cur.badges);
    $("#ref-box").innerHTML = `<h3>vs SIFRES v02 <small>ref_compare.py · band level vs ref</small></h3>${Live.refHtml(cur)}
      ${cur.draft_scores ? `<h3 style="margin-top:14px">Auto-draft <small>${Live.num(cur.metrics?.draft)}/10 · Mix Judge confirms</small></h3>${Live.draftHtml(cur)}` : ""}`;
    // history: every distinct score.json (archived attempts, handoff copies, overwritten versions) for this card
    const hist = (snap.history || []).filter(h => h.card === cur.card);
    const series = hist.length > 1 ? hist : sc;
    const m = series.map(s => s.metrics || {});
    Charts.lines($("#ch-lufs"), [{ values: m.map(x => x.lufs), color: Charts.ACID, label: "LUFS", ok: v => Math.abs(v + 9) <= 0.5 }], { band: [-9.5, -8.5] });
    Charts.lines($("#ch-tp"), [{ values: m.map(x => x.tp), color: Charts.ACID, label: "dBTP", ok: v => v <= -1 }], { line: -1 });
    const judge = series.map(s => { const b = (s.badges || []).find(x => x.label === "Mix Judge"); const v = parseFloat(b && b.value); return isNaN(v) ? (s.metrics?.draft ?? null) : v; });
    const ear = series.map(s => { const b = (s.badges || []).find(x => x.label === "EarLoop"); const v = parseFloat(b && b.value); return isNaN(v) ? (s.metrics?.earloop ?? null) : v; });
    Charts.lines($("#ch-judge"), [{ values: judge, color: "#ffb020", label: "Mix Judge / draft", ok: v => v >= 6.5 }, { values: ear, color: Charts.UV, label: "EarLoop", ok: v => v >= 6.5 }], { band: [6.5, 10] });
    Charts.lines($("#ch-knock"), [{ values: m.map(x => x.knock), color: Charts.ACID, label: "knock dB", ok: v => v >= 3 }], { line: 3 });
    $("#hist-note").textContent = series === hist ? `${hist.length} versions of ${cur.card}: ${hist.map(h => (h.runs || [h.run]).join("+")).join(" → ")}` : "one point per card";
  }

  // ---------------- health ----------------
  function pushGpu(snap) {
    const g = snap.health?.gpu;
    if (g && g.ok && snap.ts !== S.lastGpuTs) { S.lastGpuTs = snap.ts; S.gpuHist.push(g.util || 0); if (S.gpuHist.length > 120) S.gpuHist.shift(); }
  }

  function renderHealth(snap) {
    const h = snap.health || {}, g = h.gpu || {};
    if (g.ok) {
      const vr = g.mem_used / g.mem_total;
      Charts.gauge($("#g-util"), (g.util || 0) / 100, `${Math.round(g.util || 0)}%`, false);
      Charts.gauge($("#g-vram"), vr, `${(g.mem_used / 1024).toFixed(1)}G`, vr > 0.97);
      $("#gpu-name").textContent = g.name || "";
      Charts.spark($("#ch-gpu"), S.gpuHist, 100);
      $("#gpu-kv").innerHTML = `<div><b>${(g.mem_used / 1024).toFixed(1)}/${(g.mem_total / 1024).toFixed(0)}</b><span>VRAM GB</span></div>
        <div><b>${g.temp ?? "–"}°</b><span>temp</span></div><div><b>${g.power != null ? Math.round(g.power) : "–"}W</b><span>power</span></div>`;
      $("#gpu-apps").innerHTML = Live.gpuAppsHtml(g);
    } else {
      $("#gpu-kv").innerHTML = `<div style="grid-column:1/-1" class="warn-text">${esc(g.error || "no GPU data")}</div>`;
    }
    const ace = h.ace?.enabled !== false;  // ACE-Step was removed
    $("#ace-card").classList.toggle("hidden", !ace);
    if (ace) $("#ace-box").innerHTML = Live.aceHtml(h.ace) + Live.journalHtml(h.journal);
    $("#local-box").innerHTML = V3.localHtml(snap);
    $("#fb-box").innerHTML = V3.fallbackHtml(snap);
    $("#proc-box").innerHTML = Live.procsHtml(snap.processes);
    const n = h.tracks_today ?? 0, cap = h.tracks_cap || 6;
    $("#cap-box").innerHTML = `<div class="cap-big">${n}<small> / ${cap}</small></div>
      <div class="segs ${n >= cap ? "full" : ""}">${Array.from({ length: cap }, (_, i) => `<i class="${i < n ? "on" : ""}"></i>`).join("")}</div>
      <div class="muted" style="font-size:12px;margin-top:6px">${n >= cap ? "Daily cap reached, Backlog waits for tomorrow." : `${cap - n} more allowed today`}</div>`;
    const d = h.disk || {};
    $("#disk-path").textContent = d.path || "";
    $("#disk-box").innerHTML = d.ok ? `<div class="disk-bar ${d.warn ? "warn" : ""}"><i style="width:${d.used_pct}%"></i></div>
      <div><b class="${d.warn ? "warn-text" : ""}" style="font-family:var(--mono);font-size:20px">${d.free_gb} GB free</b> <span class="muted">of ${d.total_gb} GB · ${d.used_pct}% used</span></div>
      ${d.warn ? `<div class="warn-text">⚠ Under ${d.warn_gb} GB: watchdog will PAUSE the machine.</div>` : ""}` : `<span class="warn-text">${esc(d.error || "no disk data")}</span>`;
    $("#svc-box").innerHTML = (h.services || []).map(s => `<div class="svc-row"><span class="sd ${esc(s.status)}"></span><b>${esc(s.name)}</b><span class="via">${esc(s.status)} · ${esc(s.via)}</span></div>`).join("") || `<span class="muted">checking…</span>`;
    $("#src-box").innerHTML = Live.freshnessHtml(snap);
    const ck = JSON.stringify([snap.machine, snap.controls?.enabled, (snap.tracks || []).map(t => t.slug), (snap.processes || []).map(p => [p.card, p.script]),
      (h.services || []).map(s => s.status)]);
    if ($("#ctl-box").dataset.key !== ck && !$("#ctl-box").contains(document.activeElement)) { $("#ctl-box").dataset.key = ck; $("#ctl-box").innerHTML = Control.centerHtml(snap); }
    $("#ctl-audit-m").innerHTML = Control.auditHtml(snap.audit);
  }

  // ---------------- chat ----------------
  function agentColor(name) {
    const n = String(name || "").toLowerCase();
    for (const k in AGENT_COLORS) if (n.includes(k)) return AGENT_COLORS[k];
    let h = 0; for (const c of n) h = (h * 31 + c.charCodeAt(0)) % 360;
    return `hsl(${h} 90% 65%)`;
  }

  function renderChat(snap) {
    const c = snap.chat || {}, msgs = c.messages || [];
    const act = S.feed === "activity";
    $("#chat").classList.toggle("hidden", act); $("#thread-row").classList.toggle("hidden", act); $("#act-m").classList.toggle("hidden", !act);
    if (act && S.screen === "chat") return renderActivityM(snap);
    if (S.screen !== "chat" && msgs.length > S.seenMsgs && S.seenMsgs) $("#chat-dot").classList.remove("hidden");
    if (!S.seenMsgs && msgs.length && S.screen !== "chat") S.seenMsgs = msgs.length;
    if (S.screen !== "chat") return;
    S.seenMsgs = msgs.length;
    const threads = c.threads || [...new Set(msgs.map(m => m.thread).filter(Boolean))].map(id => ({ id, slug: null, count: 0 }));
    const tkey = threads.map(t => t.id + t.count).join("|") + S.thread;
    if ($("#thread-row").dataset.key !== tkey) {
      $("#thread-row").dataset.key = tkey;
      $("#thread-row").innerHTML = threads.length ? `<button class="toggle" aria-pressed="${!S.thread}" data-thread="">All threads</button>` +
        threads.map(t => `<button class="toggle" aria-pressed="${S.thread === t.id}" data-thread="${esc(t.id)}">${t.slug ? "♪ " + esc(t.slug) : esc(t.id)}${t.count ? ` <b>${Number(t.count)}</b>` : ""}</button>`).join("") : "";
    }
    const shown = msgs.filter(m => !S.thread || m.thread === S.thread);
    const box = $("#chat");
    const key = shown.map(m => m.id).join(",") + c.status + S.thread + (c.identity?.name || "");
    if (box.dataset.key === key) return;
    const atBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 80;
    box.dataset.key = key;
    let html = c.status === "ok" ? `<div class="mail-id">${c.identity ? `Reading as <b>${esc(c.identity.name)}</b> (read-only) · ` : ""}${esc(c.source || "")} · ${msgs.length} message${msgs.length === 1 ? "" : "s"}</div>` : "";
    if (c.status !== "ok") {
      html += `<div class="glass" style="font-size:13px"><b style="color:var(--amber)">agent-mail: ${esc(c.status)}</b><br><span class="muted">${esc(c.error || "")}</span>
        ${(c.agents || []).length ? `<br><br>Agents in project: ${(c.agents || []).map(a => `<span class="mono" style="color:${agentColor(a.label || a.name)}">${esc(a.label || a.name)}</span> <small class="muted">${esc(a.label && a.label !== a.name ? a.name : a.program || "")}</small>`).join(", ")}` : ""}</div>`;
    }
    html += shown.map(m => {
      const who = m.label || m.from || "?";
      const right = /hermes/i.test(who);
      const col = agentColor(who);
      return `<div class="msg ${right ? "right" : ""}"><div class="avatar" style="background:${col};box-shadow:0 0 12px ${col}">${esc(who.slice(0, 2).toUpperCase())}</div>
        <div class="bubble"><div class="who" style="color:${col}" title="${esc(m.from)}">${esc(who)}</div><div class="txt">${esc(m.body || m.subject)}</div>
        <div class="meta"><span class="thread-tag">${esc(m.thread || "")}</span><span>${ago(m.ts)}</span></div></div></div>`;
    }).join("");
    if (!shown.length && c.status === "ok") html += `<div class="empty glass">${S.thread ? `No messages in <b>${esc(S.thread)}</b> yet.<br><small>Each lane posts a one-line note here when it finishes a card.</small>` : "No messages yet."}</div>`;
    box.innerHTML = html;
    if (atBottom) window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
  }

  // ---------------- wiring ----------------
  const bots = new Bots($("#bots-canvas"), (lane, L) => { haptic(12); botSheet(lane, L); });
  const player = new Player({
    a: $("#audio-a"), b: $("#audio-b"), spectrum: $("#spectrum"), scope: $("#scope"),
    meterL: $("#meter-l"), meterR: $("#meter-r"), meterDb: $("#meter-db"),
    onState: (on, msg) => { $("#play-btn").textContent = on ? "❚❚" : "▶"; $("#play-btn").classList.toggle("on", on); if (msg) toast(msg, true); },
  });

  function wire() {
    $("#tabbar").addEventListener("click", e => { const b = e.target.closest("button"); if (b) { haptic(6); show(b.dataset.screen); } });
    $("#lane-strip").addEventListener("click", e => { const b = e.target.closest(".lane-pill"); if (!b) return; haptic(5); S.laneFilter = b.dataset.lane || null; renderPipeline(S.snap); });
    $("#track-cards").addEventListener("click", e => {
      const b = e.target.closest("[data-act]"); if (b) return openReview(b.dataset.act, b.dataset.task, b.dataset.slug);
      const c = e.target.closest(".card[data-slug]"); if (c) trackSheet(c.dataset.slug);
    });
    $("#sheet-body").addEventListener("click", e => {
      const r = e.target.closest("[data-act]"); if (r && !r.id) return openReview(r.dataset.act, r.dataset.task, r.dataset.slug);
      const b = e.target.closest("[data-play]"); if (!b) return;
      const a = (S.snap.audio || []).find(x => x.url === b.dataset.play);
      if (a) { closeSheet(); show("listen"); loadTake(a); player.play(); }
    });
    $("#sheet-backdrop").addEventListener("click", closeSheet);
    let sy = null;
    $("#sheet").addEventListener("touchstart", e => { sy = e.touches[0].clientY; }, { passive: true });
    $("#sheet").addEventListener("touchend", e => { if (sy != null && e.changedTouches[0].clientY - sy > 80 && $("#sheet").scrollTop <= 0) closeSheet(); sy = null; });
    $("#mode-chip").addEventListener("click", () => {
      haptic(10); S.demo = !S.demo;  // per visit only: a reload always comes back LIVE
      S.prevLanes = {}; S.nowUrl = null; S.knownAudio.clear(); S.audioInit = false; S.gpuHist = [];
      player.pause(); connect(); toast(S.demo ? "Demo mode: fake data" : "Live mode");
    });
    $("#play-btn").addEventListener("click", () => { haptic(10); player.toggle(); });
    $("#ab-a").addEventListener("click", () => { haptic(6); player.setSide("A"); $("#ab-a").setAttribute("aria-pressed", "true"); $("#ab-b").setAttribute("aria-pressed", "false"); });
    $("#ab-b").addEventListener("click", () => { haptic(6); player.setSide("B"); $("#ab-b").setAttribute("aria-pressed", "true"); $("#ab-a").setAttribute("aria-pressed", "false"); });
    $("#follow-btn").addEventListener("click", () => { S.follow = !S.follow; $("#follow-btn").setAttribute("aria-pressed", String(S.follow)); haptic(6); });
    $("#seek").addEventListener("input", e => player.seek(e.target.value / 1000));
    $("#audio-list").addEventListener("click", e => {
      const r = e.target.closest(".row"); if (!r) return; haptic(8);
      const a = (S.snap.audio || []).find(x => x.url === r.dataset.url); if (a) { loadTake(a); player.play(); }
    });
    $("#listen-cards").addEventListener("click", e => { const b = e.target.closest("[data-card]"); if (!b) return; S.listenCard = b.dataset.card || null; renderListen(S.snap); });
    $("#score-cards").addEventListener("click", e => { const b = e.target.closest("[data-score]"); if (!b) return; haptic(5); S.scoreCard = b.dataset.score; renderScores(S.snap); });
    $("#thread-row").addEventListener("click", e => { const b = e.target.closest("[data-thread]"); if (!b) return; S.thread = b.dataset.thread || null; renderChat(S.snap); });
    $("#bell-m").addEventListener("click", alertsSheet);
    Control.init({ toast, demo: () => S.snap?.mode === "demo" });
    Control.wire($("#ctl-box")); Control.wire($("#sheet-body")); Control.wireTrack($("#sheet-body"));
    $("#feed-tabs").addEventListener("click", e => {
      const b = e.target.closest("button"); if (!b) return; haptic(5); S.feed = b.dataset.f;
      document.querySelectorAll("#feed-tabs button").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
      $("#chat").dataset.key = ""; if (S.snap) renderChat(S.snap);
    });
    $("#logout-btn").addEventListener("click", async () => { await fetch("/api/logout", { method: "POST" }); location.reload(); });
    setInterval(() => {
      const el = player.active();
      if (el.duration && isFinite(el.duration)) { $("#seek").value = Math.round(1000 * el.currentTime / el.duration); $("#t-cur").textContent = fmtT(el.currentTime); $("#t-dur").textContent = fmtT(el.duration); }
    }, 250);
    setInterval(stamp, 1000);
    window.addEventListener("resize", () => S.snap && render(S.snap));
  }

  function showLogin() { $("#app").classList.add("hidden"); $("#login").classList.remove("hidden"); }

  async function boot() {
    const r = await fetch("/api/session").then(x => x.json()).catch(() => ({ authed: false }));
    if (!r.authed) return showLogin();
    $("#login").classList.add("hidden"); $("#app").classList.remove("hidden");
    const q = new URLSearchParams(location.search);
    try { localStorage.removeItem("tekno-demo"); } catch (e) {}  // old builds remembered demo mode
    S.demo = q.has("demo") ? q.get("demo") === "1" : !!r.demo_default;
    if (q.get("screen")) S.screen = q.get("screen");
    show(S.screen);
    connect();
  }

  buildKeypad(); wire(); boot();
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
})();

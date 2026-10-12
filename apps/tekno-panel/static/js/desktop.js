/* TEKNO CONTROL desktop control room: every panel visible at once, kanban board, detail drawer, keyboard control.
   Shares the WebSocket snapshot, bots.js, player.js and charts.js with the phone UI (js/app.js). */
(function () {
  "use strict";
  const LANES = ["Plan", "Generate", "Stems", "Sound Swap", "Edit", "Filter", "Variation", "Mix", "Polish", "Master", "Score", "Review", "Done"];
  const AGENT_COLORS = { "grok bot": "#39ff14", "mix judge": "#ffb020", "claude": "#ff8a5c", "hermes": "#c77dff" };
  const $ = s => document.querySelector(s);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
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
  const STATE_LABEL = { working: "working", failed: "failed", review: "review", done: "done", queued: "queued", offboard: "files only" };

  const S = {
    snap: null, demo: false, ws: null, retry: 0, filter: store.get("tekno-dk-filter", ""), query: "",
    prevLanes: {}, cards: new Map(), drawer: null, listenCard: null, scoreCard: null, chartTab: "lufs", thread: null,
    follow: true, nowUrl: null, knownAudio: new Set(), audioInit: false, gpuHist: [], lastGpuTs: null, wsOpen: false,
  };

  function toast(msg, bad) {
    const t = $("#toast"); t.textContent = msg; t.classList.toggle("bad", !!bad); t.classList.remove("hidden");
    clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), 2800);
  }

  // ---------------- login ----------------
  $("#dk-login-form").addEventListener("submit", async e => {
    e.preventDefault();
    const r = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ passcode: $("#dk-pass").value }) });
    const j = await r.json().catch(() => ({}));
    if (r.ok) { $("#dk-pass").value = ""; $("#dk-login-err").textContent = ""; boot(); }
    else { $("#dk-login-err").textContent = j.error || "Wrong passcode"; $("#dk-pass").select(); }
  });
  function showLogin() { $("#dk-app").classList.add("hidden"); $("#dk-login").classList.remove("hidden"); $("#dk-pass").focus(); }

  // ---------------- socket ----------------
  function connect() {
    if (S.ws) { S.ws.onclose = null; S.ws.close(); }
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws${S.demo ? "?demo=1" : ""}`);
    S.ws = ws;
    ws.onopen = () => { S.retry = 0; S.wsOpen = true; $("#live-dot").classList.add("on"); };
    ws.onmessage = e => { try { render(JSON.parse(e.data)); } catch (err) { console.error(err); } };
    ws.onclose = ev => {
      S.wsOpen = false; $("#live-dot").classList.remove("on"); stamp();
      if (ev.code === 4401) return showLogin();
      setTimeout(connect, Math.min(10000, 500 * 2 ** S.retry++));
    };
  }

  // ---------------- render ----------------
  function render(snap) {
    S.snap = snap;
    const chip = $("#mode-chip");
    chip.textContent = snap.mode === "demo" ? "DEMO" : "LIVE"; chip.classList.toggle("demo", snap.mode === "demo");
    stamp();
    const errs = Object.entries(snap.sources || {}).filter(([k, v]) => v && v.ok === false && k !== "mail").map(([k, v]) => `${k}: ${v.error}`);
    $("#banner").textContent = errs.join(" · "); $("#banner").classList.toggle("hidden", !errs.length);
    const g = snap.health?.gpu;
    if (g && g.ok && snap.ts !== S.lastGpuTs) { S.lastGpuTs = snap.ts; S.gpuHist.push(g.util || 0); if (S.gpuHist.length > 160) S.gpuHist.shift(); }
    renderKpis(snap); renderBoard(snap); bots.update(snap.lanes || {}); renderListen(snap);
    renderScores(snap); renderHealth(snap); renderChat(snap); renderDrawer(); renderPage(snap);
  }

  // Time of the last snapshot the machine pushed; turns amber when it stops ticking.
  function stamp() {
    const el = $("#upd"); if (!el || !S.snap || !S.snap.ts) return;
    const ts = S.snap.ts, age = Math.max(0, Date.now() / 1000 - ts);
    const t = new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
    const stale = !S.wsOpen || age > 10;
    el.innerHTML = `<span class="muted">updated</span> ${esc(t)}${stale ? ` <span>· ${esc(ago(ts))}</span>` : ""}`;
    el.classList.toggle("stale", stale);
    el.title = `Last update from the machine: ${new Date(ts * 1000).toLocaleString()}`;
  }

  function renderKpis(snap) {
    const h = snap.health || {}, g = h.gpu || {}, a = h.ace || {}, d = h.disk || {};
    const trs = snap.tracks || [];
    const working = trs.filter(t => t.state === "working").length, failed = trs.filter(t => t.state === "failed").length;
    const review = trs.filter(t => t.state === "review").length;
    const svcs = h.services || [];
    const vr = g.ok ? g.mem_used / g.mem_total : 0;
    const k = [];
    const loc = snap.local;
    if (a.enabled === false) k.push(`<div class="kpi ${loc?.ok ? "good" : "bad"}" title="${esc(loc?.model || "")}">Qwen <b>${loc ? (loc.ok ? "up" : "down") : "?"}</b></div>`);
    else k.push(`<div class="kpi ${a.status === "generating" ? "good" : a.ok ? "" : "bad"}">ACE <b>${esc(a.status || "?")}${a.job ? ` ${Number(a.progress ?? 0)}%` : ""}</b>${a.job ? `<span class="mono">${esc(a.job.card || "UI take")}</span>` : ""}</div>`);
    const pr = snap.processes || [];
    k.push(`<div class="kpi ${pr.length ? "good" : ""}" title="${esc(pr.map(p => `${p.script} · ${p.card || "?"} · pid ${p.pid}`).join("\n") || "no lane script running")}">Running <b>${pr.length ? esc(pr[pr.length - 1].script) : "–"}</b>${pr.length > 1 ? `<span class="mono">+${pr.length - 1}</span>` : ""}</div>`);
    if (g.ok) {
      k.push(`<div class="kpi">GPU <b>${Math.round(g.util || 0)}%</b><span class="mini"><i style="width:${Math.round(g.util || 0)}%"></i></span></div>`);
      k.push(`<div class="kpi ${vr > 0.97 ? "warn" : ""}">VRAM <b>${(g.mem_used / 1024).toFixed(1)}/${(g.mem_total / 1024).toFixed(0)}G</b></div>`);
      k.push(`<div class="kpi opt">Temp <b>${g.temp ?? "–"}°</b></div>`);
    } else k.push(`<div class="kpi bad">GPU <b>n/a</b></div>`);
    if (d.ok) k.push(`<div class="kpi ${d.warn ? "bad" : ""}">Disk <b>${d.free_gb} GB</b> free</div>`);
    k.push(`<div class="kpi ${(h.tracks_today ?? 0) >= (h.tracks_cap || 6) ? "warn" : ""}">Today <b>${h.tracks_today ?? 0}/${h.tracks_cap || 6}</b></div>`);
    k.push(`<div class="kpi">Tracks <b class="mono" style="color:var(--acid)">${working}</b>run <b style="color:#ff6b85">${failed}</b>fail <b style="color:var(--uv-hot)">${review}</b>rev</div>`);
    k.push(`<div class="kpi opt ${svcs.some(s => s.status !== "up") ? "bad" : ""}" title="${esc(svcs.map(s => `${s.name}: ${s.status}`).join("\n"))}">Services <span class="dots">${svcs.map(s => `<i class="sd ${esc(s.status)}"></i>`).join("")}</span></div>`);
    $("#kpis").innerHTML = k.join("");
  }

  // ---------------- pipeline board ----------------
  const laneEls = {};
  function buildBoard() {
    const board = $("#board");
    for (const lane of LANES) {
      const el = document.createElement("div");
      el.className = "lane"; el.dataset.lane = lane;
      el.innerHTML = `<div class="lane-head"><i class="bd"></i><span>${esc(lane)}</span><b>0</b></div><div class="lane-cards"></div>`;
      board.appendChild(el);
      laneEls[lane] = { el, count: el.querySelector("b"), dot: el.querySelector(".bd"), list: el.querySelector(".lane-cards") };
    }
  }

  function visible(t) {
    if (S.filter && t.state !== S.filter) return false;
    if (S.query && !t.slug.toLowerCase().includes(S.query)) return false;
    return true;
  }

  function renderBoard(snap) {
    const trs = snap.tracks || [];
    const shown = trs.filter(visible);
    const c = { working: 0, failed: 0, review: 0 };
    trs.forEach(t => { if (c[t.state] != null) c[t.state]++; });
    $("#pipe-sum").textContent = `${trs.length} tracks · ${c.working} working · ${c.failed} failed · ${c.review} in review`;
    const byLane = {};
    shown.forEach(t => (byLane[t.lane] = byLane[t.lane] || []).push(t));
    const seen = new Set();
    for (const lane of LANES) {
      const L = laneEls[lane], here = byLane[lane] || [], bot = (snap.lanes || {})[lane];
      L.count.textContent = here.length;
      L.el.classList.toggle("empty", !here.length);
      L.el.classList.toggle("hot", here.some(t => t.state === "working"));
      L.el.classList.toggle("bad", here.some(t => t.state === "failed"));
      L.dot.className = `bd ${bot ? bot.state : ""}`;
      for (const t of here) {
        seen.add(t.slug);
        let el = S.cards.get(t.slug);
        if (!el) { el = document.createElement("div"); el.className = "tcard"; el.dataset.slug = t.slug; S.cards.set(t.slug, el); }
        const moved = S.prevLanes[t.slug] && S.prevLanes[t.slug] !== t.lane;
        S.prevLanes[t.slug] = t.lane;
        const key = JSON.stringify([t.lane, t.state, t.progress, t.loops, t.last_sendback, t.files_lane, t.review_task_id, t.current_task?.last_error, S.drawer?.slug === t.slug,
          t.live?.script, t.live?.pid, t.board_behind, (t.runs || []).length, t.plan?.bpm, t.brief]);
        if (el.dataset.key !== key) {
          el.dataset.key = key;
          el.className = `tcard state-${t.state}${S.drawer?.slug === t.slug ? " sel" : ""}`;
          el.innerHTML = cardHtml(t);
        }
        if (moved) { el.classList.remove("moved"); void el.offsetWidth; el.classList.add("moved"); }
        L.list.appendChild(el);  // appendChild moves it in order without re-creating (keeps hover)
      }
    }
    for (const [slug, el] of S.cards) if (!seen.has(slug)) { el.remove(); S.cards.delete(slug); }
  }

  function cardHtml(t) {
    const sb = t.last_sendback;
    const err = t.state === "failed" && t.current_task?.last_error && !(sb && sb.task_id === t.current_task.id) ? `<div class="tc-err">${esc(t.current_task.last_error)}</div>` : "";
    const r = (t.runs || [])[(t.runs || []).length - 1];
    const note = t.board_behind ? `<div class="tc-note" title="Board last moved ${esc(Live.ago(t.board_activity))}, card folder ${esc(Live.ago(t.disk_mtime))}">board behind disk${r ? ` · ${esc(r.name)} ${esc(r.verdict || "")}${r.draft != null ? " " + Live.num(r.draft) : ""}` : ""}</div>`
      : t.files_lane && t.files_lane !== t.lane && t.state !== "done" ? `<div class="tc-note">on disk: ${esc(t.files_lane)}</div>` : "";
    const act = t.lane === "Review" && t.review_task_id ? `<div class="tc-actions">
        <button class="btn approve" data-act="approve" data-task="${esc(t.review_task_id)}" data-slug="${esc(t.slug)}">Approve</button>
        <button class="btn reject" data-act="reject" data-task="${esc(t.review_task_id)}" data-slug="${esc(t.slug)}">Reject</button></div>` : "";
    return `<div class="tc-slug" title="${esc(t.slug)}">${esc(t.slug)}</div>${V3.chips(t)}
      <div class="tc-top"><span class="pill sm ${esc(t.state)}">${esc(STATE_LABEL[t.state] || t.state)}</span>${t.loops ? `<span class="tc-loop">↺${Number(t.loops)}</span>` : ""}<b class="tc-pct">${Number(t.progress) || 0}%</b></div>
      <div class="tc-bar"><i style="width:${Math.max(2, Number(t.progress) || 0)}%"></i></div>
      ${t.live ? `<div class="tc-live" title="${esc(t.live.script)}${t.live.pid ? " · pid " + Number(t.live.pid) : ""} · seen via ${esc(t.live.via)}"><i></i><span>${esc(t.live.script)}</span></div>` : ""}${sb ? `<div class="tc-back${sb.hypothetical ? " dry" : ""}"><b>${esc(sb.from || "?")} → ${esc(sb.to)}</b>${sb.hypothetical ? " (dry run)" : ""} ${esc(sb.reason)}</div>` : ""}${err}${note}${act}`;
  }

  // ---------------- drawer (track or lane details) ----------------
  function openDrawer(d) { S.drawer = d; $("#drawer").classList.add("open"); $("#drawer").setAttribute("aria-hidden", "false"); $("#drawer-body").dataset.key = ""; renderDrawer(); if (S.snap) renderBoard(S.snap); }
  function closeDrawer() { S.drawer = null; $("#drawer").classList.remove("open"); $("#drawer").setAttribute("aria-hidden", "true"); if (S.snap) renderBoard(S.snap); }

  function renderDrawer() {
    if (!S.drawer || !S.snap) return;
    const body = $("#drawer-body");
    if (S.drawer.slug) {
      const t = (S.snap.tracks || []).find(x => x.slug === S.drawer.slug);
      if (!t) { body.innerHTML = `<h2>${esc(S.drawer.slug)}</h2><p class="muted">This track is no longer on the board.</p>`; return; }
      const audio = (S.snap.audio || []).filter(a => a.card === t.slug);
      const score = (S.snap.scores || []).find(s => s.card === t.slug);
      const msgs = (S.snap.chat?.messages || []).filter(m => m.thread === `card-${t.slug}`);
      const key = JSON.stringify([t, audio.map(a => a.url + a.mtime), score?.mtime, S.nowUrl, msgs.length]);
      if (body.dataset.key === key) return;
      body.dataset.key = key;
      const chain = (t.tasks || []).map(x => `<div class="chain-row"><i class="cd ${esc(x.status)}"></i><span>${esc(x.lane || "?")}</span><span class="id">${esc(x.id)}</span><span class="st">${esc(x.status)}</span></div>`).join("");
      body.innerHTML = `<h2>${esc(t.slug)}</h2>${Control.trackButtons(t, S.snap)}
        <div><span class="pill ${esc(t.state)}">${esc(STATE_LABEL[t.state] || t.state)}</span> <span class="muted">lane <b style="color:var(--text)">${esc(t.lane)}</b>${t.board_lane && t.board_lane !== t.lane ? ` · board: ${esc(t.board_lane)}` : ""}${t.loops ? ` · <span style="color:var(--amber)">${Live.loops(t.loops)}${t.loops_src === "disk" ? " (run logs)" : ""}</span>` : ""}${t.files_lane && t.files_lane !== t.lane ? ` · on disk: ${esc(t.files_lane)}` : ""}</span></div>
        <div class="progress"><div class="bar"><i style="width:${Math.max(2, Number(t.progress) || 0)}%"></i></div><span class="pct">${Number(t.progress) || 0}%</span></div>
        ${Live.liveLine(t)}${Live.behindNote(t)}
        <h3>Plan</h3>${Live.planHtml(t.plan)}
        <h3>Runs on disk <small>${(t.runs || []).length}${t.attempts ? ` · ${t.attempts} archived attempts` : ""}</small></h3>${Live.runsHtml(t.runs)}
        ${t.lane === "Review" && t.review_task_id ? `<div class="review-actions"><button class="btn approve" data-act="approve" data-task="${esc(t.review_task_id)}" data-slug="${esc(t.slug)}">Approve</button><button class="btn reject" data-act="reject" data-task="${esc(t.review_task_id)}" data-slug="${esc(t.slug)}">Reject + note</button></div>` : ""}
        ${t.current_task ? `<h3>Current task</h3><div class="mono" style="font-size:13px">${esc(t.current_task.title)}</div><div class="muted" style="font-size:12px">${esc(t.current_task.id)} · ${esc(t.current_task.status)}${t.current_task.assignee ? " · " + esc(t.current_task.assignee) : ""}</div>${t.current_task.last_error ? `<div class="tc-err" style="-webkit-line-clamp:6">${esc(t.current_task.last_error)}</div>` : ""}` : ""}
        ${t.sendbacks?.length ? `<h3>Routing</h3>${t.sendbacks.map(s => `<div class="sb-row${s.hypothetical ? " dry" : ""}"><b>${esc(s.from || "?")} → ${esc(s.to)}</b>${s.hypothetical ? " <i>dry run</i>" : ""} ${esc(s.reason)}</div>`).join("")}` : ""}
        ${t.handoff?.length ? `<h3>Handoff</h3>${Live.handoffHtml(t.handoff)}` : ""}
        ${chain ? `<h3>Lane chain</h3><div class="chain">${chain}</div>` : ""}
        ${score ? `<h3>Score · <span class="${score.pass ? "" : "warn-text"}">${esc(score.verdict)}</span> <small>${esc(Live.ago(score.mtime))}</small></h3><div class="badges">${badgesHtml(score.badges)}</div>
          <h3>vs SIFRES v02</h3>${Live.refHtml(score)}${score.draft_scores ? `<h3>Auto-draft <small>${Live.num(score.metrics?.draft)}/10</small></h3>${Live.draftHtml(score)}` : ""}` : ""}
        ${Object.keys(t.reports || {}).length ? `<h3>Lane reports</h3>${Live.reportsHtml(t.reports)}` : ""}
        <h3>Thread <small>card-${esc(t.slug)}</small></h3>${Live.threadMsgsHtml(msgs, agentColor) || `<div class="muted">No messages in this thread yet.</div>`}
        <h3>Audio <small>${audio.length} files</small></h3>
        <div class="list">${audio.length ? audio.map(a => rowHtml(a)).join("") : `<div class="muted">No renders on disk yet.</div>`}</div>`;
    } else if (S.drawer.lane) {
      const lane = S.drawer.lane, L = (S.snap.lanes || {})[lane] || { state: "idle", log: [] };
      const here = (S.snap.tracks || []).filter(t => t.lane === lane);
      const key = JSON.stringify([L, here.map(t => t.slug + t.state)]);
      if (body.dataset.key === key) return;
      body.dataset.key = key;
      const col = { working: "var(--acid)", failed: "var(--red)", waiting: "var(--uv-hot)" }[L.state] || "var(--muted)";
      const log = (L.log || []).map(x => `${x.at ? new Date(x.at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : ""}  ${x.text}`).join("\n");
      body.innerHTML = `<h2>${esc(lane)} bot</h2><span class="state-tag" style="color:${col}">${esc(L.state === "idle" ? "sleeping" : L.state)}</span>
        <h3>Current task</h3>${L.task ? `<div class="mono" style="font-size:13px">${esc(L.task.title)}</div><div class="muted" style="font-size:12px">${esc(L.task.id || "")}${L.task.assignee ? " · " + esc(L.task.assignee) : ""}${L.task.via ? ` · seen via ${esc(L.task.via)}` : ""}${L.task.since ? ` · ${Live.dur(Date.now() / 1000 - L.task.since)}` : ""}</div>${L.task.error ? `<div class="tc-err" style="-webkit-line-clamp:8">${esc(L.task.error)}</div>` : ""}` : `<div class="muted">No card in this lane right now.</div>`}
        ${L.handoff_to ? `<p class="muted">Last hand-off → <b style="color:var(--acid)">${esc(L.handoff_to)}</b> ${ago(L.handoff_at)}</p>` : ""}
        <h3>Tracks here</h3>${here.length ? here.map(t => `<div class="sb-row" style="background:rgba(57,255,20,.05);border-color:var(--line);cursor:pointer" data-open="${esc(t.slug)}"><b style="color:var(--acid)">${esc(t.slug)}</b> ${esc(STATE_LABEL[t.state] || t.state)} · ${Number(t.progress) || 0}%</div>`).join("") : `<div class="muted">none</div>`}
        <h3>Last log lines</h3><div class="log" style="max-height:340px">${esc(log || "nothing logged yet")}</div>`;
    }
  }

  function badgesHtml(badges) { return Live.badgesHtml(badges); }
  function rowHtml(a, newest) {
    return `<div class="row ${a.url === S.nowUrl ? "playing" : ""}" data-url="${esc(a.url)}"><div class="ico ${a.kind === "stem" ? "stem" : ""}">${a.kind === "stem" ? "S" : "▶"}</div>
      <div class="meta"><div class="name">${esc(a.name)}</div><div class="sub">${esc(a.card)} · ${esc(a.stage)} · ${ago(a.mtime)}${a.size ? " · " + (a.size / 1e6).toFixed(1) + " MB" : ""}</div></div>${newest ? `<span class="new-badge">NEW</span>` : ""}</div>`;
  }

  // ---------------- review ----------------
  function openReview(act, task, slug) {
    $("#modal-box").innerHTML = `<h2>${act === "approve" ? "Approve" : "Reject"} ${esc(slug)}</h2>
      <p class="muted">${act === "approve" ? "Writes an APPROVED comment" : "Writes a REJECTED comment with your note"} on kanban task <span class="mono">${esc(task)}</span>.</p>
      <textarea id="rv-note" maxlength="1000" placeholder="${act === "approve" ? "Optional note, e.g. acid rolls nicely" : "What's wrong? e.g. kick too thin, bass muddy at 80 Hz"}"></textarea>
      <div class="modal-actions"><button class="ghost" id="rv-cancel">Cancel (Esc)</button><button class="btn ${act === "approve" ? "approve" : "reject"}" id="rv-go">${act === "approve" ? "Approve" : "Reject with note"}</button></div>`;
    $("#modal").classList.remove("hidden");
    $("#rv-note").focus();
    $("#rv-cancel").onclick = closeModal;
    $("#rv-go").onclick = async () => {
      const note = $("#rv-note").value.trim();
      if (act === "reject" && !note) { toast("Add a note first", true); return; }
      $("#rv-go").disabled = true;
      try {
        const r = await fetch(`/api/review/${encodeURIComponent(task)}`, {
          method: "POST", headers: { "Content-Type": "application/json", "x-tekno": "1" },
          body: JSON.stringify({ decision: act, note, demo: S.snap?.mode === "demo" }),
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(j.detail || r.statusText);
        closeModal();
        toast(j.demo ? "Demo: comment not written" : act === "approve" ? "Approved ✔ comment written" : "Rejected, note written");
      } catch (e) { toast(String(e.message || e), true); $("#rv-go").disabled = false; }
    };
  }
  function closeModal() { $("#modal").classList.add("hidden"); $("#modal-box").innerHTML = ""; }
  function showHelp() {
    $("#modal-box").innerHTML = `<h2>Keyboard</h2><div class="keys">
      <kbd>Space</kbd><span>play / pause</span><kbd>A</kbd> <span>take (A side)</span><kbd>B</kbd><span>reference track (B side)</span>
      <kbd>← →</kbd><span>seek 5 s</span><kbd>N</kbd><span>load newest take</span><kbd>1-4</kbd><span>filter: all / working / failed / review</span>
      <kbd>/</kbd><span>find a track</span><kbd>D</kbd><span>live / demo</span><kbd>F</kbd><span>fullscreen</span><kbd>Esc</kbd><span>close drawer / dialog</span></div>
      <div class="modal-actions" style="margin-top:14px"><button class="ghost" id="help-close">Close</button></div>`;
    $("#modal").classList.remove("hidden");
    $("#help-close").onclick = closeModal;
  }

  // ---------------- listen ----------------
  function loadTake(a, play) {
    S.nowUrl = a.url;
    player.load(a.url, a.name);
    $("#now-title").textContent = `${a.card} · ${a.name}`;
    $("#now-sub").textContent = `${a.stage} ${a.kind} · ${ago(a.mtime)}`;
    document.querySelectorAll(".row[data-url]").forEach(r => r.classList.toggle("playing", r.dataset.url === a.url));
    if (play) player.play();
  }
  function newestRender() { return (S.snap?.audio || []).filter(a => a.kind === "render").sort((x, y) => y.mtime - x.mtime)[0]; }

  function renderListen(snap) {
    const list = snap.audio || [];
    const fresh = list.filter(a => !S.knownAudio.has(a.url));
    list.forEach(a => S.knownAudio.add(a.url));
    if (S.follow && fresh.length && S.audioInit) {
      const n = fresh.filter(a => a.kind === "render").sort((x, y) => y.mtime - x.mtime)[0];
      if (n && n.url !== S.nowUrl) { loadTake(n); toast(`New take: ${n.card} · ${n.name}`); }
    }
    S.audioInit = true;
    if (snap.reference) player.loadRef(snap.reference.url);
    $("#ab-b").textContent = snap.reference ? `B · ${snap.reference.name || "reference"}` : "B · (no ref)";
    $("#ab-b").disabled = !snap.reference;
    if (!S.nowUrl && list.length) loadTake(list.find(a => a.kind === "render") || list[0]);
    const cards = [...new Set(list.map(a => a.card))];
    const ck = cards.join("|") + "/" + S.listenCard;
    if ($("#listen-cards").dataset.key !== ck) {
      $("#listen-cards").dataset.key = ck;
      $("#listen-cards").innerHTML = `<button class="toggle" aria-pressed="${!S.listenCard}" data-card="">All</button>` +
        cards.map(c => `<button class="toggle" aria-pressed="${S.listenCard === c}" data-card="${esc(c)}">${esc(c)}</button>`).join("");
    }
    const shown = list.filter(a => !S.listenCard || a.card === S.listenCard);
    const newest = list.length ? Math.max(...list.map(a => a.mtime)) : 0;
    const key = shown.map(a => a.url + a.mtime).join("|") + S.nowUrl;
    const box = $("#audio-list");
    if (box.dataset.key === key) return;
    box.dataset.key = key;
    box.innerHTML = shown.length ? shown.map(a => rowHtml(a, a.mtime === newest)).join("") : `<div class="empty">No renders yet.</div>`;
  }

  // ---------------- scores ----------------
  function renderScores(snap) {
    const sc = snap.scores || [];
    if (!sc.length) {
      $("#verdict-box").innerHTML = `<div class="muted">No score.json yet.</div>`; $("#badges").innerHTML = ""; $("#score-cards").innerHTML = "";
      Charts.lines($("#score-chart"), []); return;
    }
    const cur = sc.find(s => s.card === S.scoreCard) || sc[sc.length - 1];
    const key = sc.map(s => s.card).join("|") + cur.card;
    if ($("#score-cards").dataset.key !== key) {
      $("#score-cards").dataset.key = key;
      $("#score-cards").innerHTML = [...sc].reverse().map(s => `<button class="toggle" aria-pressed="${s.card === cur.card}" data-score="${esc(s.card)}">${s.pass ? "●" : "○"} ${esc(s.card)}</button>`).join("");
    }
    const go = cur.pass || cur.verdict === "GO";
    $("#verdict-box").innerHTML = `<div><span class="mono">${esc(cur.card)}</span> <span class="muted" style="font-size:12px">${ago(cur.mtime)}${cur.duration_s ? ` · ${fmtT(cur.duration_s)}` : ""}</span>
      ${cur.hard_fails?.length ? `<div class="hard">✖ ${cur.hard_fails.map(esc).join(" · ✖ ")}</div>` : ""}</div><div class="verdict ${go ? "go" : "nogo"}">${esc(cur.verdict || (go ? "GO" : "NO-GO"))}</div>`;
    $("#badges").innerHTML = badgesHtml(cur.badges);
    S.scoreCur = cur;
    drawScoreChart(sc);
  }
  function drawScoreChart(sc) {
    const cur = S.scoreCur || sc[sc.length - 1];
    const hist = (S.snap?.history || []).filter(h => h.card === cur.card);
    if (hist.length > 1) sc = hist;  // every distinct score.json of this card: attempts, handoff, overwritten versions
    $("#chart-note").textContent = hist.length > 1 ? `${cur.card}: ${hist.map(h => (h.runs || [h.run]).join("+")).join(" → ")}` : "one point per card";
    const m = sc.map(s => s.metrics || {}), cv = $("#score-chart");
    const ref = S.chartTab === "ref";
    cv.classList.toggle("hidden", ref); $("#ref-pane").classList.toggle("hidden", !ref);
    if (ref) { $("#ref-pane").innerHTML = Live.refHtml(cur); return; }
    if (S.chartTab === "knock") Charts.lines(cv, [{ values: m.map(x => x.knock), color: Charts.ACID, label: "knock dB", ok: v => v >= 3 }], { line: 3 });
    else if (S.chartTab === "lufs") Charts.lines(cv, [{ values: m.map(x => x.lufs), color: Charts.ACID, label: "LUFS", ok: v => Math.abs(v + 9) <= 0.5 }], { band: [-9.5, -8.5] });
    else if (S.chartTab === "tp") Charts.lines(cv, [{ values: m.map(x => x.tp), color: Charts.ACID, label: "dBTP", ok: v => v <= -1 }], { line: -1 });
    else {
      const pick = (s, label, fb) => { const b = (s.badges || []).find(x => x.label === label); const v = parseFloat(b && b.value); return isNaN(v) ? fb : v; };
      Charts.lines(cv, [{ values: sc.map(s => pick(s, "Mix Judge", s.metrics?.draft ?? null)), color: "#ffb020", label: "Mix Judge / draft", ok: v => v >= 6.5 },
        { values: sc.map(s => pick(s, "EarLoop", s.metrics?.earloop ?? null)), color: Charts.UV, label: "EarLoop", ok: v => v >= 6.5 }], { band: [6.5, 10] });
    }
  }

  // ---------------- health ----------------
  function renderHealth(snap) {
    const h = snap.health || {}, g = h.gpu || {}, a = h.ace || {}, d = h.disk || {};
    if (g.ok) {
      const vr = g.mem_used / g.mem_total;
      Charts.gauge($("#g-util"), (g.util || 0) / 100, `${Math.round(g.util || 0)}%`, false);
      Charts.gauge($("#g-vram"), vr, `${Math.round(vr * 100)}%`, vr > 0.97);
      $("#gpu-name").textContent = g.name || "";
      $("#gpu-kv").innerHTML = `<div><b>${(g.mem_used / 1024).toFixed(1)}G</b><span>of ${(g.mem_total / 1024).toFixed(0)}G</span></div>
        <div><b>${g.temp ?? "–"}°</b><span>temp</span></div><div><b>${g.power != null ? Math.round(g.power) : "–"}W</b><span>power</span></div>
        <div><b>${h.load?.["1m"] ?? "–"}</b><span>load</span></div>`;
      Charts.spark($("#ch-gpu"), S.gpuHist, 100);
    } else $("#gpu-kv").innerHTML = `<div class="warn-text">${esc(g.error || "no GPU data")}</div>`;
    const ace = a.enabled !== false;  // ACE-Step was removed: the box only shows when something still probes it
    $("#ace-box").innerHTML = `${ace ? `<h4>ACE-Step</h4>${Live.aceHtml(a)}${Live.journalHtml(h.journal)}` : ""}${Live.gpuAppsHtml(g)}
      <h4>Running now</h4>${Live.procsHtml(snap.processes)}`;
    $("#setup-box").innerHTML = V3.setupHtml(snap);
    $("#changes-box").innerHTML = V3.changesHtml(snap);
    $("#local-box").innerHTML = V3.localHtml(snap);
    $("#fb-box").innerHTML = V3.fallbackHtml(snap);
    $("#disk-box").innerHTML = d.ok ? `<div class="disk-line"><b class="${d.warn ? "warn-text" : ""}">${d.free_gb} GB</b> free on ${esc(d.path)} · ${d.used_pct}% used${d.warn ? ` · <span class="warn-text">under ${d.warn_gb} GB</span>` : ""}</div>
      <div class="disk-bar ${d.warn ? "warn" : ""}" style="height:8px"><i style="width:${Number(d.used_pct) || 0}%"></i></div>` : `<span class="warn-text">${esc(d.error || "no disk data")}</span>`;
    $("#svc-box").innerHTML = (h.services || []).map(s => `<div class="svc-row"><span class="sd ${esc(s.status)}"></span><b>${esc(s.name)}</b><span class="via">${esc(s.status)} · ${esc(s.via)}</span></div>`).join("")
      + `<h4>Sources <small>last real change</small></h4>${Live.freshnessHtml(snap)}`;
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
    const threads = c.threads || [...new Set(msgs.map(m => m.thread).filter(Boolean))].map(id => ({ id, slug: null, count: 0 }));
    const tk = threads.map(t => t.id + t.count).join("|") + S.thread;
    if ($("#thread-row").dataset.key !== tk) {
      $("#thread-row").dataset.key = tk;
      $("#thread-row").innerHTML = threads.length ? `<button class="toggle" aria-pressed="${!S.thread}" data-thread="">All</button>` +
        threads.slice(0, 8).map(t => `<button class="toggle" aria-pressed="${S.thread === t.id}" data-thread="${esc(t.id)}" title="${esc(t.id)}">${t.slug ? "♪ " + esc(t.slug) : esc(t.id)}${t.count ? ` <b>${Number(t.count)}</b>` : ""}</button>`).join("") : "";
    }
    const shown = msgs.filter(m => !S.thread || m.thread === S.thread);
    const box = $("#chat");
    const key = shown.map(m => m.id).join(",") + c.status + S.thread + (c.identity?.name || "");
    if (box.dataset.key === key) return;
    const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 60;
    box.dataset.key = key;
    let html = c.status === "ok" ? `<div class="mail-id">${c.identity ? `Reading as <b>${esc(c.identity.name)}</b> (read-only) · ` : ""}${esc(c.source || "")} · ${msgs.length} message${msgs.length === 1 ? "" : "s"}</div>` : "";
    if (c.status !== "ok") html += `<div class="glass" style="font-size:12px"><b style="color:var(--amber)">agent-mail: ${esc(c.status)}</b><br><span class="muted">${esc(c.error || "")}</span>
      ${(c.agents || []).length ? `<br>Agents: ${(c.agents || []).map(a => `<span class="mono" style="color:${agentColor(a.label || a.name)}" title="${esc(a.name)}">${esc(a.label || a.name)}</span>`).join(", ")}` : ""}</div>`;
    html += shown.map(m => {
      const who = m.label || m.from || "?";
      const col = agentColor(who);
      return `<div class="msg ${/hermes/i.test(who) ? "right" : ""}"><div class="avatar" style="background:${col};box-shadow:0 0 10px ${col}">${esc(who.slice(0, 2).toUpperCase())}</div>
        <div class="bubble"><div class="who" style="color:${col}" title="${esc(m.from)}">${esc(who)}</div><div class="txt">${esc(m.body || m.subject)}</div>
        <div class="meta"><span class="thread-tag">${esc(m.thread || "")}</span><span>${ago(m.ts)}</span></div></div></div>`;
    }).join("");
    if (!shown.length && c.status === "ok") html += `<div class="empty">${S.thread ? `No messages in <b>${esc(S.thread)}</b> yet. Lanes post a one-line note here when they finish a card.` : "No messages yet."}</div>`;
    box.innerHTML = html;
    if (atBottom) box.scrollTop = box.scrollHeight;
  }

  // ---------------- wiring ----------------
  const bots = new Bots($("#dk-bots"), lane => openDrawer({ lane }), { cols: w => (w / 12 >= 105 ? 12 : 6), oy: 0.38 });
  const player = new Player({
    a: $("#audio-a"), b: $("#audio-b"), spectrum: $("#spectrum"), scope: $("#scope"),
    meterL: $("#meter-l"), meterR: $("#meter-r"), meterDb: $("#meter-db"),
    onState: (on, msg) => { $("#play-btn").textContent = on ? "❚❚" : "▶"; $("#play-btn").classList.toggle("on", on); if (msg) toast(msg, true); },
  });

  function setSide(side) {
    player.setSide(side);
    $("#ab-a").setAttribute("aria-pressed", String(side === "A")); $("#ab-b").setAttribute("aria-pressed", String(side === "B"));
  }
  function setFilter(f) {
    S.filter = f; store.set("tekno-dk-filter", f);
    document.querySelectorAll("#state-filter button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.f === f)));
    if (S.snap) renderBoard(S.snap);
  }
  function toggleDemo() {
    S.demo = !S.demo;  // per visit only: a reload always comes back LIVE
    S.prevLanes = {}; S.nowUrl = null; S.knownAudio.clear(); S.audioInit = false; S.gpuHist = []; S.drawer && closeDrawer();
    S.cards.forEach(el => el.remove()); S.cards.clear();
    player.pause(); connect(); toast(S.demo ? "Demo mode: fake data" : "Live mode");
  }
  function toggleFs() { document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen().catch(() => {}); }

  function wire() {
    buildBoard();
    $("#board").addEventListener("click", e => {
      const b = e.target.closest("[data-act]");
      if (b) { e.stopPropagation(); return openReview(b.dataset.act, b.dataset.task, b.dataset.slug); }
      const c = e.target.closest(".tcard");
      if (c) (S.drawer?.slug === c.dataset.slug ? closeDrawer() : openDrawer({ slug: c.dataset.slug }));
    });
    $("#drawer").addEventListener("click", e => {
      const b = e.target.closest("[data-act]"); if (b) return openReview(b.dataset.act, b.dataset.task, b.dataset.slug);
      const r = e.target.closest(".row[data-url]");
      if (r) { const a = (S.snap.audio || []).find(x => x.url === r.dataset.url); if (a) loadTake(a, true); return; }
      const o = e.target.closest("[data-open]"); if (o) openDrawer({ slug: o.dataset.open });
    });
    $("#drawer-x").onclick = closeDrawer;
    $("#state-filter").addEventListener("click", e => { const b = e.target.closest("button"); if (b) setFilter(b.dataset.f); });
    $("#search").addEventListener("input", e => { S.query = e.target.value.trim().toLowerCase(); if (S.snap) renderBoard(S.snap); });
    $("#mode-chip").onclick = toggleDemo;
    $("#fs-btn").onclick = toggleFs;
    $("#help-btn").onclick = showHelp;
    $("#modal").addEventListener("click", e => { if (e.target.id === "modal") closeModal(); });
    $("#logout-btn").onclick = async () => { await fetch("/api/logout", { method: "POST" }); location.reload(); };
    $("#play-btn").onclick = () => player.toggle();
    $("#ab-a").onclick = () => setSide("A");
    $("#ab-b").onclick = () => setSide("B");
    $("#follow-btn").onclick = () => { S.follow = !S.follow; $("#follow-btn").setAttribute("aria-pressed", String(S.follow)); };
    $("#seek").addEventListener("input", e => player.seek(e.target.value / 1000));
    $("#audio-list").addEventListener("click", e => { const r = e.target.closest(".row"); if (!r) return; const a = (S.snap.audio || []).find(x => x.url === r.dataset.url); if (a) loadTake(a, true); });
    $("#listen-cards").addEventListener("click", e => { const b = e.target.closest("[data-card]"); if (!b) return; S.listenCard = b.dataset.card || null; renderListen(S.snap); });
    $("#score-cards").addEventListener("click", e => { const b = e.target.closest("[data-score]"); if (!b) return; S.scoreCard = b.dataset.score; renderScores(S.snap); });
    $("#chart-tabs").addEventListener("click", e => {
      const b = e.target.closest("button"); if (!b) return; S.chartTab = b.dataset.c;
      document.querySelectorAll("#chart-tabs button").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
      if (S.snap?.scores?.length) drawScoreChart(S.snap.scores);
    });
    $("#thread-row").addEventListener("click", e => { const b = e.target.closest("[data-thread]"); if (!b) return; S.thread = b.dataset.thread || null; renderChat(S.snap); });
    document.addEventListener("keydown", e => {
      if ($("#dk-app").classList.contains("hidden")) return;
      const typing = /INPUT|TEXTAREA/.test(document.activeElement?.tagName || "");
      if (e.key === "Escape") { if (!$("#modal").classList.contains("hidden")) closeModal(); else if (S.drawer) closeDrawer(); else if (typing) document.activeElement.blur(); return; }
      if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
      const el = player.active();
      const k = e.key.toLowerCase();
      if (e.key === " ") { e.preventDefault(); player.toggle(); }
      else if (k === "a") setSide("A");
      else if (k === "b" && S.snap?.reference) setSide("B");
      else if (e.key === "ArrowRight" && el.duration) el.currentTime = Math.min(el.duration, el.currentTime + 5);
      else if (e.key === "ArrowLeft" && el.duration) el.currentTime = Math.max(0, el.currentTime - 5);
      else if (k === "n") { const n = newestRender(); if (n) loadTake(n, true); }
      else if (k === "d") toggleDemo();
      else if (k === "f") toggleFs();
      else if (e.key === "/") { e.preventDefault(); $("#search").focus(); }
      else if (e.key === "?") showHelp();
      else if ("1234".includes(e.key)) setFilter(["", "working", "failed", "review"][Number(e.key) - 1]);
    });
    setInterval(() => {
      const el = player.active();
      if (el.duration && isFinite(el.duration)) { $("#seek").value = Math.round(1000 * el.currentTime / el.duration); $("#t-cur").textContent = fmtT(el.currentTime); $("#t-dur").textContent = fmtT(el.duration); }
    }, 250);
    setInterval(stamp, 1000);
    let rz;
    window.addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(() => { bots.layout(); if (S.snap) { renderHealth(S.snap); renderScores(S.snap); } }, 120); });
  }

  async function boot() {
    const r = await fetch("/api/session").then(x => x.json()).catch(() => ({ authed: false }));
    if (!r.authed) return showLogin();
    $("#dk-login").classList.add("hidden"); $("#dk-app").classList.remove("hidden");
    store.set("tekno-view", "");  // visiting the desk clears a "phone view" preference
    const q = new URLSearchParams(location.search);
    try { localStorage.removeItem("tekno-demo"); } catch (e) {}  // old builds remembered demo mode
    S.demo = q.has("demo") ? q.get("demo") === "1" : !!r.demo_default;
    setFilter(S.filter);
    bots.start();
    connect();
  }

  // ================= SaaS shell: pages, alerts, command palette =================
  const PAGES = ["overview", "tracks", "activity", "studio", "palette", "machine", "controls", "alerts"];
  Object.assign(S, { page: "overview", range: store.get("tekno-range", "1h"), actKind: "", actQ: "", actRows: null, actOlder: [],
    sort: { k: "last_activity", dir: -1 }, tracksQ: "", metrics: null, metricsAt: 0, cmdSel: 0 });
  const KIND_LABEL = { run: "run", kanban: "board", render: "render", ace: "ACE", service: "service", mail: "mail", alert: "alert", action: "you" };
  const dayName = ts => { const d = new Date(ts * 1000), t = new Date(); const y = new Date(t); y.setDate(t.getDate() - 1);
    return d.toDateString() === t.toDateString() ? "Today" : d.toDateString() === y.toDateString() ? "Yesterday" : d.toLocaleDateString([], { weekday: "long", day: "numeric", month: "short" }); };

  function route() {
    const m = location.hash.match(/^#\/(\w+)(?:\/([\w.-]+))?/) || [];
    S.page = PAGES.includes(m[1]) ? m[1] : "overview";
    document.querySelectorAll(".page").forEach(el => el.classList.toggle("hidden", el.id !== "page-" + S.page));
    document.querySelectorAll(".dk-rail a[data-page]").forEach(a => a.classList.toggle("active", a.dataset.page === S.page));
    if (S.page === "overview") { bots.start(); requestAnimationFrame(() => bots.layout()); } else bots.stop();
    if (m[1] === "tracks" && m[2]) openDrawer({ slug: m[2] });
    $("#dk-main").scrollTop = 0;
    if (S.page === "studio") V3.studio.start($("#studio-box")); else V3.studio.stop();  // frames only while the page is open
    if (S.page === "palette") V3.palette.mount($("#palette-box")); else V3.palette.stop();
    $("#agent-strip").classList.toggle("hidden", S.page !== "overview" || !(S.snap?.agents || []).length);
    if (S.page === "machine") loadMetrics(true);
    if (S.page === "activity") loadActivity();
    if (S.snap) renderPage(S.snap);
  }
  function go(page) { location.hash = "#/" + page; }

  function renderAgents(snap) {
    const list = snap.agents || [], strip = $("#agent-strip");
    strip.classList.toggle("hidden", S.page !== "overview" || !list.length);
    const html = V3.agentsHtml(list);
    if (strip.dataset.html !== html) { strip.dataset.html = html; strip.innerHTML = html; }
  }
  function renderPage(snap) {
    renderBell(snap);
    renderAgents(snap);
    if (S.page === "tracks") renderTracksPage(snap);
    else if (S.page === "activity") renderActivity(snap);
    else if (S.page === "machine") { renderMachinePage(snap); if (Date.now() - S.metricsAt > 15000) loadMetrics(); }
    else if (S.page === "alerts") renderAlerts(snap);
    else if (S.page === "controls") renderControls(snap);
    const m = snap.machine || {};
    $("#paused-banner").classList.toggle("hidden", !m.paused);
    if (m.paused) $("#paused-banner").textContent = `⏸ Machine paused ${Live.ago(m.since)}: ${m.reason || ""}. New ACE jobs won't start. Resume from Controls.`;
  }
  function renderControls(snap) {
    const box = $("#ctl-center");
    const key = JSON.stringify([snap.machine, snap.controls?.enabled, (snap.tracks || []).map(t => t.slug), (snap.processes || []).map(p => [p.card, p.script]),
      (snap.health?.services || []).map(s => s.status)]);
    if (box.dataset.key !== key && !box.contains(document.activeElement)) { box.dataset.key = key; box.innerHTML = Control.centerHtml(snap); }
    const ak = JSON.stringify((snap.audit || []).map(a => a.ts));
    if ($("#ctl-audit").dataset.key !== ak) { $("#ctl-audit").dataset.key = ak; $("#ctl-audit").innerHTML = Control.auditHtml(snap.audit); }
  }

  // ---- alerts + bell
  function alertHtml(a) {
    return `<div class="glass alert-card ${esc(a.level)}"><i></i><div><div class="t">${esc(a.title)}</div>${a.detail ? `<div class="d">${esc(a.detail)}</div>` : ""}
      ${a.card ? `<div class="d">track <b class="link" data-open="${esc(a.card)}">${esc(a.card)}</b></div>` : ""}</div><time>${esc(Live.ago(a.since))}</time></div>`;
  }
  function renderBell(snap) {
    const al = snap.alerts || [], bad = al.filter(a => a.level !== "info");
    const c = $("#bell-count"), r = $("#rail-alerts");
    c.textContent = r.textContent = al.length;
    c.classList.toggle("hidden", !al.length); r.classList.toggle("hidden", !bad.length);
    c.classList.toggle("warn", !al.some(a => a.level === "crit"));
    if (!$("#bell-pop").classList.contains("hidden")) $("#bell-pop").innerHTML = bellHtml(al);
  }
  function bellHtml(al) {
    return `<div class="page-head" style="margin:2px 4px 8px"><b>Alerts</b><a href="#/alerts" class="muted" style="font-size:12px">open all →</a></div>` +
      (al.length ? al.slice(0, 6).map(alertHtml).join("") : `<div class="empty-state"><b>✓</b>All clear</div>`);
  }
  function renderAlerts(snap) {
    const al = snap.alerts || [];
    const key = JSON.stringify(al.map(a => [a.key, a.detail]));
    if ($("#alerts-list").dataset.key === key) return;
    $("#alerts-list").dataset.key = key;
    $("#alerts-list").innerHTML = al.length ? al.map(alertHtml).join("") : `<div class="glass empty-state"><b>✓</b>No alerts. Disk, services, sources, GPU, ACE and tracks all look fine.</div>`;
  }

  // ---- tracks table
  function trackRow(t) {
    const sc = (S.snap.scores || []).find(x => x.card === t.slug);
    return { t, slug: t.slug, lane: t.lane, state: t.state, progress: Number(t.progress) || 0, loops: t.loops || 0, runs: (t.runs || []).length,
      verdict: sc ? sc.verdict + (sc.metrics?.draft != null ? " " + Live.num(sc.metrics.draft) : "") : "",
      plan: t.plan ? [t.plan.bpm ? Number(t.plan.bpm) + " BPM" : "", t.plan.key].filter(Boolean).join(" · ") : "", last_activity: t.last_activity || 0 };
  }
  function renderTracksPage(snap) {
    const q = S.tracksQ;
    let rows = (snap.tracks || []).map(trackRow).filter(r => !q || [r.slug, r.lane, r.state, r.plan].join(" ").toLowerCase().includes(q));
    const { k, dir } = S.sort;
    rows.sort((a, b) => (a[k] > b[k] ? 1 : a[k] < b[k] ? -1 : 0) * dir);
    $("#tracks-sub").textContent = `${(snap.tracks || []).length} tracks on the board and on disk · click a row for everything about it`;
    document.querySelectorAll("#tracks-table th").forEach(th => th.classList.toggle("sorted", th.dataset.k === k));
    const key = JSON.stringify(rows.map(r => [r.slug, r.lane, r.state, r.progress, r.loops, r.runs, r.verdict, Math.round(r.last_activity / 30)]));
    const body = $("#tracks-table tbody");
    if (body.dataset.key === key) return;
    body.dataset.key = key;
    body.innerHTML = rows.length ? rows.map(r => `<tr data-open="${esc(r.slug)}"><td class="slug">${esc(r.slug)}${r.t.live ? ` <span class="pill sm working">live</span>` : ""}</td>
      <td>${esc(r.lane)}</td><td><span class="pill sm ${esc(r.state)}">${esc(STATE_LABEL[r.state] || r.state)}</span></td>
      <td><span class="mini-bar"><i style="width:${r.progress}%"></i></span> <span class="mono">${r.progress}%</span></td>
      <td class="mono">${r.loops ? "↺ " + r.loops : "–"}</td><td class="mono">${r.runs || "–"}</td><td class="mono">${esc(r.verdict || "–")}</td>
      <td class="mono">${esc(r.plan || "–")}</td><td class="muted">${esc(Live.ago(r.last_activity))}</td></tr>`).join("")
      : `<tr><td colspan="9" class="empty-state">No tracks match.</td></tr>`;
  }

  // ---- activity timeline
  async function loadActivity(older) {
    const p = new URLSearchParams({ limit: "150" });
    if (S.actKind) p.set("kind", S.actKind);
    if (S.actQ) p.set("q", S.actQ);
    const rows = older ? S.actOlder.concat(S.actRows || []) : null;
    if (older) { const last = (S.actRows || S.snap?.activity || []).concat(S.actOlder).slice(-1)[0]; if (last) p.set("before", last.ts); }
    const r = await fetch("/api/activity?" + p).then(x => x.ok ? x.json() : null).catch(() => null);
    if (!r) return;
    if (older) S.actOlder = S.actOlder.concat(r.events); else S.actRows = (S.actKind || S.actQ) ? r.events : null;
    $("#act-list").dataset.key = "";
    if (S.snap) renderActivity(S.snap);
    return rows;
  }
  function renderActivity(snap) {
    const kinds = ["", "run", "kanban", "render", "ace", "service", "mail", "alert"];
    const kk = kinds.join() + S.actKind;
    if ($("#act-kinds").dataset.key !== kk) {
      $("#act-kinds").dataset.key = kk;
      $("#act-kinds").innerHTML = kinds.map(k => `<button class="toggle" aria-pressed="${S.actKind === k}" data-kind="${k}">${k ? esc(KIND_LABEL[k] || k) : "All"}</button>`).join("");
    }
    const seen = new Set();
    const list = (S.actRows || (snap.activity || [])).concat(S.actOlder).filter(e => !seen.has(e.id) && seen.add(e.id));
    const key = list.map(e => e.id).join("|");
    const box = $("#act-list");
    if (box.dataset.key === key) return;
    box.dataset.key = key;
    let day = "", html = "";
    for (const e of list) {
      const d = dayName(e.ts);
      if (d !== day) { day = d; html += `<div class="tl-day">${esc(d)}</div>`; }
      html += `<div class="tl-row ${esc(e.level)}"><time>${esc(Live.hm(e.ts))}</time><span class="tl-kind ${esc(e.kind)}">${esc(KIND_LABEL[e.kind] || e.kind)}</span>
        <span class="tl-text">${e.card ? `<b data-open="${esc(e.card)}">${esc(e.card)}</b> · ` : ""}${esc(e.text)}</span></div>`;
    }
    box.innerHTML = html || `<div class="empty-state"><b>∅</b>Nothing recorded yet.</div>`;
  }

  // ---- machine page: history charts
  const METRIC_DEFS = [
    { k: "gpu_util", label: "GPU util", unit: "%", max: 100 }, { k: "gpu_temp", label: "GPU temp", unit: "°C", line: 85 },
    { k: "vram_used", label: "VRAM used", unit: "GB", scale: 1 / 1024 }, { k: "disk_free", label: "Disk free", unit: "GB", line: 20 },
    { k: "load1", label: "CPU load (1m)", unit: "" }, { k: "working", label: "Lanes working", unit: "" },
  ];
  async function loadMetrics(force) {
    if (!force && Date.now() - S.metricsAt < 15000) return;
    S.metricsAt = Date.now();
    const r = await fetch(`/api/metrics?range=${encodeURIComponent(S.range)}`).then(x => x.ok ? x.json() : null).catch(() => null);
    if (r) { S.metrics = r; drawMetrics(); }
  }
  function drawMetrics() {
    const m = S.metrics; if (!m) return;
    const grid = $("#metric-grid");
    if (!grid.children.length) grid.innerHTML = METRIC_DEFS.map(d => `<div class="glass metric"><div class="m-head"><span>${esc(d.label)}</span><b id="mv-${d.k}">–</b></div><canvas id="mc-${d.k}" height="110"></canvas></div>`).join("");
    for (const d of METRIC_DEFS) {
      const vals = (m[d.k] || []).map(v => v == null ? null : +(v * (d.scale || 1)).toFixed(2));
      const last = [...vals].reverse().find(v => v != null);
      $("#mv-" + d.k).textContent = last == null ? "–" : `${last}${d.unit}`;
      const has = vals.some(v => v != null);
      Charts.lines($("#mc-" + d.k), has ? [{ values: vals, color: Charts.ACID, label: d.label }] : [], has && d.line != null ? { line: d.line } : {});
    }
    $("#machine-sub").textContent = `${m.t.length} points · one per ${m.bucket >= 3600 ? m.bucket / 3600 + " h" : m.bucket >= 60 ? m.bucket / 60 + " min" : m.bucket + " s"} · sampled by the panel every 15 s, kept 14 days`;
  }
  function renderMachinePage(snap) {
    const h = snap.health || {};
    $("#m-svc").innerHTML = (h.services || []).map(s => `<div class="svc-row"><span class="sd ${esc(s.status)}"></span><b>${esc(s.name)}</b><span class="via">${esc(s.status)} · ${esc(s.via)}</span></div>`).join("");
    $("#m-src").innerHTML = Live.freshnessHtml(snap);
    const ace = h.ace?.enabled !== false;  // ACE-Step was removed: hide its sections unless a config still probes it
    $("#m-ace-h").classList.toggle("hidden", !ace);
    $("#m-ace").innerHTML = (ace ? Live.aceHtml(h.ace) + Live.journalHtml(h.journal) : "") + Live.gpuAppsHtml(h.gpu) + `<h3 style="margin-top:14px">Running now</h3>` + Live.procsHtml(snap.processes);
    $("#m-local").innerHTML = V3.localHtml(snap);
    $("#m-fb").innerHTML = V3.fallbackHtml(snap);
    const st = V3.stylesHtml(snap);
    $("#m-styles-wrap").classList.toggle("hidden", !st); $("#m-styles").innerHTML = st;
    $("#m-refs").innerHTML = V3.refsHtml(snap) || `<span class="muted">No reference folder.</span>`;
  }

  // ---- command palette
  function cmdItems() {
    const snap = S.snap || {};
    const items = PAGES.map(p => ({ k: "page", label: p[0].toUpperCase() + p.slice(1), sub: "go to page", run: () => go(p) }));
    (snap.tracks || []).forEach(t => items.push({ k: "track", label: t.slug, sub: `${t.lane} · ${STATE_LABEL[t.state] || t.state}`, run: () => { go("overview"); openDrawer({ slug: t.slug }); } }));
    (snap.audio || []).filter(a => a.kind === "render").slice(0, 40).forEach(a => items.push({ k: "render", label: `${a.card} · ${a.path.split("/").pop()}`, sub: `${a.stage} · ${Live.ago(a.mtime)}`, run: () => { go("overview"); loadTake(a, true); } }));
    (snap.alerts || []).forEach(a => items.push({ k: "alert", label: a.title, sub: a.level, run: () => go("alerts") }));
    if (snap.controls?.enabled) {
      items.push(snap.machine?.paused ? { k: "control", label: "Resume the machine", sub: "PAUSED", run: Control.actions.resume }
        : { k: "control", label: "Pause the machine", sub: "writes PAUSED", run: Control.actions.pause });
      (snap.controls.services || []).forEach(sv => items.push({ k: "control", label: `Restart ${sv}`, sub: "passcode", run: () => Control.actions.restart(sv) }));
    }
    items.push({ k: "action", label: "Toggle fullscreen", sub: "F", run: toggleFs }, { k: "action", label: "Phone view", sub: "▯", run: () => { location.href = "/?view=phone"; } });
    return items;
  }
  function openCmd() { $("#cmdk").classList.remove("hidden"); $("#cmdk-input").value = ""; S.cmdSel = 0; drawCmd(); $("#cmdk-input").focus(); }
  function closeCmd() { $("#cmdk").classList.add("hidden"); }
  function drawCmd() {
    const q = $("#cmdk-input").value.trim().toLowerCase();
    S.cmdList = cmdItems().filter(i => !q || (i.label + " " + i.k + " " + i.sub).toLowerCase().includes(q)).slice(0, 40);
    S.cmdSel = Math.min(S.cmdSel, Math.max(0, S.cmdList.length - 1));
    $("#cmdk-list").innerHTML = S.cmdList.map((i, n) => `<div class="cmdk-item ${n === S.cmdSel ? "sel" : ""}" data-n="${n}" role="option"><span class="k">${esc(i.k)}</span><span>${esc(i.label)}</span><span class="s">${esc(i.sub || "")}</span></div>`).join("")
      || `<div class="empty-state">No match</div>`;
  }
  function runCmd(n) { const i = (S.cmdList || [])[n]; if (i) { closeCmd(); i.run(); } }

  function wireShell() {
    window.addEventListener("hashchange", route);
    $("#cmdk-btn").onclick = openCmd;
    $("#cmdk").addEventListener("click", e => { const it = e.target.closest(".cmdk-item"); if (it) runCmd(+it.dataset.n); else if (e.target.id === "cmdk") closeCmd(); });
    $("#cmdk-input").addEventListener("input", () => { S.cmdSel = 0; drawCmd(); });
    $("#cmdk-input").addEventListener("keydown", e => {
      if (e.key === "ArrowDown") { e.preventDefault(); S.cmdSel = Math.min(S.cmdSel + 1, S.cmdList.length - 1); drawCmd(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); S.cmdSel = Math.max(0, S.cmdSel - 1); drawCmd(); }
      else if (e.key === "Enter") { e.preventDefault(); runCmd(S.cmdSel); }
      else if (e.key === "Escape") { e.preventDefault(); closeCmd(); }
    });
    document.addEventListener("keydown", e => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k" && !$("#dk-app").classList.contains("hidden")) { e.preventDefault(); $("#cmdk").classList.contains("hidden") ? openCmd() : closeCmd(); }
    }, true);
    $("#bell-btn").onclick = e => { e.stopPropagation(); const p = $("#bell-pop"); p.classList.toggle("hidden"); if (!p.classList.contains("hidden")) p.innerHTML = bellHtml(S.snap?.alerts || []); };
    document.addEventListener("click", e => { if (!e.target.closest("#bell-pop")) $("#bell-pop").classList.add("hidden"); });
    const openFrom = e => { const o = e.target.closest("[data-open]"); if (o) { $("#bell-pop").classList.add("hidden"); openDrawer({ slug: o.dataset.open }); } };
    ["#tracks-table", "#act-list", "#alerts-list", "#bell-pop"].forEach(sel => $(sel).addEventListener("click", openFrom));
    $("#tracks-table thead").addEventListener("click", e => { const th = e.target.closest("th"); if (!th) return; S.sort = { k: th.dataset.k, dir: S.sort.k === th.dataset.k ? -S.sort.dir : (th.dataset.k === "slug" ? 1 : -1) }; $("#tracks-table tbody").dataset.key = ""; renderTracksPage(S.snap); });
    $("#tracks-q").addEventListener("input", e => { S.tracksQ = e.target.value.trim().toLowerCase(); if (S.snap) renderTracksPage(S.snap); });
    $("#act-kinds").addEventListener("click", e => { const b = e.target.closest("[data-kind]"); if (!b) return; S.actKind = b.dataset.kind; S.actOlder = []; loadActivity(); $("#act-kinds").dataset.key = ""; });
    let aq; $("#act-q").addEventListener("input", e => { clearTimeout(aq); aq = setTimeout(() => { S.actQ = e.target.value.trim(); S.actOlder = []; loadActivity(); }, 250); });
    $("#act-more").onclick = () => loadActivity(true);
    $("#range-tabs").addEventListener("click", e => { const b = e.target.closest("button"); if (!b) return; S.range = b.dataset.r; store.set("tekno-range", S.range);
      document.querySelectorAll("#range-tabs button").forEach(x => x.setAttribute("aria-pressed", String(x === b))); loadMetrics(true); });
    document.querySelectorAll("#range-tabs button").forEach(x => x.setAttribute("aria-pressed", String(x.dataset.r === S.range)));
    window.addEventListener("resize", () => { if (S.page === "machine") drawMetrics(); });
  }

  Control.init({ toast, demo: () => S.snap?.mode === "demo" });
  Control.wire($("#ctl-center")); Control.wire($("#drawer")); Control.wireTrack($("#drawer"));
  wire(); wireShell(); boot(); route();
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
})();

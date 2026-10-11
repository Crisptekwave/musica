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
    follow: true, nowUrl: null, knownAudio: new Set(), audioInit: false, gpuHist: [], lastGpuTs: null,
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
    ws.onopen = () => { S.retry = 0; $("#live-dot").classList.add("on"); };
    ws.onmessage = e => { try { render(JSON.parse(e.data)); } catch (err) { console.error(err); } };
    ws.onclose = ev => {
      $("#live-dot").classList.remove("on");
      if (ev.code === 4401) return showLogin();
      setTimeout(connect, Math.min(10000, 500 * 2 ** S.retry++));
    };
  }

  // ---------------- render ----------------
  function render(snap) {
    S.snap = snap;
    const chip = $("#mode-chip");
    chip.textContent = snap.mode === "demo" ? "DEMO" : "LIVE"; chip.classList.toggle("demo", snap.mode === "demo");
    const errs = Object.entries(snap.sources || {}).filter(([k, v]) => v && v.ok === false && k !== "mail").map(([k, v]) => `${k}: ${v.error}`);
    $("#banner").textContent = errs.join(" · "); $("#banner").classList.toggle("hidden", !errs.length);
    const g = snap.health?.gpu;
    if (g && g.ok && snap.ts !== S.lastGpuTs) { S.lastGpuTs = snap.ts; S.gpuHist.push(g.util || 0); if (S.gpuHist.length > 160) S.gpuHist.shift(); }
    renderKpis(snap); renderBoard(snap); bots.update(snap.lanes || {}); renderListen(snap);
    renderScores(snap); renderHealth(snap); renderChat(snap); renderDrawer();
  }

  function renderKpis(snap) {
    const h = snap.health || {}, g = h.gpu || {}, a = h.ace || {}, d = h.disk || {};
    const trs = snap.tracks || [];
    const working = trs.filter(t => t.state === "working").length, failed = trs.filter(t => t.state === "failed").length;
    const review = trs.filter(t => t.state === "review").length;
    const svcs = h.services || [];
    const vr = g.ok ? g.mem_used / g.mem_total : 0;
    const k = [];
    k.push(`<div class="kpi ${a.status === "generating" ? "good" : a.ok ? "" : "bad"}">ACE <b>${esc(a.status || "?")}${a.job ? ` ${Number(a.progress ?? 0)}%` : ""}</b>${a.job ? `<span class="mono">${esc(a.job.card)}</span>` : ""}</div>`);
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
        const key = JSON.stringify([t.lane, t.state, t.progress, t.loops, t.last_sendback, t.files_lane, t.review_task_id, t.current_task?.last_error, S.drawer?.slug === t.slug]);
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
    const note = t.files_lane && t.files_lane !== t.lane && t.state !== "done" ? `<div class="tc-note">on disk: ${esc(t.files_lane)}</div>` : "";
    const act = t.lane === "Review" && t.review_task_id ? `<div class="tc-actions">
        <button class="btn approve" data-act="approve" data-task="${esc(t.review_task_id)}" data-slug="${esc(t.slug)}">Approve</button>
        <button class="btn reject" data-act="reject" data-task="${esc(t.review_task_id)}" data-slug="${esc(t.slug)}">Reject</button></div>` : "";
    return `<div class="tc-slug" title="${esc(t.slug)}">${esc(t.slug)}</div>
      <div class="tc-top"><span class="pill sm ${esc(t.state)}">${esc(STATE_LABEL[t.state] || t.state)}</span>${t.loops ? `<span class="tc-loop">↺${Number(t.loops)}</span>` : ""}<b class="tc-pct">${Number(t.progress) || 0}%</b></div>
      <div class="tc-bar"><i style="width:${Math.max(2, Number(t.progress) || 0)}%"></i></div>
      ${sb ? `<div class="tc-back"><b>${esc(sb.from || "?")} → ${esc(sb.to)}</b> ${esc(sb.reason)}</div>` : ""}${err}${note}${act}`;
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
      const key = JSON.stringify([t, audio.map(a => a.url + a.mtime), score?.mtime, S.nowUrl]);
      if (body.dataset.key === key) return;
      body.dataset.key = key;
      const chain = (t.tasks || []).map(x => `<div class="chain-row"><i class="cd ${esc(x.status)}"></i><span>${esc(x.lane || "?")}</span><span class="id">${esc(x.id)}</span><span class="st">${esc(x.status)}</span></div>`).join("");
      body.innerHTML = `<h2>${esc(t.slug)}</h2>
        <div><span class="pill ${esc(t.state)}">${esc(STATE_LABEL[t.state] || t.state)}</span> <span class="muted">lane <b style="color:var(--text)">${esc(t.lane)}</b>${t.loops ? ` · <span style="color:var(--amber)">↺ ${Number(t.loops)} loops</span>` : ""}${t.files_lane && t.files_lane !== t.lane ? ` · on disk: ${esc(t.files_lane)}` : ""}</span></div>
        <div class="progress"><div class="bar"><i style="width:${Math.max(2, Number(t.progress) || 0)}%"></i></div><span class="pct">${Number(t.progress) || 0}%</span></div>
        ${t.lane === "Review" && t.review_task_id ? `<div class="review-actions"><button class="btn approve" data-act="approve" data-task="${esc(t.review_task_id)}" data-slug="${esc(t.slug)}">Approve</button><button class="btn reject" data-act="reject" data-task="${esc(t.review_task_id)}" data-slug="${esc(t.slug)}">Reject + note</button></div>` : ""}
        ${t.current_task ? `<h3>Current task</h3><div class="mono" style="font-size:13px">${esc(t.current_task.title)}</div><div class="muted" style="font-size:12px">${esc(t.current_task.id)} · ${esc(t.current_task.status)}${t.current_task.assignee ? " · " + esc(t.current_task.assignee) : ""}</div>${t.current_task.last_error ? `<div class="tc-err" style="-webkit-line-clamp:6">${esc(t.current_task.last_error)}</div>` : ""}` : ""}
        ${t.sendbacks?.length ? `<h3>Sent back</h3>${t.sendbacks.map(s => `<div class="sb-row"><b>${esc(s.from || "?")} → ${esc(s.to)}</b> ${esc(s.reason)}</div>`).join("")}` : ""}
        ${chain ? `<h3>Lane chain</h3><div class="chain">${chain}</div>` : ""}
        ${score ? `<h3>Score · <span class="${score.pass ? "" : "warn-text"}">${esc(score.verdict)}</span></h3><div class="badges">${badgesHtml(score.badges)}</div>` : ""}
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
        <h3>Current task</h3>${L.task ? `<div class="mono" style="font-size:13px">${esc(L.task.title)}</div><div class="muted" style="font-size:12px">${esc(L.task.id || "")}${L.task.assignee ? " · " + esc(L.task.assignee) : ""}</div>${L.task.error ? `<div class="tc-err">${esc(L.task.error)}</div>` : ""}` : `<div class="muted">No card in this lane right now.</div>`}
        ${L.handoff_to ? `<p class="muted">Last hand-off → <b style="color:var(--acid)">${esc(L.handoff_to)}</b> ${ago(L.handoff_at)}</p>` : ""}
        <h3>Tracks here</h3>${here.length ? here.map(t => `<div class="sb-row" style="background:rgba(57,255,20,.05);border-color:var(--line);cursor:pointer" data-open="${esc(t.slug)}"><b style="color:var(--acid)">${esc(t.slug)}</b> ${esc(STATE_LABEL[t.state] || t.state)} · ${Number(t.progress) || 0}%</div>`).join("") : `<div class="muted">none</div>`}
        <h3>Last log lines</h3><div class="log" style="max-height:340px">${esc(log || "nothing logged yet")}</div>`;
    }
  }

  function badgesHtml(badges) {
    return (badges || []).map(b => `<div class="badge ${esc(b.status)}"><div class="l"><span>${esc(b.label)}</span><span>${b.status === "pass" ? "✔" : b.status === "fail" ? "✖" : "–"}</span></div>
      <div class="v">${esc(b.value ?? "–")}</div><div class="t">${esc(b.target)}</div></div>`).join("");
  }
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
      <kbd>Space</kbd><span>play / pause</span><kbd>A</kbd> <span>take (A side)</span><kbd>B</kbd><span>SIFRES reference (B side)</span>
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
    $("#ab-b").textContent = snap.reference ? "B · SIFRES" : "B · (no ref)";
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
    drawScoreChart(sc);
  }
  function drawScoreChart(sc) {
    const m = sc.map(s => s.metrics || {}), cv = $("#score-chart");
    if (S.chartTab === "lufs") Charts.lines(cv, [{ values: m.map(x => x.lufs), color: Charts.ACID, label: "LUFS", ok: v => Math.abs(v + 9) <= 0.5 }], { band: [-9.5, -8.5] });
    else if (S.chartTab === "tp") Charts.lines(cv, [{ values: m.map(x => x.tp), color: Charts.ACID, label: "dBTP", ok: v => v <= -1 }], { line: -1 });
    else {
      const pick = (s, label, fb) => { const b = (s.badges || []).find(x => x.label === label); const v = parseFloat(b && b.value); return isNaN(v) ? fb : v; };
      Charts.lines(cv, [{ values: sc.map(s => pick(s, "Mix Judge", s.metrics?.draft ?? null)), color: "#ffb020", label: "Mix Judge", ok: v => v >= 6.5 },
        { values: sc.map(s => pick(s, "EarLoop", null)), color: Charts.UV, label: "EarLoop", ok: v => v >= 6.5 }], { band: [6.5, 10] });
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
    $("#ace-box").innerHTML = `<div class="ace-line"><span class="sd ${a.ok ? "up" : "down"}" style="width:10px;height:10px;border-radius:50%;display:inline-block"></span><b>ACE-Step ${esc(a.status || "?")}</b>
        <span class="muted mono" style="margin-left:auto;font-size:11px">${a.job ? `${esc(a.job.card)} · pid ${Number(a.job.pid)}` : ""}</span></div>
      ${a.job ? `<div class="progress"><div class="bar"><i style="width:${Number(a.progress) || 0}%"></i></div><span class="pct">${Number(a.progress) || 0}%</span></div>` : ""}
      <div class="log" style="margin-top:6px">${esc((a.log || []).slice(-3).join("\n") || "no log")}</div>`;
    $("#disk-box").innerHTML = d.ok ? `<div class="disk-line"><b class="${d.warn ? "warn-text" : ""}">${d.free_gb} GB</b> free on ${esc(d.path)} · ${d.used_pct}% used${d.warn ? ` · <span class="warn-text">under ${d.warn_gb} GB</span>` : ""}</div>
      <div class="disk-bar ${d.warn ? "warn" : ""}" style="height:8px"><i style="width:${Number(d.used_pct) || 0}%"></i></div>` : `<span class="warn-text">${esc(d.error || "no disk data")}</span>`;
    $("#svc-box").innerHTML = (h.services || []).map(s => `<div class="svc-row"><span class="sd ${esc(s.status)}"></span><b>${esc(s.name)}</b><span class="via">${esc(s.status)} · ${esc(s.via)}</span></div>`).join("")
      + Object.entries(snap.sources || {}).map(([k, v]) => `<div class="svc-row"><span class="sd ${v.ok ? "up" : "down"}"></span><span>source: ${esc(k)}</span><span class="via">${esc(v.ok ? "ok" : (v.error || "down"))}</span></div>`).join("");
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
    const threads = [...new Set(msgs.map(m => m.thread).filter(Boolean))];
    const tk = threads.join("|") + S.thread;
    if ($("#thread-row").dataset.key !== tk) {
      $("#thread-row").dataset.key = tk;
      $("#thread-row").innerHTML = threads.length ? `<button class="toggle" aria-pressed="${!S.thread}" data-thread="">All</button>` +
        threads.slice(-6).map(t => `<button class="toggle" aria-pressed="${S.thread === t}" data-thread="${esc(t)}">${esc(t)}</button>`).join("") : "";
    }
    const shown = msgs.filter(m => !S.thread || m.thread === S.thread);
    const box = $("#chat");
    const key = shown.map(m => m.id).join(",") + c.status;
    if (box.dataset.key === key) return;
    const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 60;
    box.dataset.key = key;
    let html = "";
    if (c.status !== "ok") html += `<div class="glass" style="font-size:12px"><b style="color:var(--amber)">agent-mail: ${esc(c.status)}</b><br><span class="muted">${esc(c.error || "")}</span>
      ${(c.agents || []).length ? `<br>Agents: ${(c.agents || []).map(a => `<span class="mono" style="color:${agentColor(a.name)}">${esc(a.name)}</span>`).join(", ")}` : ""}</div>`;
    html += shown.map(m => {
      const col = agentColor(m.from);
      return `<div class="msg ${/hermes/i.test(m.from) ? "right" : ""}"><div class="avatar" style="background:${col};box-shadow:0 0 10px ${col}">${esc((m.from || "?").slice(0, 2).toUpperCase())}</div>
        <div class="bubble"><div class="who" style="color:${col}">${esc(m.from)}</div><div class="txt">${esc(m.body || m.subject)}</div>
        <div class="meta"><span class="thread-tag">${esc(m.thread || "")}</span><span>${ago(m.ts)}</span></div></div></div>`;
    }).join("");
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
    S.demo = !S.demo; store.set("tekno-demo", S.demo);
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
      $("#clock").textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    }, 250);
    let rz;
    window.addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(() => { bots.layout(); if (S.snap) { renderHealth(S.snap); renderScores(S.snap); } }, 120); });
  }

  async function boot() {
    const r = await fetch("/api/session").then(x => x.json()).catch(() => ({ authed: false }));
    if (!r.authed) return showLogin();
    $("#dk-login").classList.add("hidden"); $("#dk-app").classList.remove("hidden");
    store.set("tekno-view", "");  // visiting the desk clears a "phone view" preference
    const q = new URLSearchParams(location.search);
    S.demo = q.has("demo") ? q.get("demo") === "1" : store.get("tekno-demo", !!r.demo_default);
    setFilter(S.filter);
    bots.start();
    connect();
  }

  wire(); boot();
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
})();

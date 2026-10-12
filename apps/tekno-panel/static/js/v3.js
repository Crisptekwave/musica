/* TEKNO CONTROL v3: agent roster, local model + fallback boxes, style chips, Studio (live REAPER view) and Palette.
   HTML blocks are shared by the phone UI and the desktop control room; the Studio and Palette controllers mount into a container. */
(function () {
  "use strict";
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const ago = ts => (window.Live ? window.Live.ago(ts) : "");
  const secAgo = s => (s == null ? "" : s < 90 ? `${Math.round(s)}s ago` : s < 5400 ? `${Math.round(s / 60)}m ago` : `${(s / 3600).toFixed(1)}h ago`);
  const mmss = s => (!isFinite(s) || s == null ? "–" : `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`);
  const MODEL = { sonnet: "Sonnet", opus: "Opus", fable: "Fable", qwen: "Qwen" };

  const modelChip = m => (MODEL[m] ? `<span class="v3-model ${esc(m)}">${MODEL[m]}</span>` : "");

  // ---------------- cards: style + who builds it ----------------
  function chips(t) {
    const b = t.brief;
    if (!b) return "";
    const st = (b.styles?.length ? b.styles : b.style ? [b.style] : []).map(s => `<span class="v3-style">${esc(s.replace(/_/g, " "))}</span>`).join("");
    const who = b.agent ? `<span class="v3-agent" title="agent">${esc(b.agent)}</span>` : "";
    return st || b.model || who ? `<div class="v3-chips">${st}${modelChip(b.model)}${who}</div>` : "";
  }

  // ---------------- agents roster ----------------
  function agentCard(a) {
    const cls = a.stale ? "stale" : a.state;
    const label = a.stale ? "no update" : a.state;
    return `<div class="v3-agentcard ${esc(cls)}" title="${esc(a.task || "")}">
      <div class="v3-ah">${modelChip(a.model)}<b>${esc(a.name)}</b><span class="v3-state ${esc(cls)}">${esc(label)}</span></div>
      <div class="v3-as">${a.card ? `<span class="mono">${esc(a.card)}</span> · ` : ""}${esc(a.step || a.task || a.result || "–")}</div>
      <div class="v3-at muted">updated ${esc(secAgo((Date.now() / 1000) - a.updated))}${a.state !== "working" && a.result ? ` · ${esc(a.result)}` : ""}</div></div>`;
  }
  const agentsHtml = list => (list || []).map(agentCard).join("");

  // ---------------- local model + fallback ----------------
  function localHtml(snap) {
    const l = snap.local;
    if (!l) return `<div class="muted v3-small">No local model probe yet.</div>`;
    const g = l.gpu || {};
    return `<div class="svc-row"><span class="sd ${l.ok ? "up" : "down"}"></span><b>vLLM ${l.ok ? "up" : "down"}</b>
        <span class="via">${l.ok ? esc(l.model || "?") : "no answer on " + esc(l.url || "")}${l.max_len ? ` · ${Math.round(l.max_len / 1024)}k ctx` : ""}</span></div>
      ${g.mem_total ? `<div class="v3-small muted">GPU memory <b>${(g.mem_used / 1024).toFixed(1)}</b> of ${(g.mem_total / 1024).toFixed(0)} GB used${g.model_mem ? ` · the model holds ${(g.model_mem / 1024).toFixed(1)} GB` : ""}</div>` : ""}`;
  }

  function fallbackHtml(snap) {
    const f = snap.fallback;
    if (!f || !f.present) return `<div class="muted v3-small">No fallback folder on this machine.</div>`;
    const hb = f.heartbeat_age;
    const notes = (f.notes || []).map(n => `<div class="v3-note ${esc(n.state)}"><div><b class="mono">${esc(n.slug)}</b> <span class="v3-state ${esc(n.state)}">${esc(n.state)}</span> <span class="muted">${esc(ago(n.mtime))}</span></div>
      ${(n.log || []).length ? `<div class="log v3-log">${esc(n.log.join("\n"))}</div>` : ""}</div>`).join("");
    const runs = (f.runs || []).map(r => `<div class="svc-row"><span class="sd ${r.result === "done" ? "up" : r.result ? "failed" : "idle"}"></span><b class="mono">${esc(r.name.replace(/\.log$/, ""))}</b>
      <span class="via">${esc(r.result || "no result")} · ${esc(ago(r.mtime))}</span></div>`).join("");
    return `<div class="v3-fbtop"><span class="v3-mode ${esc(f.mode)}">${esc(f.mode || "?")}</span>
        <span class="muted v3-small">Claude's last post ${hb == null ? "never seen" : esc(secAgo(hb))} · local model ${f.vllm_ok ? "up" : "down"}${f.writable ? "" : " · MODE read-only"}</span></div>
      ${notes || `<div class="muted v3-small">No open hand-off notes${f.done_notes ? ` (${f.done_notes} done)` : ""}.</div>`}
      ${runs ? `<h4>Last runs</h4>${runs}` : ""}`;
  }

  // ---------------- setup: what is installed, read from disk; and the changelog every VM job appends to
  function setupHtml(snap) {
    const u = snap.setup;
    if (!u) return `<div class="muted v3-small">No setup probe yet.</div>`;
    const list = (label, xs) => xs && xs.length ? `<div class="svc-row v3-setrow"><b>${esc(label)}</b><span class="via">${xs.map(esc).join(" · ")}</span></div>` : "";
    const p = u.plugins || {}, g = u.gateway || {}, t = (u.night || {}).tag;
    return list("Plugins (VST3)", p.vst3) + list("Plugins (CLAP)", p.clap) + list("Plugins (LV2)", p.lv2)
      + list("Audio models", u.models) + list("Tools", u.tools)
      + `<div class="svc-row v3-setrow"><span class="sd ${g.on ? "up" : "idle"}"></span><b>FreeLLMAPI gateway</b><span class="via">${g.on ? `on · model ${esc(g.model || "?")} · fallback order ${esc(g.order)}` : "off"}</span></div>`
      + `<div class="svc-row v3-setrow"><span class="sd ${t ? (t.model_errors ? "failed" : "up") : "idle"}"></span><b>Night tagging</b><span class="via">${t ? `${t.total} samples tagged · CLAP ${t.clap ? "on" : "off"} last run · ${esc(ago(t.at))}` : "not run yet"}</span></div>`
      + watchdogHtml(u.watchdog)
      + list("Deliveries", (u.deliveries || []).map(d => `${d.name} (${ago(d.mtime)})`));
  }
  function watchdogHtml(w) {
    if (!w) return `<div class="svc-row v3-setrow"><span class="sd idle"></span><b>Watchdog</b><span class="via">not running yet</span></div>`;
    const crit = w.open.filter(i => i.level === "crit").length;
    const rows = w.open.map(i => `<div class="v3-small ${i.level === "crit" ? "warn-text" : "muted"}">[${esc(i.level)}] ${esc(i.text)}${i.claude ? " · needs Claude" : ""}</div>`).join("");
    const acts = (w.actions || []).map(a => `<div class="v3-small muted">${esc(a)}</div>`).join("");
    return `<div class="svc-row v3-setrow"><span class="sd ${crit ? "failed" : w.open.length ? "idle" : "up"}"></span><b>Watchdog</b>
      <span class="via">${w.open.length ? `${w.open.length} open issue(s), ${crit} critical` : "all clear"} · checked ${esc(ago(w.last_run))}</span>${rows}${acts}</div>`;
  }
  function changesHtml(snap) {
    const c = (snap.setup || {}).changelog || [];
    return c.length ? `<div class="log v3-log">${esc(c.join("\n"))}</div>` : `<div class="muted v3-small">No changes logged yet.</div>`;
  }

  function stylesHtml(snap) {
    const s = snap.styles;
    if (!s || !s.length) return "";
    return s.map(x => `<div class="svc-row"><b>${esc(x.name)}</b><span class="via">${x.bpm ? (x.bpm[0] === x.bpm[1] ? x.bpm[0] : `${x.bpm[0]}-${x.bpm[1]}`) + " BPM" : "BPM n/a"}
      <span class="v3-state ${x.calibration === "calibrated" ? "done" : "stale"}">${esc(x.calibration || "provisional")}</span></span></div>`).join("");
  }

  function refsHtml(snap) {
    const r = snap.references;
    if (!r || !r.items?.length) return "";
    return `<div class="v3-small muted">${esc((r.readme.split("\n").find(l => /measure only/i.test(l)) || "Measure only: never sample these."))}</div>` +
      r.items.map(i => `<div class="v3-ref" title="${esc(i.file)}">${esc(i.title)}<span class="muted mono">${(i.bytes / 1e6).toFixed(1)} MB</span></div>`).join("");
  }

  // ---------------- Studio: live REAPER frames (view only) ----------------
  const studio = {
    timer: null, el: null, single: false, data: null, pick: null, busy: false, key: "",
    start(el, opts) {
      this.stop(); this.el = el; this.single = !!(opts && opts.single);
      if (!el.dataset.wired) { el.dataset.wired = "1"; this.wirePick(); }
      if (!this.vis) { this.vis = true; document.addEventListener("visibilitychange", () => { if (!document.hidden && this.timer) this.tick(); }); }
      this.tick(); this.timer = setInterval(() => this.tick(), 2000);
    },
    stop() { clearInterval(this.timer); this.timer = null; },
    async tick() {
      if (document.hidden || this.busy || !this.el) return;  // paused while the tab is hidden
      this.busy = true;
      try {
        const r = await fetch("/api/reaper");
        if (r.ok) { this.data = await r.json(); this.draw(); }
      } catch (e) { /* offline: keep the last frames */ }
      this.busy = false;
    },
    projectHtml(i) {
      const p = i.rpp;
      if (!p) return `<div class="muted v3-small">${i.project ? "Project not readable." : "No project file yet."}</div>`;
      const tr = p.tracks || [];
      return `<div class="v3-kv"><b>${p.tempo ? Number(p.tempo) : "–"}</b><span>BPM</span><b>${mmss(p.length_s)}</b><span>length</span><b>${tr.length}</b><span>tracks</span></div>
        <ul class="v3-tracks">${tr.map(t => `<li><span>${esc(t.name)}</span><b>${Number(t.items)}</b></li>`).join("")}</ul>
        <div class="v3-small muted">${esc(i.project || "")} · saved ${esc(ago(p.mtime))}</div>
        <div class="v3-small">${i.render ? `Last render <b class="mono">${esc(i.render.file)}</b> <span class="muted">${esc(ago(i.render.mtime))}</span>` : `<span class="muted">No render yet.</span>`}</div>`;
    },
    figure(i) {
      const c = i.card;
      const age = i.frame_age;
      const live = age != null && age < 12;
      return `<article class="v3-inst" data-n="${i.n}">
        <div class="v3-frame"><img alt="REAPER ${esc(i.display)}" decoding="async">
          <div class="v3-nolive hidden">Waiting for a frame…</div>
          <span class="v3-fage"></span></div>
        <aside><div class="v3-ih"><b class="mono">${esc(i.display)}</b> <b>${esc(i.slug || "unknown card")}</b>
          ${c ? `<span class="pill sm ${esc(c.state)}">${esc(c.state)}</span><span class="muted v3-small">${esc(c.lane)} · ${Number(c.progress) || 0}%</span>` : ""}</div>
          <div class="v3-small muted">pid ${Number(i.pid) || "?"}${i.uptime_s != null ? ` · up ${secAgo(i.uptime_s).replace(" ago", "")}` : ""}</div>
          <div class="v3-proj"></div></aside></article>`;
    },
    draw() {
      const d = this.data, el = this.el;
      const ins = d.instances || [];
      if (!this.pick || !ins.some(i => i.n === this.pick)) this.pick = ins[0]?.n ?? null;
      const key = ins.map(i => i.n + ":" + i.slug).join("|") + (this.single ? "#" + this.pick : "");
      if (key !== this.key) {
        this.key = key;
        const sel = this.single && ins.length > 1 ? `<div class="v3-pick">${ins.map(i => `<button class="chip${i.n === this.pick ? " sel" : ""}" data-n="${i.n}">${esc(i.display)} ${esc(i.slug || "")}</button>`).join("")}</div>` : "";
        el.innerHTML = ins.length
          ? sel + `<div class="v3-grid${this.single ? " single" : ""}">${ins.filter(i => !this.single || i.n === this.pick).map(i => this.figure(i)).join("")}</div>`
          : `<div class="glass empty-state"><b>No REAPER producer running right now</b><div class="muted">A live view appears here as soon as a build starts.${d.snapper_age != null && d.snapper_age <= 20 ? " Frame grabber alive." : ""}</div></div>`;
      }
      for (const i of ins) {
        const f = el.querySelector(`.v3-inst[data-n="${i.n}"]`);
        if (!f) continue;
        const img = f.querySelector("img"), nl = f.querySelector(".v3-nolive");
        const want = i.frame_url ? `${i.frame_url}?t=${Math.round(i.frame_ts * 1000)}` : "";  // new frame, new URL
        if (want && img.dataset.src !== want) {
          const pre = new Image();
          pre.onload = () => { img.src = want; img.dataset.src = want; };
          pre.src = want;
        }
        nl.classList.toggle("hidden", !!want);
        const stale = i.frame_age != null && i.frame_age > 12;
        f.classList.toggle("stale", stale);
        f.querySelector(".v3-fage").textContent = i.frame_age == null ? "" : stale ? `frame ${secAgo(i.frame_age)}` : "live";
        const pk = JSON.stringify([i.rpp?.mtime, i.render?.mtime, i.project]);
        const pe = f.querySelector(".v3-proj");
        if (pe.dataset.key !== pk) { pe.dataset.key = pk; pe.innerHTML = this.projectHtml(i); }
      }
      if (d.snapper_age == null || d.snapper_age > 20) {
        if (!el.querySelector(".v3-warn")) el.insertAdjacentHTML("afterbegin", `<div class="v3-warn warn-text">Frame grabber not reporting (tekno-reaper-snap.service).</div>`);
      } else el.querySelector(".v3-warn")?.remove();
    },
    wirePick() {
      this.el.addEventListener("click", e => {
        const b = e.target.closest(".v3-pick button"); if (!b) return;
        this.pick = Number(b.dataset.n); this.key = ""; this.draw();
      });
    },
  };

  // ---------------- Palette ----------------
  const palette = {
    data: null, f: { type: "", source: "", style: "", hard: false, q: "" }, limit: 150, audio: null, playing: null, el: null,
    async mount(el) {
      this.el = el;
      if (!this.data) {
        el.innerHTML = `<div class="muted">Loading the palette…</div>`;
        try { this.data = await (await fetch("/api/palette")).json(); } catch (e) { el.innerHTML = `<div class="warn-text">Palette unavailable.</div>`; return; }
      }
      this.draw();
    },
    sel(id, label, opts, cur) {
      return `<label class="v3-f">${label}<select id="${id}"><option value="">all</option>${opts.map(o => `<option${o === cur ? " selected" : ""}>${esc(o)}</option>`).join("")}</select></label>`;
    },
    rows() {
      const f = this.f, q = f.q.toLowerCase();
      return this.data.clips.filter(c => (!f.type || c.type === f.type) && (!f.source || c.source === f.source) &&
        (!f.style || (c.styles || []).includes(f.style)) && (!f.hard || c.hard_kick) && (!q || c.file.toLowerCase().includes(q)));
    },
    draw() {
      const d = this.data, el = this.el;
      if (!d.clips.length) { el.innerHTML = `<div class="glass empty-state"><b>No palette found</b><div class="muted">${d.ok ? "index.json lists no audio clips." : "index.json is missing or unreadable."}</div></div>`; return; }
      const sources = [...new Set(d.clips.map(c => c.source).filter(Boolean))].sort();
      el.innerHTML = `<div class="v3-filters">${this.sel("pal-type", "Type", d.types, this.f.type)}${this.sel("pal-source", "Source", sources, this.f.source)}
        ${this.sel("pal-style", "Style", d.styles, this.f.style)}<label class="v3-f chk"><input type="checkbox" id="pal-hard"${this.f.hard ? " checked" : ""}> hard kick</label>
        <input class="search" id="pal-q" type="search" placeholder="Filter by file name" value="${esc(this.f.q)}" aria-label="Filter clips"></div>
        <div class="v3-count muted" id="pal-count"></div><div class="glass v3-pal" id="pal-list"></div>
        <details class="glass v3-readme"><summary>README</summary><pre>${esc(d.readme || "No README.")}</pre></details>`;
      this.rowsDraw();
      const re = () => { this.limit = 150; this.rowsDraw(); };
      el.querySelector("#pal-type").onchange = e => { this.f.type = e.target.value; re(); };
      el.querySelector("#pal-source").onchange = e => { this.f.source = e.target.value; re(); };
      el.querySelector("#pal-style").onchange = e => { this.f.style = e.target.value; re(); };
      el.querySelector("#pal-hard").onchange = e => { this.f.hard = e.target.checked; re(); };
      el.querySelector("#pal-q").oninput = e => { this.f.q = e.target.value; re(); };
      el.querySelector("#pal-list").onclick = e => {
        const b = e.target.closest("[data-play]"); if (b) return this.play(b.dataset.play);
        if (e.target.closest("[data-more]")) { this.limit += 200; this.rowsDraw(); }
      };
    },
    rowsDraw() {
      const rows = this.rows();
      this.el.querySelector("#pal-count").textContent = `${rows.length} of ${this.data.clips.length} clips`;
      this.el.querySelector("#pal-list").innerHTML = rows.slice(0, this.limit).map(c => `<div class="v3-clip${this.playing === c.file ? " on" : ""}">
        <button class="icon-btn" data-play="${esc(c.file)}" aria-label="Play ${esc(c.name)}">${this.playing === c.file ? "■" : "▶"}</button>
        <div class="v3-cn"><b class="mono">${esc(c.name)}</b><span class="muted">${esc(c.source || "")} · ${esc(c.type || "")}${c.character ? " · " + esc(c.character) : ""}</span></div>
        <div class="v3-cm mono">${c.bpm ? Number(c.bpm).toFixed(0) + " bpm" : ""} ${esc(c.key || "")} ${c.bars ? c.bars + " bars" : ""}${c.tune_note ? " · " + esc(c.tune_note) : ""}</div>
        <div class="v3-cs">${c.hard_kick ? `<span class="v3-style hard">hard kick</span>` : ""}${(c.styles || []).map(s => `<span class="v3-style">${esc(s.replace(/_/g, " "))}</span>`).join("")}</div></div>`).join("")
        + (rows.length > this.limit ? `<button class="ghost" data-more>Show more (${rows.length - this.limit})</button>` : "");
    },
    play(file) {
      if (!this.audio) { this.audio = new Audio(); this.audio.onended = () => { this.playing = null; if (this.el) this.rowsDraw(); }; }
      if (this.playing === file) { this.audio.pause(); this.playing = null; return this.rowsDraw(); }
      const c = this.data.clips.find(x => x.file === file); if (!c) return;
      this.audio.src = c.url; this.audio.play().catch(() => {}); this.playing = file; this.rowsDraw();
    },
    stop() { if (this.audio) { this.audio.pause(); this.playing = null; } },
  };

  window.V3 = { chips, modelChip, agentsHtml, localHtml, fallbackHtml, setupHtml, changesHtml, stylesHtml, refsHtml, studio, palette };
})();

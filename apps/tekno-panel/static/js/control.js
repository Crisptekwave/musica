/* TEKNO CONTROL: operator actions (confirm dialog, passcode re-entry for risky ones), shared by phone + desktop. */
(function () {
  "use strict";
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  let toastFn = (m) => console.log(m), demoFn = () => false;

  function el() {
    let d = document.getElementById("ctl-dialog");
    if (!d) {
      d = document.createElement("div");
      d.id = "ctl-dialog"; d.className = "ctl-overlay hidden";
      d.innerHTML = `<div class="ctl-box glass" role="dialog" aria-modal="true"></div>`;
      document.body.appendChild(d);
      d.addEventListener("click", e => { if (e.target === d) close(); });
      document.addEventListener("keydown", e => { if (e.key === "Escape" && !d.classList.contains("hidden")) close(); });
    }
    return d;
  }
  function close() { el().classList.add("hidden"); }

  async function post(action, params) {
    const r = await fetch("/api/action", {
      method: "POST", headers: { "Content-Type": "application/json", "x-tekno": "1" },
      body: JSON.stringify({ action, params, demo: demoFn() }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || r.statusText);
    return j;
  }

  /* opts: {action, params, title, body, danger, note: {label, required, placeholder, key}, confirm} */
  function ask(o) {
    const d = el(), box = d.querySelector(".ctl-box");
    const nk = o.note?.key || "note";
    box.innerHTML = `<h2>${esc(o.title)}</h2>${o.body ? `<p class="muted">${o.body}</p>` : ""}
      ${o.note ? `<label class="ctl-label">${esc(o.note.label || "Note")}${o.note.required ? "" : " (optional)"}</label><textarea id="ctl-note" maxlength="1000" placeholder="${esc(o.note.placeholder || "")}"></textarea>` : ""}
      ${o.danger ? `<label class="ctl-label">Passcode <span class="muted">(this one runs as root/lane user)</span></label><input id="ctl-pass" type="password" inputmode="numeric" autocomplete="off" placeholder="••••••">` : ""}
      <div class="ctl-actions"><button class="ghost" id="ctl-cancel">Cancel</button><button class="btn ${o.danger ? "reject" : "approve"}" id="ctl-go">${esc(o.confirm || "Confirm")}</button></div>
      <p class="ctl-err" id="ctl-err" role="alert"></p>`;
    d.classList.remove("hidden");
    (box.querySelector("#ctl-note") || box.querySelector("#ctl-pass") || box.querySelector("#ctl-go")).focus();
    box.querySelector("#ctl-cancel").onclick = close;
    return new Promise(resolve => {
      const go = box.querySelector("#ctl-go");
      const submit = async () => {
        const params = { ...(o.params || {}) };
        const note = box.querySelector("#ctl-note")?.value.trim();
        if (o.note) { if (o.note.required && !note) { box.querySelector("#ctl-err").textContent = `${o.note.label || "A note"} is required`; return; } if (note) params[nk] = note; }
        if (o.danger) params.passcode = box.querySelector("#ctl-pass").value.trim();
        go.disabled = true; go.textContent = "Working…";
        try {
          const j = await post(o.action, params);
          close(); toastFn(j.demo ? j.summary : `✔ ${j.summary}${j.result && j.result !== "done" ? " · " + j.result : ""}`);
          resolve(j);
        } catch (e) {
          box.querySelector("#ctl-err").textContent = String(e.message || e);
          go.disabled = false; go.textContent = o.confirm || "Confirm";
        }
      };
      go.onclick = submit;
      box.querySelector("#ctl-pass")?.addEventListener("keydown", e => { if (e.key === "Enter") submit(); });
    });
  }

  // ---- ready-made actions
  const A = {
    pause: () => ask({ action: "machine.pause", title: "Pause the machine?", confirm: "Pause",
      body: "Writes <b>PAUSED</b>. New ACE jobs refuse to start; work already running finishes.", note: { label: "Reason", key: "reason", placeholder: "e.g. tweaking the LoRA" } }),
    resume: () => ask({ action: "machine.resume", title: "Resume the machine?", confirm: "Resume", body: "Removes PAUSED, also one the watchdog wrote (check disk first)." }),
    note: (task, slug) => ask({ action: "card.note", params: { task_id: task }, title: `Note on ${slug || task}`, confirm: "Add note",
      body: `Adds a comment on kanban task <span class="mono">${esc(task)}</span>.`, note: { label: "Note", required: true, placeholder: "e.g. regen drop2 with a new seed" } }),
    unblock: (task, slug) => ask({ action: "card.unblock", params: { task_id: task }, title: `Unblock ${slug || task}?`, confirm: "Unblock",
      body: `Moves <span class="mono">${esc(task)}</span> back to ready. The gateway's dispatcher may start a worker.`, note: { label: "Reason" } }),
    block: (task, slug) => ask({ action: "card.block", params: { task_id: task }, title: `Block ${slug || task}?`, confirm: "Block",
      body: `Parks <span class="mono">${esc(task)}</span> as blocked.`, note: { label: "Reason", required: true } }),
    dispatch: () => ask({ action: "kanban.dispatch", title: "Run one dispatcher pass?", confirm: "Dispatch", danger: true,
      body: "Promotes ready cards and spawns up to 3 Hermes workers." }),
    run: (slug, step) => ask({ action: "lane.run", params: { card: slug, step }, title: `Run ${slug} from ${step}?`, confirm: "Run", danger: true,
      body: `Starts <span class="mono">run_e2e.sh ${esc(slug)} ${esc(step)}</span> as the lane user. Output goes to the card's next runN.log; the bots light up as each step runs.` }),
    stop: slug => ask({ action: "lane.stop", params: { card: slug }, title: `Stop ${slug}?`, confirm: "Stop", danger: true,
      body: "Sends SIGTERM to the lane scripts and run_e2e.sh running for this card. Half-written files stay as they are." }),
    restart: svc => ask({ action: "service.restart", params: { service: svc }, title: `Restart ${svc}?`, confirm: "Restart", danger: true,
      body: svc.startsWith("acestep") ? "ACE takes ~4 minutes to load models again; a take in progress is lost." : "The service restarts now." }),
  };

  const STEPS = ["plan", "gen", "edit", "stems", "swap", "var", "mix", "master"];
  const STEP_LABEL = { plan: "Plan", gen: "Generate", edit: "Edit", stems: "Stems", swap: "Sound Swap", var: "Variation", mix: "Mix", master: "Master + Score" };

  // a compact control center (used on the desktop Controls page and the phone Machine tab)
  function centerHtml(snap) {
    const m = snap.machine || {}, c = snap.controls || {};
    if (!c.enabled) return `<p class="muted">Controls are switched off on the server (TEKNO_CONTROLS=0).</p>`;
    const cards = [...new Set((snap.tracks || []).map(t => t.slug))];
    const running = snap.processes || [];
    const busy = new Set(running.map(p => p.card).filter(Boolean));
    const svcState = Object.fromEntries((snap.health?.services || []).map(s => [s.name, s.status]));
    return `<div class="ctl-sec"><div class="ctl-row"><div><b>Machine</b><div class="muted ctl-small">${m.paused ? `PAUSED ${esc(m.reason || "")}` : "running: lanes may start ACE jobs"}</div></div>
        ${m.paused ? `<button class="btn approve" data-ctl="resume">Resume</button>` : `<button class="btn reject" data-ctl="pause">Pause</button>`}</div></div>
      <div class="ctl-sec"><b>Run the pipeline</b><div class="ctl-row ctl-form">
        <select id="ctl-card" aria-label="Card">${cards.map(s => `<option>${esc(s)}</option>`).join("")}</select>
        <select id="ctl-step" aria-label="From step">${STEPS.map(s => `<option value="${s}">${esc(STEP_LABEL[s])}</option>`).join("")}</select>
        <button class="btn approve" data-ctl="run" ${cards.length ? "" : "disabled"}>Run ▶</button></div>
        <div class="muted ctl-small">run_e2e.sh &lt;card&gt; &lt;step&gt; runs every step from there to Master + Score.</div>
        ${[...busy].map(s => `<div class="ctl-row"><span><span class="sd up" style="display:inline-block;width:8px;height:8px;border-radius:50%"></span> <b class="mono">${esc(s)}</b> <span class="muted">${esc(running.filter(p => p.card === s).map(p => p.script).join(", "))}</span></span><button class="btn reject" data-ctl="stop" data-card="${esc(s)}">Stop</button></div>`).join("")}</div>
      <div class="ctl-sec"><b>Services</b>${(c.services || []).map(s => `<div class="ctl-row"><span><span class="sd ${esc(svcState[s] || "idle")}" style="display:inline-block;width:8px;height:8px;border-radius:50%"></span> ${esc(s)}</span><button class="ghost sm" data-ctl="restart" data-svc="${esc(s)}">Restart</button></div>`).join("")}</div>
      <div class="ctl-sec"><div class="ctl-row"><div><b>Kanban dispatcher</b><div class="muted ctl-small">one pass: ready cards → Hermes workers</div></div><button class="ghost sm" data-ctl="dispatch">Dispatch</button></div></div>`;
  }

  function auditHtml(audit) {
    if (!audit?.length) return `<p class="muted">No actions yet.</p>`;
    return audit.map(a => `<div class="ctl-audit ${a.ok ? "" : "bad"}"><time>${esc(new Date(a.ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }))}</time>
      <span>${a.ok ? "✔" : "✖"} ${esc(a.summary || a.action)}</span><span class="muted">${esc(a.ip || "")}</span></div>`).join("");
  }

  // one click handler for any container that renders centerHtml / track buttons
  function wire(container) {
    container.addEventListener("click", e => {
      const b = e.target.closest("[data-ctl]"); if (!b) return;
      const k = b.dataset.ctl;
      if (k === "run") { const card = b.dataset.card || container.querySelector("#ctl-card")?.value; const step = b.dataset.step || container.querySelector("#ctl-step")?.value; if (card && step) A.run(card, step); }
      else if (k === "stop") A.stop(b.dataset.card);
      else if (k === "restart") A.restart(b.dataset.svc);
      else if (k === "note" || k === "unblock" || k === "block") A[k](b.dataset.task, b.dataset.card);
      else if (A[k]) A[k]();
    });
  }

  // buttons for one track (sheet / drawer)
  function trackButtons(t, snap) {
    if (!snap.controls?.enabled) return "";
    const task = t.current_task?.id;
    const busy = (snap.processes || []).some(p => p.card === t.slug);
    return `<div class="ctl-track">
      ${task ? `<button class="ghost sm" data-ctl="note" data-task="${esc(task)}" data-card="${esc(t.slug)}">✎ Note</button>` : ""}
      ${task && t.current_task?.status === "blocked" ? `<button class="ghost sm" data-ctl="unblock" data-task="${esc(task)}" data-card="${esc(t.slug)}">↻ Unblock</button>` : ""}
      ${task && !["blocked", "done"].includes(t.current_task?.status) ? `<button class="ghost sm" data-ctl="block" data-task="${esc(task)}" data-card="${esc(t.slug)}">⏸ Block</button>` : ""}
      ${busy ? `<button class="btn reject sm" data-ctl="stop" data-card="${esc(t.slug)}">■ Stop</button>`
        : `<select class="ctl-step-sel" aria-label="From step">${STEPS.map(s => `<option value="${s}">${esc(STEP_LABEL[s])}</option>`).join("")}</select><button class="btn approve sm" data-ctl="run-track" data-card="${esc(t.slug)}">▶ Run from</button>`}
    </div>`;
  }
  function wireTrack(container) {
    container.addEventListener("click", e => {
      const b = e.target.closest('[data-ctl="run-track"]'); if (!b) return;
      const sel = b.parentElement.querySelector(".ctl-step-sel");
      A.run(b.dataset.card, sel ? sel.value : "edit");
    });
  }

  window.Control = {
    ask, actions: A, centerHtml, auditHtml, trackButtons, wire, wireTrack,
    init(opts) { toastFn = opts.toast || toastFn; demoFn = opts.demo || demoFn; },
  };
})();

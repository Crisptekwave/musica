/* TEKNO CONTROL: HTML blocks for live machine data, shared by the phone UI and the desktop control room. */
(function () {
  "use strict";
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const toSec = ts => (typeof ts === "number" ? ts : Date.parse(ts) / 1000);
  const ago = ts => {
    if (!ts) return "";
    const d = Date.now() / 1000 - toSec(ts);
    if (!isFinite(d)) return "";
    if (d < 60) return `${Math.max(0, Math.round(d))}s ago`;
    if (d < 3600) return `${Math.round(d / 60)}m ago`;
    if (d < 86400) return `${Math.round(d / 3600)}h ago`;
    return `${Math.round(d / 86400)}d ago`;
  };
  const hm = ts => (ts ? new Date(toSec(ts) * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }) : "–");
  const hms = ts => (ts ? new Date(toSec(ts) * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }) : "–");
  const dur = s => (s == null || !isFinite(s) ? "" : s < 90 ? `${Math.round(s)}s` : s < 5400 ? `${Math.round(s / 60)}m` : `${(s / 3600).toFixed(1)}h`);
  const mmss = s => (!isFinite(s) ? "" : `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`);
  const num = (v, d = 1) => (v == null || !isFinite(v) ? "–" : Number(v).toFixed(d));
  const icon = st => ({ pass: "✔", fail: "✖", warn: "!", info: "i" }[st] || "–");

  // one line under a track: what the plan says
  function planLine(t) {
    const p = t.plan;
    if (!p) return "";
    const bits = [p.bpm ? `${Number(p.bpm)} BPM` : "", p.key || "", p.total_bars ? `${p.total_bars} bars` : "", p.total_s ? mmss(p.total_s) : ""].filter(Boolean);
    return bits.length ? `<div class="lv-meta">${bits.map(esc).join(" · ")}${t.runs?.length ? ` · ${t.runs.length} run${t.runs.length > 1 ? "s" : ""}` : ""}</div>` : "";
  }

  // what is running right now for a track (process or a run log still being written)
  function liveLine(t) {
    const l = t.live;
    if (!l) return "";
    return `<div class="lv-live"><i></i><span class="mono">${esc(l.script)}</span>${l.pid ? ` · pid ${Number(l.pid)}` : ""}${l.since ? ` · ${dur(Date.now() / 1000 - l.since)}` : ""}</div>`;
  }

  // the board stopped at an older state than the card folder
  function behindNote(t) {
    if (!t.board_behind) return "";
    const r = (t.runs || [])[t.runs.length - 1];
    return `<div class="lv-note">Board last moved ${ago(t.board_activity)}; the card folder changed ${ago(t.disk_mtime)}.${r ? ` Latest pass <b>${esc(r.name)}</b> (from ${esc(r.from || "?")}): ${esc(r.verdict || (r.finished ? "done" : "running"))}${r.draft != null ? ` · draft ${num(r.draft)}` : ""}.` : ""}</div>`;
  }

  function sendbackHtml(sb, cls = "") {
    if (!sb) return "";
    const how = sb.hypothetical ? "would send it back (dry run)" : "sent it back";
    return `<div class="${cls || "sendback"}${sb.hypothetical ? " dry" : ""}"><b>${esc(sb.from || "?")} → ${esc(sb.to)}</b> ${how}: ${esc(sb.reason)}</div>`;
  }

  function planHtml(p) {
    if (!p) return `<div class="muted">No plan/plan.json yet.</div>`;
    const secs = p.sections || [];
    const strip = secs.length ? `<div class="lv-plan">${secs.map(s => `<div class="lv-sec" style="flex:${s.bars || 1}" title="${esc(s.name)} · ${s.bars || "?"} bars · energy ${s.energy ?? "?"}">
        <i style="height:${Math.max(12, (s.energy || 0) * 10)}%"></i><span>${esc(s.name)}</span></div>`).join("")}</div>` : "";
    return `<div class="lv-kv"><b>${p.bpm ? Number(p.bpm) : "–"}</b><span>BPM</span><b>${esc(p.key || "–")}</b><span>key</span><b>${p.total_bars ?? "–"}</b><span>bars</span><b>${p.total_s ? mmss(p.total_s) : "–"}</b><span>length</span></div>
      ${strip}${p.rule ? `<div class="muted lv-small">${esc(p.rule)}</div>` : ""}${p.source === "brief" ? `<div class="muted lv-small">from study/brief.json (no plan.json)</div>` : ""}`;
  }

  function runsHtml(runs) {
    if (!runs?.length) return `<div class="muted">No run logs in the card folder.</div>`;
    return `<div class="lv-runs">${[...runs].reverse().map(r => `<div class="lv-run ${r.finished ? (r.exit === 0 ? "ok" : "bad") : "live"}">
        <span class="mono">${esc(r.name)}</span><span>from <b>${esc(r.from || "?")}</b> → ${esc(r.step || "?")}</span>
        <span>${r.started ? hm(r.started) : ""}${r.ended && r.finished ? `–${hm(r.ended)}` : r.finished ? "" : " · running"}</span>
        <span class="v">${esc(r.verdict || (r.finished ? `exit ${r.exit}` : "…"))}${r.draft != null ? ` · ${num(r.draft)}` : ""}</span>
        ${r.fails?.length ? `<span class="f">${r.fails.map(esc).join(" · ")}</span>` : ""}</div>`).join("")}</div>`;
  }

  function handoffHtml(list) {
    if (!list?.length) return "";
    return list.map(h => `<div class="lv-handoff ${h.stale ? "stale" : ""}"><div><b class="mono">${esc(h.project)}</b> <span class="muted">${ago(h.mtime)}</span></div>
      <div class="lv-small">master + ${Number(h.stems || 0)} stems · ${esc(h.verdict || "no score")}${h.draft != null ? ` · draft ${num(h.draft)}` : ""}</div>
      ${h.stale ? `<div class="lv-small warn-text">Older than the card's current master: handoff.py has not run since.</div>` : ""}</div>`).join("");
  }

  const REPORT_LABELS = {
    edit: "Edit", stems: "Stems", swap: "Sound Swap", var: "Variation", filter: "Filter", mix: "Mix", polish: "Polish / Master",
  };
  function reportsHtml(reports) {
    const ent = Object.entries(reports || {});
    if (!ent.length) return "";
    return `<div class="lv-reports">${ent.map(([k, r]) => `<div class="lv-rep"><b>${esc(REPORT_LABELS[k] || k)}</b><span>${Object.entries(r).filter(([kk]) => kk !== "mtime" && kk !== "mode")
      .map(([kk, v]) => `${esc(kk.replace(/_/g, " "))} <i>${esc(typeof v === "number" ? +v.toFixed(2) : v)}</i>`).join(" · ")}</span></div>`).join("")}</div>`;
  }

  // ref_compare.py: band level difference vs SIFRES v02, as diverging bars
  function refHtml(sc) {
    const r = sc?.ref;
    if (!r) return `<div class="muted">No ref-compare in this score.json.</div>`;
    if (r.error) return `<div class="muted">Ref-compare failed: ${esc(r.error)}</div>`;
    const bands = Object.entries(r.bands || {});
    const max = Math.max(6, ...bands.map(([, v]) => Math.abs(v || 0)));
    return `<div class="lv-ref">${bands.map(([b, v]) => {
      const w = Math.min(50, 50 * Math.abs(v || 0) / max);
      return `<div class="lv-band"><span class="n">${esc(b)}</span><div class="lv-bar"><i class="${v < 0 ? "neg" : "pos"}" style="${v < 0 ? `right:50%` : `left:50%`};width:${w}%"></i><em></em></div><span class="d mono">${v > 0 ? "+" : ""}${num(v)}</span></div>`;
    }).join("")}</div>
      <div class="lv-kv sm"><b>${num(r.dist, 2)}</b><span>dB distance</span><b>${r.tilt > 0 ? "+" : ""}${num(r.tilt)}</b><span>HF tilt</span><b>${num(r.crest)} / ${num(r.crest_ref)}</b><span>crest vs ref</span><b>${num(r.corr14, 2)} / ${num(r.corr14_ref, 2)}</b><span>width 1-4k vs ref</span></div>`;
  }

  function draftHtml(sc) {
    const d = sc?.draft_scores;
    if (!d) return "";
    return `<div class="lv-draft">${Object.entries(d).map(([k, v]) => `<div><span>${esc(k.replace(/_/g, " "))}</span><div class="lv-dbar"><i style="width:${Math.max(0, Math.min(100, (v || 0) * 10))}%" class="${v >= 6.5 ? "ok" : v >= 5 ? "mid" : "low"}"></i></div><b class="mono">${num(v)}</b></div>`).join("")}</div>`;
  }

  function badgesHtml(badges) {
    return (badges || []).map(b => `<div class="badge ${esc(b.status)}" title="${esc(b.detail || "")}"><div class="l"><span>${esc(b.label)}</span><span>${icon(b.status)}</span></div>
      <div class="v">${esc(b.value ?? "–")}</div><div class="t">${esc(b.target)}</div>${b.detail ? `<div class="dt">${esc(b.detail)}</div>` : ""}</div>`).join("");
  }

  const PHASE = { lm: "LM writing audio codes", dit: "DiT diffusion", decode: "decoding latents" };
  function aceHtml(a) {
    a = a || {};
    const j = a.job;
    let out = `<div class="svc-row"><span class="sd ${a.status === "generating" ? "up" : a.ok ? "up" : "down"}"></span><b>${esc(a.status || "unknown")}</b>
      <span class="via">${j ? `${j.card ? `card <b>${esc(j.card)}</b>` : "take from the ACE UI"}${j.script ? ` · ${esc(j.script)}` : ""}${j.pid ? ` · pid ${Number(j.pid)}` : ""}` : a.log_age_s != null ? `log quiet ${dur(a.log_age_s)}` : ""}</span></div>`;
    if (j) {
      const sec = a.sections;
      out += `<div class="progress"><div class="bar"><i style="width:${a.progress || 0}%"></i></div><span class="pct">${a.progress ?? 0}%</span></div>
        <div class="lv-small">${esc(PHASE[j.phase] || "waiting for ACE")}${j.step && j.total ? ` · step ${j.step}/${j.total}` : ""}${j.seed ? ` · seed ${esc(j.seed)}` : ""}${sec && sec.total ? ` · section ${Math.min(sec.done + 1, sec.total)}/${sec.total}` : ""}${j.since ? ` · ${dur(Date.now() / 1000 - j.since)}` : ""}</div>`;
    }
    const d = a.last_done;
    out += `<div class="lv-kv sm">${d ? `<b>${hm(d.at)}</b><span>last take${d.took_s ? ` · ${Math.round(d.took_s)} s` : ""}</span>` : ""}${a.lora ? `<b>${esc(a.lora.name)}</b><span>LoRA${a.lora.scale != null ? ` × ${a.lora.scale}` : ""}</span>` : ""}</div>`;
    if (a.last_error) out += `<div class="lv-small warn-text">Last error ${ago(a.last_error.at)}: ${esc(a.last_error.text)}</div>`;
    const ev = (a.events || []).slice(-6).map(e => `${e.at ? hms(e.at) : "        "}  ${e.text}`).join("\n");
    out += `<div class="log">${esc(ev || "no ACE activity in the log tail")}</div>`;
    return out;
  }

  function journalHtml(rows) {
    if (!rows?.length) return "";
    return `<div class="log lv-journal">${esc(rows.slice(-6).map(r => `${hm(r.at)}  ${r.text.replace(/ - ACE-Step.*$/, "").replace(/ - Load default.*$/, "")}`).join("\n"))}</div>`;
  }

  function gpuAppsHtml(g) {
    const apps = g?.apps || [];
    return apps.length ? `<div class="lv-small">${apps.map(x => `<b>${esc(x.name)}</b> pid ${Number(x.pid)} · ${x.mem != null ? (x.mem / 1024).toFixed(1) + " GB" : "?"}`).join(" · ")}</div>` : "";
  }

  function procsHtml(list) {
    if (!list?.length) return `<div class="muted lv-small">No lane script running. Bots sleep until a script or a kanban worker starts.</div>`;
    return list.map(p => `<div class="svc-row" title="pid ${Number(p.pid)}${p.task ? " · " + esc(p.task) : ""}"><span class="sd up"></span><b class="mono">${esc(p.script)}</b><span class="via">${esc((p.lanes || []).join(" + ") || "orchestrator")}${p.card ? ` · ${esc(p.card)}` : ""}${p.since ? ` · ${dur(Date.now() / 1000 - p.since)}` : ""}</span></div>`).join("");
  }

  const SRC_LABEL = { kanban: "Kanban board", files: "Track files", ace_log: "ACE log", mail: "Agent mail" };
  function freshnessHtml(snap) {
    const f = snap.freshness || {}, src = snap.sources || {};
    return Object.keys(SRC_LABEL).map(k => {
      const s = src[k] || {};
      const bad = s.ok === false;
      return `<div class="svc-row"><span class="sd ${bad ? "down" : f[k] ? "up" : "idle"}"></span><b>${SRC_LABEL[k]}</b>
        <span class="via">${bad ? esc(s.error || "down") : f[k] ? `last change ${hm(f[k])} · ${ago(f[k])}` : k === "mail" ? "no messages yet" : "–"}</span></div>`;
    }).join("");
  }

  function threadMsgsHtml(msgs, agentColor) {
    if (!msgs?.length) return "";
    return msgs.slice(-6).map(m => `<div class="lv-msg"><b style="color:${agentColor ? agentColor(m.from) : "var(--acid)"}">${esc(m.from)}</b> <span class="muted">${ago(m.ts)}</span><div>${esc(m.body || m.subject)}</div></div>`).join("");
  }

  const loops = n => `↺ ${Number(n)} loop${Number(n) === 1 ? "" : "s"}`;

  window.Live = {
    loops,
    esc, ago, hm, hms, dur, num, icon, planLine, liveLine, behindNote, sendbackHtml, planHtml, runsHtml, handoffHtml,
    reportsHtml, refHtml, draftHtml, badgesHtml, aceHtml, journalHtml, gpuAppsHtml, procsHtml, freshnessHtml, threadMsgsHtml,
  };
})();

/* Tiny canvas charts: history lines with target bands, ring gauges, sparklines. No deps. */
(function () {
  const ACID = "#39ff14", UV = "#9d4dff", RED = "#ff2d55", MUTED = "#8a9a86";

  function fit(canvas) {
    const dpr = Math.min(window.devicePixelRatio || 1, 3);
    const w = canvas.clientWidth || canvas.width, h = canvas.clientHeight || canvas.height;
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, w, h };
  }

  // series: [{values:[...], color, label}], opts: {band:[lo,hi], line:y, min, max, labels:[...]}
  function lines(canvas, series, opts = {}) {
    const { ctx, w, h } = fit(canvas);
    ctx.clearRect(0, 0, w, h);
    const all = series.flatMap(s => s.values.filter(v => v != null));
    if (opts.band) all.push(...opts.band);
    if (opts.line != null) all.push(opts.line);
    if (!all.length) { ctx.fillStyle = MUTED; ctx.font = "12px system-ui"; ctx.fillText("no data yet", 10, h / 2); return; }
    let lo = opts.min ?? Math.min(...all), hi = opts.max ?? Math.max(...all);
    if (hi - lo < 1e-6) { hi += 1; lo -= 1; }
    const pad = (hi - lo) * 0.12; lo -= pad; hi += pad;
    const L = 34, R = 8, T = 8, B = 18;
    const n = Math.max(...series.map(s => s.values.length), 2);
    const X = i => L + (w - L - R) * (n === 1 ? 0.5 : i / (n - 1));
    const Y = v => T + (h - T - B) * (1 - (v - lo) / (hi - lo));
    ctx.font = "10px ui-monospace, monospace"; ctx.fillStyle = MUTED; ctx.strokeStyle = "rgba(255,255,255,.06)"; ctx.lineWidth = 1;
    for (let k = 0; k <= 3; k++) {
      const v = lo + (hi - lo) * k / 3, y = Y(v);
      ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(w - R, y); ctx.stroke();
      ctx.fillText(v.toFixed(Math.abs(hi - lo) < 5 ? 1 : 0), 2, y + 3);
    }
    if (opts.band) {
      ctx.fillStyle = "rgba(57,255,20,.10)";
      const y1 = Y(opts.band[1]), y0 = Y(opts.band[0]);
      ctx.fillRect(L, y1, w - L - R, y0 - y1);
    }
    if (opts.line != null) {
      ctx.setLineDash([5, 4]); ctx.strokeStyle = "rgba(255,45,85,.7)";
      ctx.beginPath(); ctx.moveTo(L, Y(opts.line)); ctx.lineTo(w - R, Y(opts.line)); ctx.stroke(); ctx.setLineDash([]);
    }
    for (const s of series) {
      const pts = s.values.map((v, i) => v == null ? null : [X(i), Y(v), v]);
      ctx.strokeStyle = s.color || ACID; ctx.lineWidth = 2; ctx.shadowColor = s.color || ACID; ctx.shadowBlur = 8;
      ctx.beginPath(); let started = false;
      for (const p of pts) { if (!p) { started = false; continue; } started ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]); started = true; }
      ctx.stroke(); ctx.shadowBlur = 0;
      for (const p of pts) {
        if (!p) continue;
        const ok = s.ok ? s.ok(p[2]) : true;
        ctx.fillStyle = ok ? (s.color || ACID) : RED;
        ctx.beginPath(); ctx.arc(p[0], p[1], 3.2, 0, Math.PI * 2); ctx.fill();
      }
    }
    if (series.length > 1) {
      let x = L + 4;
      for (const s of series) { ctx.fillStyle = s.color; ctx.fillRect(x, h - 10, 10, 3); ctx.fillStyle = MUTED; ctx.fillText(s.label, x + 14, h - 6); x += 20 + ctx.measureText(s.label).width + 10; }
    }
  }

  function gauge(canvas, frac, label, warn) {
    const { ctx, w, h } = fit(canvas);
    const r = Math.min(w, h) / 2 - 10, cx = w / 2, cy = h / 2;
    ctx.clearRect(0, 0, w, h);
    const a0 = Math.PI * 0.75, a1 = Math.PI * 2.25;
    ctx.lineCap = "round"; ctx.lineWidth = 12;
    ctx.strokeStyle = "rgba(255,255,255,.07)"; ctx.beginPath(); ctx.arc(cx, cy, r, a0, a1); ctx.stroke();
    const f = Math.max(0, Math.min(1, frac || 0));
    const g = ctx.createLinearGradient(0, h, w, 0);
    g.addColorStop(0, UV); g.addColorStop(1, warn ? RED : ACID);
    ctx.strokeStyle = g; ctx.shadowColor = warn ? RED : ACID; ctx.shadowBlur = 16;
    ctx.beginPath(); ctx.arc(cx, cy, r, a0, a0 + (a1 - a0) * f); ctx.stroke(); ctx.shadowBlur = 0;
    ctx.fillStyle = warn ? RED : ACID; ctx.font = "900 26px ui-monospace, monospace"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText(label, cx, cy);
    ctx.textAlign = "start"; ctx.textBaseline = "alphabetic";
  }

  function spark(canvas, values, max, color = ACID) {
    const { ctx, w, h } = fit(canvas);
    ctx.clearRect(0, 0, w, h);
    if (values.length < 2) return;
    const X = i => w * i / (values.length - 1), Y = v => h - 4 - (h - 8) * Math.min(1, v / max);
    const g = ctx.createLinearGradient(0, 0, 0, h); g.addColorStop(0, "rgba(57,255,20,.35)"); g.addColorStop(1, "rgba(57,255,20,0)");
    ctx.beginPath(); ctx.moveTo(0, h);
    values.forEach((v, i) => ctx.lineTo(X(i), Y(v)));
    ctx.lineTo(w, h); ctx.closePath(); ctx.fillStyle = g; ctx.fill();
    ctx.beginPath(); values.forEach((v, i) => i ? ctx.lineTo(X(i), Y(v)) : ctx.moveTo(X(i), Y(v)));
    ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.shadowColor = color; ctx.shadowBlur = 8; ctx.stroke(); ctx.shadowBlur = 0;
  }

  window.Charts = { fit, lines, gauge, spark, ACID, UV, RED };
})();

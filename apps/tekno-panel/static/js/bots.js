/* Live bots: one particle "creature" per agent, drawn on a 2D canvas with additive blending.
   Defaults to the fixed pipeline lanes; setNames() swaps in any list (the phone shows the live agent roster). */
(function () {
  const LANES = ["Plan", "Generate", "Stems", "Sound Swap", "Edit", "Filter", "Variation", "Mix", "Polish", "Master", "Score", "Review"];
  const COLORS = {
    working: [57, 255, 20], idle: [140, 80, 255], failed: [255, 45, 85], waiting: [199, 125, 255],
  };
  const BEAT = 60 / 165;

  class Orb {
    constructor(lane, i) {
      this.lane = lane; this.i = i; this.state = "idle"; this.data = null;
      this.col = [...COLORS.idle]; this.energy = 0.2;
      this.parts = Array.from({ length: 70 }, () => ({
        a: Math.random() * Math.PI * 2, r: 0.35 + Math.random() * 0.75, s: 0.4 + Math.random() * 1.2,
        z: Math.random() * Math.PI * 2, size: 0.6 + Math.random() * 1.8,
      }));
    }
  }

  class Bots {
    constructor(canvas, onTap, opts = {}) {
      this.c = canvas; this.ctx = canvas.getContext("2d"); this.onTap = onTap; this.opts = opts;
      this.names = LANES;
      this.orbs = LANES.map((l, i) => new Orb(l, i));
      this.flights = []; this.lastHandoff = {}; this.running = false; this.t0 = performance.now();
      canvas.addEventListener("click", e => this.tap(e));
      window.addEventListener("resize", () => this.layout());
      this.layout();
    }

    setNames(names) {
      if (names.join("\n") === this.names.join("\n")) return;
      const old = new Map(this.orbs.map(o => [o.lane, o]));
      this.names = names;
      this.orbs = names.map((n, i) => Object.assign(old.get(n) || new Orb(n, i), { i }));
      this.flights = [];
      this.layout();
    }

    layout() {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const w = this.c.clientWidth, h = this.c.clientHeight;
      this.c.width = Math.round(w * dpr); this.c.height = Math.round(h * dpr);
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      this.w = w; this.h = h;
      const oc = this.opts.cols, n = Math.max(1, this.orbs.length);
      const cols = Math.min(n, (typeof oc === "function" ? oc(w, h) : oc) || (w > 600 ? 4 : 3));
      const rows = Math.ceil(n / cols), oy = this.opts.oy || 0.45;
      const cw = w / cols, rh = h / rows;
      this.R = Math.min(cw, rh) * 0.2;
      this.orbs.forEach((o, i) => {
        const row = Math.floor(i / cols); let col = i % cols;
        if (row % 2 === 1) col = cols - 1 - col; // serpentine: hand-offs flow like a snake
        o.x = cw * (col + 0.5); o.y = rh * (row + oy);
      });
    }

    update(lanes) {
      const now = Date.now() / 1000;
      for (const o of this.orbs) {
        const L = lanes[o.lane] || { state: "idle" };
        o.state = L.state || "idle"; o.data = L;
        if (L.handoff_to && L.handoff_at && this.lastHandoff[o.lane] !== L.handoff_at && now - L.handoff_at < 25) {
          this.lastHandoff[o.lane] = L.handoff_at;
          const to = this.orbs.find(x => x.lane === L.handoff_to);
          if (to) this.fly(o, to);
        }
      }
    }

    fly(a, b) {
      for (let k = 0; k < 46; k++) {
        this.flights.push({ a, b, t: -k * 0.018, sp: 0.55 + Math.random() * 0.35, off: (Math.random() - 0.5) * 2, size: 1 + Math.random() * 2.4 });
      }
    }

    tap(e) {
      const r = this.c.getBoundingClientRect();
      const x = e.clientX - r.left, y = e.clientY - r.top;
      let best = null, bd = 1e9;
      for (const o of this.orbs) { const d = Math.hypot(o.x - x, o.y - y); if (d < bd) { bd = d; best = o; } }
      if (best && bd < this.R * 1.9) {
        best.kick = 1;
        this.onTap && this.onTap(best.lane, best.data || { state: best.state });
      }
    }

    start() { if (!this.running) { this.running = true; this.layout(); requestAnimationFrame(t => this.frame(t)); } }
    stop() { this.running = false; }

    frame(t) {
      if (!this.running) return;
      const ctx = this.ctx, time = (t - this.t0) / 1000;
      const dt = Math.min(0.05, (t - (this.lt || t)) / 1000); this.lt = t;
      ctx.globalCompositeOperation = "source-over";
      ctx.fillStyle = "rgba(0,0,0,0.38)"; ctx.fillRect(0, 0, this.w, this.h);
      // links between lanes
      ctx.lineWidth = 1;
      for (let i = 0; i < this.orbs.length - 1; i++) {
        const a = this.orbs[i], b = this.orbs[i + 1];
        ctx.strokeStyle = "rgba(57,255,20,0.07)";
        ctx.setLineDash([3, 6]); ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
      }
      ctx.setLineDash([]);
      ctx.globalCompositeOperation = "lighter";
      const beat = (time % BEAT) / BEAT;
      for (const o of this.orbs) this.drawOrb(o, time, dt, beat);
      this.drawFlights(dt);
      ctx.globalCompositeOperation = "source-over";
      for (const o of this.orbs) this.drawLabel(o, time);
      requestAnimationFrame(tt => this.frame(tt));
    }

    drawOrb(o, time, dt, beat) {
      const ctx = this.ctx, target = COLORS[o.state] || COLORS.idle;
      for (let k = 0; k < 3; k++) o.col[k] += (target[k] - o.col[k]) * Math.min(1, dt * 4);
      const want = o.state === "working" ? 1 : o.state === "failed" ? 0.75 : o.state === "waiting" ? 0.5 : 0.18;
      o.energy += (want - o.energy) * Math.min(1, dt * 2);
      o.kick = Math.max(0, (o.kick || 0) - dt * 2.5);
      const [r, g, b] = o.col.map(Math.round);
      let pulse;
      if (o.state === "working") pulse = 1 + 0.18 * Math.pow(1 - beat, 3);
      else if (o.state === "failed") pulse = 1 + 0.08 * Math.sin(time * 18);
      else if (o.state === "waiting") pulse = 1 + 0.06 * Math.sin(time * 2.4);
      else pulse = 1 + 0.04 * Math.sin(time * 0.9 + o.i); // sleeping breath
      const R = this.R * (0.72 + 0.28 * o.energy) * pulse * (1 + 0.25 * o.kick);
      let jx = 0, jy = 0;
      if (o.state === "failed") { jx = (Math.random() - 0.5) * 3; jy = (Math.random() - 0.5) * 3; }
      const cx = o.x + jx, cy = o.y + jy;
      // halo
      const halo = ctx.createRadialGradient(cx, cy, 0, cx, cy, R * 1.9);
      halo.addColorStop(0, `rgba(${r},${g},${b},${0.16 * (0.4 + o.energy)})`);
      halo.addColorStop(1, `rgba(${r},${g},${b},0)`);
      ctx.fillStyle = halo; ctx.beginPath(); ctx.arc(cx, cy, R * 1.9, 0, Math.PI * 2); ctx.fill();
      // core
      const core = ctx.createRadialGradient(cx - R * 0.2, cy - R * 0.25, R * 0.05, cx, cy, R);
      core.addColorStop(0, `rgba(255,255,255,${0.25 + 0.35 * o.energy})`);
      core.addColorStop(0.35, `rgba(${r},${g},${b},${0.45 + 0.3 * o.energy})`);
      core.addColorStop(1, `rgba(${r},${g},${b},0.02)`);
      ctx.fillStyle = core; ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.fill();
      // particles
      const speed = o.state === "working" ? 2.6 : o.state === "failed" ? 4 : 0.35;
      for (const p of o.parts) {
        p.a += dt * p.s * speed; p.z += dt * p.s * 0.8;
        const rr = R * (0.85 + p.r * (0.35 + 0.4 * o.energy)) * (o.state === "working" ? 1 + 0.15 * Math.sin(p.z * 3) : 1);
        const x = cx + Math.cos(p.a) * rr, y = cy + Math.sin(p.a) * rr * (0.55 + 0.45 * Math.sin(p.z));
        const al = (0.2 + 0.45 * o.energy) * (0.5 + 0.5 * Math.sin(p.z * 2));
        ctx.fillStyle = `rgba(${r},${g},${b},${al})`;
        ctx.beginPath(); ctx.arc(x, y, p.size * (0.6 + 0.6 * o.energy), 0, Math.PI * 2); ctx.fill();
      }
      // eyes: the creature looks around when awake, closes them when asleep
      const ey = cy - R * 0.08, ex = R * 0.28, look = Math.sin(time * 0.7 + o.i) * R * 0.06;
      ctx.fillStyle = o.state === "idle" ? "rgba(0,0,0,0.55)" : "rgba(0,0,0,0.8)";
      for (const s of [-1, 1]) {
        ctx.beginPath();
        if (o.state === "idle") ctx.ellipse(cx + s * ex, ey, R * 0.1, R * 0.025, 0, 0, Math.PI * 2);
        else ctx.ellipse(cx + s * ex + look, ey, R * 0.07, R * (o.state === "failed" ? 0.05 : 0.11), 0, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    drawFlights(dt) {
      const ctx = this.ctx;
      this.flights = this.flights.filter(f => f.t < 1);
      for (const f of this.flights) {
        f.t += dt * f.sp;
        if (f.t < 0) continue;
        const t = f.t, a = f.a, b = f.b;
        const mx = (a.x + b.x) / 2 + (b.y - a.y) * 0.3 * f.off, my = (a.y + b.y) / 2 - (b.x - a.x) * 0.3 * f.off - 30;
        const x = (1 - t) * (1 - t) * a.x + 2 * (1 - t) * t * mx + t * t * b.x;
        const y = (1 - t) * (1 - t) * a.y + 2 * (1 - t) * t * my + t * t * b.y;
        const al = Math.sin(Math.PI * t);
        ctx.fillStyle = `rgba(57,255,20,${0.9 * al})`;
        ctx.beginPath(); ctx.arc(x, y, f.size * (1 + al), 0, Math.PI * 2); ctx.fill();
        ctx.fillStyle = `rgba(199,125,255,${0.35 * al})`;
        ctx.beginPath(); ctx.arc(x, y, f.size * 3.2, 0, Math.PI * 2); ctx.fill();
        if (t > 0.97) b.kick = 1;
      }
    }

    drawLabel(o, time) {
      const ctx = this.ctx, y = o.y + this.R * 1.95;
      ctx.textAlign = "center";
      ctx.font = "800 12px Inter, system-ui, sans-serif";
      const lw = Math.max(ctx.measureText(o.lane.toUpperCase()).width, 54) + 16;
      ctx.fillStyle = "rgba(0,0,0,0.62)";
      ctx.beginPath(); (ctx.roundRect ? ctx.roundRect(o.x - lw / 2, y - 13, lw, 31, 9) : ctx.rect(o.x - lw / 2, y - 13, lw, 31)); ctx.fill();
      ctx.fillStyle = o.state === "working" ? "#39ff14" : o.state === "failed" ? "#ff2d55" : "#e9ffe4";
      ctx.fillText(o.lane.toUpperCase(), o.x, y);
      ctx.font = "600 10px ui-monospace, monospace";
      ctx.fillStyle = "#8a9a86";
      const st = (o.data && o.data.label) || (o.state === "idle" ? "sleeping" : o.state);
      ctx.fillText(st, o.x, y + 13);
      if (o.state === "idle") {
        const k = (time * 0.5 + o.i * 0.3) % 1;
        ctx.fillStyle = `rgba(199,125,255,${0.8 * (1 - k)})`;
        ctx.font = `700 ${9 + 5 * k}px system-ui`;
        ctx.fillText("z", o.x + this.R * 0.7 + k * 10, o.y - this.R * 0.6 - k * 18);
      }
      ctx.textAlign = "start";
    }
  }

  window.Bots = Bots;
  window.BOT_LANES = LANES;
})();

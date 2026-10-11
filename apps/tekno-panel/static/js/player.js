/* Listen screen audio engine: two media elements (A = take, B = SIFRES reference) through Web Audio,
   with spectrum, stereo meters and a scrolling waveform from AnalyserNodes. */
(function () {
  class Player {
    constructor(opts) {
      this.a = opts.a; this.b = opts.b; this.spec = opts.spectrum; this.scope = opts.scope;
      this.ml = opts.meterL; this.mr = opts.meterR; this.mdb = opts.meterDb;
      this.side = "A"; this.ctx = null; this.peaks = [];
      this.onState = opts.onState || (() => {});
      for (const el of [this.a, this.b]) {
        el.addEventListener("ended", () => { if (this.active() === el) this.onState(false); });
        el.addEventListener("error", () => { if (this.active() === el && el.src) this.onState(false, "can't play this file"); });
      }
    }

    active() { return this.side === "A" ? this.a : this.b; }

    ensureGraph() {
      if (this.ctx) return;
      const AC = window.AudioContext || window.webkitAudioContext;
      this.ctx = new AC();
      const c = this.ctx;
      this.gA = c.createGain(); this.gB = c.createGain();
      this.gB.gain.value = 0;
      c.createMediaElementSource(this.a).connect(this.gA);
      c.createMediaElementSource(this.b).connect(this.gB);
      this.mix = c.createGain();
      this.gA.connect(this.mix); this.gB.connect(this.mix);
      this.an = c.createAnalyser(); this.an.fftSize = 4096; this.an.smoothingTimeConstant = 0.78;
      const split = c.createChannelSplitter(2);
      this.anL = c.createAnalyser(); this.anR = c.createAnalyser();
      this.anL.fftSize = this.anR.fftSize = 1024;
      this.mix.connect(this.an); this.mix.connect(split);
      split.connect(this.anL, 0); split.connect(this.anR, 1);
      this.mix.connect(c.destination);
      this.freq = new Uint8Array(this.an.frequencyBinCount);
      this.td = new Float32Array(this.an.fftSize);
      this.tdL = new Float32Array(1024); this.tdR = new Float32Array(1024);
      this.loop();
    }

    load(url, label) {
      const wasPlaying = !this.active().paused;
      this.a.src = url; this.a.load();
      this.aLabel = label;
      if (this.side !== "A") this.setSide("A");
      if (wasPlaying) this.play();
    }

    loadRef(url) { if (this.b.dataset.url !== url) { this.b.dataset.url = url; this.b.src = url; this.b.load(); } }

    async play() {
      this.ensureGraph();
      if (this.ctx.state === "suspended") await this.ctx.resume();
      const el = this.active(), other = el === this.a ? this.b : this.a;
      try {
        await el.play();
        if (other.src) { other.play().catch(() => {}); }  // keep B rolling silently for instant A/B
        this.onState(true);
      } catch (e) { this.onState(false, "tap play again"); }
    }

    pause() { this.a.pause(); this.b.pause(); this.onState(false); }
    toggle() { this.active().paused ? this.play() : this.pause(); }

    setSide(side) {
      if (side === this.side) return;
      const from = this.active();
      this.side = side;
      const to = this.active();
      if (to.duration && isFinite(to.duration)) to.currentTime = Math.min(from.currentTime || 0, to.duration - 0.05);
      if (this.ctx) {
        const t = this.ctx.currentTime;
        this.gA.gain.setTargetAtTime(side === "A" ? 1 : 0, t, 0.015);
        this.gB.gain.setTargetAtTime(side === "B" ? 1 : 0, t, 0.015);
      }
      if (!from.paused) { to.play().catch(() => {}); }
    }

    seek(frac) { const el = this.active(); if (el.duration && isFinite(el.duration)) el.currentTime = frac * el.duration; }

    loop() {
      const draw = () => { this.drawSpectrum(); this.drawScope(); this.drawMeters(); requestAnimationFrame(draw); };
      requestAnimationFrame(draw);
    }

    drawSpectrum() {
      const cv = this.spec; if (!cv.offsetParent) return;
      const { ctx, w, h } = Charts.fit(cv);
      this.an.getByteFrequencyData(this.freq);
      ctx.clearRect(0, 0, w, h);
      const bars = Math.min(64, Math.floor(w / 6)), sr = this.ctx.sampleRate, nyq = sr / 2, bins = this.freq.length;
      const g = ctx.createLinearGradient(0, h, 0, 0);
      g.addColorStop(0, "#9d4dff"); g.addColorStop(0.55, "#39ff14"); g.addColorStop(1, "#eaffd9");
      const bw = w / bars;
      if (!this.caps || this.caps.length !== bars) this.caps = new Array(bars).fill(0);
      for (let i = 0; i < bars; i++) {
        const f0 = 25 * Math.pow(nyq / 25, i / bars), f1 = 25 * Math.pow(nyq / 25, (i + 1) / bars);
        let s = 0, n = 0;
        for (let k = Math.floor(f0 / nyq * bins); k <= Math.min(bins - 1, Math.ceil(f1 / nyq * bins)); k++) { s = Math.max(s, this.freq[k]); n++; }
        const v = n ? s / 255 : 0, bh = v * (h - 6);
        ctx.fillStyle = g; ctx.shadowColor = "#39ff14"; ctx.shadowBlur = v > 0.6 ? 10 : 0;
        ctx.fillRect(i * bw + 1, h - bh, bw - 2, bh);
        this.caps[i] = Math.max(this.caps[i] - 0.012, v);
        ctx.shadowBlur = 0; ctx.fillStyle = "#c77dff";
        ctx.fillRect(i * bw + 1, h - this.caps[i] * (h - 6) - 3, bw - 2, 2);
      }
      ctx.fillStyle = "rgba(138,154,134,.8)"; ctx.font = "9px ui-monospace, monospace";
      for (const f of [50, 120, 500, 2000, 8000]) {
        const x = w * Math.log(f / 25) / Math.log(nyq / 25);
        ctx.fillText(f >= 1000 ? f / 1000 + "k" : f, x + 2, 10);
      }
    }

    drawScope() {
      // scrolling waveform: each frame adds a min/max column on the right, history scrolls left
      const cv = this.scope; if (!cv.offsetParent) return;
      const { ctx, w, h } = Charts.fit(cv);
      this.an.getFloatTimeDomainData(this.td);
      let lo = 0, hi = 0;
      for (let i = 0; i < this.td.length; i++) { const v = this.td[i]; if (v < lo) lo = v; if (v > hi) hi = v; }
      if (!this.active().paused) { this.peaks.push([lo, hi]); }
      const cols = Math.floor(w / 2);
      if (this.peaks.length > cols) this.peaks.splice(0, this.peaks.length - cols);
      ctx.clearRect(0, 0, w, h);
      ctx.strokeStyle = "rgba(255,255,255,.06)"; ctx.beginPath(); ctx.moveTo(0, h / 2); ctx.lineTo(w, h / 2); ctx.stroke();
      const off = cols - this.peaks.length;
      for (let i = 0; i < this.peaks.length; i++) {
        const [l, u] = this.peaks[i], x = (off + i) * 2, age = i / cols;
        ctx.fillStyle = `rgba(${Math.round(157 - 100 * age)},${Math.round(77 + 178 * age)},${Math.round(255 - 235 * age)},${0.35 + 0.65 * age})`;
        ctx.fillRect(x, h / 2 - u * h / 2, 1.6, Math.max(1, (u - l) * h / 2));
      }
    }

    drawMeters() {
      this.anL.getFloatTimeDomainData(this.tdL); this.anR.getFloatTimeDomainData(this.tdR);
      const rms = arr => { let s = 0; for (const v of arr) s += v * v; return Math.sqrt(s / arr.length); };
      const l = rms(this.tdL), r = rms(this.tdR);
      const db = v => 20 * Math.log10(Math.max(v, 1e-6));
      const pct = v => Math.max(0, Math.min(100, (db(v) + 48) / 48 * 100));
      this.ml.style.width = pct(l) + "%"; this.mr.style.width = pct(r) + "%";
      const m = Math.max(l, r);
      this.mdb.textContent = m < 1e-5 ? "-∞ dB" : db(m).toFixed(1) + " dB";
    }
  }
  window.Player = Player;
})();

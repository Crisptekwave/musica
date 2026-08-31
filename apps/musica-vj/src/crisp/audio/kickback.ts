/** On-device rave engine. Original synthesis — no ROM dumps, no packs. */

import type { GrooveSpec } from "./groove.ts";
import { stepGrooveOffsetSec } from "./groove.ts";

export const GENRE_IDS = ["slowrave", "schranz", "acid", "tekno", "mental"] as const;
export type GenreId = (typeof GENRE_IDS)[number];

export const KICK_IDS = ["tunnel", "sledge", "warehouse", "tribal"] as const;
export type KickId = (typeof KICK_IDS)[number];

export const ACID_IDS = ["scream", "wobble", "pluck", "screech"] as const;
export type AcidId = (typeof ACID_IDS)[number];

export const PERC_IDS = ["shuffle", "offbeat", "tribal", "minimal"] as const;
export type PercId = (typeof PERC_IDS)[number];

export const GRIT_IDS = ["mild", "medium", "full"] as const;
export type GritId = (typeof GRIT_IDS)[number];

export type KickbackOpts = {
  genre: GenreId;
  bpm: number;
  kick: KickId;
  acid: AcidId;
  perc: PercId;
  grit: GritId;
  bars: 16 | 32;
  seed: number;
  /** Optional 16-step kick hit pattern (from track DNA). Overrides genre default. */
  kickGrid?: boolean[];
  /** Optional groove quantize (swing/shuffle/half/dotted). Micro-timed hits. */
  groove?: GrooveSpec;
};

export const GENRE_DEFAULTS: Record<
  GenreId,
  { bpm: number; kick: KickId; acid: AcidId; perc: PercId; grit: GritId }
> = {
  slowrave: { bpm: 78, kick: "warehouse", acid: "scream", perc: "minimal", grit: "full" },
  schranz: { bpm: 155, kick: "sledge", acid: "scream", perc: "offbeat", grit: "full" },
  acid: { bpm: 148, kick: "tunnel", acid: "wobble", perc: "shuffle", grit: "medium" },
  tekno: { bpm: 158, kick: "tribal", acid: "pluck", perc: "tribal", grit: "full" },
  mental: { bpm: 168, kick: "sledge", acid: "screech", perc: "shuffle", grit: "full" },
};

export const GENRE_META: Record<GenreId, { name: string; blurb: string }> = {
  slowrave: { name: "Slowrave", blurb: "75–80 BPM warehouse stomp. Sub first." },
  schranz: { name: "Schranz", blurb: "Militant 150–160. Tunnel kick, screaming acid." },
  acid: { name: "Acid", blurb: "148-ish. Resonant runs, hypnotic loop." },
  tekno: { name: "Tekno", blurb: "Free-party drive. Offbeat clap, grit." },
  mental: { name: "Mental", blurb: "Faster, darker, more chaos on the 303." },
};

function clamp(x: number, a: number, b: number) {
  return Math.min(b, Math.max(a, x));
}

function mulberry32(seed: number) {
  let a = seed >>> 0;
  return () => {
    a += 0x6d2b79f5;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function hp1(x: number, prevX: number, prevY: number, a: number) {
  const y = a * (prevY + x - prevX);
  return y;
}

/** One-shot kick. Original synth, 909-inspired envelope — not a ROM. */
function synthKick(sr: number, style: KickId, grit: GritId, rng: () => number): Float32Array {
  const n = Math.floor(sr * (style === "warehouse" ? 0.48 : 0.34));
  const out = new Float32Array(n);
  const f0 = style === "warehouse" ? 46 : style === "sledge" ? 50 : style === "tribal" ? 56 : 52;
  const decay = style === "warehouse" ? 0.22 : style === "tribal" ? 0.11 : 0.16;
  const drive = grit === "full" ? 2.4 : grit === "medium" ? 1.6 : 1.05;
  const hpf = style === "tunnel" ? 78 : style === "sledge" ? 62 : style === "tribal" ? 70 : 42;
  const hpA = Math.exp((-2 * Math.PI * hpf) / sr);
  let phase = 0;
  let hpX = 0;
  let hpY = 0;
  for (let i = 0; i < n; i++) {
    const t = i / sr;
    const body = Math.exp(-t / decay);
    const clickEnv = Math.exp(-t / 0.004);
    const pitch = f0 * (1 + 8 * Math.exp(-t / 0.012));
    phase += (2 * Math.PI * pitch) / sr;
    const sine = Math.sin(phase);
    const click = (rng() * 2 - 1) * clickEnv * 0.35;
    let x = sine * body * 0.9 + click;
    x = Math.tanh(x * drive);
    const y = hp1(x, hpX, hpY, hpA);
    hpX = x;
    hpY = y;
    out[i] = y * 0.95;
  }
  return out;
}

function synthHat(sr: number, open: boolean, rng: () => number): Float32Array {
  const n = Math.floor(sr * (open ? 0.18 : 0.045));
  const out = new Float32Array(n);
  const decay = open ? 0.07 : 0.018;
  let hpX = 0;
  let hpY = 0;
  const a = Math.exp((-2 * Math.PI * 6200) / sr);
  for (let i = 0; i < n; i++) {
    const t = i / sr;
    const nse = rng() * 2 - 1;
    const y = hp1(nse, hpX, hpY, a);
    hpX = nse;
    hpY = y;
    out[i] = y * Math.exp(-t / decay) * (open ? 0.22 : 0.18);
  }
  return out;
}

function synthClap(sr: number, rng: () => number): Float32Array {
  const n = Math.floor(sr * 0.22);
  const out = new Float32Array(n);
  const bursts = [0, 0.012, 0.024, 0.04];
  for (const b of bursts) {
    const start = Math.floor(b * sr);
    for (let i = 0; i < Math.floor(0.03 * sr); i++) {
      const t = i / sr;
      const idx = start + i;
      if (idx >= n) break;
      out[idx] += (rng() * 2 - 1) * Math.exp(-t / 0.012) * 0.35;
    }
  }
  let hpX = 0;
  let hpY = 0;
  const a = Math.exp((-2 * Math.PI * 900) / sr);
  for (let i = 0; i < n; i++) {
    const y = hp1(out[i]!, hpX, hpY, a);
    hpX = out[i]!;
    hpY = y;
    out[i] = Math.tanh(y * 2.2) * 0.55;
  }
  return out;
}

type Svf = { l: number; b: number };

function svfLp(s: Svf, x: number, fHz: number, q: number, sr: number): number {
  const f = 2 * Math.sin(Math.PI * clamp(fHz / sr, 0.0001, 0.45));
  s.l += f * s.b;
  const h = x - s.l - q * s.b;
  s.b += f * h;
  return s.l;
}

function saw(phase: number) {
  return 2 * (phase - Math.floor(phase + 0.5));
}

function square(phase: number) {
  return phase < 0.5 ? 1 : -1;
}

const PHRYGIAN_HZ = [
  73.42, // D2
  77.78, // Eb2
  87.31, // F2
  98.0, // G2
  110.0, // A2
  116.54, // Bb2
  130.81, // C3
  146.83, // D3
];

function acidPattern(seed: number): { note: number; accent: boolean; slide: boolean }[] {
  const rng = mulberry32(seed + 99);
  const steps: { note: number; accent: boolean; slide: boolean }[] = [];
  let prev = 0;
  for (let i = 0; i < 16; i++) {
    const rest = rng() < (i % 4 === 1 ? 0.45 : 0.18);
    if (rest) {
      steps.push({ note: 0, accent: false, slide: false });
      continue;
    }
    const idx = Math.floor(rng() * PHRYGIAN_HZ.length);
    const accent = rng() < 0.32 || i % 4 === 0;
    const slide = !rest && rng() < 0.18 && prev > 0;
    steps.push({ note: PHRYGIAN_HZ[idx]!, accent, slide });
    prev = PHRYGIAN_HZ[idx]!;
  }
  return steps;
}

function addAt(dst: Float32Array, src: Float32Array, at: number, gain: number) {
  const start = Math.max(0, at | 0);
  const n = src.length;
  for (let i = 0; i < n; i++) {
    const j = start + i;
    if (j >= dst.length) break;
    dst[j]! += src[i]! * gain;
  }
}

export function describeKickback(opts: KickbackOpts): string {
  const bars = opts.bars;
  const dur = (bars * 4 * 60) / opts.bpm;
  const grid = opts.kickGrid
    ? ` · kick ${opts.kickGrid.map((h) => (h ? "x" : ".")).join("")}`
    : "";
  return `${GENRE_META[opts.genre].name} · ${opts.bpm} BPM · ${opts.kick} kick · ${opts.acid} acid · ${opts.grit} grit · ${bars} bars (~${dur.toFixed(0)}s)${grid}. Original synth, no packs.`;
}

export async function renderKickback(opts: KickbackOpts): Promise<AudioBuffer> {
  const sr = 44100;
  const rng = mulberry32(opts.seed);
  const beat = 60 / opts.bpm;
  const step = beat / 4;
  const total = opts.bars * 16;
  const len = Math.ceil((total * step + 0.6) * sr);
  const kickBus = new Float32Array(len);
  const acidBus = new Float32Array(len);
  const percBus = new Float32Array(len);

  const kickHit = synthKick(sr, opts.kick, opts.grit, rng);
  const hatClosed = synthHat(sr, false, rng);
  const hatOpen = synthHat(sr, true, rng);
  const clap = synthClap(sr, rng);
  const pattern = acidPattern(opts.seed);

  const gritDrive = opts.grit === "full" ? 2.8 : opts.grit === "medium" ? 1.9 : 1.2;
  const resQ = opts.acid === "scream" ? 0.08 : opts.acid === "screech" ? 0.05 : 0.14;
  const baseCut = opts.acid === "pluck" ? 420 : opts.acid === "wobble" ? 280 : 240;
  const envAmt = opts.acid === "scream" || opts.acid === "screech" ? 3800 : 2200;
  const osc = opts.acid === "screech" || opts.acid === "scream" ? square : saw;

  const svf: Svf = { l: 0, b: 0 };
  let phase = 0;
  let curHz = PHRYGIAN_HZ[0]!;
  let targetHz = curHz;
  let env = 0;
  const duck = new Float32Array(len);

  for (let s = 0; s < total; s++) {
    const bar = Math.floor(s / 16);
    const st = s % 16;
    const t0 = s * step;
    // Groove quantize: nudge the hit time by the per-step offset (in seconds).
    const grooved = stepGrooveOffsetSec(
      opts.groove?.kind ?? "none",
      opts.groove?.amount ?? 0,
      st,
      opts.bpm,
    );
    const at = Math.floor((t0 + grooved) * sr);

    const intro = bar < (opts.bars === 32 ? 4 : 2);
    const build =
      opts.bars === 32 ? bar >= 12 && bar < 14 : bar >= 6 && bar < 7;
    const gap = opts.bars === 32 ? bar === 14 && st >= 12 : bar === 7 && st >= 12;
    const drop = opts.bars === 32 ? bar >= 15 : bar >= 8;

    if (gap) continue;

    const kickOn = opts.kickGrid
      ? opts.kickGrid[st] === true
      : opts.genre === "slowrave"
        ? st === 0 || st === 8
        : st % 4 === 0;
    if (kickOn && !build) {
      addAt(kickBus, kickHit, at, intro ? 0.75 : 1);
      const duckN = Math.floor(0.09 * sr);
      for (let i = 0; i < duckN && at + i < len; i++) {
        const x = i / duckN;
        duck[at + i] = Math.max(duck[at + i]!, Math.exp(-x * 5));
      }
    }

    if (opts.perc === "offbeat" || opts.perc === "shuffle" || opts.perc === "tribal") {
      if (st === 4 || st === 12) addAt(percBus, clap, at, build ? 0.4 : 0.7);
    }
    if (opts.perc !== "minimal" || st % 4 === 2) {
      const swing = (st % 2 === 1 ? 0.018 : 0) * (opts.perc === "shuffle" ? 1 : 0.3);
      const hatAt = Math.floor((t0 + grooved + swing) * sr);
      const open = opts.perc === "tribal" && st % 8 === 6;
      addAt(percBus, open ? hatOpen : hatClosed, hatAt, intro ? 0.5 : 0.85);
    }

    const p = pattern[st]!;
    if (p.note > 0) {
      targetHz = p.note;
      if (!p.slide) curHz = targetHz;
      env = p.accent ? 1 : 0.62;
    }
  }

  // Acid as a continuous voice with step-locked env (second pass for filter)
  let stepIdx = -1;
  for (let i = 0; i < len; i++) {
    const t = i / sr;
    // Map through the groove offset so the acid envelope follows the swung grid.
    const st0 = Math.max(0, Math.floor(t / step));
    const off = stepGrooveOffsetSec(opts.groove?.kind ?? "none", opts.groove?.amount ?? 0, st0 % 16, opts.bpm);
    const s = Math.min(total - 1, Math.floor((t - off) / step));
    if (s !== stepIdx) {
      stepIdx = s;
      const bar = Math.floor(s / 16);
      const st = s % 16;
      const gap = opts.bars === 32 ? bar === 14 && st >= 12 : bar === 7 && st >= 12;
      const p = pattern[st]!;
      if (!gap && p.note > 0) {
        targetHz = p.note;
        if (!p.slide) curHz = targetHz;
        env = p.accent ? 1 : 0.55;
      } else if (!p.slide) {
        env *= 0.35;
      }
    }
    curHz += (targetHz - curHz) * 0.0028;
    env *= 0.9992;
    phase += curHz / sr;
    phase -= Math.floor(phase);
    const oscOut = osc(phase) * env * 0.45;
    const cut = baseCut + env * envAmt;
    let y = svfLp(svf, oscOut, cut, resQ, sr);
    y = Math.tanh(y * gritDrive);
    const d = duck[i] ?? 0;
    acidBus[i] = y * (1 - d * 0.82);
  }

  const mixL = new Float32Array(len);
  const mixR = new Float32Array(len);
  let peak = 1e-9;
  for (let i = 0; i < len; i++) {
    const k = kickBus[i]!;
    const a = acidBus[i]!;
    const p = percBus[i]!;
    const mono = k * 1.05 + a * 0.72 + p * 0.55;
    const l = mono + p * 0.08;
    const r = mono - p * 0.08;
    mixL[i] = l;
    mixR[i] = r;
    peak = Math.max(peak, Math.abs(l), Math.abs(r));
  }
  const scale = 0.86 / peak;
  for (let i = 0; i < len; i++) {
    mixL[i] = Math.tanh(mixL[i]! * scale * 1.05);
    mixR[i] = Math.tanh(mixR[i]! * scale * 1.05);
  }

  const ctx = new OfflineAudioContext(2, len, sr);
  const buffer = ctx.createBuffer(2, len, sr);
  buffer.copyToChannel(mixL, 0);
  buffer.copyToChannel(mixR, 1);
  return buffer;
}

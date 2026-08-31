/**
 * Track DNA extraction — reads an uploaded track's rhythm + vibe so the
 * Kickback synth can remake it as a NEW original loop.
 *
 * Output:
 * - bpm        : tempo estimate (reuses analyze.estimateBpm)
 * - kickGrid   : 16-step kick hit pattern (low-band onset per step)
 * - density    : kick density 0..1 (how busy the low end is)
 * - genre/kick/acid/perc/grit : best-guess synth map for the vibe
 */

import { estimateBpm } from "./analyze.ts";
import {
  GENRE_DEFAULTS,
  type AcidId,
  type GenreId,
  type GritId,
  type KickId,
  type KickbackOpts,
  type PercId,
} from "./kickback.ts";
export type TrackDna = {
  bpm: number;
  kickGrid: boolean[];
  density: number;
  genre: GenreId;
  kick: KickId;
  acid: AcidId;
  perc: PercId;
  grit: GritId;
  seed: number;
  bars: 16 | 32;
  summary: string;
};

function mixToMonoCh(buffer: AudioBuffer): Float32Array {
  const ch = buffer.getChannelData(0);
  if (buffer.numberOfChannels === 1) return ch;
  const r = buffer.getChannelData(1);
  const out = new Float32Array(ch.length);
  for (let i = 0; i < ch.length; i++) out[i] = (ch[i]! + r[i]!) * 0.5;
  return out;
}

/**
 * Low-band kick-body envelope: |x| peak per hop, then one-pole LP ~30 Hz so
 * the envelope follows the kick body (decaying tail) instead of ringing on
 * every edge. Downsampled to ~200 Hz for cheap per-step analysis.
 */
function lowBandEnvelope(mono: Float32Array, sampleRate: number): Float32Array {
  const hop = Math.max(1, Math.floor(sampleRate / 200)); // ~200 Hz envelope
  const len = Math.max(1, Math.floor(mono.length / hop));
  const env = new Float32Array(len);
  const lp = Math.exp((-2 * Math.PI * 30) / (sampleRate / hop));
  let lpY = 0;
  for (let i = 0; i < len; i++) {
    let peak = 0;
    const a = i * hop;
    const b = Math.min(mono.length, a + hop);
    for (let j = a; j < b; j++) {
      const x = Math.abs(mono[j]!);
      if (x > peak) peak = x;
    }
    lpY = peak + lp * lpY;
    env[i] = lpY;
  }
  return env;
}

function detectKickGrid(mono: Float32Array, sampleRate: number, bpm: number): boolean[] {
  const beat = 60 / bpm;
  const step = beat / 4;
  const bars = Math.min(8, Math.max(1, Math.floor((mono.length / sampleRate) / (step * 16))));
  const env = lowBandEnvelope(mono, sampleRate);
  const hop = Math.max(1, Math.floor(sampleRate / 200));
  const grid = new Array<number>(16).fill(0);

  // Accumulate low-band energy at each 16th step across bars.
  for (let bar = 0; bar < bars; bar++) {
    for (let s = 0; s < 16; s++) {
      const t = bar * 16 * step + s * step;
      const center = Math.floor((t + step * 0.15) / (1 / 200));
      const win = Math.max(1, Math.floor((step * 0.7) / (1 / 200)));
      let acc = 0;
      for (let w = 0; w < win; w++) {
        const idx = center + w;
        if (idx >= 0 && idx < env.length) acc += env[idx]!;
      }
      grid[s] = Math.max(grid[s]!, acc / Math.max(1, win));
    }
  }

  const mean = grid.reduce((a, b) => a + b, 0) / 16;
  const max = Math.max(...grid);
  const thresh = mean * 0.55 + max * 0.18;
  return grid.map((v) => v > thresh && v > 1e-5);
}

function vibeFor(bpm: number, density: number): Pick<TrackDna, "genre" | "kick" | "acid" | "perc" | "grit"> {
  if (bpm >= 70 && bpm <= 100 && density < 0.55) {
    return { genre: "slowrave", kick: "warehouse", acid: "scream", perc: "minimal", grit: "full" };
  }
  if (bpm >= 145 && bpm <= 168 && density > 0.6) {
    return { genre: "schranz", kick: "sledge", acid: "scream", perc: "offbeat", grit: "full" };
  }
  if (bpm >= 130 && bpm <= 160) {
    return { genre: "acid", kick: "tunnel", acid: "wobble", perc: "shuffle", grit: "medium" };
  }
  if (bpm >= 100 && bpm <= 145) {
    return { genre: "tekno", kick: "tribal", acid: "pluck", perc: "tribal", grit: "full" };
  }
  return { genre: "mental", kick: "sledge", acid: "screech", perc: "shuffle", grit: "full" };
}

export function gridContrast(mono: Float32Array, sampleRate: number, bpm: number): number {
  const grid = detectKickGrid(mono, sampleRate, bpm);
  const beat = 60 / bpm;
  const step = beat / 4;
  const env = lowBandEnvelope(mono, sampleRate);
  const hop = Math.max(1, Math.floor(sampleRate / 200));
  let hitE = 0;
  let offE = 0;
  let nHit = 0;
  let nOff = 0;
  for (let s = 0; s < 16; s++) {
    const t = s * step + step * 0.15;
    const idx = Math.max(0, Math.min(env.length - 1, Math.floor(t * 200)));
    if (grid[s]) {
      hitE += env[idx]!;
      nHit++;
    } else {
      offE += env[idx]!;
      nOff++;
    }
  }
  const h = hitE / Math.max(1, nHit);
  const o = offE / Math.max(1, nOff);
  return o > 1e-9 ? h / o : 10;
}

export function extractDna(buffer: AudioBuffer): TrackDna | null {
  if (buffer.duration < 2) return null;
  const mono = mixToMonoCh(buffer);
  let bpm = estimateBpm(mono, buffer.sampleRate);
  if (!bpm) return null;

  // estimateBpm can lock to 2-beat lag (half tempo) on sparse kicks. Try the
  // double and pick the one with a cleaner kick grid.
  const doubled = bpm * 2;
  if (doubled <= 190) {
    const c1 = gridContrast(mono, buffer.sampleRate, bpm);
    const c2 = gridContrast(mono, buffer.sampleRate, doubled);
    if (c2 > c1 * 1.2) bpm = doubled;
  }

  const kickGrid = detectKickGrid(mono, buffer.sampleRate, bpm);
  const density = kickGrid.filter(Boolean).length / 16;
  const vibe = vibeFor(bpm, density);
  const seed = (Math.random() * 1e9) | 0;
  const bars: 16 | 32 = vibe.genre === "slowrave" ? 32 : 16;

  const summary = `${bpm} BPM · ${Math.round(density * 100)}% kick density · ${
    kickGrid.map((h) => (h ? "x" : ".")).join("")
  } → ${vibe.genre}`;

  return { bpm, kickGrid, density, ...vibe, seed, bars, summary };
}

/** Kickback opts derived from extracted DNA — grid passed through so the
 * synth follows the track's rhythm instead of the genre default. */
export function dnaToOpts(dna: TrackDna): KickbackOpts {
  return {
    genre: dna.genre,
    bpm: dna.bpm,
    kick: dna.kick,
    acid: dna.acid,
    perc: dna.perc,
    grit: dna.grit,
    bars: dna.bars,
    seed: dna.seed,
    kickGrid: dna.kickGrid,
  };
}

export function describeDna(dna: TrackDna): string {
  const defaults = GENRE_DEFAULTS[dna.genre];
  const kickChanged = dna.kick !== defaults.kick ? ` (default ${defaults.kick})` : "";
  const acidChanged = dna.acid !== defaults.acid ? ` (default ${defaults.acid})` : "";
  return `${dna.bpm} BPM · ${dna.genre} · ${dna.kick} kick${kickChanged} · ${dna.acid} acid${acidChanged} · ${dna.bars} bars · ${dna.seed}`;
}

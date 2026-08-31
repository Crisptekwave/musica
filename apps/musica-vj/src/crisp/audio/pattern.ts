/**
 * E2S pattern (.syx) exporter — Korg electribe sampler pattern dump.
 *
 * Builds on a known-good template (public/patterns/kickback-template.syx, an
 * aitrackgen-demo dump) so reserved/unknown regions stay device-valid, and
 * patches the documented fields:
 *
 *   Pattern block (TABLE 1):
 *     0..3   'PTST'
 *     4..7   Size (0x2000 = 8192)
 *     8..11  reserved
 *     12..15 Pattern version
 *     16..33 Pattern name (null-terminated)
 *     34..35 Tempo (200..3000 = 20.0..300.0)
 *     36     Swing (-48..48)
 *     37     Length (0..3 = 1..4 bars)
 *     38     Beat (0=16, 1=32, 2=8tri, 3=16tri)
 *     39     Key (0..11)
 *
 *   Part N params (TABLE 6) at 2048 + (N-1)*816:
 *     48..59 Step1 Step Data (On/Off, Gate, Velocity, Trigger, Note Slot1..4)
 *     ...    12 bytes per step, 64 steps.
 *
 * This writes kick into Part 1 (sample kick slot) and acid into Part 2,
 * per the grid extracted from the uploaded track.
 */

export type PatternGrid = {
  name: string;
  bpm: number;
  bars: 16 | 32;
  kick: boolean[]; // 16 steps
  acid: boolean[]; // 16 steps
};

const PATTERN_BLOCK_OFFSET = 8; // after F0 42 30 00 01 24 4C 00

function writeName(block: Uint8Array, name: string) {
  const max = 18;
  for (let i = 0; i < max; i++) block[16 + i] = 0;
  const s = name.replace(/[^\x20-\x7e]/g, "_").slice(0, max - 1);
  for (let i = 0; i < s.length; i++) block[16 + i] = s.charCodeAt(i);
}

/** Write step data for one part. stepData[step] = { on, note } (16 steps, looped to 64). */
function writePartSteps(
  block: Uint8Array,
  partIndex: 0 | 1,
  steps: { on: boolean; note: number }[],
) {
  const partBase = 2048 + partIndex * 816;
  for (let s = 0; s < 64; s++) {
    const st = steps[s % steps.length] ?? { on: false, note: 0 };
    const base = partBase + 48 + s * 12;
    block[base] = st.on ? 1 : 0; // On/Off
    block[base + 1] = st.on ? 48 : 0; // Gate (96 max)
    block[base + 2] = st.on ? 112 : 0; // Velocity
    block[base + 3] = st.on ? 1 : 0; // Trigger
    block[base + 4] = st.on ? st.note : 0; // Note Slot 1
  }
}

export async function buildPatternSyx(grid: PatternGrid): Promise<Blob> {
  const res = await fetch("/patterns/kickback-template.syx");
  if (!res.ok) throw new Error("Pattern template missing (public/patterns/kickback-template.syx)");
  const raw = new Uint8Array(await res.arrayBuffer());
  if (raw.length < PATTERN_BLOCK_OFFSET + 15360) {
    throw new Error("Pattern template too short");
  }
  const block = raw.subarray(PATTERN_BLOCK_OFFSET);

  writeName(block, grid.name);
  const tempo = Math.max(200, Math.min(3000, Math.round(grid.bpm * 10)));
  block[34] = tempo & 0x7f;
  block[35] = (tempo >> 7) & 0x7f;
  block[36] = 0; // swing
  block[37] = grid.bars === 32 ? 1 : 0; // 0=1bar, 1=2bars
  block[38] = 0; // 16 beat
  block[39] = 0; // C

  // Kick into part 1, acid into part 2 (sample slots on the E2S).
  const kickSteps = grid.kick.map((on) => ({ on, note: 36 }));
  const acidSteps = grid.acid.map((on) => ({ on, note: 37 }));
  writePartSteps(block, 0, kickSteps);
  writePartSteps(block, 1, acidSteps);

  return new Blob([raw], { type: "application/octet-stream" });
}

export function patternFilename(name: string): string {
  const s = name.replace(/[^\w-]+/g, "_").slice(0, 8) || "kickback";
  return `P_${s}.syx`;
}

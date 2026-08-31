/**
 * Groove quantize — lay a swing/shuffle/half-time/dotted template over the
 * 16-step grid so the rendered audio actually grooves. The kick grid is not
 * just reported; the hits get micro-timed offsets (in beats) that the synth
 * applies when it places each hit.
 *
 * This is a PURE module (returns per-step time offsets) so it's testable in
 * Node. The synth consumes `stepGrooveOffsetSec` to nudge each hit's start.
 *
 * Grid convention: 16 steps = 1 bar of 4 beats, each beat = 4 sixteenths.
 *   beat 1: steps 0,1,2,3
 *   beat 2: steps 4,5,6,7   ...
 * An eighth-note pair within a beat is (step 0, step 2); the off-beat eighth
 * is the "and" (steps 2, 6, 10, 14 → stepIndex % 4 === 2). Swing/shuffle delay
 * that off-beat; half-time and dotted reshape the pulse instead.
 */

export type GrooveKind = "none" | "swing" | "shuffle" | "half" | "dotted";
export type GrooveSpec = { kind: GrooveKind; amount: number };

export const GROOVE_KINDS: GrooveKind[] = [
  "none",
  "swing",
  "shuffle",
  "half",
  "dotted",
];

export const GROOVE_META: Record<GrooveKind, { name: string; blurb: string }> = {
  none: { name: "None", blurb: "Straight grid." },
  swing: { name: "Swing", blurb: "Delay the off-beat eighth — laid-back, triplet-leaning." },
  shuffle: { name: "Shuffle", blurb: "Stronger off-beat push — the hip-hop / electro neck-snap." },
  half: { name: "Half-time", blurb: "Hits land on the half-beat — a slower, heavier pulse." },
  dotted: { name: "Dotted", blurb: "Off-beats pull earlier — a clipped, skipping drive." },
};

const clamp = (x: number, a: number, b: number) => Math.min(b, Math.max(a, x));

/**
 * Time offset (in beats) to add to a 16th-step's start, for a given groove.
 * Positive = later (swing/shuffle), negative = earlier (dotted), and "half"
 * shifts the whole downbeat layout to the half-beat.
 *
 * `amount` is 0..1 and scales the offset. Moderate defaults keep it musical
 * (a full swing is ~1/3 of a sixteenth ≈ ternary feel, not a hard shuffle).
 */
export function stepGrooveOffset(
  kind: GrooveKind,
  amount: number,
  stepIndex: number,
): number {
  const a = clamp(amount, 0, 1);
  if (kind === "none" || a === 0) return 0;
  switch (kind) {
    case "swing":
      // Off-beat eighth (the "and") delayed. Up to ~0.35 of a sixteenth.
      return stepIndex % 4 === 2 ? a * 0.35 : 0;
    case "shuffle":
      // Stronger push on the "and", plus a touch on the last 16th of each beat.
      if (stepIndex % 4 === 2) return a * 0.6;
      if (stepIndex % 4 === 3) return a * 0.15;
      return 0;
    case "half":
      // Half-time feel: pull the off-beats to the half-beat (earlier).
      return stepIndex % 4 === 2 ? -a * 0.3 : 0;
    case "dotted":
      // Dotted rhythm: off-beats land earlier (negative), 16th 4th pusher later.
      if (stepIndex % 4 === 2) return -a * 0.25;
      if (stepIndex % 4 === 3) return a * 0.2;
      return 0;
    default:
      return 0;
  }
}

/** Seconds to add to a step's start time for a groove, given the BPM. */
export function stepGrooveOffsetSec(
  kind: GrooveKind,
  amount: number,
  stepIndex: number,
  bpm: number,
): number {
  const beat = 60 / bpm;
  return stepGrooveOffset(kind, amount, stepIndex) * beat;
}

/** Apply a groove to a boolean kick grid, returning shifted hit times (in beats
 *  from the bar start) so the caller can read where each kick actually lands. */
export function groovedHitTimes(
  grid: boolean[],
  kind: GrooveKind,
  amount: number,
  bpm: number,
): { step: number; sec: number }[] {
  const beat = 60 / bpm;
  const out: { step: number; sec: number }[] = [];
  for (let s = 0; s < grid.length; s++) {
    if (grid[s]) {
      const baseSec = (s / 4) * beat; // 4 steps per beat
      const off = stepGrooveOffsetSec(kind, amount, s, bpm);
      out.push({ step: s, sec: baseSec + off });
    }
  }
  return out;
}

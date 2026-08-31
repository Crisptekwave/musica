import { fftRadix2 } from "./fft.ts";

export type Probe = {
  duration: number;
  sampleRate: number;
  channels: number;
  peak: number;
  peakDb: number;
  rms: number;
  rmsDb: number;
  crestDb: number;
  lufs: number;
  dc: number;
  stereoCorr: number | null;
  silentHead: number;
  silentTail: number;
  bpm: number | null;
  centroidHz: number;
  bandDb: { bass: number; punch: number; lead: number; air: number };
};

export type Spectrogram = {
  columns: number;
  bins: number;
  data: Uint8Array;
};

function db(x: number): number {
  if (x <= 1e-12) return -120;
  return 20 * Math.log10(x);
}

/** Ungated K-weighted loudness approximation (ITU-R BS.1770-ish). */
export function estimateLufs(buffer: AudioBuffer, start = 0, end?: number): number {
  const sr = buffer.sampleRate;
  const from = Math.floor(start * sr);
  const to = Math.min(buffer.length, Math.floor((end ?? buffer.duration) * sr));
  const nch = buffer.numberOfChannels;
  const chans: Float32Array[] = [];
  for (let c = 0; c < nch; c++) chans.push(buffer.getChannelData(c));
  const step = Math.max(1, Math.floor(Math.max(1, to - from) / 400_000));
  const aHp = Math.exp((-2 * Math.PI * 38) / sr);
  let x1 = 0;
  let hp = 0;
  let acc = 0;
  let n = 0;
  for (let i = from; i < to; i += step) {
    let x = 0;
    for (let c = 0; c < nch; c++) x += chans[c]![i]!;
    x /= nch;
    hp = x - x1 + aHp * hp;
    x1 = x;
    const k = hp * 1.4;
    acc += k * k;
    n++;
  }
  return -0.691 + 10 * Math.log10(acc / Math.max(1, n) + 1e-12);
}

export function analyzeBuffer(buffer: AudioBuffer): Probe {
  const channels = buffer.numberOfChannels;
  const sampleRate = buffer.sampleRate;
  const length = buffer.length;
  const duration = buffer.duration;
  const chans: Float32Array[] = [];
  for (let c = 0; c < channels; c++) chans.push(buffer.getChannelData(c));

  const step = Math.max(1, Math.floor(length / 2_000_000));
  let peak = 0;
  let sumSq = 0;
  let sum = 0;
  let n = 0;
  let prod = 0;
  let sumL = 0;
  let sumR = 0;
  let sumL2 = 0;
  let sumR2 = 0;

  for (let i = 0; i < length; i += step) {
    let mixed = 0;
    for (let c = 0; c < channels; c++) mixed += chans[c]![i]!;
    mixed /= channels;
    const a = Math.abs(mixed);
    if (a > peak) peak = a;
    sumSq += mixed * mixed;
    sum += mixed;
    n++;
    if (channels >= 2) {
      const L = chans[0]![i]!;
      const R = chans[1]![i]!;
      prod += L * R;
      sumL += L;
      sumR += R;
      sumL2 += L * L;
      sumR2 += R * R;
    }
  }

  const rms = Math.sqrt(sumSq / Math.max(1, n));
  const dc = sum / Math.max(1, n);
  let stereoCorr: number | null = null;
  if (channels >= 2) {
    const cov = prod / n - (sumL / n) * (sumR / n);
    const vL = sumL2 / n - (sumL / n) ** 2;
    const vR = sumR2 / n - (sumR / n) ** 2;
    const denom = Math.sqrt(Math.max(0, vL) * Math.max(0, vR));
    stereoCorr = denom > 1e-12 ? cov / denom : 1;
  }

  const thresh = Math.max(peak * 0.004, 0.0004);
  let head = 0;
  outerHead: for (let i = 0; i < length; i += Math.max(1, Math.floor(sampleRate / 200))) {
    for (let c = 0; c < channels; c++) {
      if (Math.abs(chans[c]![i]!) > thresh) {
        head = i / sampleRate;
        break outerHead;
      }
    }
    head = i / sampleRate;
  }
  let tail = 0;
  outerTail: for (let i = length - 1; i >= 0; i -= Math.max(1, Math.floor(sampleRate / 200))) {
    for (let c = 0; c < channels; c++) {
      if (Math.abs(chans[c]![i]!) > thresh) {
        tail = (length - 1 - i) / sampleRate;
        break outerTail;
      }
    }
    tail = (length - 1 - i) / sampleRate;
  }

  const spec = spectralStats(chans[0]!, sampleRate);

  return {
    duration,
    sampleRate,
    channels,
    peak,
    peakDb: db(peak),
    rms,
    rmsDb: db(rms),
    crestDb: db(peak) - db(rms),
    lufs: estimateLufs(buffer),
    dc,
    stereoCorr,
    silentHead: head,
    silentTail: tail,
    bpm: estimateBpm(chans[0]!, sampleRate),
    centroidHz: spec.centroidHz,
    bandDb: spec.bandDb,
  };
}

function spectralStats(channel: Float32Array, sampleRate: number) {
  const n = 1024;
  const empty = {
    centroidHz: 0,
    bandDb: { bass: -120, punch: -120, lead: -120, air: -120 },
  };
  if (channel.length < n) return empty;
  const hop = Math.max(n, Math.floor(channel.length / 48));
  const re = new Float32Array(n);
  const im = new Float32Array(n);
  const acc = new Float32Array(n / 2);
  let frames = 0;
  for (let pos = 0; pos + n < channel.length; pos += hop) {
    for (let i = 0; i < n; i++) {
      const w = 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (n - 1));
      re[i] = channel[pos + i]! * w;
      im[i] = 0;
    }
    fftRadix2(re, im);
    for (let k = 0; k < n / 2; k++) {
      acc[k] += re[k]! * re[k]! + im[k]! * im[k]!;
    }
    frames++;
  }
  const inv = 1 / Math.max(1, frames);
  let num = 0;
  let den = 0;
  const band = { bass: 0, punch: 0, lead: 0, air: 0 };
  const bandN = { bass: 0, punch: 0, lead: 0, air: 0 };
  for (let k = 1; k < n / 2; k++) {
    const mag = acc[k]! * inv;
    const hz = (k * sampleRate) / n;
    num += hz * mag;
    den += mag;
    const key = hz < 180 ? "bass" : hz < 1000 ? "punch" : hz < 5500 ? "lead" : "air";
    band[key] += mag;
    bandN[key] += 1;
  }
  const mean = (v: number, c: number) => db(Math.sqrt(v / Math.max(1, c)));
  return {
    centroidHz: den > 1e-12 ? num / den : 0,
    bandDb: {
      bass: mean(band.bass, bandN.bass),
      punch: mean(band.punch, bandN.punch),
      lead: mean(band.lead, bandN.lead),
      air: mean(band.air, bandN.air),
    },
  };
}

export function computeSpectrogram(
  buffer: AudioBuffer,
  columns = 512,
  bins = 48,
): Spectrogram {
  const n = 1024;
  const ch0 = buffer.getChannelData(0);
  const ch1 = buffer.numberOfChannels > 1 ? buffer.getChannelData(1) : null;
  const data = new Uint8Array(columns * bins);
  if (ch0.length < n) return { columns, bins, data };
  const hop = Math.max(1, Math.floor((ch0.length - n) / Math.max(1, columns)));
  const re = new Float32Array(n);
  const im = new Float32Array(n);
  const nyquist = buffer.sampleRate / 2;
  const fMin = 40;
  for (let c = 0; c < columns; c++) {
    const pos = Math.min(ch0.length - n, c * hop);
    for (let i = 0; i < n; i++) {
      const w = 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (n - 1));
      const s = ch1 ? (ch0[pos + i]! + ch1[pos + i]!) * 0.5 : ch0[pos + i]!;
      re[i] = s * w;
      im[i] = 0;
    }
    fftRadix2(re, im);
    for (let b = 0; b < bins; b++) {
      const t0 = b / bins;
      const t1 = (b + 1) / bins;
      const hz0 = fMin * Math.pow(nyquist / fMin, t0);
      const hz1 = fMin * Math.pow(nyquist / fMin, t1);
      const k0 = Math.max(1, Math.floor((hz0 / nyquist) * (n / 2)));
      const k1 = Math.max(k0 + 1, Math.floor((hz1 / nyquist) * (n / 2)));
      let acc = 0;
      for (let k = k0; k < k1 && k < n / 2; k++) {
        acc += re[k]! * re[k]! + im[k]! * im[k]!;
      }
      const mag = Math.sqrt(acc / Math.max(1, k1 - k0));
      const v = Math.max(0, Math.min(1, (db(mag) + 90) / 90));
      data[c * bins + b] = Math.round(v * 255);
    }
  }
  return { columns, bins, data };
}

export function estimateBpm(channel: Float32Array, sampleRate: number): number | null {
  const hop = Math.max(1, Math.round(sampleRate / 200));
  const window = Math.min(channel.length, sampleRate * 45);
  const envLen = Math.floor(window / hop);
  if (envLen < 400) return null;
  const env = new Float32Array(envLen);
  for (let i = 0; i < envLen; i++) {
    let m = 0;
    const start = i * hop;
    const end = Math.min(channel.length, start + hop);
    for (let j = start; j < end; j++) {
      const a = Math.abs(channel[j]!);
      if (a > m) m = a;
    }
    env[i] = m;
  }
  let mean = 0;
  for (let i = 0; i < envLen; i++) mean += env[i]!;
  mean /= envLen;
  for (let i = 0; i < envLen; i++) env[i] = Math.max(0, env[i]! - mean);

  const minLag = Math.round((60 / 190) * 200);
  const maxLag = Math.round((60 / 70) * 200);
  let bestLag = 0;
  let best = 0;
  for (let lag = minLag; lag <= maxLag && lag < envLen / 3; lag++) {
    let acc = 0;
    const n = envLen - lag;
    for (let i = 0; i < n; i++) acc += env[i]! * env[i + lag]!;
    acc /= n;
    if (acc > best) {
      best = acc;
      bestLag = lag;
    }
  }
  if (bestLag <= 0 || best < 1e-8) return null;
  let bpm = (60 * 200) / bestLag;
  if (bpm > 190) bpm /= 2;
  if (bpm < 70) bpm *= 2;
  if (bpm < 70 || bpm > 190) return null;
  return Math.round(bpm * 10) / 10;
}

export function waveformPeaks(
  buffer: AudioBuffer,
  buckets: number,
  start = 0,
  end?: number,
): { min: Float32Array; max: Float32Array } {
  const from = Math.floor(start * buffer.sampleRate);
  const to = Math.min(buffer.length, Math.floor((end ?? buffer.duration) * buffer.sampleRate));
  const span = Math.max(1, to - from);
  const min = new Float32Array(buckets);
  const max = new Float32Array(buckets);
  const ch0 = buffer.getChannelData(0);
  const ch1 = buffer.numberOfChannels > 1 ? buffer.getChannelData(1) : null;
  for (let b = 0; b < buckets; b++) {
    const a = from + Math.floor((b / buckets) * span);
    const z = from + Math.floor(((b + 1) / buckets) * span);
    let lo = 1;
    let hi = -1;
    for (let i = a; i < z; i += Math.max(1, Math.floor((z - a) / 24))) {
      const s = ch1 ? (ch0[i]! + ch1[i]!) * 0.5 : ch0[i]!;
      if (s < lo) lo = s;
      if (s > hi) hi = s;
    }
    min[b] = lo;
    max[b] = hi;
  }
  return { min, max };
}

export function matchReference(
  source: Probe,
  ref: Probe,
): { makeupDb: number; presenceGain: number; airGain: number; muddyGain: number } {
  const makeupDb = Math.max(-12, Math.min(18, ref.lufs - source.lufs));
  const tilt = (ref.bandDb.air - ref.bandDb.bass) - (source.bandDb.air - source.bandDb.bass);
  const mid = ref.bandDb.lead - source.bandDb.lead;
  return {
    makeupDb,
    presenceGain: Math.max(-2, Math.min(4, mid * 0.15)),
    airGain: Math.max(-2, Math.min(3, tilt * 0.08)),
    muddyGain: Math.max(-4, Math.min(2, (ref.bandDb.punch - source.bandDb.punch) * 0.1)),
  };
}

/**
 * Find the nearest zero-crossing (or lowest-amplitude sample) around a time.
 * Prefers true sign-change crossings; falls back to absolute minimum.
 * Used by the Sample Lab region snap so Electribe exports start/end cleanly.
 *
 * @param buffer - decoded AudioBuffer
 * @param timeSec - target time in seconds
 * @param windowMs - search radius in milliseconds (default 8 ms ≈ safe for 32–48 kHz)
 * @returns snapped time in seconds
 */
export function findNearestZeroCrossing(
  buffer: AudioBuffer,
  timeSec: number,
  windowMs = 8,
): number {
  const sr = buffer.sampleRate;
  const center = Math.round(timeSec * sr);
  const win = Math.max(1, Math.round((windowMs / 1000) * sr));
  const ch0 = buffer.getChannelData(0);
  const ch1 = buffer.numberOfChannels > 1 ? buffer.getChannelData(1) : null;
  const len = ch0.length;

  if (len < 3) return Math.max(0, Math.min(buffer.duration, timeSec));

  let bestIdx = Math.max(0, Math.min(len - 1, center));
  let bestAbs = Infinity;
  let foundCrossing = false;

  const from = Math.max(1, center - win);
  const to = Math.min(len - 1, center + win);

  for (let i = from; i <= to; i++) {
    const s = ch1 ? (ch0[i]! + ch1[i]!) * 0.5 : ch0[i]!;
    const prev = ch1 ? (ch0[i - 1]! + ch1[i - 1]!) * 0.5 : ch0[i - 1]!;
    const a = Math.abs(s);
    const crossed = (prev >= 0 && s < 0) || (prev < 0 && s >= 0);

    if (crossed) {
      if (!foundCrossing || a < bestAbs) {
        bestAbs = a;
        bestIdx = i;
        foundCrossing = true;
      }
    } else if (!foundCrossing && a < bestAbs) {
      bestAbs = a;
      bestIdx = i;
    }
  }

  return bestIdx / sr;
}

/**
 * Detect whether the leading attack is phase-inverted (negative first peak).
 * Looks at the first significant peak after silence within attackWindowMs.
 * Returns true when the attack is predominantly negative.
 */
export function detectAttackPolarity(
  channel: Float32Array,
  sampleRate: number,
  attackWindowMs = 40,
): { inverted: boolean; peak: number; peakIndex: number } {
  const len = channel.length;
  if (len < 8) return { inverted: false, peak: 0, peakIndex: 0 };

  // Skip leading silence (same spirit as silentHead)
  let peakAbs = 0;
  for (let i = 0; i < len; i += Math.max(1, Math.floor(sampleRate / 500))) {
    const a = Math.abs(channel[i]!);
    if (a > peakAbs) peakAbs = a;
  }
  const thresh = Math.max(peakAbs * 0.08, 0.002);
  let start = 0;
  for (let i = 0; i < len; i++) {
    if (Math.abs(channel[i]!) > thresh) {
      start = i;
      break;
    }
  }

  const win = Math.max(8, Math.round((attackWindowMs / 1000) * sampleRate));
  const end = Math.min(len, start + win);
  let bestPeak = 0;
  let bestIdx = start;
  for (let i = start; i < end; i++) {
    const s = channel[i]!;
    if (Math.abs(s) > Math.abs(bestPeak)) {
      bestPeak = s;
      bestIdx = i;
    }
  }

  return {
    inverted: bestPeak < 0,
    peak: bestPeak,
    peakIndex: bestIdx,
  };
}

/**
 * Force positive attack polarity by inverting the entire channel in-place
 * when the leading peak is negative. Safe for Electribe one-shots / kicks.
 * Returns true if inversion was applied.
 */
export function forcePositiveAttack(
  channel: Float32Array,
  sampleRate: number,
  attackWindowMs = 40,
): boolean {
  const { inverted } = detectAttackPolarity(channel, sampleRate, attackWindowMs);
  if (!inverted) return false;
  for (let i = 0; i < channel.length; i++) {
    channel[i] = -(channel[i]!);
  }
  return true;
}

/**
 * Apply forcePositiveAttack to every channel of a buffer (in-place).
 * Useful after renderTake / mono fold for Electribe exports.
 */
export function ensurePositiveAttack(buffer: AudioBuffer, attackWindowMs = 40): boolean {
  let any = false;
  for (let c = 0; c < buffer.numberOfChannels; c++) {
    if (forcePositiveAttack(buffer.getChannelData(c), buffer.sampleRate, attackWindowMs)) {
      any = true;
    }
  }
  return any;
}

/**
 * Strip DC offset in-place. Electribe (and most hardware samplers) hate a
 * standing offset — it burns headroom and clicks on loop points.
 * Returns the mean that was removed (0 if noop).
 */
export function stripDcOffset(channel: Float32Array): number {
  const len = channel.length;
  if (len < 2) return 0;
  let sum = 0;
  for (let i = 0; i < len; i++) sum += channel[i]!;
  const mean = sum / len;
  if (Math.abs(mean) < 1e-7) return 0;
  for (let i = 0; i < len; i++) channel[i] = channel[i]! - mean;
  return mean;
}

/** stripDcOffset on every channel; returns max |mean| removed. */
export function stripDcOffsetBuffer(buffer: AudioBuffer): number {
  let maxAbs = 0;
  for (let c = 0; c < buffer.numberOfChannels; c++) {
    const m = stripDcOffset(buffer.getChannelData(c));
    maxAbs = Math.max(maxAbs, Math.abs(m));
  }
  return maxAbs;
}

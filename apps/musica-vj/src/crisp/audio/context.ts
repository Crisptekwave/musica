/** Shared AudioContext with guaranteed resume after user gesture (iOS-safe). */

let sharedCtx: AudioContext | null = null;
let unlockBound = false;

export function getSharedAudioContext(): AudioContext {
  if (!sharedCtx) {
    const AC =
      window.AudioContext ||
      (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    sharedCtx = new AC();
  }
  return sharedCtx;
}

export async function ensureAudioContext(): Promise<AudioContext> {
  const ctx = getSharedAudioContext();
  if (ctx.state === "suspended") {
    try {
      await ctx.resume();
    } catch {
      /* user gesture required — caller should retry on click */
    }
  }
  return ctx;
}

/**
 * Bind once-per-page gesture listeners so the first tap/click/keydown
 * resumes a suspended AudioContext (required on iOS Safari).
 * Safe to call multiple times; idempotent.
 */
export function bindAudioUnlock(): void {
  if (typeof window === "undefined" || unlockBound) return;
  unlockBound = true;

  const unlock = () => {
    void ensureAudioContext().then((ctx) => {
      // iOS sometimes needs a silent buffer tick after resume
      if (ctx.state === "running") {
        try {
          const buf = ctx.createBuffer(1, 1, ctx.sampleRate);
          const src = ctx.createBufferSource();
          src.buffer = buf;
          src.connect(ctx.destination);
          src.start(0);
        } catch {
          /* ignore */
        }
      }
    });
  };

  const opts: AddEventListenerOptions = { capture: true, passive: true };
  window.addEventListener("pointerdown", unlock, opts);
  window.addEventListener("touchstart", unlock, opts);
  window.addEventListener("keydown", unlock, opts);
}

export function isMobileUA(): boolean {
  if (typeof navigator === "undefined") return false;
  return /iPhone|iPad|iPod|Android/i.test(navigator.userAgent) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
}

/** Soft warning threshold (MB). Above this we warn but still try. */
export const SOFT_FILE_MB = 80;

/** Hard limit on mobile (MB). Full decode past this usually OOMs Safari. */
export const HARD_MOBILE_MB = 120;

/** Hard limit on desktop (MB). Beyond this we refuse full decode. */
export const HARD_DESKTOP_MB = 400;

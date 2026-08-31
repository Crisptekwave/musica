# CRISP Pro Audio Workstation

Standalone single-file web workstation (no build step, no deps) — the "Desk →
Live → Track" studio concept as one self-contained HTML. This is a source
artifact: the same concepts (8×8 session grid, quantized launch, track FX,
bounce) are being wired into the Tauri desktop app and the React webapp.

## Run

Just open the file:

```bash
# any browser
open crisp-workstation.html
# or serve it so audio/AudioContext behave consistently
python3 -m http.server 8080
```

## What's inside

| View | Capability |
|---|---|
| **Desk** | Drop audio (WAV/AIFF/MP3/FLAC), decode (modern + callback fallback), waveform preview, metadata; drag to Live slots or Arrangement |
| **Live** | 8 tracks × 8 scenes, beat-quantized clip launch, per-track HPF/LPF/drive/pan/send, mixer (faders, mute/solo), scene launch, MIDI learn (Web MIDI → CC/notes), kickback instrument generator (slowrave/schranz/acid/tekno/mental) |
| **Track** | Arrangement timeline (drag clips, move between lanes), plugin rack (delay/reverb/EQ/compressor), bounce whole mix, bounce per-stem WAV |
| — | Send/return (delay+reverb convolved IR), transport scheduler (look-ahead 0.5 beat), localStorage session restore, engineer chat (local knowledge only) |

## Intent

Single-file prototype. The real multi-file, tested implementation lives in
`src/lib/audio/*` (DNA, warp, kickback synth, pattern export, master chain,
Ableton bridge). This HTML is for quick experiments and as a UI reference for
the desktop integration (`apps/musica-vj` + `src/crisp/` in the desktop fork).

## Notes / known limits (honest)

- Bounce uses OfflineAudioContext — buffers under ~1.5 GB parse; very long
  arrangements may hit memory limits.
- Session persistence stores metadata only (buffers can't go to localStorage);
  re-import files to hear audio again.
- Works standalone, but CRISP's engine functions (DNA extract, kickback synth,
  warp) live in the module tree — this file is the UI/first pass, not the DSP.
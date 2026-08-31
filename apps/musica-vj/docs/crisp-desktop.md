# CRISP Desktop — Musica VJ integration

CRISP (the on-device remix engine) embedded into the Musica VJ Tauri desktop
app as a self-contained panel. Drop a track → CRISP reads its DNA (BPM + kick
grid) → the synth writes a NEW original loop on that rhythm → WAV + E2S pattern
download. All on-device: pure TypeScript + WebAudio, **zero Rust changes**,
**zero network calls** for the DSP itself.

## What's in this branch

| Path | What |
|---|---|
| `src/crisp/audio/*` | Vendored CRISP audio modules (see sync below) |
| `src/crisp/CrispPanel.tsx` | The CRISP overlay panel (button bottom-right of the app) |
| `src/App.tsx` | Adds the floating CRISP button + panel mount |
| `public/patterns/kickback-template.syx` | Known-good E2S pattern template (fetched at export time) |
| `src-tauri/tauri.conf.json` | Windows bundle targets (`nsis`, `msi`); `externalBin` removed (POC) |
| `scripts/setup-ffmpeg-sidecar.mjs` | Patched: skips gracefully when `ffmpeg-static` is missing |
| `scripts/sync-crisp.mjs` | Re-vendors the CRISP modules from the source repo |
| `tsconfig.json` | `allowImportingTsExtensions` (CRISP sources import with `.ts`) |

## Build

The pipeline is proven on Windows (the PC this repo was built on):

```bash
cd apps/musica-vj
npm ci
npm run tauri build     # needs Rust + VS Build Tools + WebView2 (Tauri 2)
```

Artifacts land in `target/release/bundle/{nsis,msi}`.

## Syncing the vendored CRISP modules

The source of truth is `Crisptekwave/pearl-willow-shadow-branch`
(`src/lib/audio` there). To re-vendor after upstream CRISP changes:

```bash
# default source: %USERPROFILE%\pearl-willow-shadow-branch
node scripts/sync-crisp.mjs

# explicit source
node scripts/sync-crisp.mjs C:/path/to/pearl-willow-shadow-branch/src/lib/audio
```

The script verifies every module exists before copying and fails loudly if the
source is missing, so the vendored copy never silently drifts.

## Known notes (honest limitations of this POC)

- **Updater signing**: `createUpdaterArtifacts` is on but there's no private key
  (`TAURI_SIGNING_PRIVATE_KEY` unset) → bundles build fine, self-update won't sign.
- **ffmpeg**: `ffmpeg-static`'s installer was blocked on this machine; the
  sidecar step skips, which means musica's MP3/video-export features are absent
  in this build. Approve the install script (`npm config set allow-scripts`)
  and re-run to restore them.
- **CSP**: webview CSP stays locked to `self` — CRISP's DSP needs no external
  origin, so this holds.
- **Stem separation / Ableton bridge**: not yet wired into this panel (they live
  in the source repo; the Ableton client needs the local bridge on :9878).

## Branch strategy

`main` tracks upstream `ruvnet/musica` for clean upstream sync. All CRISP work
lives on `crisp-desktop`. Rebase off `main` before merging upstream changes.
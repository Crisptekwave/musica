#!/usr/bin/env node
/**
 * sync-crisp.mjs — vendor CRISP audio modules from the source repo into this
 * desktop app, so the vendored copy never silently drifts.
 *
 * Source of truth: pearl-willow-shadow-branch (Crisptekwave/pearl-willow-shadow-branch)
 * Dev source path  : %USERPROFILE%\pearl-willow-shadow-branch\src\lib\audio
 * Dest             : src/crisp/audio
 *
 * Usage:
 *   node scripts/sync-crisp.mjs            (uses default source path)
 *   node scripts/sync-crisp.mjs <srcdir>   (explicit source dir)
 */
import { copyFileSync, existsSync, mkdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { homedir } from "node:os";

const here = dirname(fileURLToPath(import.meta.url));
const appRoot = resolve(here, "..");
const DEST = join(appRoot, "src", "crisp", "audio");
const DEFAULT_SRC = join(homedir(), "pearl-willow-shadow-branch", "src", "lib", "audio");

// Modules required by CrispPanel (subset that resolves cleanly in this repo).
const MODULES = [
  "wav.ts",
  "context.ts",
  "fft.ts",
  "analyze.ts",
  "kickback.ts",
  "groove.ts",
  "dna.ts",
  "pattern.ts",
];

const srcDir = process.argv[2] ?? DEFAULT_SRC;

if (!existsSync(srcDir)) {
  console.error(`[sync-crisp] source missing: ${srcDir}`);
  console.error("[sync-crisp] clone Crisptekwave/pearl-willow-shadow-branch into your home dir, or pass an explicit path.");
  process.exit(2);
}

mkdirSync(DEST, { recursive: true });

let copied = 0;
for (const mod of MODULES) {
  const s = join(srcDir, mod);
  const d = join(DEST, mod);
  if (!existsSync(s)) {
    console.error(`[sync-crisp] module missing in source: ${mod} (${s})`);
    process.exit(2);
  }
  copyFileSync(s, d);
  copied++;
}

console.log(`[sync-crisp] copied ${copied} modules`);
console.log(`  src:  ${srcDir}`);
console.log(`  dest: ${DEST}`);
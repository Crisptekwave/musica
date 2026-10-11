# Pipeline patches applied on the VM (2026-10-11)

The tekno machine's scripts live on the VM, not in this repo. These are the measurement fixes Claude applied
there while operating the board, kept here for review. Each was verified on the original files first; the
`.bak-*` file next to the patched script restores the old version.

| Patch | Bug | Evidence (e2e-001) |
|---|---|---|
| `qc_gate_acf.diff` | The 64-beat tempo ACF searched +-5 BPM, which also covers 63/65-beat lags; with a kick on every beat it read 165 BPM as 167.6 and hard-failed the track. | Before: 4/16/64 = 165.045/164.965/167.595 (FAIL). After: 165.045/164.964/164.98 (PASS). Click tracks at 167.6 and 165.3 BPM still FAIL. |
| `fair_phase.diff` | `phase()` put the beat grid 36 ms late on a long low kick (it locked onto body ripple), so the knock windows compared the kick tail with the next kick's attack. | Before: grid 36.0 ms, knock -8..-10 dB in kick sections, the kickless break read +5.8. After: grid 0.2 ms, knock +11..+22 dB, break -9.8. SIFRES moves <= 0.4 dB. |

Open: on the corrected grid the SIFRES reference measures about -5 dB knock100-250, so the ">= +3 dB" knock
target (and the groove formula that clamps at 10) needs recalibrating. That is the owner's call.

Also on the VM, not code: the Hermes kanban dispatcher is off (`kanban.dispatch_in_gateway: false` in
`~/.hermes/config.yaml`); Claude operates the `tekno-machine` board through TEKNO CONTROL.

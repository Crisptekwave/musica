# TEKNO CONTROL

A mobile-first live dashboard for the 24/7 Tekno Machine on the Google L4 VM. It watches
the Hermes kanban, ACE-Step, the GPU, the card folders and agent-mail, and pushes a fresh
snapshot to your phone over a WebSocket every 1.5 s. You never need to reload.

**It is read-only.** The only thing it ever writes is a comment on a Review card, when you tap
**Approve** or **Reject + note**. That write goes through `hermes kanban comment`; the panel
never writes the DB directly. It never starts, stops or restarts anything.

## Screens

| Tab | What you see |
|---|---|
| **Pipeline** | Every track as a card on the lanes Plan → Generate → Stems → Sound Swap → Edit → Filter → Variation → Mix → Polish → Master → Score → Review → Done. Each card shows a 13-step rail, progress %, BPM/key/length from `plan/plan.json`, the loop count (↺, from the board or the run logs on disk), the lane script running right now, and which lane sent the track back with the reason ("would route" dry-run notes are shown in purple and not counted). Review cards get the Approve / Reject + note buttons. When the card folder moved on but the board did not (manual runs), the card says so. Tap a card for plan sections, every pass on disk (`run*.log`), routing, score, handoff package, lane reports and its `card-<slug>` mail thread. |
| **Bots** | Each lane agent is a particle creature. It is **working** while its card runs on the board *or* one of its scripts runs on the machine (song_plan, ace_sections/ace_job/regen_exact, stems + demucs, sound_swap, variation, mix_stems, lane_dsp &lt;mode&gt;, score_card, or a `hermes -p tekno-<lane>` worker), otherwise it sleeps or shows the card's failure. It pulses on the 165 BPM beat when working, sleeps (eyes shut, floating z) when idle, glows red and jitters on failure, and flies particles to the next lane on a hand-off. Tap one to see its task, how it was seen (process, run log, board) and the last log lines. |
| **Listen** | Every render and stem of the cards and the handoff packages, newest first. Live spectrum, stereo meters and a scrolling waveform (Web Audio `AnalyserNode`). A/B switches against the SIFRES v02 reference without stopping. With *Follow newest* on, a new render loads the moment it lands on disk, including a `polish/master.wav` that a new pass overwrote in place (media URLs carry the file's mtime). |
| **Scores** | From `score_card.py`'s `score/score.json`: BPM (16- and 64-beat ACF vs the target ±0.05), key, LUFS-I (-9 ±0.5), true peak (≤ -1 dBTP), sub mono S/M (≤ -30 dB), stereo correlation per band (≥ 0.3), kick knock 100-250 Hz (amber below the +3 dB design target, it is not a hard gate), ref-compare vs SIFRES v02 (band distance, HF tilt, crest, 1-4 kHz width and a bar per band), monotony (from `var/report.json`), Mix Judge (auto-draft until judged) and EarLoop. History charts follow every distinct version of a card's score: archived `attempt*/`, handoff copies and versions the panel saw get overwritten. |
| **Machine** | GPU util and VRAM gauges, temperature, power and which process holds the GPU; ACE take in flight (LM phase, DiT step x/50, decode, seed, section n/6, the GPU lock holder), last take and its time, LoRA, last error and the unit lifecycle from the journal; the lane scripts running now; tracks today vs the cap (6); disk free on `/mnt/agent-data` (red under 20 GB); acestep-api, acestep-lora, hermes-gateway and litellm; and when each source last really changed. |
| **Chat** | agent-mail as a group chat, one thread per track (`card-<slug>`, the convention in DESIGN.md), shown even before the first message. |

### Desktop control room (`/desktop`)

On a wide screen with a mouse (≥ 1100 px, fine pointer, not the installed phone app), `/` opens
`/desktop`: every panel at once, sized to fit one 1440×900 or 1920×1080 screen without scrolling.

- **Pipeline board**: a real kanban with the 13 lanes as columns. Empty lanes fold into thin strips;
  filter by working / failed / review or search by slug. Click a card for the **detail drawer**: lane
  chain with task ids, every send-back with its reason, current task and error, the card's score
  badges, and its renders and stems (click to play). Approve / Reject + note work from the card or the drawer.
- **Live bots strip**: the same creatures, 12 in one line on wide screens. Click one for its task and log.
- **Listen**, **Scores** (badges plus a tabbed history chart), **Machine** and **Agent chat** panels stay visible.
- A KPI bar at the top shows ACE job and progress, GPU, VRAM, disk, tracks today, run/fail/review counts and services.
- Keyboard: `Space` play/pause, `A`/`B` take or SIFRES, `←`/`→` seek, `N` newest take, `1`-`4` filter,
  `/` search, `D` live/demo, `F` fullscreen (wall display), `Esc` close, `?` help.

The ▯ button (or `/?view=phone`) switches a desktop browser to the phone UI; opening `/desktop` switches back.

The panel opens in **LIVE** mode. Next to the chip, *updated HH:MM:SS* is the time of the last
snapshot the machine pushed; it ticks every 1.5 s and turns amber when updates stop. Tapping the
chip switches that browser tab to demo mode (fake tracks loop through the lanes, bots hand off, a
synthetic 165 BPM loop plays, messages keep coming in) until the next reload; demo is never
remembered. Demo review buttons don't write anything. `TEKNO_DEMO=1` (or `DEMO=1`) makes demo the
default for everyone.

## Data sources

| Source | How it's read |
|---|---|
| Kanban, board `tekno-machine` | SQLite at `/home/kingzmodzmail/.hermes/kanban/boards/tekno-machine/kanban.db`, opened with `mode=ro`. Lanes come from the task titles (`[slug] LANE: …`; `MASTER-SCORE` sits in Score), chains from `task_links`, routing from comments and run summaries (`-> Edit/Generate (reason)`, `route to Mix: …`, `would route: …` = dry run). |
| Card folders | `/mnt/agent-data/tekno_machine/cards/<slug>/`: `plan/plan.json`, audio in `gen/ edit/ stems/ swap/ var/ filter/ mix/ polish/`, each lane's `report.json`, `score/score.json` (+ `score/judge.md`), the `run*.log` passes of `run_e2e.sh` (`=== <step> HH:MM:SS` headers, `score exit N`) and archived `attempt*/` folders (score history). |
| Handoff | `/mnt/agent-data/tekno_machine/handoff/<card>_…/`: master, stems, score and `HANDOFF.md`, served under `/handoff/…`. A package older than the card's current master is flagged stale. |
| Lane processes | `/proc/*/cmdline` (read only): any `…/scripts/<lane script>.py` with `…/cards/<slug>` in its arguments, demucs children, `run_e2e.sh <slug>` and `hermes -p tekno-<lane>` workers. |
| ACE-Step | `GET 127.0.0.1:8011/health`; the take in flight from `/mnt/agent-data/ace-step/logs/acestep.log` (the units write their output there, tqdm bars split by `\r`), the GPU lock `.gpu.lock` only while a flock is held (`/proc/locks`), and the lane process. The ACE units are user units of the `Gebruiker` account, so `journalctl --user` can't see them; their lifecycle is read with `journalctl USER_UNIT=acestep-api.service + USER_UNIT=acestep-lora.service` (the panel's user is in `adm`), the LoRA result from `lora_default.log`, and whether the API has processes from its cgroup. |
| GPU | `nvidia-smi --query-gpu=…` and `--query-compute-apps` |
| Services | `systemctl is-active` (system, then user), then the unit's cgroup, the journal, an HTTP probe or a process check. |
| agent-mail | MCP over HTTP on `100.84.117.126:8765`, with the bearer token read at runtime from `~/.hermes/config.yaml`. The client only sends `initialize` and `resources/read` plus one GET, `/mail/api/unified-inbox`; tools like `send_message` or `purge` and every other `/mail/api` path are blocked in code. |

### agent-mail: the panel's own read-only agent

The panel is registered on agent-mail as **`Tekno-Panel`** in project `hermes-music` (a plain
`TeknoPanel` is silently replaced by a random name by the server, so the name has a dash). Its
contact policy is `block_all`, so no agent can mail it, and the panel's client can't send. Its
token is in `~/.config/tekno-panel/agent_mail_agents.json` (mode 600) and never leaves the server:

```json
[{"name": "Tekno-Panel", "token": "<registration token>", "project": "hermes-music"}]
```

An agent token only shows that agent's own mail, so the track threads come from
`GET /mail/api/unified-inbox` (server bearer only, every project). The token is used only when
that endpoint is unavailable. agent-mail echoes resource URIs, tokens included, in its errors:
the panel redacts them before showing an error.

## Install on the VM

Run as **kingzmodzmail**, the account that owns `~/.hermes`. That kanban DB is mode 700, so
the `Gebruiker` account can't read it.

```bash
cd /mnt/agent-data/apps/tekno-panel   # installed here on the VM (kingzmodzmail owns /mnt/agent-data/apps)
deploy/install.sh
```

The installer:

- creates `.venv` (uv) with FastAPI and uvicorn
- writes `~/.config/tekno-panel/env`, bound to the Tailscale IP (`tailscale ip -4`), port 3020
- installs and starts the `tekno-panel` systemd user service (linger is on, so it survives logout and reboots)
- prints the passcode and the URL

Useful commands:

```bash
systemctl --user status tekno-panel
journalctl --user -u tekno-panel -f
systemctl --user restart tekno-panel     # after editing env / passcode / agent tokens
```

**Passcode.** A random 6-digit code is generated on first start in
`~/.config/tekno-panel/passcode`. To change it, edit that file (any digits; the keypad has OK
for codes longer than 6) and restart the service. After 5 wrong tries an IP is locked out for
5 minutes. A login lasts 30 days, in an HttpOnly, SameSite=Strict cookie.

**Network.** The server binds only to the Tailscale address. It is not on 0.0.0.0 and never
public.

## Phone homescreen (PWA)

Open `http://100.84.117.126:3020/` on the phone (Tailscale on), log in, then:

- **iPhone (Safari):** Share → *Add to Home Screen*
- **Android (Chrome):** ⋮ → *Add to Home screen* / *Install app*

It opens full-screen in black, with the acid-orb icon. The service worker caches only the
app shell; live data, audio and the WebSocket always go to the network.

## Settings (`~/.config/tekno-panel/env`)

| Variable | Default | |
|---|---|---|
| `TEKNO_HOST` / `TEKNO_PORT` | Tailscale IP / `3020` | bind address |
| `TEKNO_DEMO` (or `DEMO`) | `0` | `1` = demo data by default for everyone |
| `TEKNO_BOARD` | `tekno-machine` | kanban board slug |
| `TEKNO_KANBAN_DB` | auto | override the DB path |
| `TEKNO_MACHINE_ROOT` | `/mnt/agent-data/tekno_machine` | cards, `.gpu.lock`, SIFRES reference in `tools/` |
| `TEKNO_REFERENCE_AUDIO` | `<root>/tools/SIFRES_remake_v02_in.mp3` | B side of the A/B |
| `TEKNO_DISK_PATH` / `TEKNO_DISK_WARN_GB` | `/mnt/agent-data` / `20` | disk warning |
| `TEKNO_TRACKS_PER_DAY_CAP` | `6` | daily cap shown on Machine |
| `TEKNO_ACE_URL` / `TEKNO_ACE_LOG` | `http://127.0.0.1:8011` / `/mnt/agent-data/ace-step/logs/acestep.log` | ACE probes |
| `TEKNO_LORA_LOG` / `TEKNO_ACE_UNITS` | `…/logs/lora_default.log` / `acestep-api.service,acestep-lora.service` | LoRA result, units read from the journal |
| `TEKNO_AGENT_MAIL_URL` / `TEKNO_AGENT_MAIL_PROJECT` | `:8765/mcp/` / `hermes-music` | agent-mail |
| `TEKNO_SERVICES_JSON` | built-in list | custom service checks (`name`, `unit`, `cgroup`, `journal`, `http`, `process`) |
| `TEKNO_PUSH_INTERVAL` | `1.5` | seconds between pushes |

## Develop and test

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q                       # 88 tests: kanban + routing, card files, scores, /proc, ACE log + journal, mail, auth, review, media, demo
TEKNO_DEMO=1 TEKNO_STATE_DIR=/tmp/tp .venv/bin/python -m tekno_panel.server   # http://127.0.0.1:3020
```

The front-end is plain HTML/CSS/JS in `static/` with no build step: `js/app.js` (phone screens + socket), `js/desktop.js` + `desktop.css` (desktop room),
`js/bots.js` (creatures), `js/player.js` (Web Audio), `js/charts.js`. Icons come from
`tools/make_icons.py` (Pillow).

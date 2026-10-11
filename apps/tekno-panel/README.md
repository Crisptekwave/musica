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
| **Pipeline** | Every track as a card on the lanes Plan → Generate → Stems → Sound Swap → Edit → Filter → Variation → Mix → Polish → Master → Score → Review → Done. Each card shows a 13-step rail, progress %, the loop count (↺) and which lane sent the track back, with the reason. Review cards get the Approve / Reject + note buttons. If the files on disk are ahead of the board (manual runs), the card says so. |
| **Bots** | Each lane agent is a particle creature. It pulses on the 165 BPM beat when working, sleeps (eyes shut, floating z) when idle, glows red and jitters on failure, and flies particles to the next lane on a hand-off. Tap one to see its current task and last log lines. |
| **Listen** | Every render and stem, newest first. Live spectrum, stereo meters and a scrolling waveform (Web Audio `AnalyserNode`). A/B switches against the SIFRES v02 reference without stopping. With *Follow newest* on, a new take loads the moment it lands on disk. |
| **Scores** | BPM, key, LUFS-I (-9 ±0.5), true peak (≤ -1 dBTP), sub mono S/M (≤ -30 dB), stereo correlation, monotony, Mix Judge verdict and EarLoop as green/red badges, plus history charts across cards. |
| **Machine** | GPU util and VRAM gauges, temperature and power, ACE job + progress, tracks today vs the cap (6), disk free on `/mnt/agent-data` (red under 20 GB), status of acestep-api, acestep-lora, hermes-gateway and litellm, and the health of each data source. |
| **Chat** | The agent-mail threads as a group chat between Grok Bot, Mix Judge, Claude and Hermes. |

The **LIVE / DEMO** chip at the top switches your phone to demo mode: fake tracks loop through
the lanes, bots hand off, a synthetic 165 BPM loop plays, and messages keep coming in. Demo
review buttons don't write anything.

## Data sources

| Source | How it's read |
|---|---|
| Kanban, board `tekno-machine` | SQLite at `~/.hermes/kanban/boards/tekno-machine/kanban.db`, opened with `mode=ro`. Lanes come from the task titles (`[slug] LANE: …`), chains from `task_links`, send-backs from comments and run summaries ("… route: Filter (reason)"). |
| ACE-Step | `GET 127.0.0.1:8011/health`; the current job comes from `/mnt/agent-data/tekno_machine/.gpu.lock` (pid + card) and progress from the tail of `acestep.log`. |
| GPU | `nvidia-smi --query-gpu=…` |
| Card folders | `/mnt/agent-data/tekno_machine/cards/<slug>/{gen,stems,swap,var,edit,filter,mix,polish}` for audio, `score/score.json` (from `score_card.py`) and `score/judge.md` for scores. |
| Services | `systemctl is-active` (system, then user). Units it can't see fall back to an HTTP probe or a process check. |
| agent-mail | MCP over HTTP on `100.84.117.126:8765`, with the bearer token read at runtime from `~/.hermes/config.yaml`. The client only sends `initialize` and `resources/read`; tools like `send_message` or `purge` are blocked in code. |

### agent-mail tokens (needed for the Chat screen)

agent-mail only shows a mailbox to that agent's own token. Put the agents you want in the
feed in `~/.config/tekno-panel/agent_mail_agents.json` (mode 600):

```json
[
  {"name": "LavenderElk", "token": "<Grok Bot registration token>"},
  {"name": "<Mix Judge agent name>", "token": "<its token>"}
]
```

Then run `systemctl --user restart tekno-panel`. Until then, Chat lists the project's agents
and tells you tokens are missing.

## Install on the VM

Run as **kingzmodzmail**, the account that owns `~/.hermes`. That kanban DB is mode 700, so
the `Gebruiker` account can't read it.

```bash
cd /mnt/agent-data/tekno-panel      # or wherever this folder lives
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
| `TEKNO_DEMO` | `0` | `1` = demo data by default for everyone |
| `TEKNO_BOARD` | `tekno-machine` | kanban board slug |
| `TEKNO_KANBAN_DB` | auto | override the DB path |
| `TEKNO_MACHINE_ROOT` | `/mnt/agent-data/tekno_machine` | cards, `.gpu.lock`, SIFRES reference in `tools/` |
| `TEKNO_REFERENCE_AUDIO` | `<root>/tools/SIFRES_remake_v02_in.mp3` | B side of the A/B |
| `TEKNO_DISK_PATH` / `TEKNO_DISK_WARN_GB` | `/mnt/agent-data` / `20` | disk warning |
| `TEKNO_TRACKS_PER_DAY_CAP` | `6` | daily cap shown on Machine |
| `TEKNO_ACE_URL` / `TEKNO_ACE_LOG` | `http://127.0.0.1:8011` / ACE log | ACE probes |
| `TEKNO_AGENT_MAIL_URL` / `TEKNO_AGENT_MAIL_PROJECT` | `:8765/mcp/` / `hermes-music` | agent-mail |
| `TEKNO_SERVICES_JSON` | built-in list | custom service checks (`name`, `unit`, `http`, `process`) |
| `TEKNO_PUSH_INTERVAL` | `1.5` | seconds between pushes |

## Develop and test

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q                       # 51 tests: kanban parsing, scores, auth, review, media, demo
TEKNO_DEMO=1 TEKNO_STATE_DIR=/tmp/tp .venv/bin/python -m tekno_panel.server   # http://127.0.0.1:3020
```

The front-end is plain HTML/CSS/JS in `static/` with no build step: `js/app.js` (screens + socket),
`js/bots.js` (creatures), `js/player.js` (Web Audio), `js/charts.js`. Icons come from
`tools/make_icons.py` (Pillow).

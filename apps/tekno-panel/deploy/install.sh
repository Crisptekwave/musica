#!/usr/bin/env bash
# Install / update TEKNO CONTROL as a systemd user service.
# Usage: deploy/install.sh            (from the tekno-panel folder)
# Re-running is safe: it updates the venv and restarts the service, keeping env + passcode.
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONF="$HOME/.config/tekno-panel"
UNIT_DIR="$HOME/.config/systemd/user"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
mkdir -p "$CONF" "$UNIT_DIR"; chmod 700 "$CONF"
# keep package caches off the (small) root disk when the big data disk is there
[ -d /mnt/agent-data/uv-cache ] && export UV_CACHE_DIR="${UV_CACHE_DIR:-/mnt/agent-data/uv-cache}" PIP_CACHE_DIR="${PIP_CACHE_DIR:-/mnt/agent-data/uv-cache/pip}"

if command -v uv >/dev/null; then
  [ -d "$APP_DIR/.venv" ] || uv venv -q --python python3 "$APP_DIR/.venv"
  uv pip install -q --python "$APP_DIR/.venv/bin/python" -r "$APP_DIR/requirements.txt"
else
  [ -d "$APP_DIR/.venv" ] || python3 -m venv "$APP_DIR/.venv"
  "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
fi

if [ ! -f "$CONF/env" ]; then
  TS_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
  cat > "$CONF/env" <<ENV
# TEKNO CONTROL settings (see README). Bind to the Tailscale IP only: never 0.0.0.0.
TEKNO_HOST=${TS_IP:-127.0.0.1}
TEKNO_PORT=3020
TEKNO_DEMO=0
TEKNO_BOARD=tekno-machine
TEKNO_MACHINE_ROOT=/mnt/agent-data/tekno_machine
TEKNO_DISK_PATH=/mnt/agent-data
TEKNO_DISK_WARN_GB=20
TEKNO_TRACKS_PER_DAY_CAP=6
TEKNO_AGENT_MAIL_PROJECT=hermes-music
ENV
  chmod 600 "$CONF/env"
fi
[ -f "$CONF/agent_mail_agents.json" ] || { echo '[]' > "$CONF/agent_mail_agents.json"; chmod 600 "$CONF/agent_mail_agents.json"; }

sed "s|@APP_DIR@|$APP_DIR|g" "$APP_DIR/deploy/tekno-panel.service" > "$UNIT_DIR/tekno-panel.service"
sed "s|@APP_DIR@|$APP_DIR|g" "$APP_DIR/deploy/tekno-control-helper.service" > "$UNIT_DIR/tekno-control-helper.service"
# team chat (approved by the owner): team-mail CLI + alert relay (new panel alerts -> agent-mail thread panel-errors)
AM="$HOME/.config/agent-mail"; mkdir -p "$AM" "$HOME/.local/bin"; chmod 700 "$AM"
install -m 755 "$APP_DIR/tools/team_mail.py" "$APP_DIR/tools/alert_relay.py" "$APP_DIR/tools/fallback_watchdog.py" \
  "$APP_DIR/tools/agent_status.py" "$APP_DIR/tools/reaper_snap.py" "$APP_DIR/tools/qwen_night.py" "$APP_DIR/tools/llm.py" "$APP_DIR/tools/watchdog.py" "$AM/"
ln -sf "$AM/team_mail.py" "$HOME/.local/bin/team-mail"
ln -sf "$AM/agent_status.py" "$HOME/.local/bin/tekno-agent"
# agent status files (tekno-agent): a group-writable setgid folder like the card folders, so every agent can write
AG="${TEKNO_AGENTS_DIR:-/mnt/agent-data/tekno_machine/agents}"
if [ -d "$(dirname "$AG")" ] && [ ! -d "$AG" ]; then
  sudo -n -u "${TEKNO_ACE_USER:-Gebruiker}" bash -c 'mkdir -p "$1" && chgrp "$(stat -c %G "$(dirname "$1")")" "$1" && chmod 2775 "$1"' _ "$AG" \
    || mkdir -p "$AG"
fi
# live REAPER view (Studio page): frame grabber, only works while the Studio page is open
cp "$APP_DIR/deploy/tekno-reaper-snap.service" "$UNIT_DIR/"
cp "$APP_DIR/deploy/tekno-alert-relay.service" "$APP_DIR/deploy/tekno-alert-relay.timer" "$UNIT_DIR/"
# Claude-limit fallback (approved by the owner: allowlist tools only, stock Qwen3-Coder on the local vLLM)
FB="${TEKNO_FALLBACK_DIR:-/mnt/agent-data/tekno_machine/fallback}"
if [ -d "$(dirname "$FB")" ]; then
  mkdir -p "$FB/handoff" "$FB/runs"
  install -m 644 "$APP_DIR/tools/FALLBACK_SOUL.md" "$FB/"
  [ -f "$FB/MODE" ] || echo auto > "$FB/MODE"
  cp "$APP_DIR/deploy/tekno-fallback.service" "$APP_DIR/deploy/tekno-fallback.timer" "$UNIT_DIR/"
fi
# night shift (approved by the owner): local Qwen tags samples at night and posts a morning report; no model tools
cp "$APP_DIR"/deploy/tekno-night-{tag,digest}.{service,timer} "$APP_DIR"/deploy/tekno-watchdog.{service,timer} "$UNIT_DIR/"
systemctl --user daemon-reload
systemctl --user enable tekno-control-helper.service tekno-panel.service >/dev/null
command -v ffmpeg >/dev/null && systemctl --user enable --now tekno-reaper-snap.service >/dev/null || echo "ffmpeg missing: Studio frames are off"
# the relay needs the agent identities (identities.json) set up first
[ -f "$AM/identities.json" ] && systemctl --user enable --now tekno-alert-relay.timer >/dev/null
[ -f "$UNIT_DIR/tekno-fallback.timer" ] && grep -q '"qwen"' "$AM/identities.json" 2>/dev/null \
  && systemctl --user enable --now tekno-fallback.timer >/dev/null
grep -q '"qwen"' "$AM/identities.json" 2>/dev/null \
  && systemctl --user enable --now tekno-night-tag.timer tekno-night-digest.timer >/dev/null
systemctl --user enable --now tekno-watchdog.timer >/dev/null
systemctl --user restart tekno-control-helper.service tekno-panel.service
sleep 2
systemctl --user --no-pager --lines=5 status tekno-panel.service || true
. "$CONF/env"
[ -f "$CONF/passcode" ] && echo "Passcode: $(cat "$CONF/passcode")  (change it by editing $CONF/passcode, then restart)"
echo "Open: http://${TEKNO_HOST}:${TEKNO_PORT}/"

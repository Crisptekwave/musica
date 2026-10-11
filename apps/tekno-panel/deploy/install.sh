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
systemctl --user daemon-reload
systemctl --user enable tekno-control-helper.service tekno-panel.service >/dev/null
systemctl --user restart tekno-control-helper.service tekno-panel.service
sleep 2
systemctl --user --no-pager --lines=5 status tekno-panel.service || true
. "$CONF/env"
[ -f "$CONF/passcode" ] && echo "Passcode: $(cat "$CONF/passcode")  (change it by editing $CONF/passcode, then restart)"
echo "Open: http://${TEKNO_HOST}:${TEKNO_PORT}/"

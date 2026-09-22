#!/usr/bin/env bash
# Install (or update) Citadel as a systemd *user* service on this machine. Safe to re-run.
#
#   deploy/install.sh              # install + start
#   deploy/install.sh --mdns dash  # also publish dash.local over mDNS (needs avahi-publish)
#
# Moving to another PC: copy this folder (and data/hub.sqlite + .env), then run this script there.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
BIN_DIR="$HOME/.local/bin"
MDNS_NAME=""
while [ $# -gt 0 ]; do
  case "$1" in
    --mdns) MDNS_NAME="${2:?--mdns needs a name, e.g. dash}"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1;35m==>\033[0m %s\n' "$*"; }

command -v python3 >/dev/null || { echo "python3 not found" >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || { echo "Python 3.11+ is required" >&2; exit 1; }
command -v systemctl >/dev/null || { echo "systemd not found" >&2; exit 1; }

say "Python environment"
[ -x "$ROOT/.venv/bin/python" ] || python3 -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/pip" install --quiet --disable-pip-version-check -r "$ROOT/requirements.txt"

if [ ! -f "$ROOT/.env" ]; then
  say "Creating .env from .env.example — fill it in, then run: hub seed"
  cp "$ROOT/.env.example" "$ROOT/.env"
fi
chmod 600 "$ROOT/.env"

say "Installing the 'hub' command -> $BIN_DIR/hub"
mkdir -p "$BIN_DIR"
printf '#!/usr/bin/env bash\nexec "%s/.venv/bin/python" -m app.cli "$@"\n' "$ROOT" > "$BIN_DIR/hub"
chmod +x "$BIN_DIR/hub"

say "Installing systemd user units -> $UNIT_DIR"
mkdir -p "$UNIT_DIR"
sed "s#@ROOT@#$ROOT#g" "$ROOT/deploy/hub.service" > "$UNIT_DIR/hub.service"
if [ -n "$MDNS_NAME" ]; then
  command -v avahi-publish >/dev/null || echo "  (warning: avahi-publish not found — install avahi / avahi-utils for the .local name)"
  sed -e "s#@ROOT@#$ROOT#g" -e "s#@NAME@#$MDNS_NAME#g" "$ROOT/deploy/hub-mdns.service" > "$UNIT_DIR/hub-mdns.service"
fi
systemctl --user daemon-reload
systemctl --user enable hub.service >/dev/null
systemctl --user restart hub.service
[ -n "$MDNS_NAME" ] && { systemctl --user enable hub-mdns.service >/dev/null; systemctl --user restart hub-mdns.service; }

PORT="$(grep -E '^HUB_PORT=' "$ROOT/.env" | tail -1 | cut -d= -f2 || true)"; PORT="${PORT:-8080}"
say "Waiting for the server"
for _ in $(seq 1 30); do curl -fs "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break; sleep 0.5; done
curl -fs "http://127.0.0.1:$PORT/healthz" >/dev/null && say "Citadel is up on port $PORT" || { echo "Server did not come up — see: journalctl --user -u hub -n 50" >&2; exit 1; }

cat <<MSG

Next steps
  1. Enrol your first owner device:     hub enrol
  2. To start at BOOT without logging in (needs sudo, once):
       sudo loginctl enable-linger "$USER"
  3. To reach it from other devices, allow the port through the firewall (if ufw is active):
       sudo ufw allow from 192.168.0.0/24 to any port $PORT proto tcp comment 'Citadel'
     (adjust the subnet to your LAN; also allow the ports of projects you want to open from other devices)
  Logs:  journalctl --user -u hub -f
MSG

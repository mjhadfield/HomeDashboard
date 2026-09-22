#!/usr/bin/env bash
# Remove the Citadel service and 'hub' command. Leaves your data (data/, .env) untouched.
set -euo pipefail
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
for u in hub hub-mdns; do
  systemctl --user disable --now "$u.service" 2>/dev/null || true
  rm -f "$UNIT_DIR/$u.service"
done
systemctl --user daemon-reload
rm -f "$HOME/.local/bin/hub"
echo "Removed. Projects started from the dashboard (hub-proj-*) keep running; stop them from the UI first if you want them down."

#!/usr/bin/env bash
# Push code to mimosa and refresh the venv and systemd units.
set -euo pipefail
cd "$(dirname "$0")/.."
rsync -az --delete --exclude .venv --exclude data --exclude dist --exclude secrets.env \
  --exclude __pycache__ ./ mimosa:/opt/modelbehavior/
ssh mimosa 'set -e
  cd /opt/modelbehavior && uv sync -q
  cp deploy/modelbehavior-update.service deploy/modelbehavior-update.timer /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now modelbehavior-update.timer >/dev/null'
echo "deployed"

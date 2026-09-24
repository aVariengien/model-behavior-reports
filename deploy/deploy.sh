#!/usr/bin/env bash
# Push code to mimosa and refresh the venv and systemd units.
set -euo pipefail
cd "$(dirname "$0")/.."
rsync -az --delete --exclude .git --exclude .venv --exclude data --exclude dist --exclude secrets.env \
  --exclude __pycache__ ./ mimosa:/opt/modelbehavior/
ssh mimosa 'set -e
  cd /opt/modelbehavior && uv sync -q
  cp deploy/modelbehavior-update.service deploy/modelbehavior-update.timer deploy/modelbehavior-editor.service /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now modelbehavior-update.timer >/dev/null
  systemctl enable modelbehavior-editor >/dev/null && systemctl restart modelbehavior-editor'
echo "deployed"

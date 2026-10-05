#!/usr/bin/env bash
# One-shot installer for the MASTER bot on an Oracle Cloud VPS (Ubuntu / Debian / Oracle Linux).
# Run as your normal user from inside the cloned repo:   bash oracle_setup.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="$(id -un)"
SERVICE=epub-bot

echo "▶ Installing system packages…"
if command -v apt-get >/dev/null; then
  sudo apt-get update -y
  sudo apt-get install -y python3 python3-venv python3-pip git
elif command -v dnf >/dev/null; then
  sudo dnf install -y python3 python3-pip git
fi

echo "▶ Creating virtualenv + installing bot requirements…"
cd "$APP_DIR"
python3 -m venv venv
./venv/bin/pip install --upgrade pip wheel
# pyrogram and kurigram both install as the `pyrogram` package — remove the old
# one first on upgrades so the coloured-button fork is the one that gets imported
./venv/bin/pip uninstall -y pyrogram pyrofork >/dev/null 2>&1 || true
./venv/bin/pip install -r requirements-bot.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "▶ Created .env — edit it now (API_ID, API_HASH, BOT_TOKEN, OWNER_ID, WORKER_SECRET)."
fi
mkdir -p data

echo "▶ Writing systemd unit /etc/systemd/system/${SERVICE}.service…"
sudo tee /etc/systemd/system/${SERVICE}.service >/dev/null <<UNIT
[Unit]
Description=EPUB Translator Telegram Bot (master)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/venv/bin/python bot.py
Restart=always
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE}

# Oracle images ship with a restrictive iptables ruleset; the bot only needs
# outbound HTTPS, so nothing to open. Just make sure outbound isn't blocked.
if grep -q '^BOT_TOKEN=.\+' .env 2>/dev/null; then
  sudo systemctl restart ${SERVICE}
  echo "✔ Bot started.   Logs: journalctl -u ${SERVICE} -f"
else
  echo "⚠ .env is not filled yet. After editing run:  sudo systemctl start ${SERVICE}"
fi

cat <<'NEXT'

Next: add workers from Telegram
  /addworker https://<user>-<space>.hf.space https://<user>.pythonanywhere.com https://<proj>.vercel.app
NEXT

#!/usr/bin/env bash
# Install NoblePort MailHub as a systemd service on Debian/Ubuntu/RHEL-family Linux.
#   sudo bash deploy/install.sh
set -euo pipefail

APP_DIR=/opt/nobleport-mailhub
DATA_DIR=/var/lib/nobleport-mailhub
CONF_DIR=/etc/nobleport-mailhub
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then echo "run as root (sudo)"; exit 1; fi
command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }

id -u mailhub &>/dev/null || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin mailhub
mkdir -p "$APP_DIR" "$DATA_DIR" "$CONF_DIR"
chown mailhub:mailhub "$DATA_DIR"; chmod 750 "$DATA_DIR"

python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip >/dev/null
"$APP_DIR/venv/bin/pip" install "$SRC_DIR"

if [[ ! -f "$CONF_DIR/env" ]]; then
  SECRET=$("$APP_DIR/venv/bin/python" -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
  cat > "$CONF_DIR/env" <<ENV
MAILHUB_DATA_DIR=$DATA_DIR
MAILHUB_SECRET_KEY=$SECRET
MAILHUB_HOST=127.0.0.1
MAILHUB_PORT=8025
MAILHUB_SYNC_INTERVAL_S=120
MAILHUB_FOLDERS=INBOX
ENV
  chmod 640 "$CONF_DIR/env"; chown root:mailhub "$CONF_DIR/env"
fi

install -m 644 "$SRC_DIR/deploy/mailhub.service" /etc/systemd/system/
install -m 644 "$SRC_DIR/deploy/mailhub-sync.service" /etc/systemd/system/
install -m 644 "$SRC_DIR/deploy/mailhub-sync.timer" /etc/systemd/system/
ln -sf "$APP_DIR/venv/bin/mailhub" /usr/local/bin/mailhub

systemctl daemon-reload
systemctl enable --now mailhub.service
systemctl enable --now mailhub-sync.timer

cat <<MSG

MailHub installed.
  Web inbox : http://127.0.0.1:8025/
  Config    : $CONF_DIR/env
  Data      : $DATA_DIR
Connect a mailbox (runs as the service user so the secret store matches):
  sudo -u mailhub env \$(grep -v '^#' $CONF_DIR/env | xargs) mailhub accounts add you@gmail.com
MSG

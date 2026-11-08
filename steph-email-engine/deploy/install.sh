#!/usr/bin/env bash
# Install the Steph Email Engine as a systemd service on a Debian/Ubuntu/RHEL-family VPS.
#   sudo bash deploy/install.sh
set -euo pipefail

APP_DIR=/opt/steph-email-engine
DATA_DIR=/var/lib/steph-email-engine
CONF_DIR=/etc/steph-email-engine
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then echo "run as root (sudo)"; exit 1; fi
command -v python3 >/dev/null || { echo "python3 (3.11+) is required"; exit 1; }
python3 - <<'PY' || { echo "python 3.11+ is required"; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY

id -u steph &>/dev/null || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin steph
mkdir -p "$APP_DIR" "$DATA_DIR" "$CONF_DIR"
chown steph:steph "$DATA_DIR"; chmod 750 "$DATA_DIR"

python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip >/dev/null
"$APP_DIR/venv/bin/pip" install "$SRC_DIR"

if [[ ! -f "$CONF_DIR/env" ]]; then
  SECRET=$("$APP_DIR/venv/bin/python" -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
  sed -e "s|^STEPH_EMAIL_DATA_DIR=.*|STEPH_EMAIL_DATA_DIR=$DATA_DIR|" \
      -e "s|^#STEPH_EMAIL_SECRET_KEY=.*|STEPH_EMAIL_SECRET_KEY=$SECRET|" \
      "$SRC_DIR/.env.example" > "$CONF_DIR/env"
  chmod 640 "$CONF_DIR/env"; chown root:steph "$CONF_DIR/env"
fi

install -m 644 "$SRC_DIR/deploy/steph-email.service" /etc/systemd/system/
install -m 644 "$SRC_DIR/deploy/steph-email-sync.service" /etc/systemd/system/
install -m 644 "$SRC_DIR/deploy/steph-email-sync.timer" /etc/systemd/system/
ln -sf "$APP_DIR/venv/bin/steph-email" /usr/local/bin/steph-email

systemctl daemon-reload
systemctl enable --now steph-email.service
systemctl enable --now steph-email-sync.timer

cat <<MSG

Steph Email Engine installed.
  Dashboard : http://127.0.0.1:8030/
  Config    : $CONF_DIR/env   (owner phone, Twilio, ElevenLabs, brief time live here)
  Data      : $DATA_DIR       (SQLite, secret key, raw/ originals, audio/, evidence/)

Next (run as the service user so the secret store matches):
  sudo -u steph env \$(grep -v '^#' $CONF_DIR/env | xargs) steph-email accounts add you@gmail.com
  sudo -u steph env \$(grep -v '^#' $CONF_DIR/env | xargs) steph-email sync
  sudo -u steph env \$(grep -v '^#' $CONF_DIR/env | xargs) steph-email status
MSG

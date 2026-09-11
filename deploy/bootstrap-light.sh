#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/opt/arena17-downloads
DATA_DIR=/var/www/arena17
cd "$APP_DIR"

getent passwd arena17 >/dev/null || useradd --system --home-dir "$APP_DIR" --shell /sbin/nologin arena17
getent group arena17 >/dev/null || groupadd --system arena17
mkdir -p "$APP_DIR/instance" "$DATA_DIR/downloads" "$DATA_DIR/uploads-tmp"

tar -xf /tmp/wg-camp-code.tar -C "$APP_DIR"
mkdir -p "$APP_DIR/vendor"
/usr/bin/python3 -c "import glob,zipfile; [zipfile.ZipFile(p).extractall('$APP_DIR/vendor') for p in glob.glob('$APP_DIR/vendor-wheels/*.whl')]"
rm -rf "$APP_DIR/vendor-wheels"
chown -R arena17:arena17 "$APP_DIR" "$DATA_DIR"
chmod 750 "$APP_DIR" "$APP_DIR/instance" "$DATA_DIR" "$DATA_DIR/downloads" "$DATA_DIR/uploads-tmp"

secret=$(openssl rand -hex 32)
printf 'ARENA17_SECRET_KEY=%s\nARENA17_INSTANCE_PATH=%s/instance\nARENA17_DOWNLOADS_DIR=%s/downloads\nARENA17_UPLOAD_TMP_DIR=%s/uploads-tmp\nARENA17_MAX_UPLOAD_BYTES=8589934592\n' "$secret" "$APP_DIR" "$DATA_DIR" "$DATA_DIR" > /etc/arena17-downloads.env
chown root:arena17 /etc/arena17-downloads.env
chmod 640 /etc/arena17-downloads.env

sudo -u arena17 env PYTHONPATH="$APP_DIR/vendor" ARENA17_INSTANCE_PATH="$APP_DIR/instance" /usr/bin/python3 -c 'import flask, gunicorn; print("Python dependencies OK")'
sudo -u arena17 env PYTHONPATH="$APP_DIR/vendor" ARENA17_INSTANCE_PATH="$APP_DIR/instance" /usr/bin/python3 -c 'from wsgi import app; from app.db import init_db; app.app_context().push(); init_db()'
cp "$APP_DIR/deploy/arena17-downloads.service" /etc/systemd/system/arena17-downloads.service
systemctl daemon-reload
systemctl enable --now arena17-downloads
rm -f /tmp/wg-camp-code.tar
systemctl --no-pager --full status arena17-downloads | head -25
curl -fsS --max-time 5 http://127.0.0.1:8000/ | head -c 300

#!/usr/bin/env bash
set -euo pipefail

SCRIPT="$0"
SCRIPTPATH=$(CDPATH='' cd -- "$(dirname -- "$SCRIPT")" && pwd)

INSTALL_DIR="/opt/homekit-inspector"
DATA_DIR="/var/lib/homekit-inspector"
INSTALL_USER="${SUDO_USER:-$USER}"
PORT="8099"
UPLOAD_TOKEN_SOURCE=""
MAX_UPLOAD_BYTES="20971520"
DEVELOPMENT=false

usage() {
    cat <<EOF
Usage: sudo ./install.sh [options]

Options:
  -i, --install-dir DIR   Application directory (default: /opt/homekit-inspector)
      --data-dir DIR      Directory containing homekit_inspector.html (default: /var/lib/homekit-inspector)
  -u, --user USER         User that owns and runs the service (default: invoking user)
  -p, --port PORT         LAN port for the server (default: 8099)
      --upload-token-file FILE
                           Enable authenticated report uploads using this token
      --max-upload-bytes N Maximum report upload size (default: 20971520)
  -d, --development       Validate files only; do not install systemd service
  -h, --help              Show this help
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        -i|--install-dir) shift; INSTALL_DIR="$1" ;;
        --data-dir) shift; DATA_DIR="$1" ;;
        -u|--user) shift; INSTALL_USER="$1" ;;
        -p|--port) shift; PORT="$1" ;;
        --upload-token-file) shift; UPLOAD_TOKEN_SOURCE="$1" ;;
        --max-upload-bytes) shift; MAX_UPLOAD_BYTES="$1" ;;
        -d|--development) DEVELOPMENT=true ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
    esac
    shift
done

if ! [[ "$MAX_UPLOAD_BYTES" =~ ^[1-9][0-9]*$ ]]; then
    echo "--max-upload-bytes must be a positive integer" >&2
    exit 2
fi
if [ -n "$UPLOAD_TOKEN_SOURCE" ] && [ ! -s "$UPLOAD_TOKEN_SOURCE" ]; then
    echo "Upload token file is missing or empty: $UPLOAD_TOKEN_SOURCE" >&2
    exit 2
fi
if [ -n "$UPLOAD_TOKEN_SOURCE" ]; then
    if ! python3 -c '
import pathlib
import sys

token = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").strip()
valid = len(token) >= 32 and not any(character.isspace() for character in token)
raise SystemExit(0 if valid else 1)
' "$UPLOAD_TOKEN_SOURCE"; then
        echo "Upload token must contain at least 32 characters without whitespace" >&2
        exit 2
    fi
fi

if ! id "$INSTALL_USER" >/dev/null 2>&1; then
    echo "User does not exist: $INSTALL_USER" >&2
    exit 1
fi

INSTALL_GROUP=$(id -gn "$INSTALL_USER")
REQUIRED_FILES=("scripts/serve_inspector.py" "homekit-inspector.service")
for file in "${REQUIRED_FILES[@]}"; do
    if [ ! -f "$SCRIPTPATH/$file" ]; then
        echo "Missing required file: $file" >&2
        exit 1
    fi
done

if [ "$DEVELOPMENT" = true ]; then
    PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-/tmp/homekit-inspector-pycache}" \
        python3 -m py_compile "$SCRIPTPATH/scripts/serve_inspector.py"
    echo "Development validation complete"
    exit 0
fi

install -d -m 0755 -o "$INSTALL_USER" -g "$INSTALL_GROUP" "$INSTALL_DIR"
install -d -m 0750 -o "$INSTALL_USER" -g "$INSTALL_GROUP" "$DATA_DIR"
install -d -m 0755 -o "$INSTALL_USER" -g "$INSTALL_GROUP" "$INSTALL_DIR/scripts"
install -m 0755 -o "$INSTALL_USER" -g "$INSTALL_GROUP" "$SCRIPTPATH/scripts/serve_inspector.py" "$INSTALL_DIR/scripts/serve_inspector.py"

UPLOAD_TOKEN_PATH="$INSTALL_DIR/upload-token"
if [ -n "$UPLOAD_TOKEN_SOURCE" ]; then
    install -m 0600 -o "$INSTALL_USER" -g "$INSTALL_GROUP" "$UPLOAD_TOKEN_SOURCE" "$UPLOAD_TOKEN_PATH"
fi
if [ ! -f "$UPLOAD_TOKEN_PATH" ]; then
    UPLOAD_TOKEN_PATH=""
fi

SERVICE_FILE=$(mktemp)
sed \
    -e "s|^User=.*|User=$INSTALL_USER|g" \
    -e "s|^Group=.*|Group=$INSTALL_GROUP|g" \
    -e "s|^WorkingDirectory=.*|WorkingDirectory=$INSTALL_DIR|g" \
    -e "s|^Environment=HOMEKIT_INSPECTOR_PORT=.*|Environment=HOMEKIT_INSPECTOR_PORT=$PORT|g" \
    -e "s|^Environment=HOMEKIT_INSPECTOR_ROOT=.*|Environment=HOMEKIT_INSPECTOR_ROOT=$DATA_DIR|g" \
    -e "s|^Environment=HOMEKIT_INSPECTOR_UPLOAD_TOKEN_FILE=.*|Environment=HOMEKIT_INSPECTOR_UPLOAD_TOKEN_FILE=$UPLOAD_TOKEN_PATH|g" \
    -e "s|^Environment=HOMEKIT_INSPECTOR_MAX_UPLOAD_BYTES=.*|Environment=HOMEKIT_INSPECTOR_MAX_UPLOAD_BYTES=$MAX_UPLOAD_BYTES|g" \
    -e "s|^ExecStart=.*|ExecStart=/usr/bin/python3 $INSTALL_DIR/scripts/serve_inspector.py|g" \
    "$SCRIPTPATH/homekit-inspector.service" > "$SERVICE_FILE"

install -m 0644 "$SERVICE_FILE" /etc/systemd/system/homekit-inspector.service
rm "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable homekit-inspector.service

echo "Installed homekit-inspector.service"
if [ -n "$UPLOAD_TOKEN_PATH" ]; then
    echo "Authenticated report upload API enabled"
else
    echo "Authenticated report upload API disabled; rerun with --upload-token-file FILE to enable it"
fi
echo "Place a generated homekit_inspector.html into $DATA_DIR, then run:"
echo "  sudo systemctl restart homekit-inspector.service"

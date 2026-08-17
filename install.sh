#!/usr/bin/env bash
set -euo pipefail

SCRIPT="$0"
SCRIPTPATH=$(CDPATH='' cd -- "$(dirname -- "$SCRIPT")" && pwd)

INSTALL_DIR="/opt/homekit-inspector"
DATA_DIR="/var/lib/homekit-inspector"
CONFIG_DIR="/etc/homekit-inspector"
SERVER_CONFIG_SOURCE=""
INSTALL_USER="${SUDO_USER:-$USER}"
HOST="0.0.0.0"
PORT="8099"
PUBLISH_SECRET_SOURCE=""
TLS_CERT_SOURCE=""
TLS_KEY_SOURCE=""
ALLOW_UNAUTHENTICATED_VIEW=false
MAX_UPLOAD_BYTES="20971520"
DEVELOPMENT=false

usage() {
    cat <<EOF
Usage: sudo ./install.sh [options]

Options:
  -i, --install-dir DIR   Application directory (default: /opt/homekit-inspector)
      --data-dir DIR      Directory containing homekit_inspector.html (default: /var/lib/homekit-inspector)
  -u, --user USER         User that owns and runs the service (default: invoking user)
      --host ADDRESS      Listen address (default: 0.0.0.0)
  -p, --port PORT         LAN port for the server (default: 8099)
      --publish-secret-file FILE
                           Enable signed report publication using this secret
      --tls-cert-file FILE TLS server certificate or certificate chain
      --tls-key-file FILE  TLS private key
      --server-config-file FILE
                           Private JSON server configuration (default: admin/admin)
      --allow-unauthenticated-view
                           Allow network viewing without authentication
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
        --host) shift; HOST="$1" ;;
        -p|--port) shift; PORT="$1" ;;
        --publish-secret-file) shift; PUBLISH_SECRET_SOURCE="$1" ;;
        --tls-cert-file) shift; TLS_CERT_SOURCE="$1" ;;
        --tls-key-file) shift; TLS_KEY_SOURCE="$1" ;;
        --server-config-file) shift; SERVER_CONFIG_SOURCE="$1" ;;
        --allow-unauthenticated-view) ALLOW_UNAUTHENTICATED_VIEW=true ;;
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
if ! [[ "$PORT" =~ ^[0-9]+$ ]] || [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
    echo "--port must be between 1 and 65535" >&2
    exit 2
fi
if ! [[ "$HOST" =~ ^[A-Za-z0-9.:-]+$ ]]; then
    echo "--host contains unsupported characters" >&2
    exit 2
fi
for configured_path in "$INSTALL_DIR" "$DATA_DIR" "$CONFIG_DIR"; do
    if ! [[ "$configured_path" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
        echo "Installation paths must be absolute and contain only safe characters" >&2
        exit 2
    fi
done
if ! [[ "$INSTALL_USER" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "--user contains unsupported characters" >&2
    exit 2
fi
if [ -n "$PUBLISH_SECRET_SOURCE" ] && [ ! -s "$PUBLISH_SECRET_SOURCE" ]; then
    echo "Publication secret file is missing or empty: $PUBLISH_SECRET_SOURCE" >&2
    exit 2
fi
if [ -n "$PUBLISH_SECRET_SOURCE" ]; then
    if ! python3 -c '
import pathlib
import re
import sys

secret = pathlib.Path(sys.argv[1]).read_text(encoding="ascii").strip()
valid = re.fullmatch(r"[0-9a-f]{64}", secret) is not None
raise SystemExit(0 if valid else 1)
' "$PUBLISH_SECRET_SOURCE"; then
        echo "Publication secret must contain exactly 64 lowercase hex characters" >&2
        exit 2
    fi
fi
if { [ -n "$TLS_CERT_SOURCE" ] && [ -z "$TLS_KEY_SOURCE" ]; } || \
   { [ -z "$TLS_CERT_SOURCE" ] && [ -n "$TLS_KEY_SOURCE" ]; }; then
    echo "--tls-cert-file and --tls-key-file must be provided together" >&2
    exit 2
fi
if [ -n "$SERVER_CONFIG_SOURCE" ] && [ ! -s "$SERVER_CONFIG_SOURCE" ]; then
    echo "Server configuration file is missing or empty: $SERVER_CONFIG_SOURCE" >&2
    exit 2
fi
if [ -n "$SERVER_CONFIG_SOURCE" ] && ! HOMEKIT_INSPECTOR_SERVER_CONFIG="$SERVER_CONFIG_SOURCE" \
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$SCRIPTPATH" python3 -c \
    'from scripts.serve_inspector import build_auth_header; build_auth_header()'; then
    echo "Invalid server configuration: $SERVER_CONFIG_SOURCE" >&2
    exit 2
fi
for tls_file in "$TLS_CERT_SOURCE" "$TLS_KEY_SOURCE"; do
    if [ -n "$tls_file" ] && [ ! -s "$tls_file" ]; then
        echo "TLS file is missing or empty: $tls_file" >&2
        exit 2
    fi
done
if [ -n "$TLS_CERT_SOURCE" ]; then
    if ! python3 -c '
import ssl
import sys

context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.minimum_version = ssl.TLSVersion.TLSv1_2
context.load_cert_chain(sys.argv[1], sys.argv[2])
' "$TLS_CERT_SOURCE" "$TLS_KEY_SOURCE"; then
        echo "TLS certificate and private key could not be loaded" >&2
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

if [ "$DEVELOPMENT" = false ]; then
    case "$HOST" in
        localhost|127.0.0.1|::1) ;;
        *)
            if [ -z "$TLS_CERT_SOURCE" ] && \
               { [ ! -f "$INSTALL_DIR/server-cert.pem" ] || [ ! -f "$INSTALL_DIR/server-key.pem" ]; }; then
                echo "Network serving requires --tls-cert-file and --tls-key-file" >&2
                exit 2
            fi
            ;;
    esac
fi

if [ "$DEVELOPMENT" = true ]; then
    PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-/tmp/homekit-inspector-pycache}" \
        python3 -m py_compile "$SCRIPTPATH/scripts/serve_inspector.py"
    echo "Development validation complete"
    exit 0
fi

if [ "$(id -u "$INSTALL_USER")" -eq 0 ]; then
    echo "Refusing to run HomeKit Inspector server as root" >&2
    exit 2
fi

install -d -m 0755 -o 0 -g 0 "$INSTALL_DIR"
install -d -m 0750 -o "$INSTALL_USER" -g "$INSTALL_GROUP" "$DATA_DIR"
install -d -m 0750 -o 0 -g "$INSTALL_GROUP" "$CONFIG_DIR"
install -d -m 0755 -o 0 -g 0 "$INSTALL_DIR/scripts"
install -m 0755 -o 0 -g 0 "$SCRIPTPATH/scripts/serve_inspector.py" "$INSTALL_DIR/scripts/serve_inspector.py"

PUBLISH_SECRET_PATH="$INSTALL_DIR/publish-secret"
if [ -n "$PUBLISH_SECRET_SOURCE" ]; then
    install -m 0640 -o 0 -g "$INSTALL_GROUP" "$PUBLISH_SECRET_SOURCE" "$PUBLISH_SECRET_PATH"
fi
if [ ! -f "$PUBLISH_SECRET_PATH" ]; then
    PUBLISH_SECRET_PATH=""
else
    chown 0:"$INSTALL_GROUP" "$PUBLISH_SECRET_PATH"
    chmod 0640 "$PUBLISH_SECRET_PATH"
fi

TLS_CERT_PATH="$INSTALL_DIR/server-cert.pem"
TLS_KEY_PATH="$INSTALL_DIR/server-key.pem"
if [ -n "$TLS_CERT_SOURCE" ]; then
    install -m 0644 -o 0 -g 0 "$TLS_CERT_SOURCE" "$TLS_CERT_PATH"
    install -m 0640 -o 0 -g "$INSTALL_GROUP" "$TLS_KEY_SOURCE" "$TLS_KEY_PATH"
fi
if [ ! -f "$TLS_CERT_PATH" ] || [ ! -f "$TLS_KEY_PATH" ]; then
    TLS_CERT_PATH=""
    TLS_KEY_PATH=""
else
    chown 0:0 "$TLS_CERT_PATH"
    chmod 0644 "$TLS_CERT_PATH"
    chown 0:"$INSTALL_GROUP" "$TLS_KEY_PATH"
    chmod 0640 "$TLS_KEY_PATH"
fi

SERVER_CONFIG_PATH="$CONFIG_DIR/server.json"
if [ -n "$SERVER_CONFIG_SOURCE" ]; then
    install -m 0640 -o 0 -g "$INSTALL_GROUP" "$SERVER_CONFIG_SOURCE" "$SERVER_CONFIG_PATH"
elif [ ! -f "$SERVER_CONFIG_PATH" ]; then
    SERVER_CONFIG_TEMP=$(mktemp)
    printf '%s\n' '{"viewer":{"username":"admin","password":"admin"}}' > "$SERVER_CONFIG_TEMP"
    install -m 0640 -o 0 -g "$INSTALL_GROUP" "$SERVER_CONFIG_TEMP" "$SERVER_CONFIG_PATH"
    rm "$SERVER_CONFIG_TEMP"
fi
chown 0:"$INSTALL_GROUP" "$SERVER_CONFIG_PATH"
chmod 0640 "$SERVER_CONFIG_PATH"
if ! HOMEKIT_INSPECTOR_SERVER_CONFIG="$SERVER_CONFIG_PATH" \
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$SCRIPTPATH" \
    python3 -c 'from scripts.serve_inspector import build_auth_header; build_auth_header()' \
    2>/dev/null; then
    echo "Invalid server configuration: $SERVER_CONFIG_PATH" >&2
    exit 2
fi
if [ -z "$TLS_CERT_PATH" ]; then
    case "$HOST" in
        localhost|127.0.0.1|::1) ;;
        *)
            echo "Network serving requires TLS unless --host is loopback-only" >&2
            exit 2
            ;;
    esac
fi

SERVICE_FILE=$(mktemp)
ACTIVE_SERVER_CONFIG_PATH="$SERVER_CONFIG_PATH"
if [ "$ALLOW_UNAUTHENTICATED_VIEW" = true ]; then
    ACTIVE_SERVER_CONFIG_PATH=""
fi
sed \
    -e "s|^User=.*|User=$INSTALL_USER|g" \
    -e "s|^Group=.*|Group=$INSTALL_GROUP|g" \
    -e "s|^WorkingDirectory=.*|WorkingDirectory=$INSTALL_DIR|g" \
    -e "s|^Environment=HOMEKIT_INSPECTOR_HOST=.*|Environment=HOMEKIT_INSPECTOR_HOST=$HOST|g" \
    -e "s|^Environment=HOMEKIT_INSPECTOR_PORT=.*|Environment=HOMEKIT_INSPECTOR_PORT=$PORT|g" \
    -e "s|^Environment=HOMEKIT_INSPECTOR_ROOT=.*|Environment=HOMEKIT_INSPECTOR_ROOT=$DATA_DIR|g" \
    -e "s|^Environment=HOMEKIT_INSPECTOR_PUBLISH_SECRET_FILE=.*|Environment=HOMEKIT_INSPECTOR_PUBLISH_SECRET_FILE=$PUBLISH_SECRET_PATH|g" \
    -e "s|^Environment=HOMEKIT_INSPECTOR_MAX_UPLOAD_BYTES=.*|Environment=HOMEKIT_INSPECTOR_MAX_UPLOAD_BYTES=$MAX_UPLOAD_BYTES|g" \
    -e "s|^Environment=HOMEKIT_INSPECTOR_TLS_CERT_FILE=.*|Environment=HOMEKIT_INSPECTOR_TLS_CERT_FILE=$TLS_CERT_PATH|g" \
    -e "s|^Environment=HOMEKIT_INSPECTOR_TLS_KEY_FILE=.*|Environment=HOMEKIT_INSPECTOR_TLS_KEY_FILE=$TLS_KEY_PATH|g" \
    -e "s|^Environment=HOMEKIT_INSPECTOR_SERVER_CONFIG=.*|Environment=HOMEKIT_INSPECTOR_SERVER_CONFIG=$ACTIVE_SERVER_CONFIG_PATH|g" \
    -e "s|^Environment=HOMEKIT_INSPECTOR_ALLOW_UNAUTHENTICATED_VIEW=.*|Environment=HOMEKIT_INSPECTOR_ALLOW_UNAUTHENTICATED_VIEW=$ALLOW_UNAUTHENTICATED_VIEW|g" \
    -e "s|^ExecStart=.*|ExecStart=/usr/bin/python3 $INSTALL_DIR/scripts/serve_inspector.py|g" \
    "$SCRIPTPATH/homekit-inspector.service" > "$SERVICE_FILE"

install -m 0644 "$SERVICE_FILE" /etc/systemd/system/homekit-inspector.service
rm "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable homekit-inspector.service

echo "Installed homekit-inspector.service"
if [ -n "$PUBLISH_SECRET_PATH" ]; then
    echo "Signed report publication API enabled"
else
    echo "Report publication API disabled; rerun with --publish-secret-file FILE to enable it"
fi
if [ -n "$TLS_CERT_PATH" ]; then
    echo "TLS enabled"
else
    echo "TLS disabled"
fi
if [ "$ALLOW_UNAUTHENTICATED_VIEW" = true ]; then
    echo "Viewer authentication explicitly disabled"
else
    echo "Viewer authentication enabled using $SERVER_CONFIG_PATH"
    if grep -q '"username"[[:space:]]*:[[:space:]]*"admin"' "$SERVER_CONFIG_PATH" && \
       grep -q '"password"[[:space:]]*:[[:space:]]*"admin"' "$SERVER_CONFIG_PATH"; then
        echo "WARNING: change the default admin/admin credentials before exposing the server outside a trusted network" >&2
    fi
fi
echo "Place a generated homekit_inspector.html into $DATA_DIR, then run:"
echo "  sudo systemctl restart homekit-inspector.service"

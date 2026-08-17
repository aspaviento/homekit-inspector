#!/usr/bin/env bash
set -euo pipefail

HOSTNAME=""
OUTPUT_DIR=""
DAYS="825"

usage() {
    cat <<EOF
Usage: scripts/create-server-credentials.sh --hostname HOST --output-dir DIR [options]

Options:
  --hostname HOST   DNS name or IP address used to reach the server
  --output-dir DIR  New or empty private credential directory
  --days N          Server certificate validity in days (default: 825)
  -h, --help        Show this help
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --hostname) shift; HOSTNAME="$1" ;;
        --output-dir) shift; OUTPUT_DIR="$1" ;;
        --days) shift; DAYS="$1" ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if [ -z "$HOSTNAME" ] || [ -z "$OUTPUT_DIR" ]; then
    usage >&2
    exit 2
fi
if ! [[ "$DAYS" =~ ^[1-9][0-9]*$ ]]; then
    echo "--days must be a positive integer" >&2
    exit 2
fi
if ! command -v openssl >/dev/null 2>&1; then
    echo "OpenSSL is required" >&2
    exit 127
fi

SAN=$(
    python3 -c '
import ipaddress
import re
import sys

value = sys.argv[1]
try:
    ipaddress.ip_address(value)
except ValueError:
    if not re.fullmatch(r"[A-Za-z0-9.-]+", value):
        raise SystemExit("Unsupported hostname")
    print("DNS:" + value)
else:
    print("IP:" + value)
' "$HOSTNAME"
)

OUTPUT_DIR=$(python3 -c 'import os,sys; print(os.path.realpath(os.path.expanduser(sys.argv[1])))' "$OUTPUT_DIR")
if [ -e "$OUTPUT_DIR" ] && [ -n "$(find "$OUTPUT_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
    echo "Credential directory must be new or empty: $OUTPUT_DIR" >&2
    exit 2
fi

umask 077
mkdir -p "$OUTPUT_DIR"
WORK_DIR=$(mktemp -d "$OUTPUT_DIR/.generate.XXXXXX")
cleanup() {
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT HUP INT TERM

openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 \
    -out "$WORK_DIR/ca-key.pem"
openssl req -x509 -new -sha256 -key "$WORK_DIR/ca-key.pem" \
    -days 3650 -subj "/CN=HomeKit Inspector Local CA" \
    -out "$WORK_DIR/ca.pem"
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 \
    -out "$WORK_DIR/server-key.pem"
openssl req -new -sha256 -key "$WORK_DIR/server-key.pem" \
    -subj "/CN=$HOSTNAME" -out "$WORK_DIR/server.csr"

cat > "$WORK_DIR/server-extensions.cnf" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=$SAN
EOF

openssl x509 -req -sha256 -in "$WORK_DIR/server.csr" \
    -CA "$WORK_DIR/ca.pem" -CAkey "$WORK_DIR/ca-key.pem" -CAcreateserial \
    -days "$DAYS" -extfile "$WORK_DIR/server-extensions.cnf" \
    -out "$WORK_DIR/server-cert.pem"
openssl rand -hex 32 > "$WORK_DIR/publish-secret"
printf '%s\n' '{"viewer":{"username":"admin","password":"admin"}}' > "$WORK_DIR/server.json"

install -m 0600 "$WORK_DIR/ca-key.pem" "$OUTPUT_DIR/ca-key.pem"
install -m 0600 "$WORK_DIR/ca.pem" "$OUTPUT_DIR/ca.pem"
install -m 0600 "$WORK_DIR/server-key.pem" "$OUTPUT_DIR/server-key.pem"
install -m 0644 "$WORK_DIR/server-cert.pem" "$OUTPUT_DIR/server-cert.pem"
install -m 0600 "$WORK_DIR/publish-secret" "$OUTPUT_DIR/publish-secret"
install -m 0600 "$WORK_DIR/server.json" "$OUTPUT_DIR/server.json"

echo "Created HomeKit Inspector server credentials in $OUTPUT_DIR"
echo "Keep ca-key.pem offline and do not copy it to the server."
echo "Use ca.pem as publish.caFile on the macOS client."
echo "WARNING: server.json uses admin/admin; change it before exposing the server outside a trusted network."

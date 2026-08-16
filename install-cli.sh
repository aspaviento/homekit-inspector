#!/bin/sh
set -eu

SOURCE_ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
APP_HOME=${HOMEKIT_INSPECTOR_HOME:-"$HOME/Library/Application Support/HomeKit Inspector"}
BIN_DIR=${HOMEKIT_INSPECTOR_BIN_DIR:-"$HOME/.local/bin"}
PYTHON_BIN=${HOMEKIT_INSPECTOR_PYTHON:-python3}
CONFIG_SOURCE=
REPLACE_CONFIG=false

usage() {
    cat <<EOF
Usage: ./install-cli.sh [options]

Options:
  --app-home DIR       Private application directory
  --bin-dir DIR        Command directory (default: ~/.local/bin)
  --config FILE        Migrate a private refresh configuration and its inputs
  --replace-config     Replace an existing installed configuration
  -h, --help           Show this help

Environment:
  HOMEKIT_INSPECTOR_PYTHON   Python 3.9+ executable
  HOMEKIT_INSPECTOR_HOME     Default application directory
  HOMEKIT_INSPECTOR_BIN_DIR  Default command directory
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --app-home) shift; APP_HOME=$1 ;;
        --bin-dir) shift; BIN_DIR=$1 ;;
        --config) shift; CONFIG_SOURCE=$1 ;;
        --replace-config) REPLACE_CONFIG=true ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "Python executable not found: $PYTHON_BIN" >&2
    exit 127
fi
if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'; then
    echo "HomeKit Inspector requires Python 3.9 or later: $PYTHON_BIN" >&2
    exit 2
fi

APP_HOME=$("$PYTHON_BIN" -c 'import os,sys; print(os.path.realpath(os.path.expanduser(sys.argv[1])))' "$APP_HOME")
BIN_DIR=$("$PYTHON_BIN" -c 'import os,sys; print(os.path.realpath(os.path.expanduser(sys.argv[1])))' "$BIN_DIR")
CANONICAL_HOME=$("$PYTHON_BIN" -c 'import os; print(os.path.realpath(os.path.expanduser("~")))')
case "$APP_HOME" in
    /|"$CANONICAL_HOME"|"$CANONICAL_HOME/Library"|"$CANONICAL_HOME/Library/Application Support")
        echo "Refusing unsafe application directory: $APP_HOME" >&2
        exit 2
        ;;
esac

REQUIRED_FILES="
bin/homekit-inspector
scripts/generate_condition_diagnostics.py
scripts/generate_homekit_reports.py
scripts/generate_inspector.py
scripts/homed_extract.py
scripts/homekit_inspector_cli.py
scripts/install_refresh_config.py
"
for relative_path in $REQUIRED_FILES; do
    if [ ! -f "$SOURCE_ROOT/$relative_path" ]; then
        echo "Missing required file: $relative_path" >&2
        exit 1
    fi
done

install -d -m 0700 "$APP_HOME"
STAGING_DIR=$(mktemp -d "$APP_HOME/.app-install.XXXXXX")
cleanup() {
    rm -rf "$STAGING_DIR"
}
trap cleanup EXIT HUP INT TERM

install -d -m 0755 "$STAGING_DIR/bin" "$STAGING_DIR/scripts"
install -m 0755 "$SOURCE_ROOT/bin/homekit-inspector" "$STAGING_DIR/bin/homekit-inspector"
for script in generate_condition_diagnostics.py generate_homekit_reports.py generate_inspector.py homed_extract.py homekit_inspector_cli.py install_refresh_config.py; do
    install -m 0755 "$SOURCE_ROOT/scripts/$script" "$STAGING_DIR/scripts/$script"
done

PYTHONPYCACHEPREFIX="${TMPDIR:-/tmp}/homekit-inspector-install-pycache" \
    "$PYTHON_BIN" -m py_compile "$STAGING_DIR"/scripts/*.py

APP_DIR="$APP_HOME/app"
PREVIOUS_DIR="$APP_HOME/.app-previous.$$"
if [ -d "$APP_DIR" ]; then
    mv "$APP_DIR" "$PREVIOUS_DIR"
fi
if ! mv "$STAGING_DIR" "$APP_DIR"; then
    if [ -d "$PREVIOUS_DIR" ]; then
        mv "$PREVIOUS_DIR" "$APP_DIR"
    fi
    exit 1
fi
STAGING_DIR="$APP_HOME/.staging-complete.$$"
if [ -d "$PREVIOUS_DIR" ]; then
    rm -rf "$PREVIOUS_DIR"
fi

install -d -m 0755 "$BIN_DIR"
LINK_TMP="$BIN_DIR/.homekit-inspector-link.$$"
ln -s "$APP_DIR/bin/homekit-inspector" "$LINK_TMP"
mv -f "$LINK_TMP" "$BIN_DIR/homekit-inspector"

CONFIG_PATH="$APP_HOME/config.json"
if [ -n "$CONFIG_SOURCE" ]; then
    if [ "$REPLACE_CONFIG" = true ]; then
        "$PYTHON_BIN" "$APP_DIR/scripts/install_refresh_config.py" \
            --source "$CONFIG_SOURCE" --destination "$CONFIG_PATH" --replace
    else
        "$PYTHON_BIN" "$APP_DIR/scripts/install_refresh_config.py" \
            --source "$CONFIG_SOURCE" --destination "$CONFIG_PATH"
    fi
fi

echo "Installed HomeKit Inspector CLI"
echo "  Command: $BIN_DIR/homekit-inspector"
echo "  Application: $APP_DIR"
echo "  Configuration: $CONFIG_PATH"
if [ ! -f "$CONFIG_PATH" ]; then
    echo "  Configuration is not installed; rerun with --config FILE"
fi
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "  Add $BIN_DIR to PATH to run homekit-inspector directly" ;;
esac

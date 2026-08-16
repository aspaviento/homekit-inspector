#!/bin/sh
set -eu

APP_HOME=${HOMEKIT_INSPECTOR_HOME:-"$HOME/Library/Application Support/HomeKit Inspector"}
BIN_DIR=${HOMEKIT_INSPECTOR_BIN_DIR:-"$HOME/.local/bin"}
PYTHON_BIN=${HOMEKIT_INSPECTOR_PYTHON:-python3}
REMOVE_PRIVATE_DATA=false

usage() {
    cat <<EOF
Usage: ./uninstall-cli.sh [options]

Options:
  --app-home DIR         Private application directory
  --bin-dir DIR          Command directory (default: ~/.local/bin)
  --remove-private-data  Also remove configuration, outputs, and status
  -h, --help             Show this help
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --app-home) shift; APP_HOME=$1 ;;
        --bin-dir) shift; BIN_DIR=$1 ;;
        --remove-private-data) REMOVE_PRIVATE_DATA=true ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "Python executable not found: $PYTHON_BIN" >&2
    exit 127
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

COMMAND_PATH="$BIN_DIR/homekit-inspector"
if [ -L "$COMMAND_PATH" ]; then
    LINK_TARGET=$(readlink "$COMMAND_PATH")
    case "$LINK_TARGET" in
        "$APP_HOME"/*) rm "$COMMAND_PATH" ;;
        *) echo "Preserving unrelated symlink: $COMMAND_PATH" >&2 ;;
    esac
fi

if [ -d "$APP_HOME/app" ]; then
    rm -rf "$APP_HOME/app"
fi

if [ "$REMOVE_PRIVATE_DATA" = true ]; then
    if [ -d "$APP_HOME" ]; then
        rm -rf "$APP_HOME"
    fi
    echo "Removed HomeKit Inspector CLI and private data"
else
    echo "Removed HomeKit Inspector CLI"
    echo "Preserved private data in: $APP_HOME"
fi

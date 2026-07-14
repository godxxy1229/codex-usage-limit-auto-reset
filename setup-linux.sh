#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON_BIN=${PYTHON:-python3}

exec "$PYTHON_BIN" -I "$SCRIPT_DIR/install_linux.py" "$@"

#!/usr/bin/env bash
set -eu
cd -- "$(dirname -- "$0")"
exec "${PYTHON:-python3}" main.py "$@"

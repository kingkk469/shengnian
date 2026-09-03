#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
    echo "请先运行：bash setup-macos.command"
    exit 1
fi
exec .venv/bin/python macos/configure.py

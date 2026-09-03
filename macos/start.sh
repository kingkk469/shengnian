#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "$(uname -s)" != Darwin ]]; then
    echo "此启动脚本只适用于 macOS。"
    exit 1
fi
if [[ ! -x .venv/bin/python ]]; then
    echo "请先运行：bash setup-macos.command"
    exit 1
fi
export PATH="$PWD/.venv/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export PYTHONIOENCODING=utf-8
exec .venv/bin/python src/launcher.py "$@"

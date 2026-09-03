#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "$(uname -s)" != Darwin ]]; then
    echo "此安装脚本只适用于 macOS。Windows 请使用 setup.ps1。"
    exit 1
fi
if [[ "$(uname -m)" != arm64 ]]; then
    echo "本轮测试版面向苹果芯片 Mac。请关闭终端的 Rosetta 模式；Intel Mac 尚未验证。"
    exit 1
fi
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! command -v brew >/dev/null 2>&1; then
    echo "请先按 https://brew.sh/ 的官方说明安装 Homebrew，再重新运行此脚本。"
    exit 1
fi
brew install python@3.12 ffmpeg
python_bin="$(brew --prefix python@3.12)/bin/python3.12"
"$python_bin" -c 'import platform, sys; assert sys.version_info[:2] == (3, 12); assert platform.machine() == "arm64", "需要原生 arm64 Python"'
if [[ ! -d .venv ]]; then
    "$python_bin" -m venv .venv
fi
if [[ ! -x .venv/bin/python ]]; then
    echo "已有 .venv 不是有效 Mac 环境。请使用新的源码目录安装，避免覆盖已有环境。"
    exit 1
fi
.venv/bin/python -c 'import platform, sys; assert sys.version_info[:2] == (3, 12); assert platform.machine() == "arm64", "已有虚拟环境不匹配，请使用新的源码目录"'
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r macos/requirements-resolved.txt
.venv/bin/python -m pip check
.venv/bin/python macos/initialize.py
chmod +x setup-macos.command start-macos.command configure-macos.command
echo ""
echo "依赖安装完成。首次转写还会下载 FunASR 模型。"
echo "1. 配置 API（可稍后做）：bash configure-macos.command"
echo "2. 检查环境：.venv/bin/python macos/check.py"
echo "3. 启动声年：bash start-macos.command"

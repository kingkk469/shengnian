"""Opt-in login launcher for the source beta; does not start recording."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import sys

LABEL = "com.king.shengnian.source-launcher"


def launch_agent(project: Path) -> dict:
    # Keep the same Terminal microphone permission as the interactive launch.
    return {
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/open", "-a", "Terminal", str(project / "start-macos.command")],
        "RunAtLoad": True,
        "LimitLoadToSessionType": "Aqua",
    }


def main():
    parser = argparse.ArgumentParser(description="声年源码测试版登录自启（只打开窗口，不自动录音）")
    parser.add_argument("action", choices=["install", "uninstall"])
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise SystemExit("仅支持 macOS")
    project = Path(__file__).resolve().parents[1]
    target = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    if target.exists():
        existing = plistlib.loads(target.read_bytes())
        if existing.get("Label") != LABEL:
            raise SystemExit("同名文件不属于声年，未修改。")
        subprocess.run(["/bin/launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
    if args.action == "uninstall":
        target.unlink(missing_ok=True)
        print("已取消登录自启。声年数据与当前窗口均保留。")
        return
    if not (project / ".venv/bin/python").is_file():
        raise SystemExit("请先完成 Mac 安装。")
    command = project / "start-macos.command"
    command.chmod(command.stat().st_mode | 0o100)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(plistlib.dumps(launch_agent(project)))
    target.chmod(0o600)
    subprocess.run(["/bin/launchctl", "bootstrap", domain, str(target)], check=True)
    print("已设置登录时通过终端打开声年；录音仍需点击开始。移动源码目录后请重新设置。")


if __name__ == "__main__":
    main()

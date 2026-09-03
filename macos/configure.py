"""Store user-provided keys locally; never put them in argv or shell profiles."""
from __future__ import annotations

import getpass
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from api_settings import save_key


def main():
    if sys.platform != "darwin":
        raise SystemExit("此配置向导只用于 Mac。Windows 请继续按 README 配置环境变量。")
    from common import ROOT
    path = ROOT / "runtime" / "api-keys.json"
    print("API Key 仅保存在本机文件（仅当前用户可读写）。输入不会显示；直接回车保留原值。")
    for name, label in (("DEEPSEEK_API_KEY", "DeepSeek API Key"), ("SNAPANY_API_KEY", "SnapAny API Key（可选）")):
        value = getpass.getpass(f"{label}：").strip()
        if value:
            save_key(path, name, value)
            print(f"{label} 已保存。")
    print("配置完成，请重新启动声年。")


if __name__ == "__main__":
    main()

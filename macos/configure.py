"""Store user-provided keys locally; never put them in argv or shell profiles."""
from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def save_key(path: Path, name: str, value: str) -> None:
    if name not in {"DEEPSEEK_API_KEY", "SNAPANY_API_KEY"}:
        raise ValueError("不支持的 API 类型")
    payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(payload, dict):
        raise ValueError("已有 API 配置格式异常，请先检查原文件；未覆盖")
    payload[name] = value.strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".api-keys-", suffix=".json", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        temp_path.chmod(0o600)
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


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

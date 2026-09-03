"""Initialize a new Mac source install without replacing user configuration."""
from pathlib import Path
import shutil
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))


def main():
    target = PROJECT / "src" / "config.toml"
    if not target.exists():
        shutil.copy2(PROJECT / "src" / "config.example.toml", target)
    from common import ROOT
    target = ROOT / "hotwords.txt"
    if not target.exists():
        shutil.copy2(PROJECT / "hotwords.example.txt", target)
    print(f"数据目录：{ROOT}")


if __name__ == "__main__":
    main()

"""从 DJI Mic TX 机身导入录音。

用法:
    python importer.py            # 自动找含 DJI_AUDIO 的盘符
    python importer.py E:\\        # 指定盘符
    python importer.py --dry-run   # 只列出会导入哪些文件

策略:
- 找 <盘符>\\DJI_AUDIO\\**\\*.WAV (大小写均可)
- 按文件修改时间归到 raw\\YYYY-MM-DD\\imported\\<原文件名>
- 已经导入过(目标位置存在)就跳过
- 不删除源文件(DJI TX 自管空间)
- 复制完触发的转写由 transcriber.py 的 watchdog 自动接管
"""
from __future__ import annotations

import argparse
import datetime as dt
import shutil
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, day_dir, setup_logger

log = setup_logger("importer")


def find_dji_drive() -> list[Path]:
    """扫所有盘符,返回含 DJI_AUDIO 目录的盘符列表。"""
    if sys.platform == "darwin":
        volumes = Path("/Volumes")
        return sorted(d for d in volumes.iterdir() if (d / "DJI_AUDIO").is_dir()) if volumes.is_dir() else []
    hits = []
    for letter in string.ascii_uppercase:
        d = Path(f"{letter}:\\")
        if not d.exists():
            continue
        dji = d / "DJI_AUDIO"
        if dji.is_dir():
            hits.append(d)
    return hits


def list_wavs(drive: Path) -> list[Path]:
    return sorted((drive / "DJI_AUDIO").rglob("*.[wW][aA][vV]"))


def import_wavs(drive: Path, dry_run: bool = False) -> int:
    n = 0
    for src in list_wavs(drive):
        mtime = dt.datetime.fromtimestamp(src.stat().st_mtime).date()
        dest_dir = day_dir("raw", mtime) / "imported"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        if dest.exists() and dest.stat().st_size == src.stat().st_size:
            log.info("跳过(已存在): %s", dest.name)
            continue
        if dry_run:
            log.info("[dry-run] 将复制 %s -> %s", src, dest)
        else:
            shutil.copy2(src, dest)
            log.info("复制 %s -> %s (%.1f MB)", src.name, dest, dest.stat().st_size / 1024 / 1024)
        n += 1
    return n


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("drive", nargs="?", default=None, help="挂载目录,如 E:\\ 或 /Volumes/DJI")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.drive:
        drives = [Path(args.drive)]
    else:
        drives = find_dji_drive()
        if not drives:
            log.error("未找到含 DJI_AUDIO 的设备。请用 python importer.py 指定设备挂载目录")
            sys.exit(1)
        log.info("自动检测到 DJI 盘符: %s", [str(d) for d in drives])

    total = 0
    for d in drives:
        total += import_wavs(d, args.dry_run)
    log.info("完成,共处理 %d 个文件", total)


if __name__ == "__main__":
    main()

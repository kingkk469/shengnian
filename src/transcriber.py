"""funasr Paraformer-zh 转写常驻服务。

- 启动加载模型 → 常驻内存（Mac 测试版默认 CPU）
- watchdog 监听 raw/ 目录的新 .wav
- 转写后 append 一行到 transcripts/YYYY-MM-DD.jsonl
- 失败不阻塞后续段(写 error 字段)

使用安装了 FunASR/torch 的 Python 3.12 环境运行。Mac 安装方式见 macos/README.md。
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path

# 让 common.py 能被 import
sys.path.insert(0, str(Path(__file__).resolve().parent))

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from platform_support import RoleLock, install_worker_signals, model_device_kwargs

from common import (
    CONFIG,
    RESOURCE_ROOT,
    ROOT,
    append_jsonl,
    iso_now,
    load_speakers,
    setup_logger,
    transcript_path,
)

log = setup_logger("transcriber")

_model = None
_sv_model = None
_processing: set[str] = set()   # 正在处理中（内存锁，防并发）
_done: set[str] = set()          # 已写入 jsonl（持久化前的内存缓存）


def _model_path(slot: str, configured: str) -> str:
    """离线商业包优先使用随应用分发的模型；源码版保留模型 ID 回退。"""
    bundled = RESOURCE_ROOT / "models" / slot
    marker = (
        bundled / "campplus_cn_common.bin"
        if slot == "speaker"
        else bundled / "model.pt"
    )
    if bundled.exists() and marker.exists():
        return str(bundled)
    return configured


def get_model():
    global _model
    if _model is None:
        log.info("加载 funasr ASR 模型...")
        from funasr import AutoModel
        # FunASR 1.3.x 会在包初始化时递归导入所有子模块。PyInstaller
        # 冻结环境下可能先注册一个尚未执行到底的 CharTokenizer，导致
        # load_seg_dict/seg_tokenize 尚未进入模块全局命名空间。只在检测到
        # 该不完整状态时重载一次，让注册表指向完整实现。
        from funasr.tokenizer import char_tokenizer

        if not hasattr(char_tokenizer, "load_seg_dict"):
            import re

            def _load_seg_dict(seg_dict_file):
                seg_dict = {}
                with open(seg_dict_file, "r", encoding="utf8") as f:
                    for line in f:
                        fields = line.strip().split()
                        if fields:
                            seg_dict[fields[0]] = " ".join(fields[1:])
                return seg_dict

            def _seg_tokenize(words, seg_dict):
                pattern = re.compile(r"([\u4E00-\u9FA5A-Za-z0-9])")
                output = []
                for word in words:
                    word = word.lower()
                    if word in seg_dict:
                        output.extend(seg_dict[word].split())
                    elif pattern.match(word):
                        for char in word:
                            output.extend(seg_dict.get(char, "<unk>").split())
                    else:
                        output.append("<unk>")
                return output

            char_tokenizer.load_seg_dict = _load_seg_dict
            char_tokenizer.seg_tokenize = _seg_tokenize
        cfg = CONFIG["transcriber"]
        asr_model = _model_path("asr", cfg["asr_model"])
        vad_model = _model_path("vad", cfg["vad_model"])
        punc_model = _model_path("punc", cfg["punc_model"])
        if Path(asr_model).is_absolute():
            log.info("使用安装包内置离线模型: %s", RESOURCE_ROOT / "models")
        _model = AutoModel(
            model=asr_model,
            vad_model=vad_model,
            punc_model=punc_model,
            disable_update=True,
            **model_device_kwargs(cfg),
        )
        log.info("ASR 模型加载完成")
    return _model


def get_sv_model():
    global _sv_model
    if _sv_model is None:
        log.info("加载 CAM++ 声纹模型...")
        from funasr import AutoModel
        speaker_model = _model_path("speaker", CONFIG["speaker"]["sv_model"])
        if Path(speaker_model).is_absolute():
            log.info("使用安装包内置声纹模型: %s", speaker_model)
        _sv_model = AutoModel(model=speaker_model, disable_update=True,
                             **model_device_kwargs(CONFIG["transcriber"]))
        log.info("声纹模型加载完成")
    return _sv_model


def extract_embedding(wav_path: Path):
    """返回归一化后的 192 维 numpy 向量,失败返回 None。"""
    try:
        import numpy as np
        res = get_sv_model().generate(input=str(wav_path))
        if not res:
            return None
        emb = res[0].get("spk_embedding")
        if emb is None:
            return None
        v = emb.cpu().numpy().squeeze()
        norm = np.linalg.norm(v)
        if norm < 1e-6:
            return None
        return v / norm
    except Exception as e:
        log.warning("抽 embedding 失败 %s: %s", wav_path.name, e)
        return None


def match_speaker(emb, speakers: list[dict]) -> tuple[str | None, str | None, float]:
    """匹配声纹库,返回 (speaker_id, speaker_name, similarity)。
    没有匹配返回 (None, None, 最高相似度);相似度 < threshold 算未匹配。"""
    if emb is None or not speakers:
        return None, None, 0.0
    import numpy as np
    best_sim = -1.0
    best_sp = None
    for sp in speakers:
        ref = np.array(sp["embedding"], dtype=np.float32)
        sim = float(np.dot(emb, ref))
        if sim > best_sim:
            best_sim = sim
            best_sp = sp
    threshold = CONFIG["speaker"]["match_threshold"]
    if best_sim >= threshold and best_sp is not None:
        return best_sp["id"], best_sp["name"], best_sim
    return None, None, best_sim


def _load_hotwords() -> str:
    """从 hotwords.txt 读热词,返回空格分隔的字符串。"""
    p = ROOT / "hotwords.txt"
    if not p.exists():
        return ""
    words = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            words.append(line)
    return " ".join(words)


_HOTWORDS_CACHE: str | None = None
_CORRECTIONS_CACHE: dict[str, str] | None = None


def get_hotwords() -> str:
    global _HOTWORDS_CACHE
    if _HOTWORDS_CACHE is None:
        _HOTWORDS_CACHE = _load_hotwords()
        if _HOTWORDS_CACHE:
            log.info("已加载 %d 个热词", len(_HOTWORDS_CACHE.split()))
    return _HOTWORDS_CACHE


def _load_corrections() -> dict[str, str]:
    """从 corrections.json 读同音字纠正表。"""
    import json
    p = ROOT / "corrections.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("replacements", {})
    except Exception as e:
        log.warning("读 corrections.json 失败: %s", e)
        return {}


def get_corrections() -> dict[str, str]:
    global _CORRECTIONS_CACHE
    if _CORRECTIONS_CACHE is None:
        _CORRECTIONS_CACHE = _load_corrections()
        if _CORRECTIONS_CACHE:
            log.info("已加载 %d 条同音字纠正", len(_CORRECTIONS_CACHE))
    return _CORRECTIONS_CACHE


def _apply_corrections(text: str) -> str:
    """跑一遍同音字替换表。"""
    if not text:
        return text
    corrections = get_corrections()
    for wrong, right in corrections.items():
        if wrong in text:
            text = text.replace(wrong, right)
    return text


def transcribe_wav_detailed(wav_path: Path) -> tuple[str, list]:
    """转写并保留模型返回的相对时间信息。

    时间戳只写入本地 JSONL，当前界面不展示；后续可以用于“定位原音”。
    """
    cfg = CONFIG["transcriber"]
    kwargs = {"input": str(wav_path), "batch_size_s": cfg["batch_size_s"]}
    hot = get_hotwords()
    if hot:
        kwargs["hotword"] = hot
    result = get_model().generate(**kwargs)
    if not result:
        return "", []
    item = result[0]
    text = item.get("text", "") if isinstance(item, dict) else str(item)
    timestamps = item.get("timestamp", []) if isinstance(item, dict) else []
    if not isinstance(timestamps, list):
        timestamps = []
    # 跑同音字纠正
    return _apply_corrections(text), timestamps


def transcribe_wav(wav_path: Path) -> str:
    text, _timestamps = transcribe_wav_detailed(wav_path)
    return text


def _parse_day_from_path(wav_path: Path) -> dt.date:
    """raw/YYYY-MM-DD/HH-MM-SS-NNNN.wav → date。
    路径可能多嵌一层 imported/,所以向上找直到匹配 YYYY-MM-DD。"""
    for parent in wav_path.parents:
        try:
            return dt.date.fromisoformat(parent.name)
        except ValueError:
            continue
    return dt.date.today()


def _wait_until_stable(path: Path, max_wait: float = 5.0) -> bool:
    """等待文件大小稳定(写入完成)。返回 True=稳定;False=超时。"""
    last_size = -1
    start = time.time()
    while time.time() - start < max_wait:
        try:
            size = path.stat().st_size
        except OSError:
            time.sleep(0.1)
            continue
        if size > 0 and size == last_size:
            return True
        last_size = size
        time.sleep(0.2)
    return last_size > 0


def _rel(wav_path: Path) -> str:
    """返回相对于 ROOT 的正斜杠路径（用作去重 key）。"""
    return str(wav_path.relative_to(ROOT)).replace("\\", "/")


def _import_manifest(wav_path: Path) -> dict:
    """读取“导入录音”在 WAV 就绪前写好的本地清单。"""
    path = wav_path.with_suffix(".import.json")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _already_done(wav_path: Path) -> bool:
    """检查这个 WAV 是否已经写入过 jsonl（内存缓存 + 磁盘双重校验）。"""
    key = _rel(wav_path)
    if key in _done or key in _processing:
        return True
    # 磁盘校验：读对应日期的 jsonl
    day = _parse_day_from_path(wav_path)
    from common import read_jsonl
    for rec in read_jsonl(transcript_path(day)):
        wav_field = rec.get("wav", "")
        if _norm(wav_field) == _norm(key):
            _done.add(key)  # 加入内存缓存，下次不再读磁盘
            return True
    return False


def process_file(wav_path: Path, source: str = "live") -> None:
    key = _rel(wav_path)

    # 三层去重：内存处理中 / 内存已完成 / 磁盘 jsonl 已有记录
    if key in _processing:
        return
    if _already_done(wav_path):
        log.debug("跳过已转写 %s", wav_path.name)
        return

    _processing.add(key)
    try:
        if not _wait_until_stable(wav_path):
            log.warning("文件大小未稳定: %s", wav_path)
            return
        day = _parse_day_from_path(wav_path)
        out = transcript_path(day)
        import_meta = _import_manifest(wav_path)
        recorded_at = str(import_meta.get("recorded_at") or "").strip()
        duration = float(import_meta.get("duration_sec") or 0.0)
        if duration <= 0:
            duration = round(wav_path.stat().st_size / (16000 * 2), 2)
        record = {
            "start": recorded_at or iso_now(),
            "wav": key,   # 已是正斜杠
            "source": source,
            "duration_sec": round(duration, 2),
        }
        if import_meta:
            record["original_name"] = import_meta.get("original_name")
            record["original_audio"] = import_meta.get("original_copy")
            record["recording_time_source"] = import_meta.get(
                "recording_time_source"
            )
            record["recording_time_confidence"] = import_meta.get(
                "recording_time_confidence"
            )
            record["relative_timeline_ms"] = import_meta.get(
                "relative_timeline_ms", []
            )
        try:
            text, timestamps = transcribe_wav_detailed(wav_path)
            text_stripped = (text or "").strip()
            if not text_stripped:
                try:
                    wav_path.unlink()
                except OSError:
                    pass
                log.info("跳过空段 %s (已删 WAV)", wav_path.name)
                _done.add(key)
                return
            record["text"] = text_stripped
            if timestamps:
                record["asr_timestamps_ms"] = timestamps
            speaker_enabled = CONFIG["speaker"].get("enabled", True)
            min_dur = CONFIG["speaker"]["min_segment_for_sv"]
            if speaker_enabled and record["duration_sec"] >= min_dur:
                emb = extract_embedding(wav_path)
                if emb is not None:
                    speakers = load_speakers()
                    sp_id, sp_name, sim = match_speaker(emb, speakers)
                    record["speaker_id"] = sp_id
                    record["speaker_name"] = sp_name or "未知"
                    record["speaker_sim"] = round(sim, 3)
                    record["embedding"] = [round(float(x), 4) for x in emb.tolist()]
            elif speaker_enabled:
                record["speaker_name"] = "未知"
                record["speaker_sim"] = 0.0
            log.info("OK %s -> %d 字 [%s sim=%.2f]: %s",
                     wav_path.name, len(text_stripped),
                     record.get("speaker_name", "?"),
                     record.get("speaker_sim", 0),
                     text_stripped[:50])
        except Exception as e:
            record["text"] = None
            record["error"] = repr(e)
            log.error("转写失败 %s: %s", wav_path.name, e)
        append_jsonl(out, record)
        _done.add(key)   # 写完才加入已完成集合
    finally:
        _processing.discard(key)


class Handler(FileSystemEventHandler):
    def _handle(self, path: Path) -> None:
        if path.suffix.lower() != ".wav":
            return
        source = "tx_import" if "imported" in path.parts else "live"
        process_file(path, source)

    def on_created(self, event):
        if not event.is_directory:
            self._handle(Path(event.src_path))

    def on_modified(self, event):
        # recorder 写完 WAV 时会触发 modified；_already_done 会过滤掉已处理的
        if not event.is_directory:
            self._handle(Path(event.src_path))

    def on_moved(self, event):
        if not event.is_directory:
            self._handle(Path(event.dest_path))


def _norm(p: str) -> str:
    """路径统一用正斜杠小写，消除 Windows/Linux 差异。"""
    return p.replace("\\", "/").lower()


def scan_pending() -> None:
    """启动时扫一遍 raw/，把还没在 jsonl 里出现的 wav 补转写。

    路径比对统一用正斜杠小写，避免重启后重复处理已转写的文件。
    """
    raw_root = ROOT / "raw"
    if not raw_root.exists():
        return

    from common import read_jsonl
    transcribed: set[str] = set()
    for jsonl_path in (ROOT / "transcripts").glob("*.jsonl"):
        for rec in read_jsonl(jsonl_path):
            wav_field = rec.get("wav")
            if wav_field:
                transcribed.add(_norm(wav_field))

    pending = []
    for wav in raw_root.rglob("*.wav"):
        rel = _norm(str(wav.relative_to(ROOT)))
        if rel not in transcribed:
            pending.append(wav)

    if pending:
        log.info("启动扫描: %d 个未转写 WAV", len(pending))
        for w in sorted(pending):
            source = "tx_import" if "imported" in w.parts else "live"
            process_file(w, source)
    else:
        log.info("启动扫描: 无遗漏 WAV，全部已转写")


def _run() -> None:
    log.info("transcriber 启动")
    # 预热模型(避免第一段说话才开始下载/加载)
    try:
        get_model()
    except Exception:
        log.exception("ASR 模型初始化失败")
        raise
    scan_pending()

    obs = Observer()
    obs.schedule(Handler(), str(ROOT / "raw"), recursive=True)
    obs.start()
    log.info("watchdog 监听 %s", ROOT / "raw")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("收到 Ctrl+C,退出")
    finally:
        obs.stop()
        obs.join()


def main() -> None:
    lock = RoleLock(ROOT, "transcriber")
    if not lock.acquire():
        log.info("已有 transcriber 实例在运行，本次启动直接退出")
        return
    install_worker_signals()
    try:
        _run()
    except KeyboardInterrupt:
        log.info("转写服务已停止")
    finally:
        lock.release()


if __name__ == "__main__":
    main()

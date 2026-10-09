"""为声年 Windows 发行包生成许可证清单、SBOM 和模型 manifest。

只把 PyInstaller Analysis TOC 中真实出现的运行时发行组件写入清单。构建环境里
存在但被明确排除的包不会被误写成已发行组件；严格模式仍会扫描 TOC，阻止仅限
评估、AGPL 或未知许可的代码进入成品。
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata as metadata
import json
import os
import re
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = PROJECT_ROOT / "windows" / "legal-static"
DEFAULT_MODEL_ROOT = Path(r"D:\AI_Cache\modelscope\models\iic")
APP_VERSION = "0.3.2"
SOURCE_ROOT = Path(os.environ["VOICE_JOURNAL_LGPL_SOURCE_ROOT"])
LGPL_SOURCE_BUNDLE = SOURCE_ROOT / "VoiceJournal-LGPL-Sources-6.11.1.zip"
FFMPEG_MANIFEST_PATH = PROJECT_ROOT / "windows" / "ffmpeg-lgpl-manifest.json"
FFMPEG_ROOT = Path(os.environ["VOICE_JOURNAL_FFMPEG_ROOT"])

MODELS = {
    "asr": {
        "id": "iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        "purpose": "本地中文语音识别",
        "marker": "model.pt",
    },
    "vad": {
        "id": "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
        "purpose": "本地语音活动检测",
        "marker": "model.pt",
    },
    "punc": {
        "id": "iic/punc_ct-transformer_cn-en-common-vocab471067-large",
        "purpose": "本地中英文标点恢复",
        "marker": "model.pt",
    },
    "speaker": {
        "id": "iic/speech_campplus_sv_zh-cn_16k-common",
        "purpose": "本地说话人声纹匹配",
        "marker": "campplus_cn_common.bin",
    },
}

LICENSE_CLASSIFIER_MAP = {
    "Apache Software License": "Apache-2.0",
    "BSD License": "BSD-3-Clause",
    "MIT License": "MIT",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "Python Software Foundation License": "PSF-2.0",
    "The Unlicense (Unlicense)": "Unlicense",
    "ISC License (ISCL)": "ISC",
}

FORBIDDEN_SOURCE_MARKERS = (
    "software license agreement for evaluation",
    "non-commercial use only",
    "not for commercial use",
)
FORBIDDEN_RUNTIME_PATHS = (
    "site-packages\\kaldiio\\",
    "site-packages\\pytorch_wpe.py",
    "site-packages\\torch_complex\\",
)


def iter_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, (tuple, list, set)):
        for item in value:
            yield from iter_strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from iter_strings(key)
            yield from iter_strings(item)


def load_toc_strings(path: Path) -> set[str]:
    if not path.exists():
        raise FileNotFoundError(f"找不到 PyInstaller Analysis TOC：{path}")
    value = ast.literal_eval(path.read_text(encoding="utf-8"))
    return set(iter_strings(value))


def normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def top_level_names(dist: metadata.Distribution) -> set[str]:
    names: set[str] = set()
    top_level = dist.read_text("top_level.txt")
    if top_level:
        names.update(line.strip() for line in top_level.splitlines() if line.strip())
    for file in dist.files or ():
        parts = Path(str(file)).parts
        if not parts:
            continue
        first = parts[0]
        lowered = first.lower()
        if lowered.endswith((".dist-info", ".data")) or first.startswith("_"):
            continue
        if first.endswith(".py"):
            names.add(Path(first).stem)
        elif len(parts) > 1:
            names.add(first)
    return names


def distribution_is_in_toc(dist: metadata.Distribution, toc_strings: set[str]) -> bool:
    # 只按 TOC 中的真实源文件路径判断。TOC 也会记录 Analysis 的 excludes
    # 名称；如果只按模块名匹配，会把明确排除的包误判成已发行组件。
    lowered_strings = {value.replace("/", "\\").lower() for value in toc_strings}
    for file in dist.files or ():
        path = str(Path(dist.locate_file(file))).replace("/", "\\").lower()
        if path in lowered_strings:
            return True
    return False


def infer_license(dist: metadata.Distribution) -> str:
    expression = (dist.metadata.get("License-Expression") or "").strip()
    raw = (dist.metadata.get("License") or "").strip()
    if expression:
        return expression
    if raw and raw.upper() not in {"UNKNOWN", "UNKNOWN LICENSE"} and len(raw) < 240:
        return " ".join(raw.split())
    for classifier in dist.metadata.get_all("Classifier") or ():
        marker = "License :: OSI Approved :: "
        if marker in classifier:
            label = classifier.split(marker, 1)[1]
            if label in LICENSE_CLASSIFIER_MAP:
                return LICENSE_CLASSIFIER_MAP[label]
    return "NOASSERTION"


def project_url(dist: metadata.Distribution) -> str:
    for value in dist.metadata.get_all("Project-URL") or ():
        if "," in value:
            _label, url = value.split(",", 1)
            if url.strip().startswith("http"):
                return url.strip()
    home = (dist.metadata.get("Home-page") or "").strip()
    return home if home.startswith("http") else "NOASSERTION"


def license_files(dist: metadata.Distribution) -> list[tuple[str, Path]]:
    result = []
    for file in dist.files or ():
        name = Path(str(file)).name.lower()
        if name.startswith(("license", "licence", "copying", "notice", "copyright")):
            path = Path(dist.locate_file(file))
            if path.is_file():
                result.append((str(file), path))
    return result


def safe_component_dir(name: str, version: str) -> str:
    return re.sub(r"[^A-Za-z0-9._+-]+", "-", f"{name}-{version}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def copy_static_files(output: Path) -> None:
    for source in STATIC_DIR.rglob("*"):
        if source.is_file():
            target = output / source.relative_to(STATIC_DIR)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def build_model_manifest(model_root: Path, output: Path) -> dict:
    items = []
    model_license_dir = output / "licenses" / "models"
    model_license_dir.mkdir(parents=True, exist_ok=True)
    for slot, declared in MODELS.items():
        model_id = declared["id"]
        model_dir = model_root / model_id.split("/", 1)[1]
        if not (model_dir / declared["marker"]).exists():
            raise FileNotFoundError(f"缺少 {slot} 模型：{model_dir}")
        files = []
        for path in sorted(model_dir.rglob("*")):
            if path.is_file() and not any(part in {"example", "fig"} for part in path.parts):
                files.append(
                    {
                        "path": path.relative_to(model_dir).as_posix(),
                        "size": path.stat().st_size,
                        "sha256": sha256_file(path),
                    }
                )
        readme = model_dir / "README.md"
        if readme.exists():
            shutil.copy2(readme, model_license_dir / f"{slot}-MODEL-CARD.md")
        config = {}
        config_path = model_dir / "configuration.json"
        if config_path.exists():
            config = json.loads(config_path.read_text(encoding="utf-8"))
        items.append(
            {
                "slot": slot,
                "model_id": model_id,
                "purpose": declared["purpose"],
                "license": "Apache-2.0",
                "source": f"https://modelscope.cn/models/{model_id}",
                "modified": False,
                "upstream_configuration": config,
                "files": files,
            }
        )
    manifest = {
        "schema_version": 1,
        "application": "声年",
        "application_version": APP_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "models": items,
    }
    (output / "MODEL-MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def generate(args) -> list[str]:
    toc_path = Path(args.analysis_toc).resolve()
    output = Path(args.output).resolve()
    model_root = Path(args.model_root).resolve()
    frozen_payload = (
        Path(args.frozen_payload).resolve()
        if getattr(args, "frozen_payload", "")
        else None
    )
    toc_strings = load_toc_strings(toc_path)
    lowered_toc = "\n".join(toc_strings).replace("/", "\\").lower()
    errors = []
    for marker in FORBIDDEN_RUNTIME_PATHS:
        if marker in lowered_toc:
            errors.append(f"发行包包含禁止再分发的运行时代码：{marker}")

    if output.exists():
        raise FileExistsError(f"使用新的材料目录，避免覆盖已有文件：{output}")
    output.mkdir(parents=True)
    copy_static_files(output)
    for required_license in (
        "Apache-2.0.txt",
        "GPL-2.0-only.txt",
        "GPL-3.0-only.txt",
        "LGPL-3.0-only.txt",
        "PySide6-COPYING.txt",
        "Shiboken6-COPYING.txt",
        "7-Zip-LICENSE.txt",
    ):
        if not (output / "licenses" / required_license).is_file():
            errors.append(f"缺少标准许可证原文：licenses/{required_license}")

    if LGPL_SOURCE_BUNDLE.is_file():
        source_bundle_info = {
            "file": LGPL_SOURCE_BUNDLE.name,
            "size": LGPL_SOURCE_BUNDLE.stat().st_size,
            "sha256": sha256_file(LGPL_SOURCE_BUNDLE),
            "qt_version": "6.11.1",
            "pyside_version": "6.11.1",
        }
        (output / "SOURCE-BUNDLE.json").write_text(
            json.dumps(source_bundle_info, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    else:
        errors.append(f"缺少由发行方保存的 LGPL 对应源码包：{LGPL_SOURCE_BUNDLE}")

    included = []
    for dist in metadata.distributions():
        name = dist.metadata.get("Name") or "unknown"
        if distribution_is_in_toc(dist, toc_strings) or normalized_name(name) == "pyinstaller":
            included.append(dist)
    included.sort(key=lambda dist: normalized_name(dist.metadata.get("Name") or ""))

    components = []
    license_root = output / "licenses" / "python"
    license_root.mkdir(parents=True, exist_ok=True)
    for dist in included:
        name = dist.metadata.get("Name") or "unknown"
        version = dist.version
        declared = infer_license(dist)
        files = license_files(dist)
        component_dir = license_root / safe_component_dir(name, version)
        component_dir.mkdir(parents=True, exist_ok=True)
        copied = []
        for index, (relative, source) in enumerate(files, start=1):
            filename = Path(relative).name
            target = component_dir / filename
            if target.exists():
                target = component_dir / f"{index}-{filename}"
            shutil.copy2(source, target)
            copied.append(target.relative_to(output).as_posix())
            text = source.read_text(encoding="utf-8", errors="ignore").lower()
            if any(marker in text for marker in FORBIDDEN_SOURCE_MARKERS):
                errors.append(f"{name} {version} 包含仅限评估/非商业许可证")
        if not files:
            raw = (dist.metadata.get("License") or "").strip()
            if raw and len(raw) >= 240:
                target = component_dir / "METADATA-LICENSE.txt"
                target.write_text(raw + "\n", encoding="utf-8")
                copied.append(target.relative_to(output).as_posix())
        if declared == "NOASSERTION" and not copied:
            errors.append(f"{name} {version} 无法确认许可证且没有许可证文件")
        lowered_license = declared.lower()
        if "agpl" in lowered_license:
            errors.append(f"{name} {version} 使用 AGPL，未获准进入闭源商业包")
        if "gpl" in lowered_license and "lgpl" not in lowered_license and " or " not in lowered_license:
            if normalized_name(name) != "pyinstaller":
                errors.append(f"{name} {version} 疑似 GPL-only，需要人工批准")
        components.append(
            {
                "name": name,
                "version": version,
                "license": declared,
                "source": project_url(dist),
                "license_files": copied,
            }
        )

    ffmpeg_manifest = json.loads(
        FFMPEG_MANIFEST_PATH.read_text(encoding="utf-8")
    )
    ffmpeg_executable = FFMPEG_ROOT / "bin" / "ffmpeg.exe"
    ffmpeg_license = FFMPEG_ROOT / "LICENSE.txt"
    frozen_ffmpeg = (
        frozen_payload / "_internal" / "tools" / "ffmpeg.exe"
        if frozen_payload is not None
        else None
    )
    if "ffmpeg.exe" not in lowered_toc and not (
        frozen_ffmpeg is not None and frozen_ffmpeg.is_file()
    ):
        errors.append("发行包没有包含声明的 FFmpeg LGPL 本地解码组件")
    if not ffmpeg_executable.is_file() or not ffmpeg_license.is_file():
        errors.append(f"缺少 FFmpeg LGPL 构建或许可证：{FFMPEG_ROOT}")
    ffmpeg_source_bundle = (
        SOURCE_ROOT / ffmpeg_manifest["source_bundle"]
    )
    (output / "FFMPEG-MANIFEST.json").write_text(
        json.dumps(ffmpeg_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if not ffmpeg_source_bundle.is_file():
        errors.append(
            "缺少由发行方保存并与安装包共同提供的 FFmpeg 完整对应源码包："
            f"{ffmpeg_source_bundle}"
        )
    ffmpeg_license_files = []
    if ffmpeg_license.is_file():
        ffmpeg_license_dir = output / "licenses" / "ffmpeg"
        ffmpeg_license_dir.mkdir(parents=True, exist_ok=True)
        target = ffmpeg_license_dir / "FFmpeg-LICENSE.txt"
        shutil.copy2(ffmpeg_license, target)
        ffmpeg_license_files.append(target.relative_to(output).as_posix())
    components.append(
        {
            "name": ffmpeg_manifest["name"],
            "version": ffmpeg_manifest["version"],
            "license": ffmpeg_manifest["license"],
            "source": (
                "https://github.com/FFmpeg/FFmpeg/commit/"
                f"{ffmpeg_manifest['ffmpeg_commit']}"
            ),
            "license_files": ffmpeg_license_files,
        }
    )

    model_manifest = build_model_manifest(model_root, output)

    notices = [
        "# 声年第三方组件声明",
        "",
        f"应用版本：`{APP_VERSION}`  ",
        f"生成时间：`{datetime.now(timezone.utc).isoformat()}`  ",
        "清单范围：本次 PyInstaller Analysis TOC 中实际进入发行包的 Python/二进制组件，以及安装器和本地模型。",
        "",
        "声年业务代码以 MIT 协议免费开源。下列组件分别受其许可证约束，许可证原文位于 `licenses/`。",
        "Qt/PySide6 的 LGPL/GPL 原文和 COPYING 已位于 `licenses/`；对应源码包的校验信息见 `SOURCE-BUNDLE.json`。",
        "",
        "## Python 与二进制组件",
        "",
        "| 组件 | 版本 | 声明许可证 | 许可证文件 |",
        "|---|---:|---|---|",
    ]
    for item in components:
        files = "<br>".join(f"`{path}`" for path in item["license_files"]) or "见上游元数据"
        notices.append(f"| {item['name']} | {item['version']} | {item['license']} | {files} |")
    notices += [
        "",
        "## 本地模型",
        "",
    ]
    for item in model_manifest["models"]:
        notices.append(
            f"- `{item['model_id']}`：{item['purpose']}；Apache-2.0；"
            f"[模型卡]({item['source']})。"
        )
    notices += [
        "",
        "## 构建与安装工具",
        "",
        "- PyInstaller 的 GPL 许可证附带 Bootloader Exception，允许用其构建和分发闭源商业程序；许可证原文已随包保留。",
        "- 本版本使用标准 ZIP 便携包，解压后直接启动声年.exe。",
        "- 导入录音使用独立 FFmpeg `win64-lgpl-shared` 可执行文件和动态库；完整对应源码包必须与安装包共同提供。",
        "",
        "## 明确排除",
        "",
        "本发行包不包含 NTT `kaldiio`、`pytorch-wpe` 或依赖它的 DNN-WPE 可选前端。产品仅支持普通 WAV 等音频输入；Kaldi ARK/SCP 输入未开放。",
    ]
    (output / "THIRD_PARTY_NOTICES.md").write_text("\n".join(notices) + "\n", encoding="utf-8")

    packages = []
    relationships = []
    for index, item in enumerate(components, start=1):
        spdx_id = f"SPDXRef-Package-{index}"
        packages.append(
            {
                "SPDXID": spdx_id,
                "name": item["name"],
                "versionInfo": item["version"],
                "downloadLocation": item["source"],
                "filesAnalyzed": False,
                "licenseConcluded": "NOASSERTION",
                "licenseDeclared": item["license"] if len(item["license"]) < 160 else "NOASSERTION",
                "copyrightText": "NOASSERTION",
            }
        )
        relationships.append(
            {"spdxElementId": "SPDXRef-DOCUMENT", "relationshipType": "DESCRIBES", "relatedSpdxElement": spdx_id}
        )
    spdx = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"Voice-Journal-{APP_VERSION}",
        "documentNamespace": f"https://voice-journal.local/spdx/{uuid.uuid4()}",
        "creationInfo": {
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "creators": ["Tool: VoiceJournal-generate_compliance_bundle.py"],
        },
        "packages": packages,
        "relationships": relationships,
    }
    (output / "SBOM.spdx.json").write_text(
        json.dumps(spdx, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    report = [
        "# 发行合规检查报告",
        "",
        f"- TOC：`{toc_path}`",
        f"- 识别到发行组件：{len(components)} 个",
        f"- 本地模型：{len(model_manifest['models'])} 个",
        f"- 严格检查错误：{len(errors)} 个",
        "",
    ]
    if errors:
        report += ["## 阻塞项", ""] + [f"- {error}" for error in errors]
    else:
        report += ["## 结论", "", "本次依赖和模型清单未发现自动规则定义的许可证阻塞项。"]
    report += [
        "",
        "## 分发说明",
        "",
        "- LGPL 对应源码随便携包放在 `legal/sources`，应一并保留。",
        "- 声年业务代码按 MIT 协议免费开源。",
        "- 当前程序未做代码签名。",
    ]
    (output / "REVIEW-REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--analysis-toc", required=True)
    parser.add_argument("--output", default=str(PROJECT_ROOT / "packaging" / "legal"))
    parser.add_argument("--model-root", default=str(DEFAULT_MODEL_ROOT))
    parser.add_argument(
        "--frozen-payload",
        default="",
        help="PyInstaller 后处理完成的应用目录；用于核对构建后复制的发行组件。",
    )
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    errors = generate(args)
    print(f"合规材料已生成：{Path(args.output).resolve()}")
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
    return 2 if args.strict and errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

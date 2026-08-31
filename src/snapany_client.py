"""SnapAny 官方 API 客户端：解析并下载微信视频号公开视频。

隐私与安全边界：
- 只把用户主动粘贴的 ``weixin.qq.com/sph/`` 分享链接发给 SnapAny；
- API Key 只从本机环境变量/用户环境注册表读取，不写入日志或文件；
- SnapAny 返回的短时下载地址只在内存中使用，下载完成后不保存；
- 原始视频仅作为本地转写的临时文件，由上层流程在转写后删除。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests


API_ENDPOINT = "https://api.snapany.com/openapi/v1/extract/post"
_TEMPORARY_CODES = {"extract_failed", "retryable", "timeout", "unknown"}
_MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
ProgressCB = Callable[[str, str], None]


class SnapAnyError(RuntimeError):
    """可直接展示给用户的 SnapAny 解析或下载错误。"""


def _read_user_environment(name: str) -> str:
    """读取本机用户环境变量，兼容 setx 后当前进程尚未重启的情况。"""
    value = os.environ.get(name, "").strip()
    if value or sys.platform != "win32":
        return value
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value).strip()
    except Exception:
        return ""


def snapany_api_key() -> str:
    return _read_user_environment("SNAPANY_API_KEY")


def validate_wechat_channels_url(url: str) -> str:
    """只接受视频号公开分享页，避免把任意文本或网址提交给第三方。"""
    value = (url or "").strip()
    try:
        parsed = urlparse(value)
    except Exception as exc:
        raise SnapAnyError("视频号链接格式不正确") from exc
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host != "weixin.qq.com":
        raise SnapAnyError("只支持 https://weixin.qq.com/sph/ 开头的视频号分享链接")
    if not re.match(r"^/sph/[A-Za-z0-9_-]+/?$", parsed.path or ""):
        raise SnapAnyError("这不是有效的视频号 sph 分享链接")
    return value


def _error_text(status: int, payload: dict[str, Any] | None) -> tuple[str, str]:
    body = payload or {}
    code = str(body.get("code") or "").strip()
    detail = str(body.get("detail") or body.get("message") or "").strip()
    known = {
        "invalid_api_key": "SnapAny API Key 无效，请在 SnapAny 开发者平台重新生成",
        "insufficient_credits": "SnapAny 点数不足，请先在开发者平台充值",
        "rate_limited": "SnapAny 请求过于频繁，请稍后重试",
        "invalid_url": "视频号链接无效",
        "unsupported_url": "SnapAny 无法识别这个视频号链接",
        "unsupported_site": "SnapAny 当前不支持这个链接来源",
        "content_deleted": "该视频已删除或链接已失效",
        "private_content": "该视频不是公开内容，无法解析",
        "members_only_content": "该视频仅限成员观看，无法解析",
        "age_restricted": "该视频存在年龄限制，无法解析",
        "region_restricted": "该视频存在地区限制，无法解析",
        "live_stream_not_supported": "暂不支持解析视频号直播",
        "timeout": "SnapAny 解析超时，请稍后重试",
        "extract_failed": "SnapAny 暂时没有解析出视频，请稍后重试",
        "retryable": "SnapAny 暂时无法解析，请稍后重试",
        "unknown": "SnapAny 解析失败，请稍后重试",
    }
    message = known.get(code)
    if not message:
        message = detail or f"SnapAny 请求失败（HTTP {status}）"
    return code, message


def extract_post(
    url: str,
    *,
    api_key: str | None = None,
    session: Any = requests,
    timeout: float = 60,
) -> dict[str, Any]:
    """调用官方帖子解析接口，返回规范化后的 data 对象。"""
    share_url = validate_wechat_channels_url(url)
    key = (api_key or snapany_api_key()).strip()
    if not key:
        raise SnapAnyError(
            "尚未配置 SNAPANY_API_KEY。请先在 SnapAny 开发者平台创建 Key，"
            "再在本机设置用户环境变量 SNAPANY_API_KEY；不要把 Key 发到聊天里。"
        )

    headers = {
        "Authorization": f"Bearer {key}",
        "Accept-Language": "zh",
        "Content-Type": "application/json",
    }
    last_message = "SnapAny 解析失败"
    for attempt in range(2):
        try:
            response = session.post(
                API_ENDPOINT,
                headers=headers,
                json={"url": share_url},
                timeout=timeout,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_message = "连接 SnapAny 超时或网络不可用"
            if attempt == 0:
                continue
            raise SnapAnyError(last_message) from exc

        try:
            payload = response.json()
        except Exception:
            payload = None

        if response.ok and isinstance(payload, dict):
            data = payload.get("data", payload)
            if isinstance(data, dict):
                return data
            raise SnapAnyError("SnapAny 返回的数据格式异常")

        code, last_message = _error_text(response.status_code, payload)
        temporary = response.status_code == 429 or response.status_code >= 500 or code in _TEMPORARY_CODES
        if attempt == 0 and temporary:
            retry_after = str(response.headers.get("Retry-After", "")).strip()
            try:
                delay = min(max(float(retry_after), 0.0), 3.0)
            except ValueError:
                delay = 0.5
            if delay:
                time.sleep(delay)
            continue
        raise SnapAnyError(last_message)
    raise SnapAnyError(last_message)


def _author_name(data: dict[str, Any]) -> str:
    author = data.get("author")
    if isinstance(author, dict):
        return str(author.get("name") or author.get("nickname") or "").strip()
    return str(author or "").strip()


def _numeric_quality(item: dict[str, Any]) -> int:
    for key in ("height", "width", "bitrate"):
        try:
            return int(item.get(key) or 0)
        except (TypeError, ValueError):
            pass
    quality = str(item.get("quality") or item.get("resolution") or "")
    found = re.findall(r"\d+", quality)
    return max((int(part) for part in found), default=0)


def _select_download(data: dict[str, Any]) -> tuple[str, str | None, dict[str, str]]:
    medias = data.get("medias") or data.get("media") or []
    if isinstance(medias, dict):
        medias = [medias]
    if not isinstance(medias, list):
        medias = []

    videos = [
        item for item in medias
        if isinstance(item, dict)
        and str(item.get("media_type") or item.get("type") or "").lower() in {"video", "movie"}
    ]
    if not videos:
        videos = [item for item in medias if isinstance(item, dict)]

    for media in videos:
        resource_url = str(media.get("resource_url") or media.get("url") or "").strip()
        if resource_url.startswith("https://"):
            raw_headers = media.get("headers") or {}
            download_headers = {
                str(k): "" if v is None else str(v)
                for k, v in raw_headers.items()
            } if isinstance(raw_headers, dict) else {}
            return resource_url, None, download_headers

    choices: list[tuple[int, str, str | None, dict[str, str]]] = []
    for media in videos:
        media_headers = media.get("headers") if isinstance(media.get("headers"), dict) else {}
        variants = media.get("variants") or []
        if isinstance(variants, dict):
            variants = [variants]
        for variant in variants if isinstance(variants, list) else []:
            if not isinstance(variant, dict):
                continue
            video_url = str(
                variant.get("video_url") or variant.get("resource_url") or variant.get("url") or ""
            ).strip()
            audio_url = str(variant.get("audio_url") or "").strip() or None
            if not video_url.startswith("https://"):
                continue
            combined_headers = dict(media_headers)
            if isinstance(variant.get("headers"), dict):
                combined_headers.update(variant["headers"])
            choices.append((
                _numeric_quality(variant),
                video_url,
                audio_url if audio_url and audio_url.startswith("https://") else None,
                {str(k): "" if v is None else str(v) for k, v in combined_headers.items()},
            ))
    if choices:
        _, video_url, audio_url, headers = max(choices, key=lambda item: item[0])
        return video_url, audio_url, headers
    raise SnapAnyError("SnapAny 已返回帖子信息，但没有可下载的视频流")


def _download_stream(
    url: str,
    target: Path,
    headers: dict[str, str],
    *,
    session: Any,
    timeout: tuple[float, float] = (15, 120),
) -> None:
    part = target.with_suffix(target.suffix + ".part")
    try:
        with session.get(url, headers=headers, stream=True, timeout=timeout) as response:
            if not response.ok:
                raise SnapAnyError(f"视频直链下载失败（HTTP {response.status_code}）")
            content_type = str(response.headers.get("Content-Type", "")).lower()
            if "text/html" in content_type or "application/json" in content_type:
                raise SnapAnyError("视频直链返回了网页或错误信息，可能已经过期")
            expected = response.headers.get("Content-Length")
            if expected:
                try:
                    if int(expected) > _MAX_DOWNLOAD_BYTES:
                        raise SnapAnyError("视频超过 2GB 安全上限，已停止下载")
                except ValueError:
                    pass
            total = 0
            with part.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > _MAX_DOWNLOAD_BYTES:
                        raise SnapAnyError("视频超过 2GB 安全上限，已停止下载")
                    handle.write(chunk)
        if total < 1024:
            raise SnapAnyError("下载到的视频文件为空或过小")
        os.replace(part, target)
    except Exception:
        try:
            part.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _merge_video_audio(video: Path, audio: Path, output: Path) -> None:
    try:
        from audio_import import _ffmpeg_executable

        executable = _ffmpeg_executable()
    except Exception:
        executable = None
    if not executable:
        raise SnapAnyError("缺少 FFmpeg，无法合并视频号的分离音视频流")
    result = subprocess.run(
        [executable, "-y", "-i", str(video), "-i", str(audio), "-c", "copy", str(output)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_NO_WINDOW,
    )
    if result.returncode != 0 or not output.exists() or output.stat().st_size < 1024:
        raise SnapAnyError("FFmpeg 合并视频号音视频失败")


def download_wechat_channels(
    url: str,
    output_dir: Path,
    *,
    api_key: str | None = None,
    session: Any = requests,
    on_progress: ProgressCB | None = None,
) -> tuple[str, dict[str, Any]]:
    """解析视频号链接并立即下载，返回临时视频路径及非敏感元信息。"""
    progress = on_progress or (lambda _stage, _message: None)
    progress("api", "通过 SnapAny 官方 API 解析视频号链接...")
    data = extract_post(url, api_key=api_key, session=session)
    video_url, audio_url, headers = _select_download(data)

    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"wechat-channels-{uuid.uuid4().hex[:12]}"
    video_part = output_dir / f"{stem}-video.mp4"
    audio_part = output_dir / f"{stem}-audio.m4a"
    output = output_dir / f"{stem}.mp4"
    progress("download", "已解析直链，正在下载视频到本机...")
    try:
        _download_stream(video_url, video_part, headers, session=session)
        if audio_url:
            _download_stream(audio_url, audio_part, headers, session=session)
            progress("merge", "正在本机合并视频和音频...")
            _merge_video_audio(video_part, audio_part, output)
            video_part.unlink(missing_ok=True)
            audio_part.unlink(missing_ok=True)
        else:
            os.replace(video_part, output)
    except Exception:
        for path in (video_part, audio_part, output):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
        raise

    title = str(data.get("title") or data.get("body") or "视频号视频").strip()
    meta = {
        "title": title[:200] or "视频号视频",
        "author": _author_name(data) or "视频号作者",
        "platform": "wechat_channels",
    }
    return str(output), meta

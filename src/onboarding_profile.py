"""Create the first-run personal profile without requiring an external Skill."""
from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path


ONBOARDING_VERSION = 1


def _clean(value: object, limit: int = 500) -> str:
    return " ".join(str(value or "").strip().split())[:limit]


def _feature_names(answers: dict) -> list[str]:
    names = []
    mapping = (
        ("today_summary", "今日总结"),
        ("yesterday_review", "昨日复盘"),
        ("todos", "待办"),
        ("moments", "朋友圈"),
        ("short_video", "短视频口播"),
        ("article", "长文"),
    )
    for key, label in mapping:
        if bool(answers.get(key)):
            names.append(label)
    return names


def build_user_profile(answers: dict, cloud_enabled: bool) -> dict:
    display_name = _clean(answers.get("display_name"), 80)
    if not display_name:
        raise ValueError("请填写希望软件称呼你的名字")

    scenario = _clean(answers.get("scenario"), 120) or "日常语音记录"
    identity = _clean(answers.get("identity"), 300)
    audience = _clean(answers.get("audience"), 300)
    product = _clean(answers.get("product"), 300)
    voice = _clean(answers.get("voice"), 300)
    boundaries = _clean(answers.get("boundaries"), 1000)
    features = _feature_names(answers)
    if not features:
        features = ["今日总结", "待办"]

    content_types = [name for name in features if name in {"朋友圈", "短视频口播", "长文"}]
    return {
        "schema_version": 2,
        "display_name": display_name,
        "identity": [identity] if identity else [],
        "audiences": ([{"name": audience, "problems": [], "desired_outcomes": []}] if audience else []),
        "products": ([{"name": product, "facts": [], "proof": [], "boundaries": []}] if product else []),
        "goals": [{"name": name, "priority": index + 1, "notes": scenario} for index, name in enumerate(features)],
        "workflow_preferences": {
            "summaries": {
                "today": {"enabled": "今日总结" in features, "frequency": "daily" if "今日总结" in features else "off", "preferred_time": ""},
                "yesterday_review": {"enabled": "昨日复盘" in features, "frequency": "daily" if "昨日复盘" in features else "off", "preferred_time": ""},
            },
            "todos": {
                "enabled": "待办" in features,
                "frequency": "daily" if "待办" in features else "off",
                "capture_mode": "confirmed_and_suggested",
                "types": ["本人明确承诺", "截止事项", "会议行动项", "项目下一步"],
                "max_items_per_run": 20,
                "pin_rules": ["用户手动标记", "临近截止"],
                "pin_keywords": [],
                "never_generate": [],
            },
            "content": {
                "enabled": bool(content_types),
                "frequency": "when_material_ready" if content_types else "off",
                "types": content_types,
                "max_items_per_run": 20,
                "priority_topics": [],
                "excluded_topics": [],
            },
            "other_generators": [],
        },
        "voice": {
            "traits": [voice] if voice else ["自然", "具体", "保留本人语气"],
            "avoid": ["编造经历", "夸张承诺", "AI 套话"],
        },
        "boundaries": [boundaries] if boundaries else ["涉及他人隐私和未公开信息时先提醒确认"],
        "approved_examples": [],
        "cloud_context_consent": bool(cloud_enabled),
        "primary_scenario": scenario,
    }


def _bullets(values: list[str]) -> str:
    cleaned = [_clean(value, 1000) for value in values if _clean(value, 1000)]
    return "\n".join(f"- {value}" for value in cleaned) or "- 暂未填写"


def _write_text(path: Path, value: str) -> None:
    path.write_text(value.rstrip() + "\n", encoding="utf-8")


def write_user_profile(data_root: Path, profile: dict) -> Path:
    data_root = Path(data_root).expanduser().resolve()
    profiles_root = data_root / "profiles"
    target = profiles_root / "default"
    staging = profiles_root / f".staging-{uuid.uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=False)

    try:
        _write_text(staging / "user-profile.json", json.dumps(profile, ensure_ascii=False, indent=2))
        identity = profile.get("identity") or []
        audience_names = [item.get("name", "") for item in profile.get("audiences") or []]
        product_names = [item.get("name", "") for item in profile.get("products") or []]
        goal_names = [item.get("name", "") for item in profile.get("goals") or []]
        voice = profile.get("voice") or {}
        boundaries = profile.get("boundaries") or []
        workflow = profile.get("workflow_preferences") or {}

        profile_md = f"""# 我的声年配置

## 希望的称呼

{profile['display_name']}

## 主要场景

{profile.get('primary_scenario', '日常语音记录')}

## 身份

{_bullets(identity)}

## 服务对象

{_bullets(audience_names)}

## 产品或服务

{_bullets(product_names)}

## 希望生成的内容

{_bullets(goal_names)}

## 表达偏好

{_bullets(voice.get('traits') or [])}

## 隐私与事实边界

{_bullets(boundaries)}
"""
        _write_text(staging / "profile.md", profile_md)

        summaries = workflow.get("summaries") or {}
        todos = workflow.get("todos") or {}
        content = workflow.get("content") or {}
        ai_context = f"""# 当前用户个性化上下文

这部分只提供用户确认的事实和偏好。不得冒用他人经历，不得编造事实，不得绕过隐私检查。

称呼：{profile['display_name']}

身份：{'；'.join(identity)}

主要场景：{profile.get('primary_scenario', '')}

受众：{'；'.join(audience_names)}

产品或服务：{'；'.join(product_names)}

主要目标：{'；'.join(goal_names)}

总结偏好：今日={summaries.get('today', {}).get('frequency', 'off')}；昨日复盘={summaries.get('yesterday_review', {}).get('frequency', 'off')}

待办偏好：frequency={todos.get('frequency', 'off')}；capture_mode={todos.get('capture_mode', 'explicit_only')}

内容偏好：frequency={content.get('frequency', 'off')}；types={'；'.join(content.get('types') or [])}

表达风格：{'；'.join(voice.get('traits') or [])}

避免表达：{'；'.join(voice.get('avoid') or [])}

个人边界：{'；'.join(boundaries)}
"""
        _write_text(staging / "ai-context.md", ai_context)
        manifest = {
            "schema_version": 2,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "profile_name": "default",
            "cloud_context_consent": bool(profile.get("cloud_context_consent")),
            "source": "desktop-onboarding",
            "files": ["user-profile.json", "profile.md", "ai-context.md", "profile-manifest.json"],
        }
        _write_text(staging / "profile-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

        profiles_root.mkdir(parents=True, exist_ok=True)
        if target.exists():
            backup = profiles_root / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(backup))
        staging.replace(target)
        return target
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def load_user_profile(data_root: Path) -> dict:
    path = Path(data_root) / "profiles" / "default" / "user-profile.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def onboarding_state_path(data_root: Path) -> Path:
    return Path(data_root) / "runtime" / "onboarding.json"


def onboarding_complete(data_root: Path) -> bool:
    path = onboarding_state_path(data_root)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value.get("completed") is True and int(value.get("version", 0)) >= ONBOARDING_VERSION
    except (OSError, ValueError, TypeError):
        return False


def mark_onboarding_complete(data_root: Path, profile_root: Path) -> None:
    path = onboarding_state_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": ONBOARDING_VERSION,
        "completed": True,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "profile_root": str(profile_root),
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)

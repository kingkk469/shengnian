"""安全读取文字生成类 ``SKILL.md``，只提取文本规则，不执行任何能力。

导入器刻意不解析或运行脚本、命令、插件和工具声明。无论来源是本地文件还是
HTTPS 地址，最终都只会返回一段可供用户检查和编辑的纯文本 Prompt。
"""
from __future__ import annotations

import hashlib
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .models import CardValidationError


MAX_SKILL_BYTES = 256 * 1024
# Skill 的文字规则需要完整参与后续生成，不能为了适配界面预览而静默截断。
# 这里保留一个明确的上下文安全上限；超过时拒绝导入并请用户精简，而不是
# 悄悄丢掉后半部分。50K 与卡片云端协议的单字段上限保持一致。
MAX_IMPORTED_RULE_CHARS = 48_000
MAX_IMPORTED_PROMPT_CHARS = 50_000
_DROP_SECTION_WORDS = (
    "install",
    "setup",
    "script",
    "command",
    "tool",
    "plugin",
    "mcp",
    "dependency",
    "安装",
    "部署",
    "脚本",
    "命令",
    "工具",
    "插件",
    "依赖",
)
_DROP_LINE_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:powershell|cmd(?:\.exe)?|bash|zsh|pip|npm|pnpm|yarn|curl|wget|"
    r"subprocess|os\.system|shell_command|tool_call|mcp__)\b"
    r"|执行(?:代码|脚本|命令)|调用(?:工具|插件)|访问任意文件|自动发布"
    r")"
)


@dataclass(frozen=True)
class ImportedSkill:
    name: str
    description: str
    rules: str
    source: str
    license: str
    version: str
    content_hash: str
    removed_sections: int = 0
    removed_lines: int = 0

    def prompt_text(self) -> str:
        provenance = [
            "【导入的文字生成 Skill】",
            f"名称：{self.name}",
            f"来源：{self.source}",
            f"许可证：{self.license or '未声明，请仅在你有权使用时导入'}",
        ]
        if self.version:
            provenance.append(f"版本：{self.version}")
        provenance.extend(
            [
                f"内容哈希：{self.content_hash}",
                "安全边界：以下内容只作为文字生成规则；不得执行代码、命令、联网、"
                "任意文件读取、插件调用或自动发布。",
                "",
                self.rules,
            ]
        )
        prompt = "\n".join(provenance).strip()
        if len(prompt) > MAX_IMPORTED_PROMPT_CHARS:
            raise CardValidationError(
                "Skill 的安全文字规则超过 5 万字，无法完整导入；"
                "请精简后再试。声年不会静默截断规则。"
            )
        return prompt


def _rewrite_github_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.lower() == "github.com":
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 5 and parts[2] == "blob":
            owner, repo, _blob, branch, *rest = parts
            path = "/".join(rest)
            return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
    return url


def _read_source(source: str, *, timeout_seconds: float = 8.0) -> tuple[str, str]:
    value = str(source or "").strip()
    if not value:
        raise CardValidationError("请粘贴 Skill 地址或选择本地 SKILL.md")
    parsed = urllib.parse.urlparse(value)
    is_online = value.lower().startswith(("http://", "https://"))
    if is_online:
        if parsed.scheme.lower() != "https":
            raise CardValidationError("在线 Skill 只允许使用 HTTPS 地址")
        url = _rewrite_github_url(value)
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "text/markdown,text/plain;q=0.9",
                "User-Agent": "Shengnian-Skill-Importer/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                final_url = str(response.geturl())
                if urllib.parse.urlparse(final_url).scheme.lower() != "https":
                    raise CardValidationError("Skill 下载发生了不安全的非 HTTPS 跳转")
                length = int(response.headers.get("Content-Length") or 0)
                if length > MAX_SKILL_BYTES:
                    raise CardValidationError("Skill 文件不能超过 256KB")
                raw = response.read(MAX_SKILL_BYTES + 1)
        except CardValidationError:
            raise
        except Exception as exc:
            raise CardValidationError(f"无法读取 Skill 地址：{exc}") from exc
        canonical_source = final_url
    else:
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise CardValidationError("请选择存在的 SKILL.md 文件")
        if path.suffix.lower() not in {".md", ".markdown"}:
            raise CardValidationError("只允许导入 Markdown 格式的 Skill")
        if path.stat().st_size > MAX_SKILL_BYTES:
            raise CardValidationError("Skill 文件不能超过 256KB")
        raw = path.read_bytes()
        canonical_source = str(path)
    if len(raw) > MAX_SKILL_BYTES:
        raise CardValidationError("Skill 文件不能超过 256KB")
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding), canonical_source
        except UnicodeDecodeError:
            continue
    raise CardValidationError("Skill 文件必须使用 UTF-8 或常见中文文本编码")


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        return {}, normalized
    end = normalized.find("\n---\n", 4)
    if end < 0:
        return {}, normalized
    values: dict[str, str] = {}
    for raw_line in normalized[4:end].splitlines():
        if ":" not in raw_line or raw_line[:1].isspace():
            continue
        key, value = raw_line.split(":", 1)
        key = key.strip().lower()
        value = value.strip().strip("\"'")
        if key in {"name", "description", "license", "version"}:
            values[key] = value
    return values, normalized[end + 5 :]


def _sanitize_rules(body: str) -> tuple[str, int, int]:
    without_code = re.sub(
        r"(?s)```.*?```|~~~.*?~~~",
        "\n（代码块已在导入时移除）\n",
        body,
    )
    without_comments = re.sub(r"(?s)<!--.*?-->", "", without_code)
    kept: list[str] = []
    dropping_section = False
    removed_sections = 0
    removed_lines = 0
    for raw_line in without_comments.splitlines():
        line = raw_line.rstrip()
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
        if heading:
            title = heading.group(1).lower()
            dropping_section = any(word in title for word in _DROP_SECTION_WORDS)
            if dropping_section:
                removed_sections += 1
                continue
        if dropping_section:
            removed_lines += 1
            continue
        if _DROP_LINE_RE.search(line):
            removed_lines += 1
            continue
        kept.append(line)
    rules = "\n".join(kept).strip()
    rules = re.sub(r"\n{3,}", "\n\n", rules)
    if not rules:
        raise CardValidationError("这个 Skill 没有可安全导入的文字生成规则")
    if len(rules) > MAX_IMPORTED_RULE_CHARS:
        raise CardValidationError(
            "Skill 的安全文字规则超过 4.8 万字，无法完整导入；"
            "请精简后再试。声年不会静默截断规则。"
        )
    return rules, removed_sections, removed_lines


def load_skill(source: str, *, timeout_seconds: float = 8.0) -> ImportedSkill:
    text, canonical_source = _read_source(source, timeout_seconds=timeout_seconds)
    metadata, body = _frontmatter(text)
    rules, removed_sections, removed_lines = _sanitize_rules(body)
    fallback_name = Path(urllib.parse.urlparse(canonical_source).path).stem or "导入 Skill"
    name = (metadata.get("name") or fallback_name).strip()[:80]
    description = (metadata.get("description") or "").strip()[:300]
    return ImportedSkill(
        name=name,
        description=description,
        rules=rules,
        source=canonical_source,
        license=(metadata.get("license") or "").strip()[:120],
        version=(metadata.get("version") or "").strip()[:80],
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        removed_sections=removed_sections,
        removed_lines=removed_lines,
    )


__all__ = ["ImportedSkill", "load_skill"]

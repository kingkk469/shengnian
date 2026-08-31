"""本地卡片协调器：不调用云端 AI，只处理版本、确认与 Markdown 镜像。"""
from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

from .knowledge import CardSourceResolver
from .models import (
    CardSpec,
    CardValidationError,
    ContentRevision,
    SourceRef,
)
from .store import CardStore


_INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class CardEngine:
    """桌面端可复用的本地卡片用例层。

    AI 请求由上层网关完成。上层取得结果后调用 ``save_generated_result``；
    只有 ``accept_current`` 才会把自定义卡片镜像为可由 Obsidian 打开的文件。
    """

    def __init__(
        self,
        data_root: str | Path,
        store: CardStore | None = None,
        resolver: CardSourceResolver | None = None,
    ) -> None:
        self.data_root = Path(data_root).expanduser().resolve()
        self.store = store or CardStore(self.data_root)
        self.resolver = resolver or CardSourceResolver(self.data_root, self.store)
        self.output_root = (
            self.data_root / "notes" / "自定义卡片"
        ).resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)

    def create_custom_card(self, spec: CardSpec | str, **fields) -> CardSpec:
        return self.store.create_custom_card(spec, **fields)

    def source_bundle(
        self,
        card_id: str,
        *,
        query: str | list[str] | None = None,
        max_chars: int = 50_000,
        refresh: bool = True,
    ) -> tuple[list[SourceRef], str]:
        card = self.store.get_card(card_id)
        refs = self.resolver.build_source_bundle(
            card,
            max_chars=max_chars,
            query=query,
            refresh=refresh,
        )
        return refs, self.resolver.bundle_hash(refs)

    def save_generated_result(
        self,
        card_id: str,
        content: str,
        *,
        source_hash: str,
        kind: str = "generated",
    ) -> ContentRevision:
        return self.store.add_content_revision(
            card_id,
            content,
            kind=kind,
            source_hash=source_hash,
            accepted=False,
        )

    def save_manual_edit(self, card_id: str, content: str) -> ContentRevision:
        current = self.store.current_revision(card_id)
        return self.store.add_content_revision(
            card_id,
            content,
            kind="manual",
            source_hash=current.source_hash if current else "",
            accepted=False,
        )

    def accept_current(self, card_id: str) -> ContentRevision:
        card = self.store.get_card(card_id)
        revision = self.store.accept_current(card_id)
        if not card.is_default:
            self._mirror(card, revision)
        return revision

    def confirm_revision(
        self, card_id: str, revision_id: str | None = None
    ) -> ContentRevision:
        card = self.store.get_card(card_id)
        revision = self.store.confirm_revision(card_id, revision_id)
        if not card.is_default:
            self._mirror(card, revision)
        return revision

    def _mirror(self, card: CardSpec, revision: ContentRevision) -> Path:
        safe_name = _INVALID_FILENAME_RE.sub("_", card.name).strip(" ._")
        safe_name = (safe_name or "自定义卡片")[:80]
        filename = f"{safe_name}--{card.card_id[-8:]}.md"
        target = (self.output_root / filename).resolve()
        try:
            target.relative_to(self.output_root)
        except ValueError as exc:  # pragma: no cover - 文件名已净化
            raise CardValidationError("卡片镜像路径无效") from exc
        previous_relative = self.store.mirror_path(card.card_id)
        header = (
            "---\n"
            f"card_id: {card.card_id}\n"
            f"card_name: {json.dumps(card.name, ensure_ascii=False)}\n"
            f"revision_id: {revision.revision_id}\n"
            f"confirmed_at: {revision.created_at}\n"
            f"source_hash: {revision.source_hash}\n"
            "ai_generated: true\n"
            "ai_service_provider: 声年\n"
            f"content_id: {card.card_id}:{revision.revision_id}\n"
            "---\n\n"
            f"# {card.name}\n\n"
            "> AI 生成内容，使用前请核实。\n\n"
            f"{revision.content.rstrip()}\n"
        )
        self._atomic_write(target, header)
        relative = target.relative_to(self.data_root).as_posix()
        self.store.set_mirror_path(card.card_id, relative)
        if previous_relative and previous_relative != relative:
            previous = (self.data_root / previous_relative).resolve()
            try:
                previous.relative_to(self.output_root)
            except ValueError:
                previous = target
            if previous != target:
                try:
                    if previous.is_file():
                        previous.unlink()
                except OSError:
                    pass
        return target

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                pass

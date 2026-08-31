"""SQLite/WAL 卡片存储、版本历史和偏好。

数据库只保存本机信息。所有写操作使用显式事务；迁移与默认卡片初始化均幂等。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .models import (
    ALLOWED_PREFERENCE_SCOPES,
    ALLOWED_REVISION_KINDS,
    CardDependencyError,
    CardLimitError,
    CardNotFoundError,
    CardRun,
    CardSpec,
    CardValidationError,
    ContentRevision,
    PreferenceRule,
    _clean_enum,
    _clean_text,
    new_record_id,
    utc_now_iso,
)
from .registry import CardRegistry


MAX_ENABLED_CARDS = 20
MAX_SCHEDULED_CARDS = 10
MAX_CONTENT_CHARS = 200_000
MAX_HISTORY_ITEMS = 20
HISTORY_RETENTION_DAYS = 30
DELETED_RETENTION_DAYS = 30
GENERATION_FIELDS = frozenset(
    {
        "name",
        "purpose",
        "item_limit",
        "sources",
        "time_range",
        "rules",
        "user_prompt",
        "output_type",
        "trigger_mode",
        "dependencies",
    }
)


class CardStore:
    """卡片核心持久化 API。

    Args:
        data_root: 声年数据根目录。
        db_path: 测试或迁移工具可覆盖数据库位置；默认
            ``<data_root>/runtime/cards.db``。
    """

    def __init__(
        self,
        data_root: str | Path,
        db_path: str | Path | None = None,
    ) -> None:
        self.data_root = Path(data_root).expanduser().resolve()
        self.db_path = (
            Path(db_path).expanduser().resolve()
            if db_path is not None
            else self.data_root / "runtime" / "cards.db"
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.registry = CardRegistry()
        self._migrate()
        self.initialize_defaults()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    def _migrate(self) -> None:
        with self._transaction() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS card_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cards (
                    card_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    purpose TEXT NOT NULL DEFAULT '',
                    item_limit INTEGER NOT NULL DEFAULT 3
                        CHECK(item_limit BETWEEN 1 AND 20),
                    card_type TEXT NOT NULL CHECK(card_type IN ('default', 'custom')),
                    sources_json TEXT NOT NULL,
                    time_range TEXT NOT NULL,
                    rules TEXT NOT NULL DEFAULT '',
                    user_prompt TEXT NOT NULL DEFAULT '',
                    output_type TEXT NOT NULL,
                    trigger_mode TEXT NOT NULL,
                    dependencies_json TEXT NOT NULL DEFAULT '[]',
                    width TEXT NOT NULL,
                    height INTEGER NOT NULL DEFAULT 320,
                    position INTEGER NOT NULL DEFAULT 0,
                    visible INTEGER NOT NULL DEFAULT 1 CHECK(visible IN (0, 1)),
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                    is_default INTEGER NOT NULL DEFAULT 0 CHECK(is_default IN (0, 1)),
                    rules_version INTEGER NOT NULL DEFAULT 1,
                    current_revision_id TEXT,
                    initial_revision_id TEXT,
                    confirmed_revision_id TEXT,
                    mirror_path TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_cards_layout
                    ON cards(deleted_at, visible, position);
                CREATE INDEX IF NOT EXISTS idx_cards_enabled
                    ON cards(deleted_at, enabled, trigger_mode);

                CREATE TABLE IF NOT EXISTS card_spec_revisions (
                    spec_revision_id TEXT PRIMARY KEY,
                    card_id TEXT NOT NULL REFERENCES cards(card_id) ON DELETE CASCADE,
                    rules_version INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(card_id, rules_version)
                );
                CREATE INDEX IF NOT EXISTS idx_card_spec_history
                    ON card_spec_revisions(card_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS card_content_revisions (
                    revision_id TEXT PRIMARY KEY,
                    card_id TEXT NOT NULL REFERENCES cards(card_id) ON DELETE CASCADE,
                    parent_revision_id TEXT
                        REFERENCES card_content_revisions(revision_id) ON DELETE SET NULL,
                    content TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    source_hash TEXT NOT NULL DEFAULT '',
                    accepted INTEGER NOT NULL DEFAULT 0 CHECK(accepted IN (0, 1)),
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_card_content_history
                    ON card_content_revisions(card_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_card_content_parent
                    ON card_content_revisions(card_id, parent_revision_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS card_preferences (
                    preference_id TEXT PRIMARY KEY,
                    card_id TEXT REFERENCES cards(card_id) ON DELETE CASCADE,
                    scope TEXT NOT NULL CHECK(scope IN ('card', 'global')),
                    rule_text TEXT NOT NULL,
                    source_revision_id TEXT
                        REFERENCES card_content_revisions(revision_id) ON DELETE SET NULL,
                    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
                    created_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_card_preferences_active
                    ON card_preferences(card_id, active, created_at DESC);

                CREATE TABLE IF NOT EXISTS card_runs (
                    run_id TEXT PRIMARY KEY,
                    card_id TEXT NOT NULL REFERENCES cards(card_id) ON DELETE CASCADE,
                    rules_version INTEGER NOT NULL,
                    source_hash TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    revision_id TEXT
                        REFERENCES card_content_revisions(revision_id) ON DELETE SET NULL,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_card_runs_card
                    ON card_runs(card_id, created_at DESC);
                """
            )
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(cards)").fetchall()
            }
            if "purpose" not in columns:
                conn.execute(
                    "ALTER TABLE cards ADD COLUMN purpose TEXT NOT NULL DEFAULT ''"
                )
            if "item_limit" not in columns:
                conn.execute(
                    "ALTER TABLE cards ADD COLUMN item_limit INTEGER NOT NULL DEFAULT 3"
                )
            if "height" not in columns:
                conn.execute(
                    "ALTER TABLE cards ADD COLUMN height INTEGER NOT NULL DEFAULT 320"
                )
            layout_version = conn.execute(
                "SELECT value FROM card_meta WHERE key = 'direct_card_layout_version'"
            ).fetchone()
            if layout_version is None:
                conn.execute(
                    "UPDATE cards SET width = 'standard' WHERE width = 'narrow'"
                )
                conn.execute(
                    """
                    UPDATE cards
                    SET width = 'full', height = 420
                    WHERE card_id = 'short_video'
                    """
                )
                conn.execute(
                    """
                    INSERT INTO card_meta(key, value)
                    VALUES('direct_card_layout_version', '1')
                    """
                )
            conn.execute(
                """
                INSERT INTO card_meta(key, value) VALUES('schema_version', '1')
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """
            )
            conn.execute("PRAGMA user_version = 1")

    # ------------------------------------------------------------------
    # 卡片定义与布局

    def initialize_defaults(self) -> list[CardSpec]:
        """仅补齐不存在的内置卡片，不覆盖用户已经调整过的规则或布局。"""
        inserted: list[CardSpec] = []
        with self._transaction() as conn:
            now = utc_now_iso()
            for factory in self.registry.list_defaults():
                existing = conn.execute(
                    "SELECT 1 FROM cards WHERE card_id = ?", (factory.card_id,)
                ).fetchone()
                if existing:
                    continue
                spec = replace(factory, created_at=now, updated_at=now).validated()
                self._insert_card(conn, spec)
                self._insert_spec_revision(conn, spec)
                inserted.append(spec)
            self._assert_limits(conn)
            self._validate_dependency_graph(conn)
            conn.execute(
                """
                INSERT INTO card_meta(key, value)
                VALUES('default_cards_initialized', ?)
                ON CONFLICT(key) DO NOTHING
                """,
                (now,),
            )
        return inserted

    def list_cards(
        self,
        include_hidden: bool = False,
        include_deleted: bool = False,
    ) -> list[CardSpec]:
        clauses: list[str] = []
        if not include_deleted:
            clauses.append("deleted_at IS NULL")
        if not include_hidden:
            clauses.append("visible = 1")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM cards
                {where}
                ORDER BY
                    CASE WHEN deleted_at IS NULL THEN 0 ELSE 1 END,
                    position ASC, created_at ASC, card_id ASC
                """
            ).fetchall()
        return [CardSpec.from_record(row) for row in rows]

    def get_card(
        self, card_id: str, *, include_deleted: bool = False
    ) -> CardSpec:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM cards
                WHERE card_id = ?
                  AND (? = 1 OR deleted_at IS NULL)
                """,
                (card_id, int(include_deleted)),
            ).fetchone()
        if row is None:
            raise CardNotFoundError(f"找不到卡片：{card_id}")
        return CardSpec.from_record(row)

    def create_custom_card(
        self,
        spec: CardSpec | str,
        **fields: Any,
    ) -> CardSpec:
        """创建自定义卡片。

        ``spec`` 可以是已验证的 :class:`CardSpec`，也可以直接传卡片名称并通过
        关键字设置其他字段，方便 UI 使用。
        """
        if isinstance(spec, str):
            candidate = CardSpec.new_custom(spec, **fields)
        elif isinstance(spec, CardSpec):
            if fields:
                raise CardValidationError("传入 CardSpec 时不能再附加字段")
            candidate = spec.validated()
        else:
            raise CardValidationError("spec 必须是 CardSpec 或卡片名称")
        if candidate.is_default or candidate.card_type != "custom":
            raise CardValidationError("create_custom_card 只能创建自定义卡片")
        if candidate.deleted_at is not None:
            raise CardValidationError("新卡片不能处于已删除状态")
        now = utc_now_iso()
        candidate = replace(
            candidate,
            created_at=candidate.created_at or now,
            updated_at=now,
            rules_version=1,
        ).validated()
        with self._transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM cards WHERE card_id = ?", (candidate.card_id,)
            ).fetchone():
                raise CardValidationError(f"卡片 ID 已存在：{candidate.card_id}")
            self._insert_card(conn, candidate)
            self._insert_spec_revision(conn, candidate)
            self._assert_limits(conn)
            self._validate_dependency_graph(conn)
        return candidate

    create_card = create_custom_card

    def update_card(self, card_id: str, **changes: Any) -> CardSpec:
        forbidden = {
            "card_id",
            "card_type",
            "is_default",
            "created_at",
            "rules_version",
            "deleted_at",
        }
        bad = set(changes) & forbidden
        if bad:
            raise CardValidationError(f"不允许直接修改字段：{sorted(bad)}")
        with self._transaction() as conn:
            old = self._get_card_tx(conn, card_id)
            generation_changed = bool(set(changes) & GENERATION_FIELDS)
            now = utc_now_iso()
            candidate = old.with_updates(
                **changes,
                rules_version=old.rules_version + int(generation_changed),
                updated_at=now,
            )
            self._update_card_row(conn, candidate)
            self._assert_limits(conn)
            self._validate_dependency_graph(conn)
            if generation_changed:
                self._insert_spec_revision(conn, candidate)
                self._prune_spec_history_tx(conn, card_id, now=now)
        return candidate

    def restore_factory_rules(self, card_id: str) -> CardSpec:
        current = self.get_card(card_id)
        factory = self.registry.get_default(card_id)
        snapshot = factory.generation_snapshot()
        return self.update_card(current.card_id, **snapshot)

    def restore_initial_rules(self, card_id: str) -> CardSpec:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT snapshot_json
                FROM card_spec_revisions
                WHERE card_id = ?
                ORDER BY rules_version ASC
                LIMIT 1
                """,
                (card_id,),
            ).fetchone()
        if row is None:
            raise CardNotFoundError(f"卡片没有初始规则：{card_id}")
        snapshot = json.loads(row["snapshot_json"])
        return self.update_card(card_id, **snapshot)

    def update_layout(
        self,
        card_id: str,
        *,
        position: int | None = None,
        width: str | None = None,
        height: int | None = None,
    ) -> CardSpec:
        changes: dict[str, Any] = {}
        if position is not None:
            changes["position"] = position
        if width is not None:
            changes["width"] = width
        if height is not None:
            changes["height"] = height
        if not changes:
            return self.get_card(card_id)
        return self.update_card(card_id, **changes)

    def set_layout(
        self,
        card_id: str,
        *,
        width: str | None = None,
        height: int | None = None,
        hidden: bool | None = None,
        position: int | None = None,
    ) -> CardSpec:
        changes: dict[str, Any] = {}
        if width is not None:
            changes["width"] = width
        if height is not None:
            changes["height"] = height
        if hidden is not None:
            changes["visible"] = not hidden
        if position is not None:
            changes["position"] = position
        return self.update_card(card_id, **changes) if changes else self.get_card(card_id)

    def reorder_cards(self, ordered_card_ids: Sequence[str]) -> list[CardSpec]:
        ordered = list(ordered_card_ids)
        if len(ordered) != len(set(ordered)):
            raise CardValidationError("排序列表不能包含重复卡片")
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT card_id FROM cards
                WHERE deleted_at IS NULL
                ORDER BY position ASC, created_at ASC, card_id ASC
                """
            ).fetchall()
            active_ids = [row["card_id"] for row in rows]
            unknown = set(ordered) - set(active_ids)
            if unknown:
                raise CardNotFoundError(f"排序包含不存在的卡片：{sorted(unknown)}")
            final_order = ordered + [
                card_id for card_id in active_ids if card_id not in set(ordered)
            ]
            now = utc_now_iso()
            for position, item_id in enumerate(final_order):
                conn.execute(
                    """
                    UPDATE cards
                    SET position = ?, updated_at = ?
                    WHERE card_id = ?
                    """,
                    (position, now, item_id),
                )
        return self.list_cards(include_hidden=True)

    def hide_card(self, card_id: str) -> CardSpec:
        return self.update_card(card_id, visible=False)

    def show_card(self, card_id: str) -> CardSpec:
        return self.update_card(card_id, visible=True)

    def soft_delete_card(self, card_id: str) -> CardSpec:
        with self._transaction() as conn:
            card = self._get_card_tx(conn, card_id)
            if card.is_default:
                raise CardValidationError("默认卡片只能隐藏，不能删除")
            dependents = []
            for row in conn.execute(
                """
                SELECT card_id, dependencies_json FROM cards
                WHERE deleted_at IS NULL AND card_id <> ?
                """,
                (card_id,),
            ).fetchall():
                if card_id in json.loads(row["dependencies_json"]):
                    dependents.append(row["card_id"])
            if dependents:
                raise CardDependencyError(
                    f"请先移除这些卡片的引用：{sorted(dependents)}"
                )
            now = utc_now_iso()
            deleted = card.with_updates(
                visible=False,
                enabled=False,
                deleted_at=now,
                updated_at=now,
            )
            self._update_card_row(conn, deleted)
            self._validate_dependency_graph(conn)
        return deleted

    soft_delete = soft_delete_card

    def restore_deleted(self, card_id: str, *, enabled: bool = True) -> CardSpec:
        with self._transaction() as conn:
            card = self._get_card_tx(conn, card_id, include_deleted=True)
            if card.deleted_at is None:
                return card
            restored = card.with_updates(
                deleted_at=None,
                visible=True,
                enabled=enabled,
                updated_at=utc_now_iso(),
            )
            self._update_card_row(conn, restored)
            self._assert_limits(conn)
            self._validate_dependency_graph(conn)
        return restored

    def purge_deleted(self, *, now: datetime | None = None) -> list[str]:
        reference = now or datetime.now(timezone.utc)
        cutoff = (reference - timedelta(days=DELETED_RETENTION_DAYS)).isoformat(
            timespec="seconds"
        )
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT card_id, mirror_path FROM cards
                WHERE deleted_at IS NOT NULL AND deleted_at <= ?
                """,
                (cutoff,),
            ).fetchall()
            card_ids = [row["card_id"] for row in rows]
            mirror_paths = [row["mirror_path"] for row in rows if row["mirror_path"]]
            if card_ids:
                placeholders = ",".join("?" for _ in card_ids)
                conn.execute(
                    f"DELETE FROM cards WHERE card_id IN ({placeholders})",
                    card_ids,
                )
        for relative in mirror_paths:
            self._safe_remove_mirror(relative)
        return card_ids

    # ------------------------------------------------------------------
    # 内容版本

    def add_content_revision(
        self,
        card_id: str,
        content: str,
        kind: str = "manual",
        source_hash: str = "",
        accepted: bool = False,
        make_current: bool = True,
    ) -> ContentRevision:
        if accepted and not make_current:
            raise CardValidationError("候选版本不能直接标记为已确认")
        clean_content = _clean_text(
            content, field="卡片内容", limit=MAX_CONTENT_CHARS
        )
        clean_kind = _clean_enum(
            kind, field="revision kind", allowed=ALLOWED_REVISION_KINDS
        )
        clean_hash = _clean_text(source_hash, field="source_hash", limit=128)
        revision_id = new_record_id("rev")
        now = utc_now_iso()
        with self._transaction() as conn:
            card = self._get_card_tx(conn, card_id)
            pointers = conn.execute(
                """
                SELECT current_revision_id, initial_revision_id
                FROM cards WHERE card_id = ?
                """,
                (card_id,),
            ).fetchone()
            parent_id = pointers["current_revision_id"]
            conn.execute(
                """
                INSERT INTO card_content_revisions(
                    revision_id, card_id, parent_revision_id, content, kind,
                    source_hash, accepted, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    card.card_id,
                    parent_id,
                    clean_content,
                    clean_kind,
                    clean_hash,
                    int(accepted),
                    now,
                ),
            )
            if make_current:
                initial_id = pointers["initial_revision_id"] or revision_id
                conn.execute(
                    """
                    UPDATE cards
                    SET current_revision_id = ?,
                        initial_revision_id = ?,
                        confirmed_revision_id =
                            CASE WHEN ? = 1 THEN ? ELSE confirmed_revision_id END,
                        updated_at = ?
                    WHERE card_id = ?
                    """,
                    (
                        revision_id,
                        initial_id,
                        int(accepted),
                        revision_id,
                        now,
                        card_id,
                    ),
                )
            self._prune_content_history_tx(conn, card_id, now=now)
        return ContentRevision(
            revision_id=revision_id,
            card_id=card_id,
            parent_revision_id=parent_id,
            content=clean_content,
            kind=clean_kind,
            source_hash=clean_hash,
            accepted=accepted,
            created_at=now,
            is_current=bool(make_current),
        )

    add_revision = add_content_revision

    def current_revision(self, card_id: str) -> ContentRevision | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT r.*, 1 AS is_current
                FROM cards c
                JOIN card_content_revisions r
                  ON r.revision_id = c.current_revision_id
                WHERE c.card_id = ? AND c.deleted_at IS NULL
                """,
                (card_id,),
            ).fetchone()
        if row is None:
            self.get_card(card_id)
            return None
        return self._revision_from_row(row)

    def get_revision(self, revision_id: str) -> ContentRevision:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT r.*,
                    CASE WHEN c.current_revision_id = r.revision_id
                         THEN 1 ELSE 0 END AS is_current
                FROM card_content_revisions r
                JOIN cards c ON c.card_id = r.card_id
                WHERE r.revision_id = ?
                """,
                (revision_id,),
            ).fetchone()
        if row is None:
            raise CardNotFoundError(f"找不到内容版本：{revision_id}")
        return self._revision_from_row(row)

    def list_revisions(self, card_id: str) -> list[ContentRevision]:
        self.get_card(card_id, include_deleted=True)
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT r.*,
                    CASE WHEN c.current_revision_id = r.revision_id
                         THEN 1 ELSE 0 END AS is_current
                FROM card_content_revisions r
                JOIN cards c ON c.card_id = r.card_id
                WHERE r.card_id = ?
                ORDER BY r.created_at ASC, r.rowid ASC
                """,
                (card_id,),
            ).fetchall()
        return [self._revision_from_row(row) for row in rows]

    def undo(self, card_id: str) -> ContentRevision | None:
        with self._transaction() as conn:
            card = self._get_card_tx(conn, card_id)
            row = conn.execute(
                """
                SELECT r.parent_revision_id
                FROM cards c
                JOIN card_content_revisions r
                  ON r.revision_id = c.current_revision_id
                WHERE c.card_id = ?
                """,
                (card.card_id,),
            ).fetchone()
            if row is None:
                return None
            target = row["parent_revision_id"]
            if target is None:
                target = conn.execute(
                    "SELECT current_revision_id FROM cards WHERE card_id = ?",
                    (card_id,),
                ).fetchone()["current_revision_id"]
            conn.execute(
                """
                UPDATE cards SET current_revision_id = ?, updated_at = ?
                WHERE card_id = ?
                """,
                (target, utc_now_iso(), card_id),
            )
            result = self._get_revision_tx(conn, target, current=True)
        return result

    def redo(self, card_id: str) -> ContentRevision | None:
        with self._transaction() as conn:
            card = self._get_card_tx(conn, card_id)
            pointer = conn.execute(
                "SELECT current_revision_id FROM cards WHERE card_id = ?",
                (card_id,),
            ).fetchone()["current_revision_id"]
            if pointer is None:
                return None
            row = conn.execute(
                """
                SELECT revision_id
                FROM card_content_revisions
                WHERE card_id = ? AND parent_revision_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (card.card_id, pointer),
            ).fetchone()
            if row is None:
                return self._get_revision_tx(conn, pointer, current=True)
            target = row["revision_id"]
            conn.execute(
                """
                UPDATE cards SET current_revision_id = ?, updated_at = ?
                WHERE card_id = ?
                """,
                (target, utc_now_iso(), card_id),
            )
            result = self._get_revision_tx(conn, target, current=True)
        return result

    def restore_initial(self, card_id: str) -> ContentRevision | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT r.content, r.source_hash
                FROM cards c
                JOIN card_content_revisions r
                  ON r.revision_id = c.initial_revision_id
                WHERE c.card_id = ? AND c.deleted_at IS NULL
                """,
                (card_id,),
            ).fetchone()
        if row is None:
            self.get_card(card_id)
            return None
        return self.add_content_revision(
            card_id,
            row["content"],
            kind="restore",
            source_hash=row["source_hash"],
            accepted=False,
        )

    def restore_revision(
        self, card_id: str, revision_id: str
    ) -> ContentRevision:
        """把历史内容复制成一个新的当前版本，因而该操作本身也可以撤销。"""
        revision = self.get_revision(revision_id)
        if revision.card_id != card_id:
            raise CardValidationError("内容版本不属于该卡片")
        return self.add_content_revision(
            card_id,
            revision.content,
            kind="restore",
            source_hash=revision.source_hash,
            accepted=False,
        )

    def accept_current(self, card_id: str) -> ContentRevision:
        with self._transaction() as conn:
            self._get_card_tx(conn, card_id)
            row = conn.execute(
                "SELECT current_revision_id FROM cards WHERE card_id = ?",
                (card_id,),
            ).fetchone()
            revision_id = row["current_revision_id"]
            if revision_id is None:
                raise CardValidationError("卡片还没有可确认的内容")
            conn.execute(
                """
                UPDATE card_content_revisions SET accepted = 1
                WHERE revision_id = ? AND card_id = ?
                """,
                (revision_id, card_id),
            )
            conn.execute(
                """
                UPDATE cards
                SET confirmed_revision_id = ?, updated_at = ?
                WHERE card_id = ?
                """,
                (revision_id, utc_now_iso(), card_id),
            )
            result = self._get_revision_tx(conn, revision_id, current=True)
        return result

    def confirm_revision(
        self, card_id: str, revision_id: str | None = None
    ) -> ContentRevision:
        if revision_id is None:
            return self.accept_current(card_id)
        with self._transaction() as conn:
            self._get_card_tx(conn, card_id)
            row = conn.execute(
                """
                SELECT revision_id FROM card_content_revisions
                WHERE revision_id = ? AND card_id = ?
                """,
                (revision_id, card_id),
            ).fetchone()
            if row is None:
                raise CardNotFoundError("内容版本不属于该卡片")
            conn.execute(
                "UPDATE card_content_revisions SET accepted = 1 WHERE revision_id = ?",
                (revision_id,),
            )
            conn.execute(
                """
                UPDATE cards
                SET confirmed_revision_id = ?, updated_at = ?
                WHERE card_id = ?
                """,
                (revision_id, utc_now_iso(), card_id),
            )
            current_id = conn.execute(
                "SELECT current_revision_id FROM cards WHERE card_id = ?",
                (card_id,),
            ).fetchone()["current_revision_id"]
            result = self._get_revision_tx(
                conn, revision_id, current=(current_id == revision_id)
            )
        return result

    def confirmed_revision(self, card_id: str) -> ContentRevision | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT r.*,
                    CASE WHEN c.current_revision_id = r.revision_id
                         THEN 1 ELSE 0 END AS is_current
                FROM cards c
                JOIN card_content_revisions r
                  ON r.revision_id = c.confirmed_revision_id
                WHERE c.card_id = ? AND c.deleted_at IS NULL
                """,
                (card_id,),
            ).fetchone()
        return self._revision_from_row(row) if row is not None else None

    # ------------------------------------------------------------------
    # 已确认的长期偏好

    def add_preference(
        self,
        rule_text: str,
        *,
        card_id: str | None = None,
        scope: str = "card",
        source_revision_id: str | None = None,
        confirmed: bool = False,
    ) -> PreferenceRule:
        if not confirmed:
            raise CardValidationError("只有用户明确确认的长期要求才能保存为偏好")
        clean_scope = _clean_enum(
            scope, field="preference scope", allowed=ALLOWED_PREFERENCE_SCOPES
        )
        clean_rule = _clean_text(
            rule_text, field="长期偏好", limit=2_000, required=True
        )
        if clean_scope == "card" and not card_id:
            raise CardValidationError("卡片级偏好必须指定 card_id")
        if clean_scope == "global" and card_id is not None:
            raise CardValidationError("全局偏好不能绑定 card_id")
        preference_id = new_record_id("pref")
        now = utc_now_iso()
        with self._transaction() as conn:
            if card_id:
                self._get_card_tx(conn, card_id)
            if source_revision_id:
                row = conn.execute(
                    """
                    SELECT card_id, accepted
                    FROM card_content_revisions
                    WHERE revision_id = ?
                    """,
                    (source_revision_id,),
                ).fetchone()
                if row is None or not bool(row["accepted"]):
                    raise CardValidationError("长期偏好只能引用已经确认的内容版本")
                if card_id and row["card_id"] != card_id:
                    raise CardValidationError("偏好引用的内容版本不属于该卡片")
            conn.execute(
                """
                INSERT INTO card_preferences(
                    preference_id, card_id, scope, rule_text,
                    source_revision_id, active, created_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    preference_id,
                    card_id,
                    clean_scope,
                    clean_rule,
                    source_revision_id,
                    now,
                ),
            )
        return PreferenceRule(
            preference_id=preference_id,
            card_id=card_id,
            scope=clean_scope,
            rule_text=clean_rule,
            source_revision_id=source_revision_id,
            active=True,
            created_at=now,
        )

    def list_preferences(
        self,
        card_id: str | None = None,
        *,
        include_global: bool = True,
        active_only: bool = True,
    ) -> list[PreferenceRule]:
        clauses: list[str] = []
        params: list[Any] = []
        if card_id is not None:
            if include_global:
                clauses.append("(card_id = ? OR scope = 'global')")
            else:
                clauses.append("card_id = ?")
            params.append(card_id)
        elif not include_global:
            clauses.append("scope = 'card'")
        if active_only:
            clauses.append("active = 1")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM card_preferences
                {where}
                ORDER BY created_at ASC, rowid ASC
                """,
                params,
            ).fetchall()
        return [self._preference_from_row(row) for row in rows]

    def revoke_preference(self, preference_id: str) -> PreferenceRule:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM card_preferences WHERE preference_id = ?",
                (preference_id,),
            ).fetchone()
            if row is None:
                raise CardNotFoundError(f"找不到偏好：{preference_id}")
            revoked_at = row["revoked_at"] or utc_now_iso()
            conn.execute(
                """
                UPDATE card_preferences
                SET active = 0, revoked_at = ?
                WHERE preference_id = ?
                """,
                (revoked_at, preference_id),
            )
            updated = dict(row)
            updated["active"] = 0
            updated["revoked_at"] = revoked_at
        return self._preference_from_row(updated)

    # ------------------------------------------------------------------
    # 生成任务记录（不保存提示词或正文）

    def start_run(
        self,
        card_id: str,
        *,
        source_hash: str,
        idempotency_key: str,
    ) -> CardRun:
        clean_hash = _clean_text(source_hash, field="source_hash", limit=128)
        clean_key = _clean_text(
            idempotency_key, field="idempotency_key", limit=200, required=True
        )
        with self._transaction() as conn:
            card = self._get_card_tx(conn, card_id)
            existing = conn.execute(
                "SELECT * FROM card_runs WHERE idempotency_key = ?",
                (clean_key,),
            ).fetchone()
            if existing is not None:
                return self._run_from_row(existing)
            run_id = new_record_id("run")
            now = utc_now_iso()
            conn.execute(
                """
                INSERT INTO card_runs(
                    run_id, card_id, rules_version, source_hash,
                    idempotency_key, status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    run_id,
                    card.card_id,
                    card.rules_version,
                    clean_hash,
                    clean_key,
                    now,
                ),
            )
        return CardRun(
            run_id=run_id,
            card_id=card_id,
            rules_version=card.rules_version,
            source_hash=clean_hash,
            idempotency_key=clean_key,
            status="pending",
            revision_id=None,
            error_code=None,
            created_at=now,
            finished_at=None,
        )

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        revision_id: str | None = None,
        error_code: str | None = None,
    ) -> CardRun:
        if status not in {"succeeded", "failed", "cancelled", "stale"}:
            raise CardValidationError("无效的任务完成状态")
        clean_error = (
            _clean_text(error_code, field="error_code", limit=100)
            if error_code
            else None
        )
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM card_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise CardNotFoundError(f"找不到卡片任务：{run_id}")
            if revision_id:
                revision = conn.execute(
                    """
                    SELECT card_id FROM card_content_revisions
                    WHERE revision_id = ?
                    """,
                    (revision_id,),
                ).fetchone()
                if revision is None or revision["card_id"] != row["card_id"]:
                    raise CardValidationError("任务结果版本不属于该卡片")
            finished_at = utc_now_iso()
            conn.execute(
                """
                UPDATE card_runs
                SET status = ?, revision_id = ?, error_code = ?, finished_at = ?
                WHERE run_id = ?
                """,
                (status, revision_id, clean_error, finished_at, run_id),
            )
            updated = conn.execute(
                "SELECT * FROM card_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return self._run_from_row(updated)

    def get_run_by_idempotency(self, idempotency_key: str) -> CardRun | None:
        clean_key = _clean_text(
            idempotency_key, field="idempotency_key", limit=200, required=True
        )
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM card_runs WHERE idempotency_key = ?",
                (clean_key,),
            ).fetchone()
        return self._run_from_row(row) if row is not None else None

    def latest_run(
        self,
        card_id: str,
        *,
        statuses: Sequence[str] | None = None,
    ) -> CardRun | None:
        self.get_card(card_id, include_deleted=True)
        clean_statuses = tuple(statuses or ())
        allowed = {"pending", "succeeded", "failed", "cancelled", "stale"}
        if set(clean_statuses) - allowed:
            raise CardValidationError("包含无效的任务状态")
        params: list[Any] = [card_id]
        status_clause = ""
        if clean_statuses:
            placeholders = ",".join("?" for _ in clean_statuses)
            status_clause = f"AND status IN ({placeholders})"
            params.extend(clean_statuses)
        with self.connection() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM card_runs
                WHERE card_id = ? {status_clause}
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
        return self._run_from_row(row) if row is not None else None

    # ------------------------------------------------------------------
    # 镜像路径与维护

    def mirror_path(self, card_id: str) -> str | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT mirror_path FROM cards WHERE card_id = ?", (card_id,)
            ).fetchone()
        if row is None:
            raise CardNotFoundError(f"找不到卡片：{card_id}")
        return row["mirror_path"]

    def set_mirror_path(self, card_id: str, relative_path: str | None) -> None:
        if relative_path is not None:
            candidate = (self.data_root / relative_path).resolve()
            try:
                candidate.relative_to(self.data_root)
            except ValueError as exc:
                raise CardValidationError("镜像路径必须位于声年数据目录内") from exc
            relative_path = candidate.relative_to(self.data_root).as_posix()
        with self._transaction() as conn:
            self._get_card_tx(conn, card_id, include_deleted=True)
            conn.execute(
                "UPDATE cards SET mirror_path = ?, updated_at = ? WHERE card_id = ?",
                (relative_path, utc_now_iso(), card_id),
            )

    def prune_history(
        self, card_id: str | None = None, *, now: datetime | None = None
    ) -> None:
        now_iso = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
        with self._transaction() as conn:
            if card_id is None:
                rows = conn.execute("SELECT card_id FROM cards").fetchall()
                card_ids = [row["card_id"] for row in rows]
            else:
                self._get_card_tx(conn, card_id, include_deleted=True)
                card_ids = [card_id]
            for item_id in card_ids:
                self._prune_content_history_tx(conn, item_id, now=now_iso)
                self._prune_spec_history_tx(conn, item_id, now=now_iso)

    # ------------------------------------------------------------------
    # 内部帮助方法

    @staticmethod
    def _insert_card(conn: sqlite3.Connection, spec: CardSpec) -> None:
        record = spec.to_record()
        conn.execute(
            """
            INSERT INTO cards(
                card_id, name, card_type, sources_json, time_range, rules,
                purpose, item_limit, user_prompt, output_type, trigger_mode, dependencies_json,
                width, height, position, visible, enabled, is_default, rules_version,
                created_at, updated_at, deleted_at
            ) VALUES(
                :card_id, :name, :card_type, :sources_json, :time_range, :rules,
                :purpose, :item_limit, :user_prompt, :output_type, :trigger_mode, :dependencies_json,
                :width, :height, :position, :visible, :enabled, :is_default,
                :rules_version, :created_at, :updated_at, :deleted_at
            )
            """,
            record,
        )

    @staticmethod
    def _update_card_row(conn: sqlite3.Connection, spec: CardSpec) -> None:
        record = spec.to_record()
        conn.execute(
            """
            UPDATE cards SET
                name=:name,
                purpose=:purpose,
                item_limit=:item_limit,
                sources_json=:sources_json,
                time_range=:time_range,
                rules=:rules,
                user_prompt=:user_prompt,
                output_type=:output_type,
                trigger_mode=:trigger_mode,
                dependencies_json=:dependencies_json,
                width=:width,
                height=:height,
                position=:position,
                visible=:visible,
                enabled=:enabled,
                rules_version=:rules_version,
                updated_at=:updated_at,
                deleted_at=:deleted_at
            WHERE card_id=:card_id
            """,
            record,
        )

    @staticmethod
    def _insert_spec_revision(
        conn: sqlite3.Connection, spec: CardSpec
    ) -> None:
        conn.execute(
            """
            INSERT INTO card_spec_revisions(
                spec_revision_id, card_id, rules_version,
                snapshot_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                new_record_id("spec"),
                spec.card_id,
                spec.rules_version,
                json.dumps(spec.generation_snapshot(), ensure_ascii=False),
                spec.updated_at or utc_now_iso(),
            ),
        )

    @staticmethod
    def _get_card_tx(
        conn: sqlite3.Connection,
        card_id: str,
        *,
        include_deleted: bool = False,
    ) -> CardSpec:
        row = conn.execute(
            """
            SELECT * FROM cards
            WHERE card_id = ? AND (? = 1 OR deleted_at IS NULL)
            """,
            (card_id, int(include_deleted)),
        ).fetchone()
        if row is None:
            raise CardNotFoundError(f"找不到卡片：{card_id}")
        return CardSpec.from_record(row)

    def _assert_limits(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END) AS enabled_count,
                SUM(CASE WHEN enabled = 1 AND trigger_mode = 'daily'
                         THEN 1 ELSE 0 END) AS scheduled_count
            FROM cards
            WHERE deleted_at IS NULL
            """
        ).fetchone()
        enabled = int(row["enabled_count"] or 0)
        scheduled = int(row["scheduled_count"] or 0)
        if enabled > MAX_ENABLED_CARDS:
            raise CardLimitError(f"最多只能启用 {MAX_ENABLED_CARDS} 张卡片")
        if scheduled > MAX_SCHEDULED_CARDS:
            raise CardLimitError(f"最多只能启用 {MAX_SCHEDULED_CARDS} 张定时卡片")

    def _validate_dependency_graph(
        self,
        conn: sqlite3.Connection,
        *,
        allow_deleted_dependencies: bool = False,
    ) -> None:
        rows = conn.execute(
            "SELECT card_id, dependencies_json, deleted_at FROM cards"
        ).fetchall()
        all_ids = {row["card_id"] for row in rows}
        active_ids = {
            row["card_id"] for row in rows if row["deleted_at"] is None
        }
        graph: dict[str, tuple[str, ...]] = {}
        for row in rows:
            if row["deleted_at"] is not None:
                continue
            dependencies = tuple(json.loads(row["dependencies_json"]))
            missing = set(dependencies) - (
                all_ids if allow_deleted_dependencies else active_ids
            )
            if missing:
                if allow_deleted_dependencies:
                    dependencies = tuple(
                        dependency
                        for dependency in dependencies
                        if dependency in active_ids
                    )
                else:
                    raise CardDependencyError(
                        f"依赖卡片不存在或已删除：{sorted(missing)}"
                    )
            graph[row["card_id"]] = dependencies

        visiting: set[str] = set()
        depths: dict[str, int] = {}

        def depth(card_id: str) -> int:
            if card_id in depths:
                return depths[card_id]
            if card_id in visiting:
                raise CardDependencyError("卡片依赖不能形成循环")
            visiting.add(card_id)
            value = 0
            for dependency in graph.get(card_id, ()):
                if dependency == card_id:
                    raise CardDependencyError("卡片不能依赖自己")
                value = max(value, 1 + depth(dependency))
            visiting.remove(card_id)
            depths[card_id] = value
            if value > 2:
                raise CardDependencyError("卡片依赖深度最多为 2 层")
            return value

        for item_id in graph:
            depth(item_id)

    @staticmethod
    def _revision_from_row(row: Mapping[str, Any]) -> ContentRevision:
        return ContentRevision(
            revision_id=row["revision_id"],
            card_id=row["card_id"],
            parent_revision_id=row["parent_revision_id"],
            content=row["content"],
            kind=row["kind"],
            source_hash=row["source_hash"],
            accepted=bool(row["accepted"]),
            created_at=row["created_at"],
            is_current=bool(row["is_current"]),
        )

    @classmethod
    def _get_revision_tx(
        cls,
        conn: sqlite3.Connection,
        revision_id: str,
        *,
        current: bool,
    ) -> ContentRevision:
        row = conn.execute(
            "SELECT * FROM card_content_revisions WHERE revision_id = ?",
            (revision_id,),
        ).fetchone()
        if row is None:
            raise CardNotFoundError(f"找不到内容版本：{revision_id}")
        payload = dict(row)
        payload["is_current"] = int(current)
        return cls._revision_from_row(payload)

    @staticmethod
    def _preference_from_row(row: Mapping[str, Any]) -> PreferenceRule:
        return PreferenceRule(
            preference_id=row["preference_id"],
            card_id=row["card_id"],
            scope=row["scope"],
            rule_text=row["rule_text"],
            source_revision_id=row["source_revision_id"],
            active=bool(row["active"]),
            created_at=row["created_at"],
            revoked_at=row["revoked_at"],
        )

    @staticmethod
    def _run_from_row(row: Mapping[str, Any]) -> CardRun:
        return CardRun(
            run_id=row["run_id"],
            card_id=row["card_id"],
            rules_version=int(row["rules_version"]),
            source_hash=row["source_hash"],
            idempotency_key=row["idempotency_key"],
            status=row["status"],
            revision_id=row["revision_id"],
            error_code=row["error_code"],
            created_at=row["created_at"],
            finished_at=row["finished_at"],
        )

    @staticmethod
    def _cutoff(now: str, days: int) -> str:
        parsed = datetime.fromisoformat(now)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (parsed - timedelta(days=days)).isoformat(timespec="seconds")

    def _prune_content_history_tx(
        self, conn: sqlite3.Connection, card_id: str, *, now: str
    ) -> None:
        cutoff = self._cutoff(now, HISTORY_RETENTION_DAYS)
        rows = conn.execute(
            """
            SELECT revision_id, created_at
            FROM card_content_revisions
            WHERE card_id = ?
            ORDER BY created_at DESC, rowid DESC
            """,
            (card_id,),
        ).fetchall()
        pointers = conn.execute(
            """
            SELECT current_revision_id, initial_revision_id, confirmed_revision_id
            FROM cards WHERE card_id = ?
            """,
            (card_id,),
        ).fetchone()
        protected = {
            item
            for item in (
                pointers["current_revision_id"],
                pointers["initial_revision_id"],
                pointers["confirmed_revision_id"],
            )
            if item
        }
        keep_recent = {
            row["revision_id"] for row in rows[:MAX_HISTORY_ITEMS]
        }
        remove = [
            row["revision_id"]
            for row in rows
            if row["revision_id"] not in protected
            and (
                row["revision_id"] not in keep_recent
                or row["created_at"] < cutoff
            )
        ]
        if remove:
            placeholders = ",".join("?" for _ in remove)
            conn.execute(
                f"""
                DELETE FROM card_content_revisions
                WHERE revision_id IN ({placeholders})
                """,
                remove,
            )

    def _prune_spec_history_tx(
        self, conn: sqlite3.Connection, card_id: str, *, now: str
    ) -> None:
        cutoff = self._cutoff(now, HISTORY_RETENTION_DAYS)
        rows = conn.execute(
            """
            SELECT spec_revision_id, rules_version, created_at
            FROM card_spec_revisions
            WHERE card_id = ?
            ORDER BY rules_version DESC
            """,
            (card_id,),
        ).fetchall()
        latest = rows[0]["spec_revision_id"] if rows else None
        initial = rows[-1]["spec_revision_id"] if rows else None
        keep_recent = {
            row["spec_revision_id"] for row in rows[:MAX_HISTORY_ITEMS]
        }
        remove = [
            row["spec_revision_id"]
            for row in rows
            if row["spec_revision_id"] not in {latest, initial}
            and (
                row["spec_revision_id"] not in keep_recent
                or row["created_at"] < cutoff
            )
        ]
        if remove:
            placeholders = ",".join("?" for _ in remove)
            conn.execute(
                f"DELETE FROM card_spec_revisions "
                f"WHERE spec_revision_id IN ({placeholders})",
                remove,
            )

    def _safe_remove_mirror(self, relative_path: str) -> None:
        candidate = (self.data_root / relative_path).resolve()
        custom_root = (self.data_root / "notes" / "自定义卡片").resolve()
        try:
            candidate.relative_to(custom_root)
        except ValueError:
            return
        try:
            if candidate.is_file():
                candidate.unlink()
        except OSError:
            pass

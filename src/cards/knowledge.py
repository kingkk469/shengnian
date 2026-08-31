"""本地白名单知识库索引与卡片资料解析。

只索引 ``transcripts/*.jsonl`` 和用户主动导入后由声年托管的 TXT/MD。
不会递归扫描 notes，更不会读取 raw、logs、runtime 或程序目录。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import uuid
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .models import (
    CardSpec,
    CardValidationError,
    SourceRef,
    clean_time_range,
    utc_now_iso,
)
from .store import CardStore


SUPPORTED_IMPORT_SUFFIXES = frozenset({".txt", ".md"})
MAX_IMPORT_BYTES = 20 * 1024 * 1024
DEFAULT_CHUNK_CHARS = 2_000
DEFAULT_CHUNK_OVERLAP = 200
MAX_QUERY_CHARS = 500
MAX_SEARCH_LIMIT = 100
_SAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_QUERY_SPLIT_RE = re.compile(r"[\s,，。；;：:、！？!?（）()\[\]{}<>《》\"']+")


class KnowledgeIndexError(RuntimeError):
    """本地索引不可用或资料无法安全导入。"""


@dataclass(frozen=True, slots=True)
class ImportedDocument:
    document_id: str
    original_name: str
    managed_path: Path
    file_hash: str
    size: int
    imported_at: str
    indexed_at: str


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_filename_stem(stem: str) -> str:
    cleaned = _SAFE_FILENAME_RE.sub("_", stem).strip(" ._")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned or "导入资料")[:80]


def _read_text_file(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    if len(raw) > MAX_IMPORT_BYTES:
        raise CardValidationError("单个导入文件不能超过 20MB")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise CardValidationError("文件必须是 UTF-8 或常见中文文本编码")


def _chunks(text: str) -> Iterator[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return
    start = 0
    length = len(normalized)
    while start < length:
        end = min(length, start + DEFAULT_CHUNK_CHARS)
        if end < length:
            boundary = normalized.rfind("\n", start + 800, end)
            if boundary > start:
                end = boundary
        chunk = normalized[start:end].strip()
        if chunk:
            yield chunk
        if end >= length:
            break
        start = max(start + 1, end - DEFAULT_CHUNK_OVERLAP)


class CardSourceResolver:
    """维护本地索引，并为一张卡片构建最小来源包。"""

    def __init__(
        self,
        data_root: str | Path,
        store: CardStore,
        *,
        knowledge_root: str | Path | None = None,
    ) -> None:
        self.data_root = Path(data_root).expanduser().resolve()
        self.store = store
        self.db_path = store.db_path
        self.transcripts_root = (self.data_root / "transcripts").resolve()
        self.imports_root = (
            self.data_root / "notes" / "导入资料"
        ).resolve()
        self.knowledge_root = (
            Path(knowledge_root).expanduser().resolve()
            if knowledge_root is not None
            else (self.data_root / "notes" / "第二大脑").resolve()
        )
        self.transcripts_root.mkdir(parents=True, exist_ok=True)
        self.imports_root.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def _migrate(self) -> None:
        try:
            with self.store._transaction() as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS knowledge_documents (
                        document_id TEXT PRIMARY KEY,
                        source_type TEXT NOT NULL
                            CHECK(source_type IN ('transcript', 'import')),
                        managed_path TEXT NOT NULL UNIQUE,
                        original_name TEXT NOT NULL,
                        original_path TEXT,
                        file_hash TEXT NOT NULL,
                        file_size INTEGER NOT NULL,
                        mtime_ns INTEGER NOT NULL,
                        imported_at TEXT,
                        indexed_at TEXT NOT NULL,
                        encoding TEXT,
                        deleted_at TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_knowledge_documents_type
                        ON knowledge_documents(source_type, deleted_at, indexed_at);

                    CREATE TABLE IF NOT EXISTS knowledge_chunks (
                        chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        document_id TEXT NOT NULL
                            REFERENCES knowledge_documents(document_id)
                            ON DELETE CASCADE,
                        chunk_no INTEGER NOT NULL,
                        started_at TEXT,
                        content TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        UNIQUE(document_id, chunk_no)
                    );
                    CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_document
                        ON knowledge_chunks(document_id, chunk_no);
                    CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_started
                        ON knowledge_chunks(started_at);

                    CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_fts
                    USING fts5(
                        content,
                        content='knowledge_chunks',
                        content_rowid='chunk_id',
                        tokenize='trigram'
                    );

                    CREATE TRIGGER IF NOT EXISTS knowledge_chunks_ai
                    AFTER INSERT ON knowledge_chunks BEGIN
                        INSERT INTO knowledge_chunks_fts(rowid, content)
                        VALUES (new.chunk_id, new.content);
                    END;
                    CREATE TRIGGER IF NOT EXISTS knowledge_chunks_ad
                    AFTER DELETE ON knowledge_chunks BEGIN
                        INSERT INTO knowledge_chunks_fts(
                            knowledge_chunks_fts, rowid, content
                        ) VALUES ('delete', old.chunk_id, old.content);
                    END;
                    CREATE TRIGGER IF NOT EXISTS knowledge_chunks_au
                    AFTER UPDATE ON knowledge_chunks BEGIN
                        INSERT INTO knowledge_chunks_fts(
                            knowledge_chunks_fts, rowid, content
                        ) VALUES ('delete', old.chunk_id, old.content);
                        INSERT INTO knowledge_chunks_fts(rowid, content)
                        VALUES (new.chunk_id, new.content);
                    END;
                    """
                )
                conn.execute(
                    """
                    INSERT INTO card_meta(key, value)
                    VALUES('knowledge_schema_version', '1')
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """
                )
        except sqlite3.OperationalError as exc:
            raise KnowledgeIndexError(
                "当前 SQLite 不支持 FTS5 trigram，无法建立中文本地索引"
            ) from exc

    # ------------------------------------------------------------------
    # 白名单增量索引

    def refresh_index(self) -> dict[str, int]:
        """增量索引按日转写，并核对由声年托管的导入文件。"""
        indexed = 0
        unchanged = 0
        removed = 0
        seen_paths: set[str] = set()
        for path in sorted(self.transcripts_root.glob("*.jsonl")):
            try:
                resolved = path.resolve()
                resolved.relative_to(self.transcripts_root)
            except (OSError, ValueError):
                continue
            if not resolved.is_file():
                continue
            relative = resolved.relative_to(self.data_root).as_posix()
            seen_paths.add(relative)
            if self._index_transcript(resolved):
                indexed += 1
            else:
                unchanged += 1

        with self.store._transaction() as conn:
            rows = conn.execute(
                """
                SELECT document_id, managed_path
                FROM knowledge_documents
                WHERE source_type = 'transcript' AND deleted_at IS NULL
                """
            ).fetchall()
            stale = [
                row["document_id"]
                for row in rows
                if row["managed_path"] not in seen_paths
            ]
            if stale:
                placeholders = ",".join("?" for _ in stale)
                conn.execute(
                    f"DELETE FROM knowledge_documents "
                    f"WHERE document_id IN ({placeholders})",
                    stale,
                )
                removed += len(stale)

        with self.store.connection() as conn:
            imported_rows = conn.execute(
                """
                SELECT *
                FROM knowledge_documents
                WHERE source_type = 'import' AND deleted_at IS NULL
                """
            ).fetchall()
        missing_imports: list[str] = []
        for row in imported_rows:
            try:
                managed = (self.data_root / row["managed_path"]).resolve()
                managed.relative_to(self.imports_root)
            except (OSError, ValueError):
                missing_imports.append(row["document_id"])
                continue
            if not managed.is_file():
                missing_imports.append(row["document_id"])
                continue
            if self._refresh_imported_document(row, managed):
                indexed += 1
            else:
                unchanged += 1
        if missing_imports:
            with self.store._transaction() as conn:
                placeholders = ",".join("?" for _ in missing_imports)
                conn.execute(
                    f"DELETE FROM knowledge_documents "
                    f"WHERE document_id IN ({placeholders})",
                    missing_imports,
                )
                removed += len(missing_imports)
        return {"indexed": indexed, "unchanged": unchanged, "removed": removed}

    def _index_transcript(self, path: Path) -> bool:
        stat_after = path.stat()
        relative = path.relative_to(self.data_root).as_posix()
        document_id = "transcript_" + hashlib.sha256(
            relative.encode("utf-8")
        ).hexdigest()[:24]
        with self.store.connection() as conn:
            existing = conn.execute(
                """
                SELECT file_hash, file_size, mtime_ns
                FROM knowledge_documents
                WHERE managed_path = ? AND deleted_at IS NULL
                """,
                (relative,),
            ).fetchone()
        if (
            existing is not None
            and int(existing["file_size"]) == stat_after.st_size
            and int(existing["mtime_ns"]) == stat_after.st_mtime_ns
        ):
            return False
        file_hash = _sha256_file(path)
        stat_after_hash = path.stat()
        if (
            stat_after_hash.st_size != stat_after.st_size
            or stat_after_hash.st_mtime_ns != stat_after.st_mtime_ns
        ):
            # 转写器正好在追加：再取一次稳定快照，避免把旧 hash 配给新 stat。
            file_hash = _sha256_file(path)
            stat_after = path.stat()
        else:
            stat_after = stat_after_hash

        segments: list[tuple[int, str | None, str, str, str]] = []
        with path.open("r", encoding="utf-8-sig") as stream:
            chunk_no = 0
            for line_no, line in enumerate(stream, start=1):
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                text = str(record.get("text") or "").strip()
                if not text:
                    continue
                metadata = {
                    "line": line_no,
                    "source": str(record.get("source") or ""),
                    "speaker_name": str(record.get("speaker_name") or ""),
                }
                started_at = str(record.get("start") or "") or None
                for part in _chunks(text):
                    segments.append(
                        (
                            chunk_no,
                            started_at,
                            part,
                            _sha256_text(part),
                            json.dumps(metadata, ensure_ascii=False),
                        )
                    )
                    chunk_no += 1

        now = utc_now_iso()
        with self.store._transaction() as conn:
            conn.execute(
                "DELETE FROM knowledge_documents WHERE managed_path = ?",
                (relative,),
            )
            conn.execute(
                """
                INSERT INTO knowledge_documents(
                    document_id, source_type, managed_path, original_name,
                    original_path, file_hash, file_size, mtime_ns,
                    imported_at, indexed_at, encoding
                ) VALUES (?, 'transcript', ?, ?, NULL, ?, ?, ?, NULL, ?, 'utf-8')
                """,
                (
                    document_id,
                    relative,
                    path.name,
                    file_hash,
                    stat_after.st_size,
                    stat_after.st_mtime_ns,
                    now,
                ),
            )
            conn.executemany(
                """
                INSERT INTO knowledge_chunks(
                    document_id, chunk_no, started_at, content,
                    content_hash, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (document_id, chunk_no, started_at, text, digest, metadata)
                    for chunk_no, started_at, text, digest, metadata in segments
                ],
            )
        return True

    def _refresh_imported_document(
        self, row: Mapping[str, Any], managed: Path
    ) -> bool:
        stat = managed.stat()
        if (
            int(row["file_size"]) == stat.st_size
            and int(row["mtime_ns"]) == stat.st_mtime_ns
        ):
            return False
        text, encoding = _read_text_file(managed)
        file_hash = _sha256_file(managed)
        if file_hash == row["file_hash"]:
            with self.store._transaction() as conn:
                conn.execute(
                    """
                    UPDATE knowledge_documents
                    SET file_size = ?, mtime_ns = ?, indexed_at = ?
                    WHERE document_id = ?
                    """,
                    (
                        stat.st_size,
                        stat.st_mtime_ns,
                        utc_now_iso(),
                        row["document_id"],
                    ),
                )
            return False
        metadata = json.dumps(
            {"original_name": row["original_name"], "encoding": encoding},
            ensure_ascii=False,
        )
        parts = list(_chunks(text))
        now = utc_now_iso()
        with self.store._transaction() as conn:
            conn.execute(
                "DELETE FROM knowledge_chunks WHERE document_id = ?",
                (row["document_id"],),
            )
            conn.execute(
                """
                UPDATE knowledge_documents
                SET file_hash = ?, file_size = ?, mtime_ns = ?,
                    indexed_at = ?, encoding = ?
                WHERE document_id = ?
                """,
                (
                    file_hash,
                    stat.st_size,
                    stat.st_mtime_ns,
                    now,
                    encoding,
                    row["document_id"],
                ),
            )
            conn.executemany(
                """
                INSERT INTO knowledge_chunks(
                    document_id, chunk_no, started_at, content,
                    content_hash, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row["document_id"],
                        index,
                        row["imported_at"] or now,
                        part,
                        _sha256_text(part),
                        metadata,
                    )
                    for index, part in enumerate(parts)
                ],
            )
        return True

    # ------------------------------------------------------------------
    # 主动导入资料

    def import_document(self, source_path: str | Path) -> ImportedDocument:
        source = Path(source_path).expanduser().resolve()
        if not source.is_file():
            raise CardValidationError("请选择存在的文本文件")
        suffix = source.suffix.lower()
        if suffix not in SUPPORTED_IMPORT_SUFFIXES:
            raise CardValidationError("第一版只支持导入 TXT 和 Markdown 文件")
        text, encoding = _read_text_file(source)
        if not text.strip():
            raise CardValidationError("不能导入空文件")
        file_hash = _sha256_file(source)
        size = source.stat().st_size
        with self.store.connection() as conn:
            existing = conn.execute(
                """
                SELECT * FROM knowledge_documents
                WHERE source_type = 'import'
                  AND file_hash = ?
                  AND deleted_at IS NULL
                ORDER BY imported_at ASC
                LIMIT 1
                """,
                (file_hash,),
            ).fetchone()
        if existing is not None:
            managed = (self.data_root / existing["managed_path"]).resolve()
            if managed.is_file():
                return self._import_from_row(existing)

        safe_stem = _safe_filename_stem(source.stem)
        target = self.imports_root / f"{safe_stem}--{file_hash[:12]}{suffix}"
        if not target.exists():
            temporary = self.imports_root / (
                f".{target.name}.{uuid.uuid4().hex}.tmp"
            )
            try:
                with source.open("rb") as incoming, temporary.open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                os.replace(temporary, target)
            finally:
                try:
                    if temporary.exists():
                        temporary.unlink()
                except OSError:
                    pass
        relative = target.relative_to(self.data_root).as_posix()
        document_id = f"import_{uuid.uuid4().hex}"
        now = utc_now_iso()
        target_stat = target.stat()
        metadata = json.dumps(
            {"original_name": source.name, "encoding": encoding},
            ensure_ascii=False,
        )
        chunks = list(_chunks(text))
        try:
            with self.store._transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO knowledge_documents(
                        document_id, source_type, managed_path, original_name,
                        original_path, file_hash, file_size, mtime_ns,
                        imported_at, indexed_at, encoding
                    ) VALUES (?, 'import', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document_id,
                        relative,
                        source.name,
                        str(source),
                        file_hash,
                        target_stat.st_size,
                        target_stat.st_mtime_ns,
                        now,
                        now,
                        encoding,
                    ),
                )
                conn.executemany(
                    """
                    INSERT INTO knowledge_chunks(
                        document_id, chunk_no, started_at, content,
                        content_hash, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            document_id,
                            index,
                            now,
                            chunk,
                            _sha256_text(chunk),
                            metadata,
                        )
                        for index, chunk in enumerate(chunks)
                    ],
                )
        except Exception:
            # 若目标已被既有同哈希文档占用，不能误删；这里只清理由本次创建且
            # 尚未进入数据库的目标。
            with self.store.connection() as conn:
                referenced = conn.execute(
                    """
                    SELECT 1 FROM knowledge_documents
                    WHERE managed_path = ?
                    """,
                    (relative,),
                ).fetchone()
            if referenced is None:
                try:
                    target.unlink()
                except OSError:
                    pass
            raise
        return ImportedDocument(
            document_id=document_id,
            original_name=source.name,
            managed_path=target,
            file_hash=file_hash,
            size=size,
            imported_at=now,
            indexed_at=now,
        )

    def list_imports(self) -> list[ImportedDocument]:
        with self.store.connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM knowledge_documents
                WHERE source_type = 'import' AND deleted_at IS NULL
                ORDER BY imported_at DESC, document_id DESC
                """
            ).fetchall()
        return [self._import_from_row(row) for row in rows]

    def delete_import(self, document_id: str) -> bool:
        with self.store._transaction() as conn:
            row = conn.execute(
                """
                SELECT managed_path FROM knowledge_documents
                WHERE document_id = ? AND source_type = 'import'
                """,
                (document_id,),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                "DELETE FROM knowledge_documents WHERE document_id = ?",
                (document_id,),
            )
        managed = (self.data_root / row["managed_path"]).resolve()
        try:
            managed.relative_to(self.imports_root)
        except ValueError:
            return True
        try:
            if managed.is_file():
                managed.unlink()
        except OSError as exc:
            raise KnowledgeIndexError("资料已从索引删除，但本地副本删除失败") from exc
        return True

    def _import_from_row(self, row: Mapping[str, Any]) -> ImportedDocument:
        return ImportedDocument(
            document_id=row["document_id"],
            original_name=row["original_name"],
            managed_path=(self.data_root / row["managed_path"]).resolve(),
            file_hash=row["file_hash"],
            size=int(row["file_size"]),
            imported_at=row["imported_at"],
            indexed_at=row["indexed_at"],
        )

    # ------------------------------------------------------------------
    # 检索与来源包

    def search(
        self,
        query: str | Sequence[str],
        time_range: str = "all",
        limit: int = 20,
        *,
        source_types: Iterable[str] = ("transcript", "import"),
        today: date | None = None,
    ) -> list[SourceRef]:
        clean_range = clean_time_range(time_range)
        if not isinstance(limit, int) or not 1 <= limit <= MAX_SEARCH_LIMIT:
            raise CardValidationError(
                f"limit 必须在 1 到 {MAX_SEARCH_LIMIT} 之间"
            )
        allowed_types = {"transcript", "import"}
        selected_types = tuple(dict.fromkeys(source_types))
        if not selected_types or set(selected_types) - allowed_types:
            raise CardValidationError("检索来源只能是 transcript 或 import")
        terms = self._query_terms(query)
        conditions = ["d.deleted_at IS NULL"]
        params: list[Any] = []
        placeholders = ",".join("?" for _ in selected_types)
        conditions.append(f"d.source_type IN ({placeholders})")
        params.extend(selected_types)
        start, end = self._time_bounds(clean_range, today=today)
        if start is not None:
            conditions.append("COALESCE(c.started_at, d.indexed_at) >= ?")
            params.append(start)
        if end is not None:
            conditions.append("COALESCE(c.started_at, d.indexed_at) < ?")
            params.append(end)
        base_conditions = list(conditions)
        base_params = list(params)
        used_fts = False

        if terms and any(len(term) >= 3 for term in terms):
            used_fts = True
            match_query = " OR ".join(
                f'"{term.replace(chr(34), chr(34) * 2)}"'
                for term in terms
                if len(term) >= 3
            )
            conditions.append("knowledge_chunks_fts MATCH ?")
            params.append(match_query)
            sql = f"""
                SELECT c.*, d.source_type, d.managed_path,
                       bm25(knowledge_chunks_fts) AS rank
                FROM knowledge_chunks_fts
                JOIN knowledge_chunks c
                  ON c.chunk_id = knowledge_chunks_fts.rowid
                JOIN knowledge_documents d
                  ON d.document_id = c.document_id
                WHERE {' AND '.join(conditions)}
                ORDER BY rank ASC, c.started_at DESC, c.chunk_id DESC
                LIMIT ?
            """
        elif terms:
            like_conditions = []
            for term in terms:
                like_conditions.append("c.content LIKE ? ESCAPE '\\'")
                escaped = (
                    term.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                params.append(f"%{escaped}%")
            conditions.append(f"({' OR '.join(like_conditions)})")
            sql = f"""
                SELECT c.*, d.source_type, d.managed_path, 0.0 AS rank
                FROM knowledge_chunks c
                JOIN knowledge_documents d
                  ON d.document_id = c.document_id
                WHERE {' AND '.join(conditions)}
                ORDER BY c.started_at DESC, c.chunk_id DESC
                LIMIT ?
            """
        else:
            sql = f"""
                SELECT c.*, d.source_type, d.managed_path, 0.0 AS rank
                FROM knowledge_chunks c
                JOIN knowledge_documents d
                  ON d.document_id = c.document_id
                WHERE {' AND '.join(conditions)}
                ORDER BY c.started_at DESC, c.chunk_id DESC
                LIMIT ?
            """
        params.append(limit)
        with self.store.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
            if not rows and used_fts:
                fallback_terms: list[str] = []
                for term in terms:
                    if len(term) < 3:
                        fallback_terms.append(term)
                        continue
                    fallback_terms.extend(
                        term[index : index + 3]
                        for index in range(0, len(term) - 2, 2)
                    )
                fallback_terms = list(dict.fromkeys(fallback_terms))[:24]
                fallback_conditions = list(base_conditions)
                fallback_params = list(base_params)
                like_conditions: list[str] = []
                for term in fallback_terms:
                    escaped = (
                        term.replace("\\", "\\\\")
                        .replace("%", "\\%")
                        .replace("_", "\\_")
                    )
                    like_conditions.append("c.content LIKE ? ESCAPE '\\'")
                    fallback_params.append(f"%{escaped}%")
                if like_conditions:
                    fallback_conditions.append(
                        f"({' OR '.join(like_conditions)})"
                    )
                    fallback_params.append(limit)
                    rows = conn.execute(
                        f"""
                        SELECT c.*, d.source_type, d.managed_path,
                               0.0 AS rank
                        FROM knowledge_chunks c
                        JOIN knowledge_documents d
                          ON d.document_id = c.document_id
                        WHERE {' AND '.join(fallback_conditions)}
                        ORDER BY c.started_at DESC, c.chunk_id DESC
                        LIMIT ?
                        """,
                        fallback_params,
                    ).fetchall()
        return [self._source_from_row(row) for row in rows]

    def build_source_bundle(
        self,
        card: CardSpec,
        max_chars: int = 50_000,
        *,
        query: str | Sequence[str] | None = None,
        refresh: bool = True,
    ) -> list[SourceRef]:
        card = card.validated()
        if not isinstance(max_chars, int) or not 1 <= max_chars <= 50_000:
            raise CardValidationError("来源包最多允许 50000 个字符")
        if refresh:
            self.refresh_index()
        source_types: list[str] = []
        if "transcripts" in card.sources:
            source_types.append("transcript")
        if "imports" in card.sources:
            source_types.append("import")
        refs: list[SourceRef] = []
        if source_types:
            search_query = query
            if search_query is None:
                search_query = card.user_prompt or card.rules or card.name
            refs.extend(
                self.search(
                    search_query,
                    time_range=card.time_range,
                    limit=80,
                    source_types=source_types,
                )
            )
        if "confirmed_cards" in card.sources:
            for dependency_id in card.dependencies:
                dependency = self.store.get_card(dependency_id)
                revision = self.store.confirmed_revision(dependency_id)
                if revision is None:
                    continue
                refs.append(
                    SourceRef(
                        ref_id=(
                            f"confirmed_card:{dependency.card_id}:"
                            f"{revision.revision_id}"
                        ),
                        source_type="confirmed_card",
                        source_path=f"card://{dependency.card_id}",
                        content=revision.content,
                        content_hash=_sha256_text(revision.content),
                        started_at=revision.created_at,
                        fact_level="confirmed_output",
                        metadata={
                            "card_id": dependency.card_id,
                            "card_name": dependency.name,
                            "revision_id": revision.revision_id,
                        },
                        score=0.0,
                    )
                )
        if "todos" in card.sources:
            refs.extend(self._structured_todo_refs(completed=False))
        if "done" in card.sources:
            refs.extend(self._structured_todo_refs(completed=True))

        bundle: list[SourceRef] = []
        used = 0
        seen: set[str] = set()
        for ref in refs:
            if ref.ref_id in seen:
                continue
            seen.add(ref.ref_id)
            remaining = max_chars - used
            if remaining <= 0:
                break
            content = ref.content[:remaining]
            if not content:
                continue
            bundle.append(replace(ref, content=content))
            used += len(content)
        return bundle

    def _structured_todo_refs(self, *, completed: bool) -> list[SourceRef]:
        """只读取待办总览的指定区段，不把整个 notes 纳入检索。"""
        path = (self.knowledge_root / "待办总览.md").resolve()
        try:
            path.relative_to(self.knowledge_root)
        except ValueError:
            return []
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            return []
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except (OSError, UnicodeDecodeError):
            return []
        in_target = False
        refs: list[SourceRef] = []
        wanted_box = "[x]" if completed else "[ ]"
        section_words = ("已完成", "已办") if completed else ("待完成", "待办")
        for line_no, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("##"):
                in_target = any(word in stripped for word in section_words)
                continue
            if not in_target:
                continue
            normalized = stripped.lower()
            if not normalized.startswith(f"- {wanted_box}"):
                continue
            content = stripped[len(f"- {wanted_box}") :].strip()
            if not content:
                continue
            date_match = re.search(r"20\d{2}-\d{2}-\d{2}", content)
            digest = _sha256_text(content)
            source_type = "structured_done" if completed else "structured_todo"
            refs.append(
                SourceRef(
                    ref_id=f"{source_type}:{line_no}:{digest[:12]}",
                    source_type=source_type,
                    source_path=self._display_source_path(path),
                    content=content,
                    content_hash=digest,
                    started_at=date_match.group(0) if date_match else None,
                    fact_level="user_confirmed_state",
                    metadata={"line": line_no, "completed": completed},
                    score=0.0,
                )
            )
        return refs

    def _display_source_path(self, path: Path) -> str:
        try:
            return path.relative_to(self.data_root).as_posix()
        except ValueError:
            # 外部 Obsidian vault 是用户明确配置的白名单路径。该路径只保存在
            # 本机 SourceRef，发送云端时只使用 source_id/date/text。
            return str(path)

    @staticmethod
    def bundle_hash(refs: Sequence[SourceRef]) -> str:
        digest = hashlib.sha256()
        for ref in refs:
            digest.update(ref.ref_id.encode("utf-8"))
            digest.update(b"\0")
            digest.update(ref.content_hash.encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest()

    @staticmethod
    def _query_terms(query: str | Sequence[str]) -> list[str]:
        raw_items = [query] if isinstance(query, str) else list(query)
        terms: list[str] = []
        for raw in raw_items:
            if not isinstance(raw, str):
                raise CardValidationError("检索词必须是文本")
            if len(raw) > MAX_QUERY_CHARS:
                raw = raw[:MAX_QUERY_CHARS]
            for part in _QUERY_SPLIT_RE.split(raw):
                cleaned = part.strip()
                if not cleaned:
                    continue
                if len(cleaned) <= 16:
                    terms.append(cleaned)
                else:
                    # 长句不是可靠关键词；取分段窗口，以 OR 召回候选片段。
                    terms.extend(
                        cleaned[index : index + 8]
                        for index in range(0, min(len(cleaned), 64), 8)
                        if len(cleaned[index : index + 8]) >= 2
                    )
        return list(dict.fromkeys(terms))[:12]

    @staticmethod
    def _time_bounds(
        time_range: str, *, today: date | None
    ) -> tuple[str | None, str | None]:
        current = today or date.today()
        if time_range == "all":
            return None, None
        if time_range.startswith("custom:"):
            _prefix, start_text, end_text = time_range.split(":", 2)
            start = date.fromisoformat(start_text)
            end = date.fromisoformat(end_text) + timedelta(days=1)
            return start.isoformat(), end.isoformat()
        if time_range == "today":
            start = current
            end = current + timedelta(days=1)
        elif time_range == "yesterday":
            start = current - timedelta(days=1)
            end = current
        elif time_range == "last_7_days":
            start = current - timedelta(days=6)
            end = current + timedelta(days=1)
        elif time_range == "last_30_days":
            start = current - timedelta(days=29)
            end = current + timedelta(days=1)
        else:  # pragma: no cover - CardSpec/enum 已拦截
            raise CardValidationError("无效的时间范围")
        return start.isoformat(), end.isoformat()

    @staticmethod
    def _source_from_row(row: Mapping[str, Any]) -> SourceRef:
        source_type = (
            "original_transcript"
            if row["source_type"] == "transcript"
            else "imported_document"
        )
        rank = float(row["rank"] or 0.0)
        return SourceRef(
            ref_id=(
                f"{source_type}:{row['document_id']}:{int(row['chunk_no'])}"
            ),
            source_type=source_type,
            source_path=row["managed_path"],
            content=row["content"],
            content_hash=row["content_hash"],
            started_at=row["started_at"],
            fact_level="primary",
            metadata=json.loads(row["metadata_json"]),
            score=-rank,
        )

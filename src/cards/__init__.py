"""声年自定义卡片核心。

公共对象均不依赖 PySide，可由桌面端、测试和后台任务共同使用。
"""
from .engine import CardEngine
from .knowledge import CardSourceResolver, ImportedDocument, KnowledgeIndexError
from .models import (
    CardDependencyError,
    CardError,
    CardLimitError,
    CardNotFoundError,
    CardRun,
    CardSpec,
    CardValidationError,
    ContentRevision,
    PreferenceRule,
    SourceRef,
)
from .registry import CardRegistry, DEFAULT_CARD_IDS, DEFAULT_CARDS
from .store import CardStore

__all__ = [
    "CardDependencyError",
    "CardEngine",
    "CardError",
    "CardLimitError",
    "CardNotFoundError",
    "CardRegistry",
    "CardRun",
    "CardSourceResolver",
    "CardSpec",
    "CardStore",
    "CardValidationError",
    "ContentRevision",
    "DEFAULT_CARD_IDS",
    "DEFAULT_CARDS",
    "ImportedDocument",
    "KnowledgeIndexError",
    "PreferenceRule",
    "SourceRef",
]

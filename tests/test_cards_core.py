from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cards import (  # noqa: E402
    CardDependencyError,
    CardLimitError,
    CardNotFoundError,
    CardSpec,
    CardStore,
    CardValidationError,
    DEFAULT_CARD_IDS,
)


class CardStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = CardStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_defaults_are_idempotent_and_migration_uses_wal(self):
        self.assertEqual(
            {card.card_id for card in self.store.list_cards()},
            DEFAULT_CARD_IDS,
        )
        self.store.update_layout(
            "today_brief", width="full", height=470, position=9
        )
        second = CardStore(self.root)
        self.assertEqual(second.initialize_defaults(), [])
        self.assertEqual(len(second.list_cards()), 5)
        self.assertEqual(second.get_card("today_brief").width, "full")
        self.assertEqual(second.get_card("today_brief").height, 470)
        with second.connection() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(
                conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal"
            )

    def test_controlled_fields_reject_oversized_or_invalid_values(self):
        with self.assertRaises(CardValidationError):
            CardSpec.new_custom("x", user_prompt="a" * 50_001)
        with self.assertRaises(CardValidationError):
            CardSpec.new_custom("x", rules="a" * 2001)
        with self.assertRaises(CardValidationError):
            CardSpec.new_custom("x", purpose="a" * 301)
        with self.assertRaises(CardValidationError):
            CardSpec.new_custom("x", item_limit=21)
        with self.assertRaises(CardValidationError):
            CardSpec.new_custom("x", width="giant")
        with self.assertRaises(CardValidationError):
            CardSpec.new_custom("x", height=900)
        with self.assertRaises(CardValidationError):
            CardSpec.new_custom("x", sources=("logs",))
        with self.assertRaises(CardValidationError):
            CardSpec.new_custom(
                "x", sources=("confirmed_cards",), dependencies=()
            )

    def test_enabled_and_scheduled_limits_are_transactional(self):
        for index in range(15):
            self.store.create_custom_card(
                CardSpec.new_custom(f"卡片{index}", enabled=True)
            )
        with self.assertRaises(CardLimitError):
            self.store.create_custom_card(CardSpec.new_custom("超过上限"))
        self.assertEqual(
            len(self.store.list_cards(include_hidden=True)), 20
        )

        with tempfile.TemporaryDirectory() as directory:
            store = CardStore(directory)
            for index in range(10):
                store.create_custom_card(
                    CardSpec.new_custom(
                        f"定时{index}", trigger_mode="daily"
                    )
                )
            with self.assertRaises(CardLimitError):
                store.create_custom_card(
                    CardSpec.new_custom("第十一张定时", trigger_mode="daily")
                )

    def test_layout_hide_restore_and_soft_delete(self):
        card = self.store.create_custom_card(
            CardSpec.new_custom("客户线索", position=8)
        )
        hidden = self.store.set_layout(
            card.card_id, hidden=True, width="standard", height=390
        )
        self.assertFalse(hidden.visible)
        self.assertEqual(hidden.width, "standard")
        self.assertEqual(hidden.height, 390)
        self.assertNotIn(
            card.card_id, {item.card_id for item in self.store.list_cards()}
        )
        self.assertTrue(self.store.show_card(card.card_id).visible)
        with self.assertRaises(CardValidationError):
            self.store.soft_delete_card("today_brief")

        deleted = self.store.soft_delete_card(card.card_id)
        self.assertIsNotNone(deleted.deleted_at)
        with self.assertRaises(CardNotFoundError):
            self.store.get_card(card.card_id)
        restored = self.store.restore_deleted(card.card_id)
        self.assertIsNone(restored.deleted_at)
        self.assertTrue(restored.enabled)

    def test_purge_deleted_removes_only_expired_custom_mirror(self):
        card = self.store.create_custom_card(CardSpec.new_custom("临时卡片"))
        mirror = self.root / "notes" / "自定义卡片" / "old.md"
        mirror.parent.mkdir(parents=True)
        mirror.write_text("old", encoding="utf-8")
        self.store.set_mirror_path(
            card.card_id, mirror.relative_to(self.root).as_posix()
        )
        self.store.soft_delete_card(card.card_id)
        old = (
            datetime.now(timezone.utc) - timedelta(days=31)
        ).isoformat(timespec="seconds")
        with self.store._transaction() as conn:
            conn.execute(
                "UPDATE cards SET deleted_at = ? WHERE card_id = ?",
                (old, card.card_id),
            )
        purged = self.store.purge_deleted()
        self.assertEqual(purged, [card.card_id])
        self.assertFalse(mirror.exists())
        with self.assertRaises(CardNotFoundError):
            self.store.get_card(card.card_id, include_deleted=True)

    def test_dependency_dag_rejects_missing_cycle_and_depth_over_two(self):
        with self.assertRaises(CardDependencyError):
            self.store.create_custom_card(
                CardSpec.new_custom(
                    "缺失依赖",
                    sources=("confirmed_cards",),
                    dependencies=("card_missing",),
                )
            )
        a = self.store.create_custom_card(CardSpec.new_custom("A"))
        b = self.store.create_custom_card(
            CardSpec.new_custom(
                "B",
                sources=("confirmed_cards",),
                dependencies=(a.card_id,),
            )
        )
        with self.assertRaises(CardDependencyError):
            self.store.update_card(
                a.card_id,
                sources=("confirmed_cards",),
                dependencies=(b.card_id,),
            )
        c = self.store.create_custom_card(
            CardSpec.new_custom(
                "C",
                sources=("confirmed_cards",),
                dependencies=(b.card_id,),
            )
        )
        with self.assertRaises(CardDependencyError):
            self.store.create_custom_card(
                CardSpec.new_custom(
                    "D",
                    sources=("confirmed_cards",),
                    dependencies=(c.card_id,),
                )
            )
        with self.assertRaises(CardDependencyError):
            self.store.soft_delete_card(a.card_id)

    def test_revision_undo_redo_restore_accept_and_preferences(self):
        card = self.store.create_custom_card(CardSpec.new_custom("每日复盘"))
        initial = self.store.add_content_revision(
            card.card_id, "初稿", "initial", "source-1"
        )
        ai = self.store.add_content_revision(
            card.card_id, "AI 修改", "ai", "source-1"
        )
        manual = self.store.add_content_revision(
            card.card_id, "手动修改", "manual", "source-1"
        )
        self.assertEqual(self.store.undo(card.card_id).revision_id, ai.revision_id)
        self.assertEqual(
            self.store.redo(card.card_id).revision_id, manual.revision_id
        )
        restored = self.store.restore_revision(card.card_id, ai.revision_id)
        self.assertEqual(restored.content, "AI 修改")
        self.assertEqual(restored.kind, "restore")
        original = self.store.restore_initial(card.card_id)
        self.assertEqual(original.content, "初稿")
        with self.assertRaises(CardValidationError):
            self.store.add_preference(
                "以后都短一点", card_id=card.card_id, confirmed=False
            )
        with self.assertRaises(CardValidationError):
            self.store.add_preference(
                "以后都短一点",
                card_id=card.card_id,
                source_revision_id=ai.revision_id,
                confirmed=True,
            )
        accepted = self.store.accept_current(card.card_id)
        preference = self.store.add_preference(
            "以后都短一点",
            card_id=card.card_id,
            source_revision_id=accepted.revision_id,
            confirmed=True,
        )
        self.assertTrue(preference.active)
        self.assertEqual(
            self.store.list_preferences(card.card_id)[0].rule_text,
            "以后都短一点",
        )
        self.assertFalse(
            self.store.revoke_preference(preference.preference_id).active
        )
        self.assertEqual(self.store.list_preferences(card.card_id), [])
        self.assertEqual(initial.parent_revision_id, None)

    def test_stale_ai_result_can_be_kept_without_moving_current_pointer(self):
        card = self.store.create_custom_card(CardSpec.new_custom("并发保护"))
        current = self.store.add_content_revision(
            card.card_id, "用户刚刚保存的新内容", "manual", "source-new"
        )
        candidate = self.store.add_content_revision(
            card.card_id,
            "较早请求返回的 AI 内容",
            "ai",
            "source-old",
            make_current=False,
        )
        self.assertFalse(candidate.is_current)
        self.assertEqual(
            self.store.current_revision(card.card_id).revision_id,
            current.revision_id,
        )
        self.assertIn(
            candidate.revision_id,
            {
                revision.revision_id
                for revision in self.store.list_revisions(card.card_id)
            },
        )
        with self.assertRaises(CardValidationError):
            self.store.add_content_revision(
                card.card_id,
                "不能直接确认的候选",
                "ai",
                accepted=True,
                make_current=False,
            )

    def test_rule_versions_ignore_layout_and_can_restore_initial_rules(self):
        card = self.store.create_custom_card(
            CardSpec.new_custom("摘要", rules="最初规则")
        )
        layout = self.store.update_layout(card.card_id, width="full")
        self.assertEqual(layout.rules_version, 1)
        changed = self.store.update_card(card.card_id, rules="新的规则")
        self.assertEqual(changed.rules_version, 2)
        restored = self.store.restore_initial_rules(card.card_id)
        self.assertEqual(restored.rules, "最初规则")
        self.assertEqual(restored.rules_version, 3)

    def test_custom_source_time_range_is_validated_and_versioned(self):
        card = self.store.create_custom_card(
            CardSpec.new_custom(
                "自定义时间",
                time_range="custom:2026-07-01:2026-07-20",
            )
        )
        self.assertEqual(
            card.time_range, "custom:2026-07-01:2026-07-20"
        )
        changed = self.store.update_card(
            card.card_id,
            time_range="custom:2026-07-05:2026-07-10",
        )
        self.assertEqual(changed.rules_version, 2)
        with self.assertRaisesRegex(
            CardValidationError, "开始日期不能晚于结束日期"
        ):
            self.store.update_card(
                card.card_id,
                time_range="custom:2026-07-20:2026-07-01",
            )

    def test_history_keeps_at_most_twenty_recent_plus_initial(self):
        card = self.store.create_custom_card(CardSpec.new_custom("版本上限"))
        for index in range(25):
            self.store.add_content_revision(
                card.card_id, f"版本 {index}", "manual", f"hash-{index}"
            )
        revisions = self.store.list_revisions(card.card_id)
        self.assertLessEqual(len(revisions), 21)
        self.assertEqual(revisions[0].content, "版本 0")

        for index in range(25):
            self.store.update_card(card.card_id, rules=f"规则 {index}")
        with self.store.connection() as conn:
            count = conn.execute(
                """
                SELECT COUNT(*) FROM card_spec_revisions WHERE card_id = ?
                """,
                (card.card_id,),
            ).fetchone()[0]
        self.assertLessEqual(count, 21)
        self.assertEqual(
            self.store.restore_initial_rules(card.card_id).rules, ""
        )

    def test_run_idempotency_and_revision_ownership(self):
        card = self.store.create_custom_card(CardSpec.new_custom("运行记录"))
        run = self.store.start_run(
            card.card_id, source_hash="abc", idempotency_key="same-key"
        )
        same = self.store.start_run(
            card.card_id, source_hash="abc", idempotency_key="same-key"
        )
        self.assertEqual(run.run_id, same.run_id)
        revision = self.store.add_content_revision(
            card.card_id, "结果", "generated", "abc"
        )
        finished = self.store.finish_run(
            run.run_id, status="succeeded", revision_id=revision.revision_id
        )
        self.assertEqual(finished.status, "succeeded")
        self.assertIsNotNone(finished.finished_at)


if __name__ == "__main__":
    unittest.main()

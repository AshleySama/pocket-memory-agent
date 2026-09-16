from __future__ import annotations

import base64
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from pocket_memory.storage import MAX_NOTE_CONTENT_CHARS, MAX_TAGS_PER_NOTE, MAX_TABS_PER_NOTE, NoteStore


class NoteStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = NoteStore(Path(self.temp.name) / "data")

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_create_update_search_and_delete_text_note(self) -> None:
        note = self.store.create_note("", "供应商异常监控方案\n记录采购风险")
        self.assertEqual(note.title, "供应商异常监控方案")
        self.assertTrue((self.store.data_dir / note.markdown_path).exists())

        matches = self.store.search_notes("商异")
        self.assertEqual([item.id for item in matches], [note.id])

        updated = self.store.update_note(note.id, "采购风控", "PR PO GR IR 异常检查")
        self.assertEqual(updated.title, "采购风控")
        self.assertEqual([item.id for item in self.store.search_notes("PO GR")], [note.id])

        self.store.delete_note(note.id)
        self.assertEqual(self.store.list_notes(), [])
        self.assertFalse((self.store.data_dir / note.markdown_path).exists())

    def test_clipboard_image_is_copied_and_linked_from_markdown(self) -> None:
        image = base64.b64encode(b"fake-png").decode("ascii")
        note = self.store.create_note("截图", "后续补充 OCR", f"data:image/png;base64,{image}", "clipboard.png")
        self.assertEqual(note.source_type, "image")
        self.assertTrue((self.store.data_dir / note.attachment_path).exists())
        markdown = (self.store.data_dir / note.markdown_path).read_text(encoding="utf-8")
        self.assertIn(note.attachment_path, markdown)

    def test_welcome_note_is_visible_but_excluded_from_user_data(self) -> None:
        image_path = Path(self.temp.name) / "welcome.jpg"
        image_path.write_bytes(b"welcome-image")

        welcome = self.store.ensure_welcome_note(image_path)

        self.assertIsNotNone(welcome)
        self.assertEqual(welcome.source_type, "welcome")
        self.assertEqual(welcome.title, "欢迎使用 Pocket Memory")
        self.assertIn("从这里开始", welcome.content)
        self.assertTrue((self.store.data_dir / welcome.attachment_path).is_file())
        self.assertIn(welcome.attachment_path, (self.store.data_dir / welcome.markdown_path).read_text(encoding="utf-8"))
        self.assertEqual(self.store.list_notes(), [])
        self.assertEqual([note.id for note in self.store.list_notes(include_welcome=True)], [welcome.id])
        self.assertEqual(self.store.search_notes("Pocket Memory"), [])
        self.assertEqual(self.store.get_taxonomy()["total"], 0)
        self.assertEqual(self.store.note_count(), 0)
        self.assertEqual(welcome.category, "未分类")
        self.assertIsNone(welcome.folder_id)
        self.assertEqual(self.store.get_folders_tree(), [])
        self.assertEqual(self.store.ensure_welcome_note(image_path).id, welcome.id)

    def test_legacy_welcome_note_is_detached_from_its_system_folder(self) -> None:
        image_path = Path(self.temp.name) / "welcome.jpg"
        image_path.write_bytes(b"welcome-image")
        folder = self.store.create_folder(None, "开始使用")
        legacy = self.store.create_note("欢迎使用 Pocket Memory", "旧欢迎内容", folder_id=folder["id"])
        with self.store.connection:
            self.store.connection.execute(
                "UPDATE notes SET source_type = 'welcome' WHERE id = ?", (legacy.id,)
            )

        welcome = self.store.ensure_welcome_note(image_path)

        self.assertEqual(welcome.id, legacy.id)
        self.assertEqual(welcome.category, "未分类")
        self.assertIsNone(welcome.folder_id)
        self.assertEqual(self.store.get_folders_tree(), [])

    def test_welcome_note_can_be_explicitly_restored_for_an_existing_library(self) -> None:
        image_path = Path(self.temp.name) / "welcome.jpg"
        image_path.write_bytes(b"welcome-image")
        self.store.create_note("已有资料", "不应因恢复欢迎笔记而被修改")

        welcome = self.store.restore_welcome_note(image_path)

        self.assertEqual(welcome.source_type, "welcome")
        self.assertEqual(welcome.title, "欢迎使用 Pocket Memory")
        self.assertEqual([note.title for note in self.store.list_notes()], ["已有资料"])
        self.assertEqual(self.store.restore_welcome_note(image_path).id, welcome.id)

    def test_migration_keeps_old_data_and_switches_store(self) -> None:
        note = self.store.create_note("迁移测试", "迁移后仍然可以搜索")
        old_dir = self.store.data_dir
        target = Path(self.temp.name) / "migrated"

        self.store.migrate_data_dir(target)

        self.assertEqual(self.store.data_dir, target.resolve())
        self.assertTrue((old_dir / note.markdown_path).exists())
        self.assertEqual([item.id for item in self.store.search_notes("可以搜索")], [note.id])

    def test_migration_rejects_nested_target(self) -> None:
        with self.assertRaisesRegex(ValueError, "不能互相包含"):
            self.store.migrate_data_dir(self.store.data_dir / "nested")

    def test_complete_backup_contains_database_notes_tabs_and_attachments(self) -> None:
        image = base64.b64encode(b"backup-image").decode("ascii")
        parent = self.store.create_note(
            "项目复盘", "主笔记内容", f"data:image/png;base64,{image}", "proof.png"
        )
        child = self.store.create_note(
            "风险页签", "页签内容", parent_note_id=parent.id, tab_name="风险"
        )
        backup_path = Path(self.temp.name) / "pocket-memory-backup.zip"

        self.store.create_backup(backup_path)

        self.assertTrue(backup_path.is_file())
        with zipfile.ZipFile(backup_path) as archive:
            names = set(archive.namelist())
            self.assertIn("pocket_memory.db", names)
            self.assertIn("manifest.json", names)
            self.assertIn(parent.markdown_path, names)
            self.assertIn(child.markdown_path, names)
            self.assertIn(parent.attachment_path, names)
            manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(manifest["format"], "pocket-memory-backup")
        self.assertEqual(manifest["note_count"], 2)
        self.assertEqual([note["id"] for note in self.store.export_all_notes()], [parent.id, child.id])

        restored_dir = Path(self.temp.name) / "restored"
        with zipfile.ZipFile(backup_path) as archive:
            archive.extractall(restored_dir)
        restored_store = NoteStore(restored_dir)
        try:
            restored_parent = restored_store.get_note(parent.id)
            self.assertEqual(restored_parent.title, "项目复盘")
            self.assertEqual(restored_store.get_note_tabs(parent.id)[1]["id"], child.id)
            self.assertTrue((restored_dir / restored_parent.attachment_path).is_file())
        finally:
            restored_store.close()

    def test_note_tabs_and_content_have_stable_limits(self) -> None:
        parent = self.store.create_note("多页笔记", "主页内容")
        for index in range(MAX_TABS_PER_NOTE - 1):
            self.store.create_note(
                f"页签{index + 2}", "内容", parent_note_id=parent.id, tab_name=f"页签{index + 2}"
            )
        with self.assertRaisesRegex(ValueError, "最多支持"):
            self.store.create_note("页签超限", "内容", parent_note_id=parent.id, tab_name="页签超限")
        with self.assertRaisesRegex(ValueError, "字符"):
            self.store.update_note(parent.id, content="x" * (MAX_NOTE_CONTENT_CHARS + 1))

    def test_categories_tags_and_markdown_metadata(self) -> None:
        note = self.store.create_note(
            "供应商明细",
            "| 供应商 | PO |\n| --- | --- |\n| 示例公司 | 1001 |",
            category="工作",
            subcategory="采购风控",
            tags=["供应商", "#异常监控", "供应商"],
        )
        self.assertEqual(note.category, "工作")
        self.assertEqual(note.subcategory, "采购风控")
        self.assertEqual(note.tags, ["供应商", "异常监控"])
        self.assertEqual([item.id for item in self.store.list_notes(category="工作")], [note.id])
        self.assertEqual([item.id for item in self.store.list_notes(tag="异常监控")], [note.id])
        self.assertEqual([item.id for item in self.store.search_notes("示例公司", tag="供应商")], [note.id])

        taxonomy = self.store.get_taxonomy()
        self.assertEqual(taxonomy["total"], 1)
        self.assertEqual(taxonomy["categories"][0]["name"], "工作")
        markdown = (self.store.data_dir / note.markdown_path).read_text(encoding="utf-8")
        self.assertIn('category: "工作"', markdown)
        self.assertIn("tags: [供应商, 异常监控]", markdown)

    def test_tag_governance_limits_ai_tags_and_merges_reviewed_duplicates(self) -> None:
        first = self.store.create_note("项目计划", "项目复盘", tags=["项目管理", "复盘"])
        second = self.store.create_note("项目进度", "项目推进", tags=["项目管理"])
        third = self.store.create_note("项目管理", "待合并", tags=["项目 管理"])

        self.store.apply_ai_suggestions(
            first.id, "工作", "项目管理", ["项目管理", "排期", "风险", "复盘", "协作", "超出"], "模型整理"
        )
        updated = self.store.get_note(first.id)
        self.assertLessEqual(len(updated.tags), MAX_TAGS_PER_NOTE)
        self.assertIn("项目管理", self.store.tag_vocabulary())

        governance = self.store.tag_governance()
        # The two-tag ceiling applies to manual and AI tags alike, so the
        # three test notes retain only their three distinct manual tags.
        self.assertEqual(governance["total"], 3)
        affected = self.store.merge_tags("项目 管理", "项目管理")
        self.assertEqual(affected, 1)
        self.assertNotIn("项目 管理", [tag["name"] for tag in self.store.get_taxonomy()["tags"]])
        self.assertEqual(self.store.get_note(third.id).tags, ["项目管理"])

    def test_ai_suggestions_preserve_manual_values(self) -> None:
        note = self.store.create_note(
            "人工分类",
            "采购风险",
            category="技术",
            subcategory="SQL",
            tags=["手工标签"],
        )
        note = self.store.apply_ai_suggestions(note.id, "工作", "采购风控", ["供应商", "风险"])

        self.assertEqual(note.category, "技术")
        self.assertEqual(note.subcategory, "SQL")
        self.assertEqual(note.ai_status, "done")
        self.assertEqual(note.tags, ["供应商", "手工标签"])


    def test_ai_suggestions_ignore_existing_manual_tag(self) -> None:
        note = self.store.create_note(
            "duplicate tag",
            "local model tidy",
            tags=["model"],
        )

        note = self.store.apply_ai_suggestions(note.id, "technology", "local model", ["model", "local"])

        self.assertEqual(note.ai_status, "done")
        self.assertEqual(note.tags, ["local", "model"])
        source = self.store.connection.execute(
            """
            SELECT note_tags.source
            FROM note_tags JOIN tags ON note_tags.tag_id = tags.id
            WHERE note_tags.note_id = ? AND tags.name = ?
            """,
            (note.id, "model"),
        ).fetchone()["source"]
        self.assertEqual(source, "manual")

    def test_ai_suggestions_reject_generic_reused_tags(self) -> None:
        first = self.store.create_note("采购审批", "合同金额和授权签字人决定审批层级")
        second = self.store.create_note("RAG验收", "回答必须能够回到来源证据")
        # Simulate an older build that already stored this generic tag as AI output.
        self.store._replace_tags(first.id, ["证据链"], "auto")
        self.store._replace_tags(second.id, ["证据链"], "auto")

        updated = self.store.apply_ai_suggestions(
            first.id, "工作", "采购审批", ["证据链", "采购审批"]
        )

        self.assertEqual(updated.tags, ["采购审批"])
        self.assertNotIn("证据链", self.store.tag_vocabulary())

    def test_reopening_library_purges_only_legacy_generic_auto_tags(self) -> None:
        automatic = self.store.create_note("自动标签", "自动整理曾错误写入泛化标签")
        manual = self.store.create_note("手工标签", "用户明确保留的标签不应被删除")
        with self.store.connection:
            self.store._replace_tags(automatic.id, ["证据链"], "auto")
            self.store._replace_tags(manual.id, ["证据链"], "manual")
        data_dir = self.store.data_dir

        self.store.close()
        self.store = NoteStore(data_dir)

        self.assertEqual(self.store.get_note(automatic.id).tags, [])
        self.assertEqual(self.store.get_note(manual.id).tags, ["证据链"])

    def test_multitab_ai_tags_refresh_and_removed_tags_stay_removed(self) -> None:
        parent = self.store.create_note("项目复盘", "主页记录采购流程")
        child = self.store.create_note(
            "风险页", "供应商风险和付款例外", parent_note_id=parent.id, tab_name="风险"
        )

        self.store.apply_ai_suggestions(child.id, "工作", "采购风控", ["采购", "风险", "供应商"])
        self.store.update_note(parent.id, content="主页已更新：补充采购复盘结论")
        source = self.store.connection.execute(
            "SELECT category_source FROM notes WHERE id = ?", (parent.id,)
        ).fetchone()["category_source"]
        self.assertEqual(source, "auto")

        self.store.remove_group_tag(child.id, "风险")
        self.store.apply_ai_suggestions(parent.id, "工作", "采购风控", ["采购", "风险", "供应商", "复盘"])

        expected = {"采购", "供应商"}
        self.assertEqual(set(self.store.get_note(parent.id).tags), expected)
        self.assertEqual(set(self.store.get_note(child.id).tags), expected)
        self.assertEqual(self.store.excluded_group_tags(parent.id), ["风险"])

    def test_favorite_is_shared_by_tabs_and_manual_order_persists(self) -> None:
        first = self.store.create_note("第一篇", "内容")
        second = self.store.create_note("第二篇", "内容")
        third = self.store.create_note("第三篇", "内容")
        child = self.store.create_note("页签二", "内容", parent_note_id=first.id, tab_name="页签二")

        self.assertEqual(self.store.toggle_favorite(child.id), 1)
        self.assertEqual(self.store.get_note(first.id).favorite, 1)
        self.assertEqual(self.store.get_note(child.id).favorite, 1)
        self.assertEqual([note.id for note in self.store.list_notes(favorite=True)], [first.id])

        self.store.move_note(first.id, second.id)
        self.assertEqual([note.id for note in self.store.list_notes()], [third.id, first.id, second.id])

        welcome = self.store.create_note("欢迎", "系统说明")
        self.store.connection.execute("UPDATE notes SET source_type = 'welcome' WHERE id = ?", (welcome.id,))
        with self.assertRaisesRegex(ValueError, "当前.*列表"):
            self.store.move_note(second.id, welcome.id)

    def test_folder_count_does_not_count_note_tabs_multiple_times(self) -> None:
        folder = self.store.create_folder(None, "项目")
        parent = self.store.create_note("项目总览", "首页", folder_id=folder["id"])
        self.store.create_note("页签二", "第二页", parent_note_id=parent.id, tab_name="页签二")
        self.store.create_note("页签三", "第三页", parent_note_id=parent.id, tab_name="页签三")

        tree = self.store.get_folders_tree()
        self.assertEqual(tree[0]["note_count"], 1)

    def test_ai_organised_note_creates_and_binds_its_navigation_folder(self) -> None:
        parent = self.store.create_note("傍晚散步", "记录秋日的城市散步")
        child = self.store.create_note("补充", "拍摄时间与路线", parent_note_id=parent.id, tab_name="补充")

        self.store.apply_ai_suggestions(parent.id, "生活", "秋日黄昏", ["散步"])

        tree = self.store.get_folders_tree()
        self.assertEqual(tree[0]["name"], "生活")
        self.assertEqual(tree[0]["children"][0]["name"], "秋日黄昏")
        self.assertEqual(tree[0]["children"][0]["note_count"], 1)
        self.assertEqual(self.store.get_note(parent.id).folder_id, tree[0]["children"][0]["id"])
        self.assertEqual(self.store.get_note(child.id).folder_id, tree[0]["children"][0]["id"])

    def test_folder_tree_repairs_a_preexisting_categorised_note_without_folder(self) -> None:
        note = self.store.create_note("秋天", "旧笔记", category="生活", subcategory="秋日黄昏")
        self.assertIsNone(note.folder_id)

        tree = self.store.get_folders_tree()
        self.assertEqual(tree[0]["name"], "生活")
        self.assertIsNotNone(self.store.get_note(note.id).folder_id)


if __name__ == "__main__":
    unittest.main()

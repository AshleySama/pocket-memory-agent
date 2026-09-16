from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pocket_memory.storage import NoteStore
from pocket_memory.wiki import WikiService


class StubWikiEngine:
    available = True
    unavailable_reason = ""

    def generate_wiki_card(self, title, template, evidence_blocks):
        first = evidence_blocks[0]
        return {
            "facts": [
                {"field": "基本情况", "claim": first["text"], "evidence": [first["key"]]},
                {"field": "主营业务", "claim": "不存在于资料中的断言", "evidence": [first["key"]]},
            ]
        }


class WikiServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = NoteStore(Path(self.temp.name) / "data")
        self.service = WikiService(self.store, StubWikiEngine())

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_draft_keeps_only_directly_supported_claims_and_marks_template_gaps(self) -> None:
        note = self.store.create_note("XX公司简介", "XX公司成立于2018年，主营本地知识管理软件。")
        card = self.service.create_draft("XX公司", "company", [{"kind": "note", "id": note.id}])

        verified = [item for item in card["items"] if item["item_type"] == "verified"]
        missing = [item for item in card["items"] if item["item_type"] == "missing"]
        self.assertEqual(card["status"], "draft")
        self.assertEqual(len(verified), 1)
        self.assertEqual(verified[0]["claim"], "XX公司成立于2018年，主营本地知识管理软件。")
        self.assertTrue(verified[0]["evidence"])
        self.assertEqual(verified[0]["evidence"][0]["target_note_id"], note.id)
        self.assertNotIn("不存在于资料中的断言", [item["claim"] for item in card["items"]])
        self.assertGreaterEqual(len(missing), 3)
        self.assertTrue(all(item["claim"] == "未在当前资料范围找到明确来源" for item in missing))

    def test_confirmed_card_becomes_stale_when_source_changes(self) -> None:
        note = self.store.create_note("项目复盘", "项目目标是完成本地知识库试点。")
        card = self.service.create_draft("知识库试点", "project", [{"kind": "note", "id": note.id}])
        confirmed = self.store.confirm_wiki_card(card["id"])
        self.assertEqual(confirmed["status"], "confirmed")

        self.store.update_note(note.id, content="项目目标调整为完成本地知识库正式上线。")
        stale = self.store.get_wiki_card(card["id"])
        self.assertEqual(stale["status"], "stale")
        with self.assertRaisesRegex(ValueError, "重新生成"):
            self.store.confirm_wiki_card(card["id"])


if __name__ == "__main__":
    unittest.main()

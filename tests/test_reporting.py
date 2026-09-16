from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pocket_memory.reporting import ReportService
from pocket_memory.storage import NoteStore
from pocket_memory.intelligence import LlamaCppTextEngine


class StubReportEngine:
    available = True
    unavailable_reason = ""

    def generate_report_claims(self, title, template, evidence_blocks):
        return [
            {
                "topic": "资料要点",
                "claim": f"资料明确说明：{item['text'][:32]}",
                "quote": item["text"][:100],
                "evidence": [item["key"]],
            }
            for item in evidence_blocks[:2]
        ]

    def generate_report_structure(self, title, template, claims):
        return {
            "summary": {"text": "所选资料覆盖本地知识库试点的建设目标，并记录已经完成的导入、引用定位与验证范围等关键进展。", "claim_ids": [item["id"] for item in claims]},
            "sections": [
                {"title": "建设目标", "intro": "资料说明了本地知识库试点的建设目标、实施范围和需要完成的验证工作。", "claim_ids": [claims[0]["id"]]},
                {"title": "已完成能力", "intro": "资料记录了已经完成的文档导入能力，以及可回到原文核对的引用定位能力。", "claim_ids": [claims[1]["id"]]},
            ],
        }


class ReportServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = NoteStore(Path(self.temp.name) / "data")
        self.service = ReportService(self.store, StubReportEngine())

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_report_keeps_only_selected_sources_and_uses_adaptive_sections(self) -> None:
        first = self.store.create_note("项目背景", "项目目标是建立本地知识库，并完成试点验证。")
        second = self.store.create_note("项目进展", "当前已完成文档导入和可定位引用能力。")
        ignored = self.store.create_note("无关资料", "这一条资料不应进入报告。")

        report = self.service.create_draft("项目复盘", "project", [
            {"kind": "note", "id": first.id},
            {"kind": "note", "id": second.id},
        ])

        self.assertEqual(report["template_label"], "项目复盘")
        self.assertEqual([item["id"] for item in report["sources"]], [first.id, second.id])
        content = "\n".join(claim["text"] for section in report["sections"] for claim in section["claims"])
        self.assertIn("本地知识库", content)
        self.assertIn("可定位引用", content)
        self.assertNotIn("不应进入报告", content)
        self.assertEqual([section["title"] for section in report["sections"]], ["建设目标", "已完成能力"])
        self.assertIn("本地知识库", report["summary"]["text"])
        self.assertTrue(all(section["claim_ids"] for section in report["sections"]))
        self.assertTrue(all(claim["quote"] for section in report["sections"] for claim in section["claims"]))

    def test_report_allows_one_source_when_it_has_multiple_facts(self) -> None:
        note = self.store.create_note("单条资料", "项目目标是建立本地知识库并完成试点验证。当前已完成文档导入和可定位引用能力。")
        report = self.service.create_draft("单资料报告", "adaptive", [{"kind": "note", "id": note.id}])
        self.assertEqual([item["id"] for item in report["sources"]], [note.id])
        self.assertGreaterEqual(report["quality"]["claim_count"], 2)

    def test_report_requires_at_least_one_source(self) -> None:
        with self.assertRaisesRegex(ValueError, "至少选择 1 项"):
            self.service.create_draft("空资料报告", "adaptive", [])

    def test_single_source_uses_verbatim_facts_when_model_under_produces(self) -> None:
        class OneClaimEngine(StubReportEngine):
            def generate_report_claims(self, title, template, evidence_blocks):
                item = evidence_blocks[0]
                return [{"topic": "项目目标", "claim": item["text"], "quote": item["text"], "evidence": [item["key"]]}]

        note = self.store.create_note("技术资料", "第一项已完成本地索引能力建设。第二项已完成来源定位与原文核对能力。")
        report = ReportService(self.store, OneClaimEngine()).create_draft("技术资料", "adaptive", [{"kind": "note", "id": note.id}])
        facts = [claim for section in report["sections"] for claim in section["claims"]]
        self.assertGreaterEqual(len(facts), 2)
        self.assertTrue(all(claim["quote"] in {"第一项已完成本地索引能力建设。", "第二项已完成来源定位与原文核对能力。"} for claim in facts))

    def test_report_evidence_removes_markdown_table_noise_without_rewriting(self) -> None:
        source = "| 编号 | 结论 |\n| --- | --- |\n| 01 | 已完成本地部署 |\n> **下一步**：安排验收"
        cleaned = ReportService._clean_evidence(source)
        self.assertNotIn("---", cleaned)
        self.assertNotIn("|", cleaned)
        self.assertIn("编号；结论", cleaned)
        self.assertIn("已完成本地部署", cleaned)
        self.assertIn("下一步：安排验收", cleaned)

    def test_report_rejects_claim_without_verbatim_source_quote(self) -> None:
        class UnsupportedEngine(StubReportEngine):
            def generate_report_claims(self, title, template, evidence_blocks):
                return [{
                    "topic": "上线结论",
                    "claim": "资料已经全面验证并可以立即在所有环境上线。",
                    "quote": "原文中不存在的虚构引句。",
                    "evidence": [evidence_blocks[0]["key"]],
                }]

        first = self.store.create_note("资料一", "项目已经完成本地试点，并保留每条结论的来源定位。")
        second = self.store.create_note("资料二", "下一步需要完成真实场景验收，确认结果可以稳定复现。")
        service = ReportService(self.store, UnsupportedEngine())
        report = service.create_draft("试点报告", "project", [
            {"kind": "note", "id": first.id}, {"kind": "note", "id": second.id},
        ])
        facts = [claim for section in report["sections"] for claim in section["claims"]]
        self.assertGreaterEqual(len(facts), 2)
        self.assertNotIn("立即在所有环境上线", "\n".join(claim["text"] for claim in facts))
        self.assertTrue(all(
            claim["quote"] in {
                "项目已经完成本地试点，并保留每条结论的来源定位。",
                "下一步需要完成真实场景验收，确认结果可以稳定复现。",
            }
            for claim in facts
        ))

    def test_structure_is_free_to_use_material_driven_headings(self) -> None:
        class MaterialDrivenEngine(StubReportEngine):
            def generate_report_claims(self, title, template, evidence_blocks):
                return [
                    {
                        "topic": "验收方式",
                        "claim": "每个典型问题都需要记录三轮测试的性能数据。",
                        "quote": "每个典型问题都需要记录三轮测试的性能数据。",
                        "evidence": [evidence_blocks[0]["key"]],
                    },
                    {
                        "topic": "发布边界",
                        "claim": "真实资料验收完成前不能将结果写成正式发布结论。",
                        "quote": "真实资料验收完成前不能将结果写成正式发布结论。",
                        "evidence": [evidence_blocks[1]["key"]],
                    },
                ]

            def generate_report_structure(self, title, template, claims):
                return {
                    "summary": {"text": "资料同时明确了测试记录要求和发布结论的使用边界，相关验收动作需要在同一轮验证中一并执行。", "claim_ids": [item["id"] for item in claims]},
                    "sections": [
                        {"title": "验收记录", "intro": "测试过程需要保留可重复核验的数据记录，便于比较不同轮次的结果。", "claim_ids": [claims[0]["id"]]},
                        {"title": "发布边界", "intro": "在真实资料验收完成之前，不能将局部验证结论扩大为正式发布结论。", "claim_ids": [claims[1]["id"]]},
                    ],
                }

        first = self.store.create_note("测试要求", "每个典型问题都需要记录三轮测试的性能数据。")
        second = self.store.create_note("发布边界", "真实资料验收完成前不能将结果写成正式发布结论。")
        report = ReportService(self.store, MaterialDrivenEngine()).create_draft("RAG", "proposal", [
            {"kind": "note", "id": first.id}, {"kind": "note", "id": second.id},
        ])
        sections = {section["title"]: section["claims"] for section in report["sections"]}
        self.assertEqual(set(sections), {"验收记录", "发布边界"})

    def test_evidence_budget_keeps_selected_sources_instead_of_using_first_document_only(self) -> None:
        notes = [
            self.store.create_note(f"资料{i}", "这是一段完整的项目资料，用于说明关键目标、实施步骤和验收边界。" * 8)
            for i in range(1, 4)
        ]
        _, evidence = self.service._collect_sources(
            [{"kind": "note", "id": note.id} for note in notes], "项目验收"
        )
        self.assertLessEqual(len(evidence), 10)
        self.assertLessEqual(sum(len(item.text) + 50 for item in evidence), 1800 + 470)
        self.assertEqual({item.source_id for item in evidence}, {note.id for note in notes})

    def test_fallback_layout_never_discards_verified_claims(self) -> None:
        claims = [
            {"id": f"C{index}", "topic": f"主题{index}", "text": "已核验事实"}
            for index in range(1, 7)
        ]
        layout = self.service._fallback_layout(claims)
        retained = [claim_id for section in layout["sections"] for claim_id in section["claim_ids"]]
        self.assertEqual(retained, [item["id"] for item in claims])
        self.assertLessEqual(len(layout["sections"]), 4)

    def test_nested_quotes_do_not_become_duplicate_report_facts(self) -> None:
        class DuplicateEvidenceEngine(StubReportEngine):
            def generate_report_claims(self, title, template, evidence_blocks):
                return [
                    {"topic": "发布边界", "claim": "真实资料验收完成前不能扩大已有发布结论。", "quote": "真实资料验收完成前不能扩大已有发布结论。", "evidence": [evidence_blocks[0]["key"]]},
                    {"topic": "发布边界", "claim": "验收完成前不能扩大已有发布结论。", "quote": "验收完成前不能扩大已有发布结论。", "evidence": [evidence_blocks[1]["key"]]},
                ]

        first = self.store.create_note("资料一", "真实资料验收完成前不能扩大已有发布结论。")
        second = self.store.create_note("资料二", "验收完成前不能扩大已有发布结论。")
        service = ReportService(self.store, DuplicateEvidenceEngine())
        with self.assertRaisesRegex(RuntimeError, "可核验专题内容"):
            service.create_draft("发布边界", "adaptive", [
                {"kind": "note", "id": first.id}, {"kind": "note", "id": second.id},
            ])

    def test_model_prompt_forbids_recombining_source_technical_terms(self) -> None:
        class CaptureEngine(LlamaCppTextEngine):
            @property
            def available(self):
                return True

            def _chat(self, messages, max_tokens, temperature):
                self.captured = messages[0]["content"]
                return "技术架构|资料采用全文与向量索引。|资料采用全文与向量索引。|E1"

        engine = object.__new__(CaptureEngine)
        claims = engine.generate_report_claims(
            "RAG", {"writing_focus": "按资料内容自然组织"},
            [{"key": "E1", "source_label": "资料", "location": "正文", "text": "资料采用全文与向量索引。"}],
        )
        self.assertIn("不得拼接或改变原文技术短语的修饰关系", engine.captured)
        self.assertIn("全文抽取", engine.captured)
        self.assertEqual(claims[0]["evidence"], ["E1"])

    def test_report_structure_parser_accepts_three_field_summary_line(self) -> None:
        class CaptureEngine(LlamaCppTextEngine):
            @property
            def available(self):
                return True

            def _chat(self, messages, max_tokens, temperature):
                return "\n".join([
                    "摘要|资料先完成本地检索能力建设，再通过同题对比和真实资料验收确认效果与发布边界。|C1,C2",
                    "章节|本地能力|资料说明本地检索能力的实现方式和关键组成。|C1",
                    "章节|验收边界|资料要求通过同题对比与真实资料验收确认结论。|C2",
                ])

        engine = object.__new__(CaptureEngine)
        structure = engine.generate_report_structure("RAG", {"writing_focus": "按资料内容自然组织"}, [
            {"id": "C1", "topic": "本地能力", "text": "本地建立检索索引。"},
            {"id": "C2", "topic": "验收边界", "text": "真实资料验收后再发布。"},
        ])
        self.assertEqual(structure["summary"]["claim_ids"], "C1,C2")
        self.assertEqual(len(structure["sections"]), 2)


if __name__ == "__main__":
    unittest.main()

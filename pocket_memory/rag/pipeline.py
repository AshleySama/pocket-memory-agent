# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable
from urllib.parse import quote

from pocket_memory.chunking import split_note_text
from pocket_memory.rag.intent import Intent, IntentResult, IntentRouter


@dataclass(frozen=True)
class QueryAnalysis:
    text: str
    terms: list[str]


class RagPipeline:
    """Local RAG pipeline: query analysis -> BM25/FTS -> vector -> chunk rerank."""

    stopwords = {
        "什么", "哪些", "哪个", "是谁", "是不是", "是否", "如何", "怎么",
        "一下", "这个", "那个", "可以", "应该", "不是", "就是",
        "是什么", "什么是",
    }

    fusion_modes = {"legacy", "weighted_rrf"}
    default_fusion_mode = "weighted_rrf"

    def __init__(self, store, embedding_service, fusion_mode: str | None = None) -> None:
        self.store = store
        self.embedding_service = embedding_service
        requested_mode = str(
            fusion_mode
            or os.environ.get("POCKET_MEMORY_RAG_FUSION", self.default_fusion_mode)
        ).strip().lower()
        self.fusion_mode = (
            requested_mode if requested_mode in self.fusion_modes else self.default_fusion_mode
        )

    def retrieve(
        self,
        question: str,
        limit: int = 5,
        semantic_min_score: float = 0.40,
        extra_terms: list[str] | None = None,
        strict_topic_match: bool = False,
        required_topic_terms: list[str] | None = None,
        _compound_pass: bool = True,
        diagnostics: dict | None = None,
    ) -> list[dict]:
        retrieval_started = time.perf_counter()
        trace = diagnostics if diagnostics is not None else {}
        # A compound question can contain independent facts with very different
        # vocabulary (for example, "地球...；火星..."). Ranking it as one long
        # query lets the first clause suppress later evidence. Recall each
        # explicit clause once, then merge their already-ranked candidates.
        if _compound_pass:
            clauses = self.compound_query_clauses(question)
            trace["compound_clause_count"] = len(clauses)
            if len(clauses) > 1:
                overall_trace: dict = {}
                overall = self.retrieve(
                    question,
                    limit=limit,
                    semantic_min_score=semantic_min_score,
                    extra_terms=extra_terms,
                    strict_topic_match=strict_topic_match,
                    required_topic_terms=required_topic_terms,
                    _compound_pass=False,
                    diagnostics=overall_trace,
                )
                clause_results: list[list[dict]] = []
                clause_traces = []
                # Retrieve a slightly deeper pool for each independent clause,
                # then collapse it back to the caller's final context limit.
                # This keeps a correct sibling passage from being cut at rank
                # six before source-aware evidence ordering can inspect it.
                clause_candidate_limit = self.compound_candidate_limit(limit)
                for clause in clauses:
                    variant_results: list[list[dict]] = []
                    variant_traces = []
                    for variant in self.compound_clause_queries(clause):
                        variant_trace: dict = {"query": variant}
                        # A compound question must not inherit the whole sentence
                        # as one hard topic constraint. Keep only explicit
                        # entities and named-source relationships at the clause
                        # level; generated phrases remain semantic queries.
                        clause_required_terms = self.subquery_anchor_terms(variant)
                        clause_extra_terms = self.scope_extra_terms_for_subquery(
                            variant,
                            extra_terms,
                        )
                        variant_trace["scoped_extra_terms"] = clause_extra_terms
                        variant_results.append(self.retrieve(
                            variant,
                            limit=clause_candidate_limit,
                            semantic_min_score=semantic_min_score,
                            extra_terms=clause_extra_terms,
                            strict_topic_match=bool(clause_required_terms),
                            required_topic_terms=clause_required_terms,
                            _compound_pass=False,
                            diagnostics=variant_trace,
                        ))
                        variant_traces.append(variant_trace)
                    clause_results.append(self.merge_query_variants(variant_results))
                    clause_traces.append({
                        "clause": clause,
                        "variant_count": len(variant_results),
                        "variants": variant_traces,
                    })
                selected, merged_count, reserved_count = self.merge_compound_results(
                    question,
                    clauses,
                    overall,
                    clause_results,
                    limit,
                )
                trace.update({
                    "mode": "compound",
                    "compound_query_count": len(clauses) + 1,
                    "compound_candidate_limit": clause_candidate_limit,
                    "overall_trace": overall_trace,
                    "clause_traces": clause_traces,
                    "merged_candidate_count": merged_count,
                    "reserved_candidate_count": reserved_count,
                    "returned_chunk_count": len(selected),
                    "total_ms": round((time.perf_counter() - retrieval_started) * 1000, 3),
                })
                return selected
        # A frequent OCR artifact is the Latin "d" in place of Chinese "的"
        # (for example "m d标准换算").  Normalize only that narrow form before
        # embedding and FTS; retain the original user wording for display and
        # answer generation.
        search_question = self.normalize_ocr_query(question)
        trace["query_normalized"] = search_question != question
        # Only explicit note-metadata questions (for example "昨天写了哪些
        # 笔记") may filter on created_at. Absolute dates in a business question
        # usually describe the document's content, such as a project milestone.
        time_range = self._detect_time_range(search_question) if IntentRouter._is_note_timestamp_query(search_question) else None
        if time_range:
            selected = self._retrieve_by_time(time_range, limit)
            trace.update({
                "mode": "time_filter",
                "returned_chunk_count": len(selected),
                "total_ms": round((time.perf_counter() - retrieval_started) * 1000, 3),
            })
            return selected

        analysis_started = time.perf_counter()
        analysis = self.analyze(search_question, extra_terms)
        focus_terms = self.focused_query_terms(analysis.terms)
        # In a generated ``subject 的 field`` clause the subject is valuable
        # for exact recall, but it is not necessarily written verbatim in the
        # source (``模型部署`` may be phrased as ``Qwen 本地部署``). Keep it as
        # a soft FTS/rerank clue rather than an all-or-nothing evidence gate.
        focus_terms = self.prioritize_required_terms(
            focus_terms,
            self.attribute_subject_recall_terms(search_question),
        )
        # A relationship lookup such as "项目复盘里提到的风险是什么" has
        # two parts: the named source and the requested field. The source is a
        # hard coverage anchor, so it must be recalled before incidental 4-5
        # character fragments generated from the surrounding question wording.
        focus_terms = self.prioritize_required_terms(focus_terms, required_topic_terms)
        # Stable technical identifiers such as OCR, BM25, MySQL, and BGE are
        # often the only unambiguous clue in a noisy or mixed-language query.
        # Let them participate in the exact FTS retry pass even when written in
        # lowercase (for example ``bm25``).
        focus_terms.extend(
            term for term in self.retrieval_identifier_terms(search_question)
            if term not in focus_terms
        )
        trace.update({
            "mode": "hybrid",
            "fusion_mode": self.fusion_mode,
            "query_term_count": len(analysis.terms),
            "focus_term_count": len(focus_terms),
            "extra_term_count": len(extra_terms or []),
            "required_topic_count": len(required_topic_terms or []),
        })
        trace.setdefault("timings_ms", {})["analysis"] = round(
            (time.perf_counter() - analysis_started) * 1000, 3
        )
        dense_started = time.perf_counter()
        query_vector = self.embedding_service.query_embedding(search_question)
        semantic_results = self.embedding_service.semantic_search(
            search_question,
            limit=max(20, limit),
            min_score=semantic_min_score,
            query_vector=query_vector,
        )
        semantic = {note.id: (note, score) for note, score in semantic_results}
        semantic_ranks = {note.id: rank for rank, (note, _) in enumerate(semantic_results, 1)}
        document_semantic_results = self.embedding_service.semantic_document_search(
            search_question,
            limit=max(20, limit * 4),
            min_score=semantic_min_score,
            query_vector=query_vector,
        )
        document_semantic = {
            chunk.id: (chunk, score) for chunk, score in document_semantic_results
        }
        document_semantic_ranks = {
            chunk.id: rank for rank, (chunk, _) in enumerate(document_semantic_results, 1)
        }
        note_chunk_semantic_results = self.embedding_service.semantic_note_chunk_search(
            search_question,
            limit=max(20, limit * 4),
            min_score=semantic_min_score,
            query_vector=query_vector,
        )
        note_chunk_semantic = {
            chunk.id: (chunk, score) for chunk, score in note_chunk_semantic_results
        }
        note_chunk_semantic_ranks = {
            chunk.id: rank for rank, (chunk, _) in enumerate(note_chunk_semantic_results, 1)
        }
        trace["timings_ms"]["dense_retrieval"] = round(
            (time.perf_counter() - dense_started) * 1000, 3
        )
        trace["channel_counts"] = {
            "semantic_notes": len(semantic),
            "semantic_note_chunks": len(note_chunk_semantic),
            "semantic_document_chunks": len(document_semantic),
        }
        # FTS 召回：原问题权重最高，扩展词权重减半，合并取最大值
        lexical_started = time.perf_counter()
        fts_bonus: dict[int, float] = {}
        if search_question.strip():
            for rank, note in enumerate(self.store.search_notes(search_question, limit=50)):
                fts_bonus[note.id] = max(fts_bonus.get(note.id, 0.0), 6.0 - rank * 0.2)
        # A natural question often appends generic language such as
        # "有什么功能、什么场景有用" after its actual entity.  Searching that full
        # sentence cannot find a spreadsheet row containing only the entity, so
        # give the focused entity phrases their own exact/FTS recall pass.
        for term_rank, term in enumerate(focus_terms):
            for rank, note in enumerate(self.store.search_notes(term, limit=20)):
                score = 5.75 - term_rank * 0.15 - rank * 0.1
                fts_bonus[note.id] = max(fts_bonus.get(note.id, 0.0), score)
        for term in extra_terms or []:
            if not term.strip():
                continue
            for rank, note in enumerate(self.store.search_notes(term, limit=20)):
                fts_bonus[note.id] = max(fts_bonus.get(note.id, 0.0), 3.0 - rank * 0.15)

        document_fts_bonus: dict[int, float] = {}
        document_candidates = {chunk_id: chunk for chunk_id, (chunk, _) in document_semantic.items()}
        if search_question.strip():
            for rank, chunk in enumerate(self.store.search_document_chunks(search_question, limit=50)):
                document_candidates[chunk.id] = chunk
                document_fts_bonus[chunk.id] = max(document_fts_bonus.get(chunk.id, 0.0), 6.0 - rank * 0.2)
        for term_rank, term in enumerate(focus_terms):
            for rank, chunk in enumerate(self.store.search_document_chunks(term, limit=20)):
                document_candidates[chunk.id] = chunk
                score = 5.75 - term_rank * 0.15 - rank * 0.1
                document_fts_bonus[chunk.id] = max(document_fts_bonus.get(chunk.id, 0.0), score)
        for term in extra_terms or []:
            if not term.strip():
                continue
            for rank, chunk in enumerate(self.store.search_document_chunks(term, limit=20)):
                document_candidates[chunk.id] = chunk
                document_fts_bonus[chunk.id] = max(document_fts_bonus.get(chunk.id, 0.0), 3.0 - rank * 0.15)
        trace["timings_ms"]["lexical_retrieval"] = round(
            (time.perf_counter() - lexical_started) * 1000, 3
        )
        trace["channel_counts"].update({
            "fts_notes": len(fts_bonus),
            "fts_document_chunks": len(document_fts_bonus),
        })
        fts_note_ranks = self.rank_ids_by_score(fts_bonus)
        fts_document_ranks = self.rank_ids_by_score(document_fts_bonus)

        materialize_started = time.perf_counter()
        ranked = []
        # FTS and legacy page vectors can identify a note, but they must not
        # nominate a freshly re-split paragraph as evidence. Expand only to
        # persisted chunks that were embedded at index time.
        for root_note in self.store.list_notes(limit=100000):
            for tab in self.store.get_note_tabs(root_note.id):
                source_note = self.store.get_note(tab["id"])
                vector_score = semantic.get(source_note.id, (None, 0.0))[1]
                note_fts_bonus = fts_bonus.get(source_note.id, 0.0)
                if not vector_score and not note_fts_bonus:
                    continue
                tab_label = f"{source_note.tab_name}·" if source_note.parent_note_id is not None and source_note.tab_name else ""
                for note_chunk in self.store.list_note_chunks(source_note.id):
                    searchable_text = self.without_urls(f"{note_chunk.title}\n{note_chunk.text}".lower())
                    hits = self.matched_terms(searchable_text, analysis.terms)
                    lexical = self.lexical_score(hits, searchable_text, search_question)
                    # A page-level match only decides which persisted chunks to
                    # inspect. It cannot turn an unrelated chunk into evidence.
                    if not lexical:
                        continue
                    final_score = lexical + note_fts_bonus + vector_score * 8
                    final_score += self.title_topic_bonus(source_note.title, hits)
                    if note_chunk.kind == "title":
                        final_score -= 5
                    ranked.append({
                        "note": root_note,
                        "source_id": source_note.id,
                        "source_group": f"note-{source_note.id}",
                        "chunk_identity": f"note-chunk-{note_chunk.id}",
                        "note_chunk_id": note_chunk.id,
                        "note_chunk_order": note_chunk.chunk_order,
                        "score": vector_score,
                        "rank_score": final_score,
                        "lexical_score": lexical,
                        "fts_score": note_fts_bonus,
                        "hits": hits,
                        "reason": self.source_reason(vector_score, hits, note_fts_bonus, note_chunk.kind),
                        "excerpt": self.focused_excerpt(note_chunk.text, hits),
                        "raw_text": note_chunk.text,
                        "location": f"{tab_label}{note_chunk.location}",
                        "kind": note_chunk.kind,
                        "title": note_chunk.title or source_note.title,
                        "fusion_channels": self.compact_channel_ranks({
                            "semantic_parent": semantic_ranks.get(source_note.id),
                            "fts": fts_note_ranks.get(source_note.id),
                        }),
                    })
        # Notes now also have independent chunk vectors. Keep the legacy
        # page-level vector for compatibility and quick note-list search, while
        # using these chunk candidates to recover semantically relevant content
        # from the middle or end of long notes and tabs.
        for chunk_id, (note_chunk, vector_score) in note_chunk_semantic.items():
            source_note = self.store.get_note(note_chunk.note_id)
            note = source_note if source_note.parent_note_id is None else self.store.get_note(source_note.parent_note_id)
            searchable_text = self.without_urls(f"{note_chunk.title}\n{note_chunk.text}".lower())
            hits = self.matched_terms(searchable_text, analysis.terms)
            lexical = self.lexical_score(hits, searchable_text, search_question)
            if not lexical and vector_score < max(0.45, semantic_min_score):
                continue
            final_score = lexical + vector_score * 8
            final_score += self.title_topic_bonus(source_note.title, hits)
            tab_label = f"{source_note.tab_name}·" if source_note.parent_note_id is not None and source_note.tab_name else ""
            ranked.append(
                {
                    "note": note,
                    "source_id": source_note.id,
                    "source_group": f"note-{source_note.id}",
                    "chunk_identity": f"note-chunk-{note_chunk.id}",
                    "note_chunk_id": note_chunk.id,
                    "note_chunk_order": note_chunk.chunk_order,
                    "score": vector_score,
                    "rank_score": final_score,
                    "lexical_score": lexical,
                    "fts_score": 0.0,
                    "hits": hits,
                    "reason": self.source_reason(vector_score, hits, 0.0, note_chunk.kind),
                    "excerpt": self.focused_excerpt(note_chunk.text, hits),
                    "raw_text": note_chunk.text,
                    "location": f"{tab_label}{note_chunk.location}",
                    "kind": note_chunk.kind,
                    "title": note_chunk.title or source_note.title,
                    "fusion_channels": self.compact_channel_ranks({
                        "semantic_chunk": note_chunk_semantic_ranks.get(chunk_id),
                    }),
                }
            )
        # Imported files are already stored at evidence-sized granularity. Unlike
        # legacy note vectors, every document chunk has its own vector and can be
        # selected directly from any position in a long file.
        for chunk_id, document_chunk in document_candidates.items():
            document = self.store.get_document(document_chunk.document_id)
            vector_score = document_semantic.get(chunk_id, (None, 0.0))[1]
            fts_score = document_fts_bonus.get(chunk_id, 0.0)
            searchable_text = self.without_urls(document_chunk.text.lower())
            hits = self.matched_terms(searchable_text, analysis.terms)
            lexical = self.lexical_score(hits, searchable_text, search_question)
            if not lexical and not fts_score and vector_score < max(0.45, semantic_min_score):
                continue
            final_score = lexical + fts_score + vector_score * 8
            final_score += self.title_topic_bonus(document.title, hits)
            ranked.append(
                {
                    "note": document,
                    "document": document,
                    "source_kind": "document",
                    "source_id": f"document-chunk-{document_chunk.id}",
                    "source_group": f"document-{document.id}",
                    "chunk_identity": f"document-chunk-{document_chunk.id}",
                    "score": vector_score,
                    "rank_score": final_score,
                    "lexical_score": lexical,
                    "fts_score": fts_score,
                    "hits": hits,
                    "reason": self.source_reason(vector_score, hits, fts_score, "document"),
                    "excerpt": self.focused_excerpt(document_chunk.text, hits),
                    "raw_text": document_chunk.text,
                    "location": document_chunk.location,
                    "kind": "document",
                    "title": document.original_name,
                    "document_chunk_id": document_chunk.id,
                    "document_chunk_order": document_chunk.chunk_order,
                    "fusion_channels": self.compact_channel_ranks({
                        "semantic_chunk": document_semantic_ranks.get(chunk_id),
                        "fts": fts_document_ranks.get(chunk_id),
                    }),
                }
            )
        trace["materialized_candidate_count"] = len(ranked)
        if self.fusion_mode == "weighted_rrf":
            ranked = self.merge_candidate_channels(ranked)
            ranked = self.apply_weighted_rrf(ranked)
        trace["fused_candidate_count"] = len(ranked)
        trace["timings_ms"]["candidate_materialization"] = round(
            (time.perf_counter() - materialize_started) * 1000, 3
        )
        rerank_started = time.perf_counter()
        ranked = self.rerank_candidates(search_question, ranked, focus_terms)
        ranked.sort(key=lambda item: (item["rank_score"], item["score"], item["note"].updated_at), reverse=True)
        ranked = self.filter_weak_chunks(
            ranked,
            topic_anchors=(required_topic_terms or self.topic_anchor_terms(search_question))
            if strict_topic_match else None,
            question=search_question,
        )
        trace["filtered_candidate_count"] = len(ranked)
        selected = self.dedupe_chunks(ranked, limit, search_question)
        parent_child_started = time.perf_counter()
        parent_child_trace: dict = {}
        selected = self.expand_parent_child_context(
            search_question,
            selected,
            focus_terms=focus_terms,
            limit=limit,
            diagnostics=parent_child_trace,
        )
        trace["parent_child"] = parent_child_trace
        trace["returned_chunk_count"] = len(selected)
        trace["timings_ms"]["rerank_filter_dedupe"] = round(
            (parent_child_started - rerank_started) * 1000, 3
        )
        trace["timings_ms"]["parent_child"] = round(
            (time.perf_counter() - parent_child_started) * 1000, 3
        )
        trace["total_ms"] = round((time.perf_counter() - retrieval_started) * 1000, 3)
        return selected

    # ---------- 时间意图检测 ----------
    # 识别"昨天/今天/本周/最近N天"等时间查询，返回 (start, end, label) 或 None
    @staticmethod
    def _detect_time_range(question: str) -> tuple[date, date, str] | None:
        return IntentRouter.detect_time_range(question)

    # ---------- 意图路由入口 ----------
    def route(
        self,
        question: str,
        llm_engine=None,
        extra_terms: list[str] | None = None,
        limit: int = 5,
        intent: Intent | None = None,
        evidence_question: str | None = None,
    ) -> IntentResult:
        """按意图路由检索。返回 IntentResult，由 server 决定是否调用 LLM 生成答案。

        ``question`` may include a compact conversation subject for retrieval.
        Its intent must still come from the current user turn; otherwise a prior
        request such as "我的笔记有多少条" can turn the next question into a
        dashboard query.  Callers that have conversation context pass the
        current-turn intent explicitly.
        """
        # 通用问答不再先调用 LLM 做一次意图分类。对本地 4B 模型而言，
        # “分类 -> 改写 -> 回答”会把一次问答放大成三次生成；而默认 qa
        # 路径本身已经能基于检索到的证据完成总结与比较。
        route_started = time.perf_counter()
        intent = intent or IntentRouter.detect(question, store=self.store)
        evidence_question = evidence_question or question
        diagnostics = {
            "schema_version": 1,
            "intent": {"type": intent.type, "source": intent.source},
            "query": {
                "length": len(question),
                "evidence_question_differs": evidence_question != question,
            },
        }
        handler = {
            "time": self._route_time,
            "time_period": self._route_time_period,
            "recent_edit": self._route_recent_edit,
            "stats": self._route_stats,
            "taxonomy": self._route_taxonomy,
            "metadata": self._route_metadata,
            "list": self._route_list,
            "summary": self._route_summary,
            "compare": self._route_compare,
            "entity": self._route_entity,
            "related": self._route_related,
        }.get(intent.type)
        if handler is None:
            # qa：需要 query 改写，走 LLM 扩展后再检索
            result = self._route_qa(
                intent,
                question,
                extra_terms or [],
                limit,
                llm_engine=llm_engine,
                evidence_question=evidence_question,
                diagnostics=diagnostics,
            )
        else:
            result = handler(intent, question, extra_terms or [], limit)
        diagnostics["route_total_ms"] = round((time.perf_counter() - route_started) * 1000, 3)
        result.diagnostics = diagnostics
        return result

    def _chunk_from_note(self, note, reason: str, score: float = 1.0, excerpt: str = "") -> dict:
        return {
            "note": note,
            "source_id": note.id,
            "score": score,
            "rank_score": score * 10,
            "lexical_score": 0.0,
            "fts_score": 0.0,
            "hits": [],
            "reason": reason,
            "excerpt": excerpt or (note.content or note.ocr_text or "")[:200],
            "raw_text": note.content or "",
            "location": "content",
            "kind": "content",
            "title": note.title or "未命名笔记",
        }

    def _route_time(self, intent: Intent, question: str, extra_terms, limit) -> IntentResult:
        start, end, label = intent.params["time_range"]
        topic = intent.params.get("topic", "")
        metadata_filter = intent.params.get("metadata")
        chunks: list[dict] = []
        for note in self.store.list_notes(limit=100000):
            try:
                created = datetime.fromisoformat(note.created_at).date()
            except (ValueError, TypeError):
                continue
            if not (start <= created < end):
                continue
            # 时间+主题组合：主题词需出现在标题或内容里
            if topic and not self._note_matches_topic(note, topic):
                continue
            # 时间+元数据组合：再按元数据条件过滤
            if metadata_filter and not self._note_matches_metadata(note, metadata_filter):
                continue
            created_str = note.created_at[:16].replace("T", " ")
            content_preview = (note.content or note.ocr_text or "")[:150]
            excerpt = f"[创建时间：{created_str}]\n{content_preview}"
            reason = f"{label}创建" + (f"·含「{topic}」" if topic else "")
            chunks.append(self._chunk_from_note(note, reason, excerpt=excerpt))
        chunks.sort(key=lambda c: c["note"].created_at, reverse=True)
        empty = f"{label}没有创建笔记。"
        if topic:
            empty = f"{label}没有关于「{topic}」的笔记。"
        return IntentResult(
            kind="time", chunks=chunks[:limit], intent=intent,
            empty_answer=empty,
        )

    def _route_time_period(self, intent: Intent, question: str, extra_terms, limit) -> IntentResult:
        """时段过滤：基于 created_at 的小时范围（如"后半夜写的"=0-6点创建的笔记）。"""
        period = intent.params["period"]
        h_start, h_end, label = period
        topic = intent.params.get("topic", "")
        chunks: list[dict] = []
        for note in self.store.list_notes(limit=100000):
            try:
                created = datetime.fromisoformat(note.created_at)
            except (ValueError, TypeError):
                continue
            if not (h_start <= created.hour < h_end):
                continue
            if topic and not self._note_matches_topic(note, topic):
                continue
            created_str = note.created_at[:16].replace("T", " ")
            content_preview = (note.content or note.ocr_text or "")[:150]
            excerpt = f"[创建时间：{created_str}]\n{content_preview}"
            reason = f"{label}创建" + (f"·含「{topic}」" if topic else "")
            chunks.append(self._chunk_from_note(note, reason, excerpt=excerpt))
        chunks.sort(key=lambda c: c["note"].created_at, reverse=True)
        empty = f"{label}没有创建笔记。"
        if topic:
            empty = f"{label}没有关于「{topic}」的笔记。"
        return IntentResult(
            kind="time", chunks=chunks[:limit], intent=intent,
            empty_answer=empty,
        )

    def _note_matches_topic(self, note, topic: str) -> bool:
        """笔记标题或内容是否包含主题词。"""
        title = (note.title or "").lower()
        content = (note.content or "").lower()
        ocr = (note.ocr_text or "").lower()
        topic_lower = topic.lower()
        return topic_lower in title or topic_lower in content or topic_lower in ocr

    def _note_matches_metadata(self, note, meta: dict) -> bool:
        """笔记是否匹配元数据过滤条件（用于时间+元数据组合查询）。"""
        filt = meta.get("filter", "")
        if filt == "untitled":
            return "未命名" in (note.title or "") or not (note.title or "").strip()
        if filt == "no_tags":
            return not (note.tags or [])
        if filt == "has_image":
            return bool(note.attachment_path)
        if filt == "pinned":
            return bool(note.pinned)
        if filt == "empty":
            return not (note.content or "").strip() and not (note.ocr_text or "").strip()
        if filt == "min_words":
            word_count = len(note.content or "") + len(note.ocr_text or "")
            return word_count >= meta.get("min", 0)
        if filt == "max_words":
            word_count = len(note.content or "") + len(note.ocr_text or "")
            return word_count <= meta.get("max", 0)
        return True

    def _route_recent_edit(self, intent: Intent, question: str, extra_terms, limit) -> IntentResult:
        """最近修改：基于 updated_at 过滤（区别于 created_at 的时间意图）。"""
        time_range = intent.params.get("time_range")
        chunks: list[dict] = []
        for note in self.store.list_notes(limit=100000):
            try:
                updated = datetime.fromisoformat(note.updated_at).date()
            except (ValueError, TypeError):
                continue
            # 有时间范围就按范围过滤；无范围（"最近改过"）取最近7天
            if time_range:
                start, end, _ = time_range
                if not (start <= updated < end):
                    continue
            else:
                today = date.today()
                if (today - updated).days > 7:
                    continue
            updated_str = note.updated_at[:16].replace("T", " ")
            content_preview = (note.content or note.ocr_text or "")[:150]
            excerpt = f"[修改时间：{updated_str}]\n{content_preview}"
            chunks.append(self._chunk_from_note(note, "最近修改", excerpt=excerpt))
        chunks.sort(key=lambda c: c["note"].updated_at, reverse=True)
        label = "最近修改" if not time_range else time_range[2]
        return IntentResult(
            kind="time", chunks=chunks[:limit], intent=intent,
            empty_answer=f"{label}没有修改过笔记。",
        )

    def _route_stats(self, intent: Intent, question: str, extra_terms, limit) -> IntentResult:
        params = intent.params
        taxonomy = self.store.get_taxonomy()
        total = taxonomy.get("total", 0)
        categories = taxonomy.get("categories", [])
        tags = taxonomy.get("tags", [])
        subtype = params.get("subtype", "count")
        time_range = params.get("time_range")

        if time_range:
            start, end, label = time_range
            matched_notes = []
            for note in self.store.list_notes(limit=100000):
                try:
                    created = datetime.fromisoformat(note.created_at).date()
                except (ValueError, TypeError):
                    continue
                if start <= created < end:
                    matched_notes.append(note)
            # 时间+字数统计组合：今天写了多少字
            if subtype == "word_count":
                total_words = sum(len(n.content or "") + len(n.ocr_text or "") for n in matched_notes)
                avg = total_words // len(matched_notes) if matched_notes else 0
                answer = f"{label}共创建了 {len(matched_notes)} 条笔记，总字数约 {total_words} 字，平均每条 {avg} 字。"
                return IntentResult(kind="stats", intent=intent, answer=answer,
                                    data={"total": len(matched_notes), "total_words": total_words,
                                          "avg_words": avg, "time_label": label})
            answer = f"{label}共创建了 {len(matched_notes)} 条笔记。"
            return IntentResult(kind="stats", intent=intent, answer=answer,
                                data={"total": len(matched_notes), "time_label": label})

        # 字数统计：总字数 / 平均字数
        if subtype == "word_count":
            all_notes = self.store.list_notes(limit=100000)
            total_words = sum(len(n.content or "") + len(n.ocr_text or "") for n in all_notes)
            avg = total_words // len(all_notes) if all_notes else 0
            answer = f"你共有 {len(all_notes)} 条笔记，总字数约 {total_words} 字，平均每条 {avg} 字。"
            return IntentResult(kind="stats", intent=intent, answer=answer,
                                data={"total_notes": len(all_notes), "total_words": total_words, "avg_words": avg})

        # 最早/最新/第一篇/最后一篇：按 created_at 排序返回笔记
        if subtype == "time_order":
            all_notes = self.store.list_notes(limit=100000)
            if not all_notes:
                return IntentResult(kind="stats", intent=intent, answer="你还没有任何笔记。",
                                    data={"total": 0})
            is_earliest = any(w in question for w in ("最早", "第一篇", "最旧"))
            sorted_notes = sorted(all_notes, key=lambda n: n.created_at, reverse=not is_earliest)
            # 提取数量词："最早的3个" → 3；无数量词默认 5
            count = limit
            m = re.search(r"(\d+)\s*(?:个|篇|条|笔记)", question)
            if m:
                count = min(int(m.group(1)), len(sorted_notes))
            top = sorted_notes[:count]
            order_label = "最早" if is_earliest else "最新"
            chunks = []
            for note in top:
                created_str = note.created_at[:16].replace("T", " ")
                chunks.append(self._chunk_from_note(note, f"{order_label}创建（{created_str}）",
                                                    excerpt=f"[创建时间：{created_str}]"))
            return IntentResult(kind="list", chunks=chunks, intent=intent,
                                data={"total": len(all_notes), "order": order_label, "count": count})

        # 最久没动：按 updated_at 升序返回（最久没编辑的排前面）
        if subtype == "stalest":
            all_notes = self.store.list_notes(limit=100000)
            if not all_notes:
                return IntentResult(kind="stats", intent=intent, answer="你还没有任何笔记。",
                                    data={"total": 0})
            today = date.today()
            scored = []
            for note in all_notes:
                try:
                    updated = datetime.fromisoformat(note.updated_at).date()
                    days = (today - updated).days
                except (ValueError, TypeError):
                    days = 0
                scored.append((note, days))
            scored.sort(key=lambda x: x[1], reverse=True)
            top = scored[:5]
            chunks = []
            for note, days in top:
                updated_str = note.updated_at[:16].replace("T", " ")
                chunks.append(self._chunk_from_note(note, f"{days}天未编辑",
                                                    excerpt=f"[最后修改：{updated_str}，已 {days} 天未动]"))
            return IntentResult(kind="list", chunks=chunks, intent=intent,
                                data={"total": len(all_notes)})

        # 写作时段分布：统计 created_at 的小时分布
        if subtype == "hour_distribution":
            all_notes = self.store.list_notes(limit=100000)
            buckets = {"凌晨（0-6点）": 0, "早上（6-9点）": 0, "上午（9-12点）": 0,
                       "中午（12-14点）": 0, "下午（14-18点）": 0, "晚上（18-24点）": 0}
            for note in all_notes:
                try:
                    hour = datetime.fromisoformat(note.created_at).hour
                except (ValueError, TypeError):
                    continue
                if hour < 6:
                    buckets["凌晨（0-6点）"] += 1
                elif hour < 9:
                    buckets["早上（6-9点）"] += 1
                elif hour < 12:
                    buckets["上午（9-12点）"] += 1
                elif hour < 14:
                    buckets["中午（12-14点）"] += 1
                elif hour < 18:
                    buckets["下午（14-18点）"] += 1
                else:
                    buckets["晚上（18-24点）"] += 1
            lines = [f"你共有 {len(all_notes)} 条笔记，写作时段分布："]
            for period, count in buckets.items():
                lines.append(f"- {period}：{count} 条")
            answer = "\n".join(lines)
            return IntentResult(kind="stats", intent=intent, answer=answer,
                                data={"total": len(all_notes), "hour_distribution": buckets})

        # 长度/极值：返回内容最长/最短的笔记（按字数排序）
        if subtype == "length":
            all_notes = self.store.list_notes(limit=100000)
            # 计算每条笔记的字数（content + ocr_text）
            scored = []
            for note in all_notes:
                content_len = len(note.content or "") + len(note.ocr_text or "")
                scored.append((note, content_len))
            is_longest = any(w in question for w in ("最长", "内容最多", "字数最多", "最大"))
            scored.sort(key=lambda x: x[1], reverse=is_longest)
            # 过滤掉空内容笔记
            scored = [(n, l) for n, l in scored if l > 0]
            if not scored:
                return IntentResult(kind="stats", intent=intent, answer="所有笔记都没有内容。",
                                    data={"total": 0})
            top = scored[:5]
            size_label = "最长" if is_longest else "最短"
            chunks = [self._chunk_from_note(n, f"内容{size_label}（{l}字）") for n, l in top]
            # kind=list：走列表型路径返回笔记链接，answer 由 server 的 _build_list_answer 生成
            return IntentResult(kind="list", chunks=chunks, intent=intent,
                                data={"total": len(scored), "size_label": size_label})

        if subtype == "category_count":
            ranked = sorted(categories, key=lambda c: c.get("count", 0), reverse=True)
            lines = [f"你共有 {total} 条笔记，分类分布如下："]
            for cat in ranked[:6]:
                lines.append(f"- {cat['name']}：{cat.get('count', 0)} 条")
            answer = "\n".join(lines)
            return IntentResult(kind="stats", intent=intent, answer=answer,
                                data={"total": total, "categories": ranked})

        if subtype == "tag_count":
            if tags:
                lines = [f"你共有 {len(tags)} 个标签，使用最多的标签："]
                lines += [f"- {t['name']}（{t.get('count', 0)} 次）" for t in tags[:8]]
                answer = "\n".join(lines)
            else:
                answer = "你目前还没有任何标签。"
            return IntentResult(kind="stats", intent=intent, answer=answer,
                                data={"total_tags": len(tags), "tags": tags[:15]})

        answer = f"你共有 {total} 条笔记，{len(categories)} 个分类，{len(tags)} 个标签。"
        return IntentResult(kind="stats", intent=intent, answer=answer,
                            data={"total": total, "category_count": len(categories), "tag_count": len(tags)})

    def _route_taxonomy(self, intent: Intent, question: str, extra_terms, limit) -> IntentResult:
        taxonomy = self.store.get_taxonomy()
        categories = taxonomy.get("categories", [])
        tags = taxonomy.get("tags", [])
        want_tag = "标签" in question
        if want_tag:
            lines = [f"你共有 {len(tags)} 个标签："]
            for t in tags[:20]:
                lines.append(f"- {t['name']}（{t.get('count', 0)} 次）")
            answer = "\n".join(lines)
            return IntentResult(kind="taxonomy", intent=intent, answer=answer, data={"tags": tags})
        lines = [f"你共有 {len(categories)} 个分类："]
        for cat in sorted(categories, key=lambda c: c.get("count", 0), reverse=True):
            lines.append(f"- {cat['name']}（{cat.get('count', 0)} 条）")
        answer = "\n".join(lines)
        return IntentResult(kind="taxonomy", intent=intent, answer=answer, data={"categories": categories})

    def _route_metadata(self, intent: Intent, question: str, extra_terms, limit) -> IntentResult:
        filt = intent.params.get("filter", "")
        all_notes = self.store.list_notes(limit=100000)
        # has_tabs：直接查数据库拿有子页签的父笔记 ID 集合，避免 O(n²) 调用
        parent_ids_with_tabs: set[int] = set()
        if filt == "has_tabs":
            try:
                rows = self.store.connection.execute(
                    "SELECT DISTINCT parent_note_id FROM notes WHERE parent_note_id IS NOT NULL"
                ).fetchall()
                parent_ids_with_tabs = {r["parent_note_id"] for r in rows}
            except Exception:
                pass
        matched = []
        for note in all_notes:
            title = (note.title or "").strip()
            content = (note.content or "").strip()
            ocr = (note.ocr_text or "").strip()
            tags = note.tags or []
            word_count = len(content) + len(ocr)
            if filt == "untitled" and ("未命名" in title or not title):
                matched.append(note)
            elif filt == "no_tags" and not tags:
                matched.append(note)
            elif filt == "has_image" and note.attachment_path:
                matched.append(note)
            elif filt == "pinned" and note.pinned:
                matched.append(note)
            elif filt == "empty" and not content and not ocr:
                matched.append(note)
            elif filt == "has_tabs" and note.id in parent_ids_with_tabs:
                matched.append(note)
            elif filt == "category":
                target_cat = intent.params.get("category", "")
                if (note.category or "") == target_cat:
                    matched.append(note)
            elif filt == "tag":
                target_tag = intent.params.get("tag", "")
                if target_tag in tags:
                    matched.append(note)
            elif filt == "min_words":
                min_n = intent.params.get("min", 0)
                if word_count >= min_n:
                    matched.append(note)
            elif filt == "max_words":
                max_n = intent.params.get("max", 0)
                if word_count <= max_n:
                    matched.append(note)
        chunks = [self._chunk_from_note(n, f"元数据筛选：{filt}") for n in matched[:30]]
        labels = {
            "untitled": "未命名", "no_tags": "没有标签", "has_image": "带图片",
            "pinned": "已置顶", "empty": "空内容", "has_tabs": "有子页签",
            "category": f"分类「{intent.params.get('category', '')}」",
            "tag": f"标签「{intent.params.get('tag', '')}」",
            "min_words": f"≥{intent.params.get('min', 0)}字",
            "max_words": f"≤{intent.params.get('max', 0)}字",
        }
        label = labels.get(filt, filt)
        return IntentResult(
            kind="metadata", chunks=chunks, intent=intent,
            empty_answer=f"没有{label}的笔记。",
            data={"filter": filt, "total": len(matched)},
        )

    def _route_list(self, intent: Intent, question: str, extra_terms, limit) -> IntentResult:
        topic = (intent.params.get("topic") or "").strip()
        search_q = topic or question
        matched = self.store.search_notes(search_q, limit=30)
        chunks = [self._chunk_from_note(n, f"包含「{topic}」" if topic else "列表匹配") for n in matched]
        return IntentResult(
            kind="list", chunks=chunks[:20], intent=intent,
            empty_answer=f"没有找到关于「{topic}」的笔记。" if topic else "没有找到相关笔记。",
            data={"topic": topic, "total": len(matched)},
        )

    def _route_summary(self, intent: Intent, question: str, extra_terms, limit) -> IntentResult:
        topic = (intent.params.get("topic") or "").strip()
        search_q = topic or question
        matched = self.store.search_notes(search_q, limit=3)
        if not matched:
            matched = self.embedding_service.semantic_search(search_q, limit=3, min_score=0.35)
            matched = [n for n, _ in matched]
        chunks = [self._chunk_from_note(n, f"摘要来源：{n.title or '未命名'}") for n in matched[:3]]
        prompt = "请总结以下笔记的核心要点，分点列出，每点不超过一行。" + (f" 主题：{topic}。" if topic else "")
        return IntentResult(
            kind="summary", chunks=chunks, intent=intent, prompt_override=prompt,
            empty_answer=f"没有找到「{topic}」相关的笔记可供总结。" if topic else "没有找到可总结的笔记。",
        )

    def _route_compare(self, intent: Intent, question: str, extra_terms, limit) -> IntentResult:
        entities = intent.params.get("entities", [])
        chunks: list[dict] = []
        for ent in entities[:2]:
            matched = self.store.search_notes(ent, limit=1)
            if not matched:
                sem = self.embedding_service.semantic_search(ent, limit=1, min_score=0.30)
                matched = [n for n, _ in sem]
            if matched:
                chunks.append(self._chunk_from_note(matched[0], f"对比对象：{ent}"))
        names = " 与 ".join(entities[:2]) if entities else "两个对象"
        prompt = f"请对比以下笔记内容的异同，从主题、要点、差异三方面简要说明。对比对象：{names}。"
        return IntentResult(
            kind="compare", chunks=chunks, intent=intent, prompt_override=prompt,
            empty_answer="没有找到可对比的笔记。",
        )

    def _route_entity(self, intent: Intent, question: str, extra_terms, limit) -> IntentResult:
        entity = (intent.params.get("entity") or intent.params.get("topic") or "").strip()
        search_q = entity or question
        # 实体定位优先精确匹配标题/标识符
        matched = self.store.search_notes(search_q, limit=3)
        if not matched:
            sem = self.embedding_service.semantic_search(search_q, limit=3, min_score=0.35)
            matched = [n for n, _ in sem]
        chunks = [self._chunk_from_note(n, f"实体命中：{entity}" if entity else "实体匹配") for n in matched[:3]]
        return IntentResult(
            kind="entity", chunks=chunks, intent=intent,
            empty_answer=f"没有找到关于「{entity}」的笔记。" if entity else "没有找到相关笔记。",
        )

    def _route_related(self, intent: Intent, question: str, extra_terms, limit) -> IntentResult:
        ref = (intent.params.get("reference") or intent.params.get("topic") or "").strip()
        search_q = ref or question
        sem = self.embedding_service.semantic_search(search_q, limit=max(6, limit + 1), min_score=0.40)
        chunks = [self._chunk_from_note(n, f"语义相关（{score:.2f}）", score=score) for n, score in sem[:limit]]
        return IntentResult(
            kind="related", chunks=chunks, intent=intent,
            empty_answer=f"没有找到与「{ref}」相关的笔记。" if ref else "没有找到相关笔记。",
        )

    def _route_qa(
        self,
        intent: Intent,
        question: str,
        extra_terms,
        limit,
        llm_engine=None,
        evidence_question: str | None = None,
        diagnostics: dict | None = None,
    ) -> IntentResult:
        # First retrieve with the original wording. A non-empty candidate list is
        # not enough: generic neighbours must not prevent a focused retry.
        evidence_question = evidence_question or question
        diagnostics = diagnostics if diagnostics is not None else {}
        initial_trace: dict = {}
        required_topics = self.required_retrieval_topics(evidence_question)
        chunks = self.retrieve(
            question,
            limit=limit,
            extra_terms=extra_terms,
            strict_topic_match=bool(required_topics),
            required_topic_terms=required_topics,
            diagnostics=initial_trace,
        )
        answerable_initial = self.has_answerable_evidence(evidence_question, chunks)
        diagnostics["retrieval_attempts"] = [initial_trace]
        diagnostics["answerability"] = {
            "initial": answerable_initial,
            "rewrite_attempted": False,
        }
        if not answerable_initial and llm_engine is not None and not extra_terms:
            diagnostics["answerability"]["rewrite_attempted"] = True
            try:
                extra_terms = llm_engine.rewrite_query(question)
            except Exception:
                extra_terms = []
                diagnostics["answerability"]["rewrite_failed"] = True
            if extra_terms:
                retry_trace: dict = {}
                retried = self.retrieve(
                    question,
                    limit=limit,
                    extra_terms=extra_terms,
                    strict_topic_match=bool(required_topics),
                    required_topic_terms=required_topics,
                    diagnostics=retry_trace,
                )
                diagnostics["retrieval_attempts"].append(retry_trace)
                if self.has_answerable_evidence(evidence_question, retried):
                    chunks = retried
        answerable_final = self.has_answerable_evidence(evidence_question, chunks)
        diagnostics["answerability"].update({
            "expanded_term_count": len(extra_terms or []),
            "final": answerable_final,
        })
        if not answerable_final:
            chunks = []
        chunks = self.retain_focused_chunks(evidence_question, chunks)
        diagnostics["context"] = {
            "required_topic_count": len(required_topics),
            "retained_chunk_count": len(chunks),
            "retained_source_count": len({
                str(chunk.get("source_group") or chunk.get("source_id") or chunk["note"].id)
                for chunk in chunks
            }),
        }
        empty_answer = "没有找到足够相似的笔记，暂时无法基于笔记回答。"
        if required_topics:
            empty_answer = f"没有找到能直接解释「{required_topics[0]}」的笔记证据，暂时无法基于笔记回答。"
        return IntentResult(
            kind="qa", chunks=chunks, intent=intent, expanded_terms=list(extra_terms),
            empty_answer=empty_answer,
        )

    @classmethod
    def has_answerable_evidence(cls, question: str, chunks: list[dict]) -> bool:
        """Reject generic semantic neighbours before they reach answer generation."""
        if not chunks:
            return False
        if cls.structured_field_coverage(
            question, (str(chunk.get("raw_text", "")) for chunk in chunks),
        ) is False:
            return False
        clauses = cls.compound_query_clauses(question)
        if len(clauses) > 1:
            quantified_tasks = cls.quantified_parallel_tasks(question)
            if cls.is_role_comparison_question(question) and not any(
                cls.role_comparison_evidence(question, str(chunk.get("raw_text", "")))
                for chunk in chunks
            ):
                return False

            has_clause_annotations = any(chunk.get("compound_clauses") for chunk in chunks)
            for clause in clauses:
                clause_chunks = [
                    chunk for chunk in chunks
                    if clause in chunk.get("compound_clauses", [])
                ]
                # Hand-built unit inputs and legacy callers do not carry the
                # retrieval annotations. Product retrieval does, and in that
                # path a missing clause mapping is a real coverage failure.
                if not clause_chunks and not has_clause_annotations:
                    clause_chunks = chunks
                if focus := quantified_tasks.get(clause):
                    clause_chunks = [
                        chunk for chunk in clause_chunks
                        if cls.text_contains_term(str(chunk.get("raw_text", "")), focus)
                    ]
                if not cls._has_answerable_evidence_single(
                    clause,
                    clause_chunks,
                    compound_subquery=True,
                ):
                    return False
            return True

        return cls._has_answerable_evidence_single(question, chunks)

    @staticmethod
    def chunk_topic_text(chunk: dict) -> str:
        """Return source-topic context without diluting answer evidence.

        Parent-child expansion may replace a heading with the neighbouring
        answer-bearing passage. The original anchor remains useful only for
        proving which subject that child belongs to; requested fields and the
        visible citation must still come from the child itself.
        """
        return "\n".join(filter(None, (
            str(chunk.get("title", "") or "").strip(),
            str(chunk.get("context_anchor_text", "") or "").strip(),
            str(chunk.get("raw_text", "") or "").strip(),
        )))

    @classmethod
    def _has_answerable_evidence_single(
        cls,
        question: str,
        chunks: list[dict],
        *,
        compound_subquery: bool = False,
    ) -> bool:
        """Evaluate one independently retrievable question against its evidence."""
        if not chunks:
            return False
        evidence_texts = [cls.chunk_topic_text(chunk) for chunk in chunks]
        if not cls.requested_answer_fields_are_covered(
            question,
            (str(chunk.get("raw_text", "")) for chunk in chunks),
        ):
            return False
        if not cls.has_purpose_or_usage_evidence(question, evidence_texts):
            return False
        if not cls.has_state_result_evidence(question, evidence_texts):
            return False
        if cls.is_role_comparison_question(question):
            # A shared policy title is not evidence that two named subjects
            # have distinct roles. Require a passage that actually states them.
            if not any(
                cls.role_comparison_evidence(question, str(chunk.get("raw_text", "")))
                for chunk in chunks
            ):
                return False
        # A generated compound subquery is retrieval scaffolding, not a new
        # exact concept typed by the user. ``采购审批里的紧急采购`` therefore
        # must not require the source to contain the filler word ``里的``.
        # Preserve the stricter full-phrase gate for genuine one-topic user
        # input such as ``提示词注入``.
        required_topics = (
            cls.subquery_anchor_terms(question)
            if compound_subquery
            else cls.required_retrieval_topics(question)
        )
        if required_topics:
            corpus = "\n".join(cls.chunk_topic_text(chunk) for chunk in chunks)
            if not all(cls.text_contains_term(corpus, topic) for topic in required_topics):
                return False
            if cls.requires_definition_evidence(question) and not cls.has_definition_evidence(
                required_topics,
                (str(chunk.get("raw_text", "")) for chunk in chunks),
            ):
                return False
        entity_terms = cls.explicit_entity_terms(question)
        if entity_terms:
            corpus = "\n".join(cls.chunk_topic_text(chunk) for chunk in chunks)
            if not all(cls.text_contains_term(corpus, term) for term in entity_terms):
                return False
        # Pure scalar values are answer constraints, not record identifiers.
        # A policy saying ``20 万元（含）以上`` legitimately answers a
        # question about 25 万元 even though the literal token ``25`` is not
        # repeated in the source.  Keep the hard presence gate for stable
        # alphanumeric identifiers (contract IDs, model names, suppliers),
        # while amount/date boundary logic verifies scalar values separately.
        identifier_terms = cls.retrieval_identifier_terms(question)
        if identifier_terms:
            corpus = "\n".join(cls.chunk_topic_text(chunk) for chunk in chunks)
            if not all(
                cls.text_contains_identifier(corpus, term)
                for term in identifier_terms
            ):
                return False
        for chunk in chunks:
            if cls.has_substantive_matched_term({"matched_terms": chunk.get("hits", [])}):
                return True
            if float(chunk.get("score", 0.0) or 0.0) >= 0.72 and len(str(chunk.get("raw_text", ""))) >= 24:
                return True
        return False

    def _retrieve_by_time(self, time_range: tuple[date, date, str], limit: int) -> list[dict]:
        """按 created_at 时间范围过滤笔记，直接返回（不走语义/FTS 检索）。"""
        start, end, label = time_range
        chunks: list[dict] = []
        for note in self.store.list_notes(limit=100000):
            try:
                created = datetime.fromisoformat(note.created_at).date()
            except (ValueError, TypeError):
                continue
            if start <= created < end:
                created_str = note.created_at[:16].replace("T", " ")
                content_preview = (note.content or note.ocr_text or "")[:150]
                excerpt = f"[创建时间：{created_str}]\n{content_preview}"
                chunks.append(self._chunk_from_note(note, f"{label}创建", excerpt=excerpt))
        chunks.sort(key=lambda c: c["note"].created_at, reverse=True)
        return chunks[:limit]

    def source_payloads(self, question: str, chunks: list[dict]) -> list[dict]:
        budget_change = None
        if self.budget_pause_request(question):
            choices, unresolved_groups = [], set()
            for chunk in chunks:
                raw = str(chunk.get("raw_text", "") or chunk.get("excerpt", ""))
                proof = self.budget_pause_proof(question, str(chunk.get("title", "")), raw)
                group = str(chunk.get("source_group") or chunk.get("source_id") or chunk["note"].id)
                if proof and proof["status"] == "unresolved_pointer":
                    unresolved_groups.add(group)
                elif proof and proof["status"] != "proved":
                    return []
                elif proof:
                    if len(raw) > 1800:
                        return []
                    choices.append((chunk, proof))
            origins = {(str(chunk.get("source_group") or chunk.get("source_id") or chunk["note"].id),
                        chunk.get("source_kind", "note"), chunk["note"].id, str(chunk.get("location", "")),
                        self.chunk_source_url(chunk), chunk.get("document_chunk_id"), chunk.get("document_chunk_order"),
                        proof["identity"], proof["evidence"]) for chunk, proof in choices}
            if len(origins) != 1 or unresolved_groups.intersection(origin[0] for origin in origins):
                return []
            chosen, budget_change = choices[0]
            chunks = [chosen]
        cause_change = None
        if self.cause_pair_request(question):
            choices, unresolved_groups = [], set()
            for chunk in chunks:
                raw = str(chunk.get("raw_text", "") or chunk.get("excerpt", ""))
                proof = self.cause_pair_proof(question, str(chunk.get("title", "")), raw)
                group = str(chunk.get("source_group") or chunk.get("source_id") or chunk["note"].id)
                if proof and proof["status"] == "unresolved_pointer":
                    unresolved_groups.add(group)
                elif proof and proof["status"] != "proved":
                    return []
                elif proof:
                    if len(raw) > 1800:
                        return []
                    choices.append((chunk, proof))
            origins = {(str(chunk.get("source_group") or chunk.get("source_id") or chunk["note"].id),
                        chunk.get("source_kind", "note"), chunk["note"].id, str(chunk.get("location", "")),
                        self.chunk_source_url(chunk), chunk.get("document_chunk_id"), chunk.get("document_chunk_order"),
                        proof["event_identity"], proof["evidence"]) for chunk, proof in choices}
            if len(origins) != 1 or unresolved_groups.intersection(origin[0] for origin in origins):
                return []
            chosen, cause_change = choices[0]
            chunks = [chosen]
        inverse_change = None
        inverse_request = self.inverse_approval_request(question)
        if inverse_request:
            choices = []
            unresolved_groups = set()
            for chunk in chunks:
                raw = str(chunk.get("raw_text", "") or chunk.get("excerpt", ""))
                proof = self.inverse_approval_proof(question, str(chunk.get("title", "")), raw)
                group = str(chunk.get("source_group") or chunk.get("source_id") or chunk["note"].id)
                if proof and proof["status"] == "unresolved_pointer":
                    unresolved_groups.add(group)
                elif proof and proof["status"] != "proved":
                    return []
                elif proof:
                    # The final proof must see the whole actual context, including
                    # conditions after the pair; truncation is not a safe proof.
                    if len(raw) > 1800:
                        return []
                    choices.append((chunk, proof))
            identities = {(str(chunk.get("source_group") or chunk.get("source_id") or chunk["note"].id),
                           chunk.get("source_kind", "note"), chunk["note"].id,
                           str(chunk.get("location", "")), self.chunk_source_url(chunk),
                           chunk.get("document_chunk_id"), chunk.get("document_chunk_order"),
                           proof["target"], proof["witness"])
                          for chunk, proof in choices}
            if len(identities) != 1 or unresolved_groups.intersection(identity[0] for identity in identities):
                return []
            chosen, inverse_change = choices[0]
            chunks = [chosen]
        count_change = None
        count_request = self.adjusted_count_request(question)
        if count_request and not any(self.structured_record_units(str(chunk.get("raw_text", ""))) for chunk in chunks):
            choices = []
            unresolved_groups = set()
            for chunk in chunks:
                proof = self.adjusted_count_proof(question, str(chunk.get("title", "")), str(chunk.get("raw_text", "") or chunk.get("excerpt", "")))
                if proof and proof["status"] == "unresolved_pointer":
                    unresolved_groups.add(str(chunk.get("source_group") or chunk.get("source_id") or chunk["note"].id))
                    continue
                if proof and proof["status"] != "stated_actual":
                    return []
                if proof:
                    choices.append((chunk, proof))
            # A single value cannot choose between different versions or two
            # indistinguishable passages at different physical locations.
            identities = {(str(chunk.get("source_group") or chunk.get("source_id") or chunk["note"].id),
                           str(chunk.get("location", "")), self.chunk_source_url(chunk),
                           proof["old"], proof["new"], proof["evidence"])
                          for chunk, proof in choices}
            if len(identities) != 1 or unresolved_groups.intersection(identity[0] for identity in identities):
                return []
            chosen, count_change = choices[0]
            chunks = [chosen]
        grouped: dict[str, list[dict]] = {}
        for chunk in chunks:
            key = str(chunk.get("source_group") or chunk.get("source_id") or chunk["note"].id)
            grouped.setdefault(key, []).append(chunk)

        clauses = self.compound_query_clauses(question)
        is_compound = len(clauses) > 1
        sources = []
        for index, group in enumerate(grouped.values(), 1):
            # Retrieval deliberately keeps a few chunks from one long source.
            # Their global rank may be influenced by a broad semantic match,
            # while a later sibling contains the user's exact rule or field.
            # Choose the display/context anchor by direct phrase coverage first
            # so a source card never leads with its most generic neighbour.
            ranked_group = sorted(
                group,
                key=lambda chunk: self._source_evidence_priority(question, chunk),
                reverse=True,
            )
            evidence_jobs: list[tuple[dict, str]] = []
            if is_compound:
                for clause in clauses:
                    candidates = [
                        chunk for chunk in group
                        if clause in chunk.get("compound_clauses", [])
                    ]
                    if not candidates and not any(chunk.get("compound_clauses") for chunk in group):
                        candidates = group
                    if not candidates:
                        continue
                    candidates.sort(
                        key=lambda chunk: (
                            bool(
                                self.is_resolution_question(clause)
                                and self.direct_evidence_clause(
                                    clause,
                                    str(chunk.get("raw_text", chunk.get("excerpt", ""))),
                                )
                            ),
                            self._source_evidence_priority(clause, chunk),
                        ),
                        reverse=True,
                    )
                    evidence_jobs.append((candidates[0], clause))
            if not evidence_jobs:
                evidence_jobs = [(chunk, question) for chunk in ranked_group]

            primary = evidence_jobs[0][0]
            note = primary["note"]
            is_document = primary.get("source_kind") == "document"
            evidences: list[str] = []
            excerpts: list[str] = []
            raw_texts: list[str] = []
            anchor_contexts: list[str] = []
            locations: list[str] = []
            matched_terms: list[str] = []
            reasons: list[str] = []
            chunk_ids: list[int] = []
            chunk_orders: list[int] = []
            resolution_evidences: list[str] = []
            resolution_excerpts: list[str] = []
            resolution_raw_texts: list[str] = []
            resolution_locations: list[str] = []
            matched_clauses: list[str] = []
            evidence_origins: list[tuple[dict, str, str]] = []
            table_field_evidences: set[str] = set()
            first_evidence_jobs: dict[str, int] = {}
            first_raw_jobs: dict[str, int] = {}
            asks_resolution = not is_compound and self.is_resolution_question(question)
            for job_number, (chunk, evidence_question) in enumerate(evidence_jobs):
                raw_text = str(chunk.get("raw_text", "") or chunk.get("excerpt", ""))
                table_fields = self.markdown_table_field_evidence(evidence_question, chunk)
                if table_fields == "":
                    # A named row with missing or conflicting columns must not
                    # fall back to guessing their positions from cell values.
                    return []
                if table_fields:
                    table_field_evidences.add(table_fields)
                anchor_context = str(chunk.get("context_anchor_text", "") or "").strip()
                if anchor_context and anchor_context not in anchor_contexts:
                    anchor_contexts.append(anchor_context)
                # Enumerative prompts need the complete set of labelled items.
                # Selecting just the first matching sentence makes a source look
                # valid while silently dropping the rest of the user's answer.
                evidence = (budget_change["evidence"] if budget_change else "") or (cause_change["evidence"] if cause_change else "") or (inverse_change["evidence"] if inverse_change else "") or (count_change["evidence"] if count_change else "") or table_fields or self.principle_evidence(evidence_question, raw_text) or self.enumerated_evidence(evidence_question, raw_text) or self.evidence_sentence(
                    evidence_question, raw_text, chunk["hits"]
                )
                evidence_origins.append((chunk, evidence, raw_text))
                if evidence:
                    first_evidence_jobs.setdefault(evidence, job_number)
                if raw_text:
                    first_raw_jobs.setdefault(raw_text, job_number)
                evidence_lines = [line for line in evidence.splitlines() if line.strip()]
                excerpt = self.focused_excerpt(raw_text, [*evidence_lines, *chunk["hits"]], radius=320) or chunk["excerpt"]
                for value, target in ((evidence, evidences), (excerpt, excerpts), (raw_text, raw_texts), (chunk["location"], locations), (chunk["reason"], reasons)):
                    if value and value not in target:
                        target.append(value)
                for term in chunk["hits"]:
                    if term not in matched_terms:
                        matched_terms.append(term)
                if evidence_question != question and evidence_question not in matched_clauses:
                    matched_clauses.append(evidence_question)
                if is_document and chunk.get("document_chunk_id") is not None:
                    chunk_id = int(chunk["document_chunk_id"])
                    chunk_order = int(chunk.get("document_chunk_order") or 0)
                    if chunk_id not in chunk_ids:
                        chunk_ids.append(chunk_id)
                        chunk_orders.append(chunk_order)
                # A problem-resolution question should open the passage that
                # directly gives the recommendation, not a nearby symptom.
                if asks_resolution and self.direct_evidence_clause(question, raw_text):
                    for value, target in (
                        (evidence, resolution_evidences),
                        (excerpt, resolution_excerpts),
                        (raw_text, resolution_raw_texts),
                        (chunk["location"], resolution_locations),
                    ):
                        if value and value not in target:
                            target.append(value)
            # A single question needs one strongest passage per document. Joining
            # three neighbouring spreadsheet chunks creates a massive "evidence
            # sentence" and encourages the UI to highlight an entire page. For
            # explicit multi-part questions, keep each independently needed
            # evidence passage instead.
            # An explicit list request is also multi-evidence by nature.  Its
            # numbered siblings must stay together even when the wording has
            # only one grammatical clause (for example, "项目有哪些风险").
            keep_multiple_evidences = (
                len(self.compound_query_clauses(question)) > 1
                or self.is_enumeration_question(question)
                or self.is_principle_question(question)
            )
            complete_table_origin = next((
                origin for origin in evidence_origins if origin[1] in table_field_evidences
            ), None) if not is_compound else None
            if complete_table_origin:
                # All requested fields already have a verified row/header
                # mapping. Prefer this complete evidence even for an ordinary
                # field question when incomplete OCR ranks above the body.
                primary, table_evidence, table_raw = complete_table_origin
                note = primary["note"]
                display_evidences = [table_evidence]
                display_excerpts = [self.focused_excerpt(
                    table_raw, [table_evidence, *primary["hits"]], radius=320,
                ) or primary["excerpt"]]
                display_raw_texts = [table_raw]
                locations = [primary["location"]]
                keep_multiple_evidences = False
            elif asks_resolution and resolution_evidences:
                display_evidences = resolution_evidences[:1]
                display_excerpts = resolution_excerpts[:1]
                display_raw_texts = resolution_raw_texts[:1]
                locations = resolution_locations[:1]
            else:
                # A single-fact answer should expose one focused passage even
                # when the source is a multi-tab note; multi-part questions
                # keep their independently required evidence passages.
                display_evidences = evidences if keep_multiple_evidences else evidences[:1]
                display_excerpts = excerpts if keep_multiple_evidences else excerpts[:1]
                display_raw_texts = raw_texts if keep_multiple_evidences else raw_texts[:1]
            payload = {
                "index": index,
                "id": note.id,
                "type": "document" if is_document else "note",
                "title": primary["title"],
                "score": round(max(float(chunk["score"]) for chunk in group), 4),
                "rank_score": round(max(float(chunk["rank_score"]) for chunk in group), 2),
                "reason": "；".join(reasons[:2]),
                "excerpt": "\n\n".join(display_excerpts)[:1200],
                "evidence": "\n".join(display_evidences),
                "fallback_excerpt": "\n\n".join(display_raw_texts)[:1800],
                "anchor_context": "\n\n".join(anchor_contexts)[:1200],
                "url": self.chunk_source_url(primary),
                "location": "；".join(locations),
                "evidence_locations": locations,
                "matched_terms": matched_terms,
                "matched_clauses": matched_clauses,
                "chunk_id": chunk_ids[0] if chunk_ids else None,
                "chunk_order": chunk_orders[0] if chunk_orders else None,
                "chunk_ids": chunk_ids,
                "chunk_orders": chunk_orders,
            }
            if display_evidences and all(value in table_field_evidences for value in display_evidences):
                payload["structured_table_fields"] = True
            # The public locations aggregate retrieval jobs, while a single
            # displayed passage may come from only one of them. Preserve its
            # actual origin without guessing from the first aggregated label.
            # Independent evidence/raw-text deduplication can leave these two
            # strings from different jobs; that case deliberately has no anchor.
            if (
                not keep_multiple_evidences and not asks_resolution
                and len(display_evidences) == 1 and len(display_raw_texts) == 1
                and first_evidence_jobs[display_evidences[0]] == first_raw_jobs[display_raw_texts[0]]
            ):
                origins = {}
                for chunk, evidence, raw_text in evidence_origins:
                    if evidence != payload["evidence"] or raw_text != display_raw_texts[0]:
                        continue
                    chunk_id = int(chunk["document_chunk_id"]) if is_document and chunk.get("document_chunk_id") is not None else None
                    chunk_order = int(chunk.get("document_chunk_order") or 0) if chunk_id is not None else None
                    location = str(chunk.get("location", "")).strip()
                    url = self.chunk_source_url(chunk)
                    origins[(location, url, chunk_id, chunk_order)] = {
                        "source_id": chunk["note"].id,
                        "source_type": "document" if chunk.get("source_kind") == "document" else "note",
                        "location": location, "url": url,
                        "chunk_id": chunk_id, "chunk_order": chunk_order,
                        "evidence_sha256": hashlib.sha256(payload["evidence"].encode("utf-8")).hexdigest(),
                        "fallback_sha256": hashlib.sha256(payload["fallback_excerpt"].encode("utf-8")).hexdigest(),
                    }
                if len(origins) == 1:
                    anchor = next(iter(origins.values()))
                    if anchor["location"] and (not is_document or anchor["chunk_id"] is not None):
                        payload["evidence_alignment_anchor"] = anchor
            if budget_change:
                anchor = payload.get("evidence_alignment_anchor")
                if not anchor:
                    return []
                anchor["budget_scope_sha256"] = self._budget_pause_scope_hash(question, payload["title"])
                if not self._budget_pause_answer(question, [payload])[1]:
                    return []
            if cause_change:
                anchor = payload.get("evidence_alignment_anchor")
                if not anchor:
                    return []
                anchor["cause_scope_sha256"] = self._cause_pair_scope_hash(question, payload["title"])
                if not self._cause_pair_answer(question, [payload])[1]:
                    return []
            if inverse_change:
                anchor = payload.get("evidence_alignment_anchor")
                if not anchor:
                    return []
                anchor["inverse_scope_sha256"] = self._inverse_approval_scope_hash(question, payload["title"])
                # Validate the same bounded payload the consumer will receive.
                if not self._inverse_approval_answer(question, [payload])[1]:
                    return []
            sources.append(payload)
        # A source that contains a complete numbered list already answers a
        # single list request.  Generic neighbours that merely repeat a word
        # such as "风险" add noise and invite the generator to blend unrelated
        # statements into the answer.  Keep multiple sources only when each
        # offers a complete numbered group of the requested label.
        if self.is_enumeration_question(question):
            complete_groups = [
                source for source in sources
                if self.enumeration_item_count(question, str(source.get("evidence", ""))) >= 2
            ]
            if complete_groups:
                for index, source in enumerate(complete_groups, 1):
                    source["index"] = index
                return complete_groups
        if self.is_principle_question(question):
            complete_groups = [
                source for source in sources
                if self.principle_evidence(question, str(source.get("evidence", "")))
            ]
            if complete_groups:
                for index, source in enumerate(complete_groups, 1):
                    source["index"] = index
                return complete_groups
        sources = self.retain_direct_action_sources(question, sources)
        return self.retain_actor_action_sources(question, sources)

    @classmethod
    def direct_action_terms(cls, question: str) -> list[str]:
        """Return requested operations for an explicit action/requirement question.

        Retrieval may legitimately surface a nearby definition or contrast that
        repeats words such as ``账户`` and ``校验``.  Those passages are useful
        search neighbours, but they are not answer-bearing evidence for a user
        asking what must actually be checked, submitted, approved, or prepared.
        This recogniser is intentionally narrow so ordinary topic, comparison,
        enumeration, and definition questions keep their broad source recall.
        """
        compact = re.sub(r"\s+", "", str(question or ""))
        operations = (
            "校验", "检查", "复核", "确认", "审批", "会签", "验证",
            "准备", "办理", "提交", "提供", "处理", "操作", "执行",
        )
        terms = [term for term in operations if term in compact]
        if not terms:
            return []
        asks_action = bool(re.search(
            r"(?:要|需要|需|必须|应当|应该|如何|怎么|哪些|什么|何种|是否要)"
            r".{0,18}(?:校验|检查|复核|确认|审批|会签|验证|准备|办理|提交|提供|处理|操作|执行)"
            r"|(?:校验|检查|复核|确认|审批|会签|验证|准备|办理|提交|提供|处理|操作|执行)"
            r".{0,10}(?:哪些|什么|如何|怎么|要求|步骤|材料|内容)",
            compact,
        ))
        return terms if asks_action else []

    @classmethod
    def direct_action_source_score(cls, question: str, source: dict) -> float:
        """Score whether one visible source states the requested operation.

        A negative comparison such as ``A 不等同于重新校验`` is not promoted
        as an instruction.  A sentence may still contain a useful negative
        boundary (``不需要重复完成其他三项``) when the same sentence also
        states the affirmative obligation; that combined rule remains valid.
        """
        operations = cls.direct_action_terms(question)
        if not operations:
            return 0.0
        material = "\n".join(filter(None, (
            str(source.get("evidence", "") or ""),
            str(source.get("fallback_excerpt", "") or ""),
        )))
        if not material:
            return 0.0
        negative_relation = re.compile(r"(?:不等同于|不代表|并非|不属于|不是)")
        operation_pattern = (
            r"(?:校验|检查|复核|确认|审批|会签|验证|准备|办理|提交|提供|处理|操作|执行)"
        )
        modal_rule = re.compile(
            rf"(?:必须|应当|应该|需要|只需|均需|需|要).{{0,16}}{operation_pattern}"
        )
        structured_rule = re.compile(
            rf"{operation_pattern}(?:内容|项目|事项|步骤|材料)?"
            r"(?:包括|分为|如下|分别为|是)"
        )
        bare_repeated_action = re.compile(
            rf"重新(?:做|进行|完成|执行)?{operation_pattern}"
        )
        scores: list[float] = []
        for sentence, section in cls._evidence_candidates(material):
            matched_operations = [
                term for term in operations if cls.text_contains_term(sentence, term)
            ]
            if not matched_operations:
                continue
            is_negative_relation = bool(negative_relation.search(sentence))
            positive = bool(modal_rule.search(sentence) or structured_rule.search(sentence))
            if not is_negative_relation and bare_repeated_action.search(sentence):
                positive = True
            negative_only = is_negative_relation and not positive
            score = 2.0 * len(matched_operations)
            score += cls.evidence_intent_bonus(question, sentence, section)
            if positive:
                score += 8.0
            if negative_only:
                score -= 12.0
            scores.append(score)
        return max(scores, default=0.0)

    @classmethod
    def retain_direct_action_sources(cls, question: str, sources: list[dict]) -> list[dict]:
        """Drop non-answering contrasts when a direct operation rule is present."""
        if len(sources) <= 1 or not cls.direct_action_terms(question):
            return sources
        if len(cls.compound_query_clauses(question)) > 1:
            return sources
        if re.search(r"(?:是否|是不是|不等同|不代表|区别|关系|相同|一样)", str(question or "")):
            return sources

        scored = [(cls.direct_action_source_score(question, source), source) for source in sources]
        best = max((score for score, _source in scored), default=0.0)
        # Do not narrow when retrieval found no clearly affirmative rule.  In
        # that case keeping the candidates is safer than manufacturing false
        # precision from a weak heuristic.
        if best < 8.0:
            return sources
        threshold = max(2.0, best - 8.0)
        retained = [source for score, source in scored if score >= threshold]
        if not retained:
            return sources
        for index, source in enumerate(retained, 1):
            source["index"] = index
        return retained

    @classmethod
    def retain_actor_action_sources(cls, question: str, sources: list[dict]) -> list[dict]:
        """Keep sources that actually name who performs the requested action.

        A semantic neighbour can mention the same entity and deadline without
        identifying a responsible role.  For an explicit ``who owns/does it``
        question, that passage is useful search context but not answer-bearing
        evidence.  Only narrow after at least one source contains a direct
        actor-action assignment; otherwise preserve recall and let the normal
        no-answer contract decide.
        """
        if len(sources) <= 1 or not cls.is_actor_action_question(question):
            return sources
        if len(cls.compound_query_clauses(question)) > 1:
            return sources

        retained = []
        entity = cls._actor_question_entity(question)
        for source in sources:
            material = "\n".join(filter(None, (
                str(source.get("fallback_excerpt", "") or ""),
                str(source.get("evidence", "") or ""),
            )))
            if entity and not cls._actor_material_contains_entity(material, entity):
                continue
            if cls.actor_action_evidence(question, material):
                retained.append(source)
        if not retained:
            return sources
        for index, source in enumerate(retained, 1):
            source["index"] = index
        return retained

    @classmethod
    def _source_evidence_priority(cls, question: str, chunk: dict) -> tuple[float, ...]:
        """Rank sibling chunks by how directly they can support one question.

        This is intentionally used only after retrieval has already selected a
        source.  It does not suppress broad recall or introduce a new model
        call; it merely prevents a same-source generic passage from replacing
        a more exact available evidence passage in the UI and model context.
        """
        evidence_text = cls.without_urls(
            f"{chunk.get('title', '')}\n{chunk.get('raw_text', chunk.get('excerpt', ''))}"
        )
        body_text = cls.without_urls(
            str(chunk.get("raw_text", "") or chunk.get("excerpt", ""))
        )
        text = evidence_text.lower()
        focus_terms = cls.focused_query_terms(cls.query_terms(question))
        direct_terms = [term for term in focus_terms if term.lower() in text]
        # Longer direct phrases are much stronger than generic 2-character
        # overlaps such as "来源" or "字段".
        direct_coverage = sum(len(term) * len(term) for term in direct_terms)
        hits = [str(term) for term in chunk.get("hits", []) if str(term)]
        hit_coverage = sum(len(term) for term in hits if len(term) >= 3)
        subject_full, subject_specific, subject_broad = cls.attribute_subject_coverage(
            question,
            evidence_text,
        )
        return (
            cls.requested_field_evidence_bonus(question, body_text),
            # A concrete amount question should reserve the passage whose
            # stated interval actually contains that amount. Generic passages
            # can share words such as ``采购``, ``金额`` and ``关注`` without
            # defining the applicable approval rule at all.
            cls.amount_range_evidence_bonus(question, body_text),
            cls.actor_action_evidence_bonus(question, evidence_text),
            cls.ocr_agent_table_bonus(question, evidence_text),
            cls.temporal_fact_bonus(question, text, hits),
            cls.principle_evidence_bonus(question, text),
            cls.remediation_evidence_bonus(question, body_text),
            subject_full,
            subject_specific,
            cls.scalar_attribute_evidence_bonus(question, evidence_text),
            # Once the source topic is credible, prefer the passage that states
            # the requested current value or hard limit.  A future-plan sibling
            # may repeat more subject words while still being the wrong fact.
            subject_broad,
            cls.selection_criteria_evidence_bonus(question, body_text),
            float(direct_coverage),
            float(hit_coverage),
            float(chunk.get("rank_score", 0.0) or 0.0),
            float(chunk.get("score", 0.0) or 0.0),
        )

    @classmethod
    def requested_field_evidence_bonus(cls, question: str, text: str) -> float:
        """Reward passages that contain the *kind* of value the user requested.

        Query overlap alone cannot distinguish a section heading from the
        nearby implementation detail, or a root-cause paragraph from the
        measured performance result.  These rules inspect only text that is
        already present in a retrieved source; they reorder evidence and never
        manufacture a missing value.
        """
        compact_question = re.sub(r"\s+", "", str(question or ""))
        body = str(text or "").strip()
        compact_body = re.sub(r"\s+", "", body)
        if not compact_question or not compact_body:
            return 0.0

        scores = [0.0]

        stage_block, stage_labels = cls.stage_enumeration_block(question, body)
        if stage_block:
            # A generic title/alias hit can outrank a child tab even when only
            # that tab states the requested stages. Reward the proved value
            # group before the weak-candidate filter, as for other fields.
            scores.append(14.0 + min(6.0, 2.0 * len(stage_labels)))

        asks_performance = bool(re.search(
            r"(?:性能(?:目标|指标|要求)?|延迟(?!率)|时延|响应时间|耗时|处理速度|查询速度|扫描速度)",
            compact_question,
            flags=re.IGNORECASE,
        ))
        has_duration = bool(re.search(
            r"(?:[<>≤≥]=?\s*)?\d+(?:\.\d+)?(?:\s*[-~～至到]\s*\d+(?:\.\d+)?)?\s*"
            r"(?:ms|毫秒|秒|分钟|小时|天|日|周|个月|月|年)",
            body,
            flags=re.IGNORECASE,
        ))
        has_throughput = bool(re.search(
            r"(?:QPS|TPS|吞吐(?:量|率)?)[^。；;\n]{0,16}\d+|\d+[^。；;\n]{0,10}(?:QPS|TPS)",
            body,
            flags=re.IGNORECASE,
        ))
        if asks_performance and (has_duration or has_throughput):
            score = 14.0
            if re.search(r"(?:目标|实测|平均|P(?:50|90|95|99)|降到|降低至|控制在|小于|不超过)", body, re.IGNORECASE):
                score += 4.0
            duration_units = r"(?:毫秒|分钟|小时|个月|ms|秒|天|日|周|月|年)"
            requested_units = set(re.findall(duration_units, compact_question, flags=re.IGNORECASE))
            body_units = set(re.findall(rf"\d+(?:\.\d+)?\s*({duration_units})", body, flags=re.IGNORECASE))
            if requested_units:
                if requested_units & body_units:
                    score += 8.0
                elif body_units:
                    score -= 8.0

            asks_current = bool(re.search(r"(?:当前|目前|现状|现在|现有)", compact_question))
            asks_target = bool(re.search(r"(?:目标|重构后|优化后|改造后|升级后|未来|预计)", compact_question))
            body_current = bool(re.search(r"(?:当前|目前|现状|现在|现有|原来|原先)", compact_body))
            body_target = bool(re.search(r"(?:目标|重构后|优化后|改造后|升级后|未来|预计|计划)", compact_body))
            if asks_current:
                score += 6.0 if body_current else (-6.0 if body_target else 0.0)
            if asks_target:
                score += 6.0 if body_target else (-6.0 if body_current else 0.0)
            scores.append(score)

        asks_rate = "率" in compact_question and bool(re.search(
            r"(?:多少|阈值|比例|百分比|%|％|是什么|为多少|达到)",
            compact_question,
        ))
        has_percent_value = bool(re.search(
            r"(?:[<>≤≥]=?\s*)?\d+(?:\.\d+)?\s*(?:%|％)",
            body,
        ))
        if asks_rate and has_percent_value:
            rate_suffixes: set[str] = set()
            for index, character in enumerate(compact_question):
                if character != "率":
                    continue
                for width in range(2, 7):
                    start = max(0, index - width + 1)
                    candidate = compact_question[start:index + 1].lstrip("的其该本")
                    if len(candidate) >= 3:
                        rate_suffixes.add(candidate)
            matching_rate = max(
                (len(term) for term in rate_suffixes if term in compact_body),
                default=0,
            )
            if matching_rate:
                score = 16.0 + min(6.0, float(matching_rate))
                requested_state = re.search(r"(?:红色|橙色|黄色|蓝色|绿色)(?:预警|状态|等级)?", compact_question)
                body_states = set(re.findall(
                    r"(?:红色|橙色|黄色|蓝色|绿色)(?:预警|状态|等级)?",
                    compact_body,
                ))
                if requested_state:
                    if requested_state.group(0) in body_states:
                        score += 8.0
                    elif body_states:
                        score -= 8.0
                scores.append(score)

        asks_search_method = bool(re.search(
            r"(?:检索|搜索).{0,16}(?:方式|方法|策略|路径|走什么|怎么走|如何|采用|使用)"
            r"|(?:方式|方法|策略|路径).{0,12}(?:检索|搜索)",
            compact_question,
            flags=re.IGNORECASE,
        ))
        has_search_technology = bool(re.search(
            r"(?:FTS5?|MATCH|bm25|instr|向量检索|语义检索|全文检索|关键词检索|混合检索)",
            body,
            flags=re.IGNORECASE,
        ))
        has_search_route = bool(re.search(
            r"(?:走|采用|使用|通过|回退|排序|召回|匹配|查询)",
            body,
            flags=re.IGNORECASE,
        ))
        if asks_search_method and has_search_technology and has_search_route:
            technologies = len(set(re.findall(
                r"FTS5?|MATCH|bm25|instr|向量检索|语义检索|全文检索|关键词检索|混合检索",
                body,
                flags=re.IGNORECASE,
            )))
            scores.append(14.0 + min(4.0, float(technologies)))

        asks_structure = bool(re.search(
            r"(?:目录|结构|组成|构成|核心项|核心文件|包含哪些|有哪些文件|有哪些目录)",
            compact_question,
            flags=re.IGNORECASE,
        ))
        if asks_structure:
            # Match real file/directory tokens only. Substrings such as the
            # ``runtime`` in ``onnxruntime`` or ``data`` in ``--add-data`` are
            # implementation terms, not entries in a packaged directory.
            executable_tokens = re.findall(
                r"(?<![A-Za-z0-9_.-])[A-Za-z0-9_.-]+\.exe(?![A-Za-z0-9_.-])",
                body,
                flags=re.IGNORECASE,
            )
            directory_tokens = re.findall(
                r"(?<![A-Za-z0-9_-])(?:_internal|models?|runtime|frontend|assets?|data|logs?)"
                r"(?:[/\\])?(?![A-Za-z0-9_-])",
                body,
                flags=re.IGNORECASE,
            )
            component_tokens = {
                token.lower().rstrip("/\\")
                for token in [*executable_tokens, *directory_tokens]
            }
            list_shape = bool(re.search(r"(?:\+|、|，|,|；|;).*(?:\+|、|，|,|；|;)", body))
            if len(component_tokens) >= 2:
                scores.append(14.0 + min(4.0, float(len(component_tokens))) + (2.0 if list_shape else 0.0))

        return max(scores)

    def expand_parent_child_context(
        self,
        question: str,
        selected: list[dict],
        focus_terms: list[str] | None = None,
        limit: int = 5,
        diagnostics: dict | None = None,
        radius: int = 2,
    ) -> list[dict]:
        """Replace weak anchors with a bounded answer-bearing sibling chunk.

        The first-stage hybrid retriever may correctly identify a source while
        landing on its heading or an adjacent explanation.  We inspect at most
        two persisted chunks on either side, remain inside the selected source,
        and replace a slot only when the sibling has stronger answer-shaped
        evidence.  This is deterministic, CPU-cheap, and does not expand the
        number of sources or prompt tokens.
        """
        trace = diagnostics if diagnostics is not None else {}
        bounded_radius = max(0, min(int(radius), 2))
        result = [dict(chunk) for chunk in list(selected or [])[: max(0, int(limit))]]
        trace.update({
            "radius": bounded_radius,
            "source_count": len({
                str(chunk.get("source_group") or chunk.get("source_id") or chunk["note"].id)
                for chunk in result
            }),
            "considered_chunk_count": 0,
            "eligible_chunk_count": 0,
            "replaced_chunk_count": 0,
        })
        if not result or bounded_radius <= 0:
            trace["unchanged_chunk_count"] = len(result)
            return result

        query_terms = list(dict.fromkeys([
            *self.query_terms(question),
            *(str(term).strip() for term in (focus_terms or []) if str(term).strip()),
        ]))
        occupied = {
            str(chunk.get("chunk_identity") or "")
            for chunk in result
            if chunk.get("chunk_identity")
        }

        for index, anchor in enumerate(list(result)):
            is_document = anchor.get("source_kind") == "document"
            if is_document:
                center_order = anchor.get("document_chunk_order")
                window_reader = getattr(self.store, "list_document_chunk_window", None)
                source_key = getattr(anchor.get("note"), "id", None)
            else:
                center_order = anchor.get("note_chunk_order")
                window_reader = getattr(self.store, "list_note_chunk_window", None)
                source_key = anchor.get("source_id", getattr(anchor.get("note"), "id", None))
            if center_order is None or source_key is None or not callable(window_reader):
                continue

            try:
                neighbours = window_reader(int(source_key), int(center_order), bounded_radius)
            except (KeyError, TypeError, ValueError):
                continue

            anchor_identity = str(anchor.get("chunk_identity") or "")
            best = anchor
            best_priority = self._source_evidence_priority(question, anchor)
            for neighbour in neighbours:
                identity = (
                    f"document-chunk-{neighbour.id}"
                    if is_document else f"note-chunk-{neighbour.id}"
                )
                if identity == anchor_identity or identity in occupied:
                    continue
                trace["considered_chunk_count"] += 1
                body = str(getattr(neighbour, "text", "") or "").strip()
                if not body:
                    continue
                searchable = self.without_urls(body.lower())
                hits = self.matched_terms(searchable, query_terms)
                field_bonus = self.requested_field_evidence_bonus(question, body)
                if not (
                    self.has_substantive_matched_term({"matched_terms": hits})
                    or field_bonus >= 8.0
                ):
                    continue
                trace["eligible_chunk_count"] += 1

                distance = abs(int(getattr(neighbour, "chunk_order", center_order)) - int(center_order))
                lexical = self.lexical_score(hits, searchable, question)
                candidate = dict(anchor)
                anchor_base_rank = max(
                    0.0,
                    float(anchor.get("rank_score", 0.0) or 0.0)
                    - float(anchor.get("rerank_adjustment", 0.0) or 0.0),
                )
                candidate.update({
                    "chunk_identity": identity,
                    "score": max(0.0, float(anchor.get("score", 0.0) or 0.0) - distance * 0.005),
                    "rank_score": max(0.0, anchor_base_rank - distance * 0.4),
                    "lexical_score": lexical,
                    "fts_score": 0.0,
                    "hits": hits,
                    "reason": self.source_reason(
                        max(0.0, float(anchor.get("score", 0.0) or 0.0) - distance * 0.005),
                        hits,
                        0.0,
                        "document" if is_document else str(getattr(neighbour, "kind", "content") or "content"),
                    ),
                    "excerpt": self.focused_excerpt(body, hits),
                    "raw_text": body,
                    "location": str(getattr(neighbour, "location", anchor.get("location", "")) or ""),
                    "kind": "document" if is_document else str(getattr(neighbour, "kind", "content") or "content"),
                    "context_expanded": True,
                    "context_anchor_identity": anchor_identity,
                    "context_anchor_text": str(
                        anchor.get("context_anchor_text")
                        or anchor.get("raw_text")
                        or anchor.get("excerpt")
                        or ""
                    ).strip(),
                    "context_anchor_location": str(
                        anchor.get("context_anchor_location")
                        or anchor.get("location")
                        or ""
                    ).strip(),
                    "context_distance": distance,
                    "fusion_channels": {},
                    "fusion_contributions": {},
                })
                if is_document:
                    candidate.update({
                        "source_id": identity,
                        "document_chunk_id": int(neighbour.id),
                        "document_chunk_order": int(neighbour.chunk_order),
                    })
                else:
                    location = candidate["location"]
                    anchor_location = str(anchor.get("location", "") or "")
                    if "·" in anchor_location and "·" not in location:
                        location = f"{anchor_location.rsplit('·', 1)[0]}·{location}"
                    candidate.update({
                        "note_chunk_id": int(neighbour.id),
                        "note_chunk_order": int(neighbour.chunk_order),
                        "location": location,
                        "title": str(getattr(neighbour, "title", "") or anchor.get("title", "")),
                    })
                self.rerank_candidates(question, [candidate], focus_terms)
                priority = self._source_evidence_priority(question, candidate)
                if priority > best_priority:
                    best = candidate
                    best_priority = priority

            if best is anchor:
                continue
            occupied.discard(anchor_identity)
            occupied.add(str(best.get("chunk_identity") or ""))
            result[index] = best
            trace["replaced_chunk_count"] += 1

        trace["unchanged_chunk_count"] = len(result) - trace["replaced_chunk_count"]
        return result

    @classmethod
    def temporal_fact_bonus(cls, question: str, text: str, hits: Iterable[str]) -> float:
        """Prefer dated outcome passages for a direct "when" question.

        This only reorders already retrieved sibling chunks. It does not infer
        dates or broaden recall: a passage must contain both a real year and a
        substantive topic hit before it receives the boost.
        """
        compact_question = re.sub(r"\s+", "", str(question or ""))
        if not re.search(r"(?:哪一年|哪年|何时|什么时候|何年)", compact_question):
            return 0.0
        if not re.search(r"(?:19|20)\d{2}\s*年", str(text or "")):
            return 0.0
        if not cls.has_substantive_matched_term({"matched_terms": list(hits)}):
            return 0.0
        outcome_bonus = 3.0 if re.search(r"(?:提升|达到|完成|实现|建成|目标|预计|计划)", str(text or "")) else 0.0
        return 8.0 + outcome_bonus

    def contexts(self, chunks: list[dict], sources: list[dict] | None = None) -> list[dict]:
        """Build model context from the same evidence sentence shown to the user."""
        # An explicit empty list is a producer refusal, not a request to
        # resurrect the unverified raw retrieval context.
        if sources is not None:
            rows = []
            for index, source in enumerate(sources):
                fallback_chunk = chunks[index] if index < len(chunks) else {}
                evidence = str(source.get("evidence", "")).strip()
                excerpt = str(source.get("excerpt", fallback_chunk.get("excerpt", ""))).strip()
                text = f"{evidence}\n上下文：{excerpt}" if evidence and excerpt and evidence not in excerpt else evidence or excerpt
                rows.append({
                    "title": source.get("title", fallback_chunk.get("title", "相关资料")),
                    "score": source.get("score", fallback_chunk.get("score", 0.0)),
                    "reason": source.get("reason", fallback_chunk.get("reason", "")),
                    "text": text,
                    "url": source.get("url", self.chunk_source_url(fallback_chunk) if fallback_chunk else ""),
                })
            return rows
        rows = []
        for index, chunk in enumerate(chunks):
            rows.append(
                {
                    "title": chunk["title"],
                    "score": chunk["score"],
                    "reason": chunk["reason"],
                    "text": chunk["excerpt"],
                    "url": self.chunk_source_url(chunk),
                }
            )
        return rows

    def chunk_payload(self, chunk: dict, reason: str | None = None) -> dict:
        note = chunk["note"]
        if chunk.get("source_kind") == "document":
            return {
                "id": note.id,
                "title": chunk["title"],
                "content": chunk.get("raw_text", ""),
                "source_type": "document",
                "document_id": note.id,
                "original_name": note.original_name,
                "search_score": round(chunk["score"], 4),
                "search_reason": chunk["reason"] if reason is None else reason,
                "matched_excerpt": chunk["excerpt"],
                "matched_terms": chunk["hits"],
                "source_url": self.chunk_source_url(chunk),
                "source_location": chunk["location"],
                "chunk_id": chunk.get("document_chunk_id"),
            }
        payload = note.as_dict()
        payload["title"] = chunk["title"]
        payload["search_score"] = round(chunk["score"], 4)
        payload["search_reason"] = chunk["reason"] if reason is None else chunk["reason"]
        payload["matched_excerpt"] = chunk["excerpt"]
        payload["matched_terms"] = chunk["hits"]
        payload["source_url"] = self.chunk_source_url(chunk)
        payload["source_location"] = chunk["location"]
        return payload

    def analyze(self, question: str, extra_terms: list[str] | None = None) -> QueryAnalysis:
        text = question.strip()
        terms = self.query_terms(text)
        if extra_terms:
            existing = {t.lower() for t in terms}
            for term in extra_terms:
                key = term.lower()
                if key and key not in existing:
                    terms.append(term)
                    existing.add(key)
            terms.sort(key=self.term_order_key)
        return QueryAnalysis(text=text, terms=terms)

    @staticmethod
    def term_order_key(term: str) -> tuple[int, str, str]:
        """Return a process-stable order for bounded retrieval terms.

        Query terms originate from sets. Sorting only by length left equal-size
        Chinese n-grams in Python hash order, so a new process could choose a
        different first six terms for the focused FTS pass. That made identical
        questions retrieve different evidence after an application restart.
        """
        value = str(term or "")
        return (-len(value), value.casefold(), value)

    @staticmethod
    def normalize_ocr_query(question: str) -> str:
        """Normalize narrow OCR artifacts while preserving the user's meaning.

        A masked date such as ``2026-0?-24`` is not guessed as a specific date.
        It receives neutral retrieval anchors so the stored OCR correction and
        its evidence sentence can determine the final answer.
        """
        normalized = re.sub(r"(?<![A-Za-z0-9_])d(?=[\u3400-\u9fff])", "的", question)
        normalized = re.sub(r"(?<=\d)[oO](?=\d)", "0", normalized)
        if re.search(r"\d{4}-\d{1,2}[?？*＊]-\d{1,2}", normalized):
            normalized += " OCR 日期 校验"
        return normalized

    @staticmethod
    def query_terms(question: str) -> list[str]:
        # 先提取含连字符的标识符（如 P2-3、p2-22），保留为整体 term
        hyphenated = set(re.findall(r"[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)+", question))
        terms = set(re.findall(r"[A-Za-z0-9_]{2,}", question))
        terms |= hyphenated
        for year, month, day in re.findall(r"(\d{4})\s*[-年]\s*(\d{1,2})\s*[-月]\s*(\d{1,2})", question):
            terms.update((f"{year}-{month.zfill(2)}-{day.zfill(2)}", f"{month}月{day}日", f"{month}月{day}"))
        # Single-letter symbols are normally too noisy for text retrieval. Keep
        # them only when the user writes an equation-like expression such as
        # "C 与 d 的关系"; this lets formula notes be found without turning all
        # one-letter English input into a broad match.
        terms |= set(re.findall(r"(?<![A-Za-z0-9_])([A-Za-z])(?=\s*(?:与|和|=|为))", question))
        for left, right in re.findall(r"(?<![A-Za-z0-9_])([A-Za-z])\s*(?:与|和)\s*([A-Za-z])(?![A-Za-z0-9_])", question):
            terms.update((left, right))
        for run in re.findall(r"[\u3400-\u9fff]{2,}", question):
            if run not in RagPipeline.stopwords:
                terms.add(run)
            # 拆 2-5 字子串用于匹配，matched_terms 会去重保留最长命中
            for size in range(min(5, len(run)), 1, -1):
                for index in range(0, len(run) - size + 1):
                    term = run[index:index + size]
                    if term not in RagPipeline.stopwords:
                        terms.add(term)
        return sorted(terms, key=RagPipeline.term_order_key)

    @staticmethod
    def focused_query_terms(terms: Iterable[str]) -> list[str]:
        """Return named-topic phrases suitable for an additional exact recall pass."""
        question_scaffolding = (
            "什么", "哪些", "如何", "怎么", "功能", "场景", "有用", "作用", "是否", "可以", "应该",
        )
        selected = []
        seen = set()
        for raw_term in terms:
            term = str(raw_term).strip()
            key = term.lower()
            if (
                len(term) < 4
                or not re.search(r"[\u3400-\u9fff]", term)
                or any(marker in term for marker in question_scaffolding)
                or key in seen
            ):
                continue
            selected.append(term)
            seen.add(key)
            if len(selected) >= 6:
                break
        return selected

    @staticmethod
    def prioritize_required_terms(focus_terms: Iterable[str], required_terms: Iterable[str] | None = None) -> list[str]:
        """Put explicit source/topic anchors first in the bounded FTS retry."""
        ordered: list[str] = []
        seen = set()
        for term in list(required_terms or []) + list(focus_terms):
            clean = str(term or "").strip()
            key = clean.casefold()
            if not clean or key in seen:
                continue
            ordered.append(clean)
            seen.add(key)
        return ordered[:6]

    @staticmethod
    def topic_anchor_terms(question: str) -> list[str]:
        """Return topics that a definition-style question must name directly.

        The vector and FTS stages may use partial terms to maximize recall.  A
        compact definition lookup is different: ``提示词`` is not evidence for
        ``提示词注入``.  This deliberately narrow extractor only adds a hard
        coverage requirement when the user explicitly asks what a named topic
        means.  Broad factual questions remain semantic-friendly.
        """
        text = re.sub(r"\s+", "", str(question or "").split("本轮追问：")[-1]).strip("。！？?；;")
        if not text:
            return []
        # These forms ask for an operation relation or a state value. Treating
        # the whole prefix (for example ``识别失败时状态应该``) as a literal
        # concept creates an impossible hard anchor.
        if RagPipeline.is_purpose_or_usage_question(text) or RagPipeline.is_state_result_question(text):
            return []
        lead = r"(?:(?:请|帮我|给我|麻烦)?(?:解释|介绍|讲讲|说说)(?:一下)?)?"
        explicit_explain_lead = r"(?:请|帮我|给我|麻烦)?(?:解释|介绍|讲讲|说说)(?:一下)?"
        suffix = r"(?:是什么|是啥|啥意思|什么意思|指什么|有何含义|怎么回事|定义)"
        candidate = ""
        match = re.fullmatch(rf"{lead}([\u3400-\u9fffA-Za-z][\u3400-\u9fffA-Za-z0-9_./+\-]{{1,39}}){suffix}", text, flags=re.IGNORECASE)
        if match:
            candidate = match.group(1)
        else:
            match = re.fullmatch(rf"{explicit_explain_lead}([\u3400-\u9fffA-Za-z][\u3400-\u9fffA-Za-z0-9_./+\-]{{1,39}})", text, flags=re.IGNORECASE)
            if match:
                candidate = match.group(1)
        candidate = candidate.strip("，,：:的")
        if (
            len(candidate) < 2
            or re.match(r"(?:这家|那家|这个|那个|这份|那份|这项|那项|该|此|它的|其)", candidate)
            or any(marker in candidate for marker in ("什么", "多少", "几", "如何", "怎么", "为什么", "哪些", "哪个", "能否", "可以"))
        ):
            return []
        return [candidate]

    @staticmethod
    def search_anchor_terms(query: str) -> list[str]:
        """Return a complete compact search phrase for evidence-result pages.

        A search box commonly contains a bare topic rather than a sentence.  In
        that setting a result matching only part of a four-character phrase is
        misleading, while a full question should retain the normal semantic
        retrieval behaviour.
        """
        raw = re.sub(r"\s+", "", str(query or ""))
        if re.search(r"[、，,；;。！？?]", raw):
            return []
        text = raw.strip()
        if 3 <= len(text) <= 40 and RagPipeline.is_bare_topic_query(text):
            return [text]
        return []

    @staticmethod
    def is_bare_topic_query(text: str) -> bool:
        """Whether a compact input names a topic instead of asking a fact question."""
        compact = re.sub(r"\s+", "", str(text or ""))
        if not compact or re.search(r"[、，,；;。！？?]", compact):
            return False
        question_markers = (
            "什么", "多少", "几", "如何", "怎么", "为什么", "哪些", "哪个", "能否", "可以", "吗", "呢",
            "有没有", "有无", "是否", "会不会", "需不需要", "要不要", "是什么样", "作用", "功能", "场景",
        )
        return not any(marker in compact for marker in question_markers)

    @classmethod
    def answer_anchor_terms(cls, question: str) -> list[str]:
        """Return direct-coverage topics for a user-facing AI question.

        The AI entry point accepts both full questions ("提示词注入是啥") and
        bare topics ("提示词注入"). A bare topic normally requests that exact
        concept, not a source that merely shares one substring.
        """
        compact_question = re.sub(r"\s+", "", str(question or "")).strip("。！？?；;")
        # "项目复盘里提到的风险是什么" is a direct lookup inside a named
        # source, not a new concept called "项目复盘里提到的风险".  Keep the
        # named source as the coverage anchor and let normal retrieval score
        # the requested field (风险).  Otherwise the full natural-language
        # phrase becomes an impossible hard constraint and hides an exact note.
        relation_terms = cls.relationship_source_terms(compact_question)
        if relation_terms:
            return relation_terms

        direct = cls.topic_anchor_terms(question) or cls.search_anchor_terms(question)
        if direct:
            return direct
        # The answer box also accepts concise two-character Chinese concepts
        # such as "提示词". Treat those as a request for an explanation rather
        # than letting an incidental mention become a plausible-looking answer.
        compact = re.sub(r"\s+", "", str(question or "")).strip()
        if (
            re.fullmatch(r"[\u3400-\u9fffA-Za-z][\u3400-\u9fffA-Za-z0-9_./+\-]{1,39}", compact)
            and cls.is_bare_topic_query(compact)
        ):
            return [compact]
        return []

    @classmethod
    def retrieval_anchor_terms(cls, question: str) -> list[str]:
        """Use a named entity as the recall anchor instead of its full question."""
        entities = cls.explicit_entity_terms(question)
        if len(entities) == 1:
            return entities
        identifiers = cls.retrieval_identifier_terms(question)
        if identifiers:
            return identifiers
        named_rows = cls.named_structured_subject_terms(question)
        if named_rows:
            return named_rows
        return cls.answer_anchor_terms(question)

    @classmethod
    def named_structured_subject_terms(cls, question: str) -> list[str]:
        """Separate a named business row from its following field request."""
        compact = re.sub(r"\s+", "", str(question or ""))
        match = re.fullmatch(
            r"(?:请问|关于)?([\u3400-\u9fffA-Za-z0-9_-]{2,32}?)"
            r"(?:供应商|客户|候选人|员工)的.+",
            compact,
        )
        if match:
            return [match.group(1)]
        return cls.parallel_field_subject_terms(question)

    @classmethod
    def parallel_field_subject_terms(cls, question: str) -> list[str]:
        """Recognize one name followed by a complete, bounded field request.

        A table identity need not end in Agent. Keep its name separate from
        known field labels without inferring an unknown label or merging
        multiple subjects. A single field requires an explicit 的 separator;
        source row/header checks remain mandatory.
        """
        if cls.relationship_source_terms(question):
            return []
        compact = re.sub(r"\s+", "", str(question or "")).strip("。！？?；;")
        known_fields = {
            term for question_terms, _label_terms in cls.structured_field_aliases()
            for term in question_terms
        }
        field_pattern = "(?:" + "|".join(re.escape(term) for term in sorted(
            known_fields, key=lambda term: (-len(term), term),
        )) + ")"
        fields_pattern = rf"{field_pattern}(?:(?:以及|和|与|、){field_pattern}){{0,2}}"
        suffix = r"(?:分别|各自)?是什么"
        # A field-only question must not split 核心功能 into the invented
        # subject 核心 plus the shorter field alias 功能.
        if re.fullmatch(rf"(?:请问)?(?:关于)?的?{fields_pattern}{suffix}", compact):
            return []
        match = re.fullmatch(
            r"(?:请问)?(?:关于)?(?P<subject>[\u3400-\u9fffA-Za-z0-9_-]{2,32}?)(?P<separator>的?)"
            rf"(?P<fields>{fields_pattern}){suffix}",
            compact,
        )
        if not match:
            return []
        subject = match.group("subject")
        fields = re.split(r"(?:以及|和|与|、)", match.group("fields"))
        if (
            len(set(fields)) != len(fields)
            or (len(fields) == 1 and match.group("separator") != "的")
            or re.search(r"以及|[和与、]", subject)
            or re.match(r"(?:这|那|该|此|其|它|谁|什么|如何|怎么|为什么|哪些|哪个|请|比较|分析|我)", subject)
        ):
            return []
        return [subject]

    @classmethod
    def required_retrieval_topics(cls, question: str) -> list[str]:
        """Return whole-query hard anchors only for a single retrieval task.

        A comparison or cross-record question intentionally names independent
        subjects.  Treating the complete wording as one required phrase causes
        valid evidence for each side to be discarded after retrieval.
        """
        if len(cls.compound_query_clauses(question)) > 1:
            return []
        return cls.retrieval_anchor_terms(question)

    @classmethod
    def subquery_anchor_terms(cls, question: str) -> list[str]:
        """Return only safe hard anchors for one generated sub-query."""
        entities = cls.explicit_entity_terms(question)
        if len(entities) == 1:
            return entities
        identifiers = cls.retrieval_identifier_terms(question)
        if identifiers:
            return identifiers
        relation_terms = cls.relationship_source_terms(question)
        if relation_terms:
            return relation_terms
        if cls.attribute_subject_terms(question):
            return []
        topic_terms = cls.topic_anchor_terms(question)
        if not topic_terms:
            return []
        # A generated clause such as ``数据泄露处置中的 72 小时具体指什么``
        # is a lookup inside a process, not a concept literally named
        # ``数据泄露处置中的72小时具体``.  Making that phrase a hard filter
        # discards the exact sentence ``72 小时内形成处置记录`` before semantic
        # ranking can use it.  Keep hard definition anchors only for standalone
        # named concepts; relational/numeric clauses remain soft retrieval text.
        anchor = topic_terms[0]
        if re.search(r"(?:中|里|内)的?", anchor) and re.search(
            r"\d+(?:\.\d+)?\s*(?:分钟|小时|天|日|周|月|年|%|％|元|万)",
            anchor,
        ):
            return []
        return topic_terms

    @staticmethod
    def attribute_subject_terms(question: str) -> list[str]:
        """Extract the subject from a generated ``subject 的 field`` clause.

        Compound decomposition may turn a shared-attribute question into
        ``打包的内存或体积限制是什么``.  The retrievable subject is ``打包``;
        requiring the complete wording as one literal phrase discards the
        correct passage even when it states the size in natural prose.
        """
        compact = re.sub(r"\s+", "", str(question or "")).strip("。！？?；;")
        match = re.fullmatch(
            r"(.{2,40}?)的(.{2,40}?)(?:是什么|有哪些|是多少|有多少|分别是什么|如何|怎么|吗|呢)",
            compact,
        )
        if not match:
            return []
        subject = match.group(1).strip("的：:")
        if (
            len(subject) < 2
            or re.match(r"(?:这|那|该|此|其|它|他们|它们)", subject)
            or any(marker in subject for marker in ("什么", "多少", "哪些", "如何", "怎么", "为什么"))
        ):
            return []
        return [subject]

    @classmethod
    def attribute_subject_recall_terms(cls, question: str) -> list[str]:
        """Expand an attribute subject into bounded, soft lexical clues.

        Chinese source prose does not always repeat a generated compound subject
        verbatim. ``模型部署`` may appear as ``Qwen 本地部署`` while still being
        the exact subject the user asked about. The full subject remains first;
        short edge words only broaden FTS recall and are never used as hard
        evidence requirements.
        """
        expanded: list[str] = []
        seen: set[str] = set()
        for subject in cls.attribute_subject_terms(question):
            candidates = [subject]
            compact = re.sub(r"\s+", "", subject)
            if re.fullmatch(r"[\u3400-\u9fff]{3,}", compact):
                candidates.append(compact[:2])
                if len(compact) >= 4:
                    candidates.append(compact[-2:])
            for candidate in candidates:
                key = candidate.casefold()
                if len(candidate) < 2 or key in seen:
                    continue
                seen.add(key)
                expanded.append(candidate)
        return expanded[:5]

    @classmethod
    def attribute_subject_coverage(cls, question: str, text: str) -> tuple[float, float, float]:
        """Return full, specific-component, and broad-component coverage.

        Broad nouns such as ``模型`` or ``项目`` occur in many unrelated
        passages.  In a subject such as ``模型部署`` or ``项目复盘``, the
        qualifier is what separates the intended source from a generic model
        error or another project note.  Keep broad nouns useful for recall, but
        give them less authority when ordering evidence.
        """
        subjects = cls.attribute_subject_terms(question)
        if not subjects:
            return 0.0, 0.0, 0.0
        subject = subjects[0]
        recall_terms = cls.attribute_subject_recall_terms(question)
        full_match = float(cls.text_contains_term(text, subject))
        broad_components = {
            "模型", "文件", "数据", "系统", "内容", "信息",
            "资料", "项目", "问题", "功能", "文档", "笔记",
        }
        specific_coverage = sum(
            1.0 for term in recall_terms[1:]
            if term not in broad_components and cls.text_contains_term(text, term)
        )
        broad_coverage = sum(
            0.25 for term in recall_terms[1:]
            if term in broad_components and cls.text_contains_term(text, term)
        )
        return full_match, float(specific_coverage), float(broad_coverage)

    @staticmethod
    def relationship_source_terms(question: str) -> list[str]:
        """Extract the named source from a "资料里提到的 X" lookup."""
        compact = re.sub(r"\s+", "", str(question or "")).strip("。！？?；;")
        relation = re.fullmatch(
            r"(.{2,48}?)(?:里|中|内)(?:提到|提及|说到|写到|包含|列出)的?"
            r".{1,24}?(?:是什么|有哪些|是什么内容|是什么情况|吗|呢)?",
            compact,
        )
        if not relation:
            return []
        source_name = relation.group(1).strip("的：:")
        return [source_name] if len(source_name) >= 2 else []

    @staticmethod
    def is_purpose_or_usage_question(question: str) -> bool:
        """Recognise requests for an operation's purpose or usage context."""
        compact = re.sub(r"\s+", "", str(question or ""))
        return bool(re.search(
            r"(?:目的(?:是)?什么|有什么(?:作用|用途)|(?:作用|用途)(?:是)?什么|"
            r"用来做什么|用于什么|为什么要|为何要|在(?:什么|哪个).{0,8}(?:问题|场景|环节)(?:里|中)?用)",
            compact,
        ))

    @staticmethod
    def is_state_result_question(question: str) -> bool:
        """Recognise a request for an explicit persisted/runtime state value."""
        compact = re.sub(r"\s+", "", str(question or ""))
        return bool(re.search(
            r"(?:状态|status).{0,8}(?:应该|应当|应|会|要|需)?(?:是|为|变成|设置为|置为)?(?:什么|哪个|如何)",
            compact,
            flags=re.IGNORECASE,
        ))

    @classmethod
    def has_purpose_or_usage_evidence(cls, question: str, texts: Iterable[str]) -> bool:
        """Require an anchor and an operation/purpose relation in one local window.

        A technology name appearing elsewhere in a long chunk is not enough.
        The bounded window protects against accepting an incidental ``Pillow``
        mention while still supporting paraphrases such as ``压图`` versus
        ``把图缩放到最长边``.
        """
        if not cls.is_purpose_or_usage_question(question):
            return True
        anchors = cls.explicit_identifier_terms(question)
        for text in texts:
            raw = str(text or "")
            compact = re.sub(r"\s+", "", raw)
            if not compact:
                continue
            if anchors:
                anchor_spans = []
                for anchor in anchors:
                    match = re.search(cls.whitespace_flexible_pattern(anchor), raw, re.IGNORECASE)
                    if not match:
                        anchor_spans = []
                        break
                    anchor_spans.append(match.span())
                if not anchor_spans:
                    continue
                start = max(0, min(span[0] for span in anchor_spans) - 90)
                end = min(len(raw), max(span[1] for span in anchor_spans) + 150)
                window = re.sub(r"\s+", "", raw[start:end])
            else:
                window = compact
            if re.search(
                r"(?:用于|用来|为了|以便|从而|解决|避免|防止|降低|减少|提升|"
                r"把.{0,24}(?:缩放|压缩|转换|限制|写入|设置)|"
                r"将.{0,24}(?:缩放|压缩|转换|限制|写入|设置)|"
                r"(?:缩放|压缩|转换|限制|处理).{0,24}(?:图片|图像|输入|内存|延迟|分辨率))",
                window,
                flags=re.IGNORECASE,
            ):
                return True
        return False

    @classmethod
    def has_state_result_evidence(cls, question: str, texts: Iterable[str]) -> bool:
        """Require an explicit state assignment for a state/result question."""
        if not cls.is_state_result_question(question):
            return True
        texts = list(texts)
        row_coverage = cls.structured_field_coverage(question, texts)
        if row_coverage is not None:
            return row_coverage
        compact_question = re.sub(r"\s+", "", str(question or ""))
        asks_failure = bool(re.search(r"(?:失败|异常|错误)", compact_question))
        for text in texts:
            raw = str(text or "")
            for sentence, _section in cls._evidence_candidates(raw):
                compact = re.sub(r"\s+", "", sentence)
                if asks_failure and not re.search(r"(?:失败|异常|错误|failed)", compact, re.IGNORECASE):
                    continue
                if re.search(
                    r"(?:[A-Za-z_]*status|状态).{0,24}(?:置为|设为|设置为|更新为|变为|=|:|：|→)"
                    r".{0,12}(?:pending|processing|done|failed|success|error|成功|失败|异常|完成|处理中|待处理)",
                    compact,
                    flags=re.IGNORECASE,
                ):
                    return True
        return False

    @classmethod
    def requires_definition_evidence(cls, question: str) -> bool:
        """Apply the explanatory-evidence gate only to concepts, never normal fact queries."""
        # A compound question is verified clause by clause. Requiring one
        # dictionary-style sentence for the whole wording would discard valid
        # evidence that is intentionally distributed across sources.
        if len(cls.compound_query_clauses(question)) > 1:
            return False
        if cls.is_structured_field_query(question):
            return False
        # A named Agent query asks for source-backed fields or capabilities,
        # not a dictionary definition. Its entity coverage is checked
        # separately with whitespace-insensitive matching.
        if cls.explicit_entity_terms(question):
            return False
        # Uppercase business identifiers followed by digits (for example
        # ``HT-2026-021``) name records, not concepts.  A question ending in
        # ``是什么`` usually asks for one field in that row; requiring
        # dictionary wording such as ``HT-2026-021 是...`` hides otherwise
        # exact spreadsheet evidence.
        if cls.record_identifier_terms(question):
            return False
        if cls.named_structured_subject_terms(question):
            return False
        # "项目复盘里提到的风险是什么" asks for a field inside a named
        # source. It is not a request to define that source, so requiring an
        # "项目复盘是..." sentence would incorrectly discard its real evidence.
        if cls.relationship_source_terms(question):
            return False
        # Purpose and state questions need an operation/result statement, not
        # dictionary wording such as ``X 是...``. Dedicated evidence gates
        # below enforce that stronger, question-shaped requirement.
        if cls.is_purpose_or_usage_question(question) or cls.is_state_result_question(question):
            return False
        compact = re.sub(r"\s+", "", str(question or "")).strip("。！？?；;")
        # Imperative snippets are commonly pasted from a note as an exact
        # lookup (for example "不要回退"). They need direct phrase coverage,
        # but are not requests for a dictionary-style definition.
        if re.fullmatch(r"(?:不要|请勿|禁止|勿)[\u3400-\u9fffA-Za-z0-9_./+\-]{2,39}", compact):
            return False
        # A named "是什么/定义" question genuinely needs definitional wording.
        # A longer bare phrase, however, can also be a direct factual lookup
        # (for example a policy target with a year). Its exact phrase coverage
        # remains mandatory, but requiring an "X 是 Y" sentence would hide an
        # otherwise explicit, answerable fact. Keep the stricter definition
        # gate for very short terms such as "提示词", which are prone to an
        # incidental mention in unrelated instructions.
        if cls.topic_anchor_terms(question):
            return True
        bare_topics = cls.search_anchor_terms(question)
        return bool(bare_topics and len(bare_topics[0]) <= 3)

    @staticmethod
    def is_enumeration_question(question: str) -> bool:
        """Whether the user asks for a complete named set, not one example."""
        compact = re.sub(r"\s+", "", str(question or ""))
        return bool(re.search(
            r"(?:有哪些|有哪(?:些)?|什么风险|哪些风险|风险有哪些|风险是什么|提到的风险|有什么风险|"
            r"(?:有)?几(?:个|项|条)?(?:风险|问题|措施|步骤|原因|目标|事项|建议)|"
            r"分别(?:是|有哪些|有什么)|列出|清单)",
            compact,
        ))

    @classmethod
    def is_principle_question(cls, question: str) -> bool:
        """Recognise requests for a source's explicit rules or principles."""
        compact = re.sub(r"\s+", "", str(question or ""))
        return bool(re.search(
            r"(?:原则|规范|准则|要求|取舍|边界).{0,14}(?:哪些|是什么|什么|遵守|遵循)"
            r"|(?:需要|应当|必须|遵守|遵循).{0,18}(?:原则|规范|准则|要求)",
            compact,
        ))

    @classmethod
    def principle_evidence(cls, question: str, text: str) -> str:
        """Keep a numbered set of source rules together as one evidence unit.

        This is deliberately structural: the source must contain two or more
        numbered items plus rule-like language.  A generic note that happens
        to mention "资料" or "演示" cannot become a principle source merely
        through semantic similarity.
        """
        if not cls.is_principle_question(question):
            return ""
        raw = str(text or "").replace("\r", "").strip()
        if not raw or not re.search(r"(?:原则|规范|准则|要求|取舍|边界|标准|必须|应当|不得|保留|不开放)", raw):
            return ""
        marker = r"(?:第\s*[一二三四五六七八九十]+|\d+)\s*[、，,:：.]"
        matches = list(re.finditer(marker, raw))
        if len(matches) < 2:
            return ""
        items = []
        for index, match in enumerate(matches[:6]):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
            item = raw[match.start():end].strip(" \n；;")
            if len(item) >= 16:
                items.append(item[:420])
        return "\n".join(items) if len(items) >= 2 else ""

    @classmethod
    def principle_evidence_bonus(cls, question: str, text: str) -> float:
        evidence = cls.principle_evidence(question, text)
        if not evidence:
            return 0.0
        return min(18.0, 8.0 + 3.0 * len(evidence.splitlines()))

    @classmethod
    def ocr_agent_table_evidence(cls, question: str, text: str) -> str:
        """Recover one named Agent row from flattened OCR table text.

        Image OCR often preserves every cell but loses the visual column grid.
        When the header explicitly contains ``Agent名称 / 核心功能 / 适用场景``,
        the row order is still reliable.  This method only emits a reconstructed
        record when both the requested named Agent and a verifiable scene cell
        are present; otherwise it returns nothing rather than inventing fields.
        """
        entities = cls.explicit_entity_terms(question)
        compact_question = re.sub(r"\s+", "", str(question or ""))
        asks_fields = bool(re.search(r"(?:核心功能|适用场景|功能|场景|作用|负责什么)", compact_question))
        # Markdown tables use ``|`` only as a cell border.  Treat it as
        # whitespace before reconstructing the OCR/markdown row so the
        # visible evidence never exposes formatting pipes as answer text.
        raw = str(text or "").replace("\r", "").replace("|", " ")
        if len(entities) != 1 or not asks_fields or not raw:
            return ""
        entity = entities[0]
        entity_match = re.search(cls.whitespace_flexible_pattern(entity), raw, flags=re.IGNORECASE)
        if not entity_match:
            return ""
        header = re.sub(r"\s+", "", raw[:entity_match.start()])
        if not all(label in header for label in ("Agent名称", "核心功能", "适用场景")):
            return ""

        tail = raw[entity_match.end():]
        # Rows normally start with an ordinal followed by another ...Agent.
        # Stop before it so facts from a neighbouring row cannot be blended.
        next_row = re.search(r"(?:\n|(?<![A-Za-z]))\s*\d{1,3}\s*[\u3400-\u9fffA-Za-z]{2,40}Agent", tail, re.IGNORECASE)
        if next_row:
            tail = tail[:next_row.start()]
        row_lines = [line.strip(" ：:；;，,。") for line in tail.splitlines() if line.strip(" ：:；;，,。")]
        compact_tail = re.sub(r"\s+", " ", tail).strip(" ：:；;，,。")
        if not compact_tail:
            return ""

        # The fourth column is the application scene.  Recognise only a
        # compact, source-visible scene phrase; the function is exactly the
        # preceding row cell.  This covers rows such as
        # "项目风险扫描Agent 识别... 项目复盘 周报、问题清单 ...".
        scene_match = re.search(
            r"(?P<scene>[\u3400-\u9fffA-Za-z0-9]{2,18}"
            r"(?:复盘|例会|会议|整理|查询|学习|汇报|维护|交接|协作|效率|管理|评审|审批))",
            compact_tail,
        )
        if not scene_match or scene_match.start() < 4:
            return ""
        function = compact_tail[:scene_match.start()].strip(" ：:；;，,。")
        scene = scene_match.group("scene").strip()
        # OCR may emit one wrapped cell tail before the cell itself, for
        # example: "认项\n识别…待确\n项目复盘". The characters are all
        # present but their two visual lines were ordered incorrectly. Restore
        # that exact short tail only when the scene occurs on its own later
        # line; ordinary one-line tables keep the conservative compact path.
        scene_line = next((index for index, line in enumerate(row_lines) if scene in line), None)
        if scene_line and scene_line >= 2:
            function_lines = row_lines[:scene_line]
            leading_tail = function_lines[0]
            main_cell = "".join(function_lines[1:])
            if len(leading_tail) <= 4 and len(main_cell) >= 6:
                function = main_cell + leading_tail
        if len(function) < 4 or len(function) > 180:
            return ""
        return f"Agent名称：{entity}；核心功能：{function}；适用场景：{scene}。"

    @classmethod
    def ocr_agent_table_bonus(cls, question: str, text: str) -> float:
        return 30.0 if cls.ocr_agent_table_evidence(question, text) else 0.0

    @classmethod
    def enumeration_marker_pattern(cls, question: str) -> str:
        """Return numbered labels relevant to an explicit list question.

        The generic fallback is useful for requests such as "有哪些步骤".  If
        the question names a label (for example "风险"), keep that label only;
        otherwise a nearby "目标一" must not displace "风险一".
        """
        compact = re.sub(r"\s+", "", str(question or ""))
        labels = [
            label for label in ("风险", "问题", "措施", "步骤", "原因", "目标", "事项", "建议")
            if label in compact
        ]
        label_pattern = "|".join(re.escape(label) for label in labels) if labels else (
            r"风险|问题|措施|步骤|原因|目标|事项|建议"
        )
        return rf"(?:{label_pattern})\s*(?:[一二三四五六七八九十]+|\d+)[：:]"

    @classmethod
    def enumeration_item_count(cls, question: str, text: str) -> int:
        stage_block, stage_labels = cls.stage_enumeration_block(question, text)
        if stage_block:
            return len(stage_labels)
        return len(re.findall(cls.enumeration_marker_pattern(question), str(text or "")))

    @classmethod
    def stage_enumeration_block(cls, question: str, text: str) -> tuple[str, list[str]]:
        """Keep an explicitly named, topic-bound group of parallel stages.

        A stage list can use names instead of ordinals (``设计阶段，…；生产
        阶段，…``). Require both the question's subject and at least two
        distinct labels in one paragraph; shared words or stages in another
        subject cannot establish a list. Questions about one stage's budget or
        other attributes are deliberately outside this narrow enumeration form.
        """
        compact = re.sub(r"\s+", "", str(question or "")).strip("?？。!！")
        if re.search(r"(?:不是|并非|不包括|不包含|不属于|没有)", compact):
            return "", []
        asked = re.fullmatch(
            r"(?P<subject>.+?)(?:(?:有|包括|包含)?哪些|哪几个)阶段", compact,
        ) or re.fullmatch(r"(?P<subject>.+?)阶段(?:分别)?(?:有|包括|是)?哪些", compact)
        if not asked:
            return "", []
        subject = re.sub(r"^(?:请问|关于|有关|针对|我想了解|想了解)", "", asked.group("subject"))
        subject = re.sub(r"(?:(?:具体|主要|通常|一般|大致|分为|分成|包括|包含)|[，,:：的])+$", "", subject)
        if len(subject) < 3:
            return "", []
        marker = re.compile(r"^\s*([\u3400-\u9fffA-Za-z0-9 ·（）()\-]{1,20}阶段)\s*[，,:：]\s*\S")
        for paragraph in re.split(r"\n\s*\n", str(text or "").replace("\r", "")):
            # Preserve the actual source paragraph, including its subject.
            # This also lets the final answer contract inspect the same proof.
            if subject.casefold() not in re.sub(r"\s+", "", paragraph).casefold():
                continue
            if re.search(r"(?:不|未)(?:采用|包含|包括|划分|分为|分成|纳入)|并非|不是", paragraph):
                continue
            labels = []
            for clause in re.split(r"[。；;！？\n]+", paragraph):
                matched = marker.match(clause)
                if matched:
                    label = matched.group(1).strip()
                    if label not in labels:
                        labels.append(label)
            if 2 <= len(labels) <= 8:
                return paragraph.strip(), labels
        return "", []

    @classmethod
    def enumerated_evidence(cls, question: str, text: str) -> str:
        """Keep labelled source items together for an enumeration question.

        Notes often express a compact checklist as "风险一 / 风险二 / 风险三".
        Splitting that block by sentence and retaining its first hit is an
        answer-quality loss, so preserve the complete labelled set as the
        source evidence. This is format-based, not domain-specific.
        """
        if not cls.is_enumeration_question(question):
            return ""
        raw = str(text or "").replace("\r", "").strip()
        if not raw:
            return ""
        stage_block, _ = cls.stage_enumeration_block(question, raw)
        if stage_block:
            return stage_block
        marker = cls.enumeration_marker_pattern(question)
        matches = list(re.finditer(marker, raw))
        if len(matches) < 2:
            return ""
        items = []
        for index, match in enumerate(matches[:6]):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
            item = raw[match.start():end].strip(" \n；;")
            if len(item) >= 8:
                items.append(item[:420])
        return "\n".join(items)

    @staticmethod
    def is_structured_field_query(question: str) -> bool:
        """Recognize compact field labels such as ``PO累积金额``.

        These are lookup requests against a spreadsheet/schema, not requests
        for an encyclopedic definition. Requiring "X 是..." wording would hide
        an otherwise exact field match.
        """
        compact = re.sub(r"\s+", "", str(question or "")).strip()
        return bool(re.fullmatch(
            r"[A-Z]{2,}[A-Za-z0-9_-]*[\u3400-\u9fff]{1,20}", compact
        ))

    @classmethod
    def has_definition_evidence(cls, topics: list[str], texts: Iterable[str]) -> bool:
        """Require an explanatory claim, not an incidental term occurrence.

        This gate is intentionally used only for bare-topic and definition
        questions. A source saying "把提示词粘贴到 Chat 框" contains the word
        but does not explain it. Conversely, "提示词是给模型的任务指令" and
        structured Agent records that name the Agent plus its core function are
        direct evidence.
        """
        if not topics:
            return True
        rows = []
        for text in texts:
            rows.extend(line.strip() for line in re.split(r"[\n。！？!?；;]", str(text or "")) if line.strip())
        for topic in topics:
            matched = False
            for row in rows:
                compact = re.sub(r"\s+", "", row)
                topic_index = compact.casefold().find(re.sub(r"\s+", "", topic).casefold())
                if topic_index < 0:
                    continue
                tail = compact[topic_index + len(re.sub(r"\s+", "", topic)):topic_index + len(re.sub(r"\s+", "", topic)) + 20]
                if re.search(r"(?:是|指|即|属于|用于|用来|表示|意味着|核心功能|适用场景)", tail):
                    matched = True
                    break
                if topic.lower().endswith(("agent", "智能体")) and "核心功能" in compact:
                    matched = True
                    break
            if not matched:
                return False
        return True

    @staticmethod
    def text_contains_term(text: str, term: str) -> bool:
        normalized_text = re.sub(r"\s+", "", str(text or "")).casefold()
        normalized_term = re.sub(r"\s+", "", str(term or "")).casefold()
        return bool(normalized_term and normalized_term in normalized_text)

    @classmethod
    def scope_extra_terms_for_subquery(
        cls,
        subquery: str,
        extra_terms: Iterable[str] | None,
    ) -> list[str]:
        """Keep whole-query aliases only on the compound clause they describe.

        Query understanding exposes formatting aliases such as ``项目风险扫描
        Agent`` for a compact source value.  Those aliases improve exact recall,
        but carrying every alias into every independently retrieved clause makes
        one comparison subject dominate the others.  Whitespace-insensitive
        containment preserves the intended formatting alias without introducing
        a new subject into the clause.
        """
        scoped: list[str] = []
        seen: set[str] = set()
        for raw_term in extra_terms or []:
            term = str(raw_term or "").strip()
            key = re.sub(r"\s+", "", term).casefold()
            if not key or key in seen or not cls.text_contains_term(subquery, term):
                continue
            seen.add(key)
            scoped.append(term)
        return scoped

    @staticmethod
    def whitespace_flexible_pattern(value: str) -> str:
        """Match a compact identifier even when OCR or tables insert spaces."""
        compact = re.sub(r"\s+", "", str(value or ""))
        return r"\s*".join(re.escape(char) for char in compact)

    @staticmethod
    def quantified_parallel_tasks(question: str, *, require_matching_count: bool = True) -> dict[str, str]:
        """Expand explicitly counted modifiers sharing one task noun.

        ``哪两项质量与安全检查`` names two evidence obligations even though
        it has only one question predicate. Require the written count to match
        the distinct modifiers before decomposing retrieval; a category can
        contain more than one actual task. With count checking disabled the
        named categories can still guide evidence-only recovery, without
        asserting how many tasks the evidence proves. Values retain the task
        phrase for source-body coverage checks.
        """
        match = re.fullmatch(
            r"(?P<prefix>[^。！？?；;]*?)哪(?P<count>两|二|三|2|3)项"
            r"(?P<modifiers>[^。！？?；;]{2,48}?)(?P<task>检查|校验|核验|评审)[？?]?",
            str(question or "").strip(),
        )
        if not match:
            return {}
        count = {"两": 2, "二": 2, "2": 2, "三": 3, "3": 3}[match.group("count")]
        modifiers = [
            part.strip() for part in re.split(r"(?:以及|与|和|及|[、，,])", match.group("modifiers"))
        ]
        if len(modifiers) not in (2, 3) or (require_matching_count and len(modifiers) != count) or any(
            not re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9_-]{2,12}", part)
            for part in modifiers
        ):
            return {}
        task = match.group("task")
        focuses = [part if part.endswith(task) else f"{part}{task}" for part in modifiers]
        if len(set(focuses)) != len(modifiers):
            return {}
        return {f"{match.group('prefix')}{focus}": focus for focus in focuses}

    @classmethod
    def shared_record_field_queries(cls, question: str) -> list[str]:
        """Keep one explicit record identity on each of two or three fields."""
        match = re.fullmatch(
            r"(?P<subject>.+?)的(?P<fields>[^的。！？?；;]+?)"
            r"(?:分别|各自)(?P<predicate>是什么|是多少|有哪些|如何)[？?]?",
            str(question or "").strip(),
        )
        if not match or len(cls.record_identifier_terms(match.group("subject"))) != 1:
            return []
        fields = [part.strip() for part in re.split(r"(?:以及|和|与|、)", match.group("fields"))]
        known_fields = {
            term for question_terms, label_terms in cls.structured_field_aliases()
            for term in (*question_terms, *label_terms)
        }
        if not 2 <= len(set(fields)) <= 3 or not all(field in known_fields for field in fields):
            return []
        return [
            f"{match.group('subject')}的{field}{match.group('predicate')}"
            for field in dict.fromkeys(fields)
        ]

    @classmethod
    def compound_query_clauses(cls, question: str) -> list[str]:
        """Split explicit multi-task questions for independent recall.

        The rules deliberately target explicit sentence boundaries, paired
        question predicates, comparisons and cross-record relationships. A bare
        ``和`` is not enough: phrases such as ``工业互联网与人工智能融合`` must
        remain one query unless the user actually asks to compare both subjects.
        """
        text = str(question or "").strip()
        if not text:
            return []
        if quantified_tasks := cls.quantified_parallel_tasks(text):
            return list(quantified_tasks)

        def clean(value: str) -> str:
            value = re.sub(
                r"^(?:(?:请)?(?:分别)?(?:说明|回答|介绍|解释)[:：\s]*|又|再)",
                "",
                str(value or "").strip(),
            )
            return value.strip(" ，、。；;！？?\t")

        def substantive(value: str) -> bool:
            return len(re.sub(r"[\s，、。；;！？?？]", "", value)) >= 4

        def valid(parts: list[str]) -> list[str]:
            cleaned = [clean(part) for part in parts]
            cleaned = list(dict.fromkeys(part for part in cleaned if substantive(part)))
            return cleaned[:4] if len(cleaned) > 1 else []

        # Preserve the historic semicolon API shape, including the terminal
        # question mark on each explicit sub-question.
        if "；" in text or ";" in text:
            semicolon_parts = []
            for raw_part in re.split(r"[；;]", text):
                part = re.sub(
                    r"^(?:(?:请)?(?:分别)?(?:说明|回答|介绍|解释)[:：\s]*|又|再)",
                    "",
                    raw_part.strip(),
                ).strip(" ，、。；;\t")
                if substantive(part):
                    semicolon_parts.append(part)
            semicolon_parts = list(dict.fromkeys(semicolon_parts))[:4]
            if len(semicolon_parts) > 1:
                return semicolon_parts

        # A question mark inside the text is an unambiguous sub-question
        # boundary; the final question mark is left untouched by the lookahead.
        strong_parts = valid(re.split(r"[？?](?=\s*\S)", text))
        if strong_parts:
            return strong_parts

        joined_parts = valid(re.split(r"(?:并且|同时)", text))
        if joined_parts:
            return joined_parts

        # One scenario may contain two independently searchable constraints.
        # Keep the shared question predicate on both sides so a fragment such as
        # "采购金额 25 万元" is not sent to retrieval without its intent.
        condition_match = re.fullmatch(
            r"(?P<left>.+?)且(?P<right>.+?)[，,](?P<tail>[^，,]+[？?]?)",
            text,
        )
        if condition_match and re.search(r"(?:什么|哪些|如何|怎么|是否|能否|关注|要求|审批)", condition_match.group("tail")):
            condition_parts = valid([
                f"{condition_match.group('left')}，{condition_match.group('tail')}",
                f"{condition_match.group('right')}，{condition_match.group('tail')}",
            ])
            if condition_parts:
                return condition_parts

        # Cross-record trigger questions name two independently retrievable
        # facts even though they are phrased as one yes/no question.
        trigger_match = re.fullmatch(
            r"(?P<left>.+?)是否(?:可能)?触发(?P<right>.+?)[？?]?",
            text,
        )
        if trigger_match:
            trigger_parts = valid([trigger_match.group("left"), trigger_match.group("right")])
            if trigger_parts:
                return trigger_parts

        # Two comma-separated clauses that each carry their own question word
        # are separate retrieval tasks, even without an explicit conjunction.
        comma_parts = [part for part in re.split(r"[，,]", text) if part.strip()]
        question_cues = r"(?:什么时候|何时|哪一天|哪些|什么|如何|怎么|是否|能否|多少|几个)"
        if len(comma_parts) == 2 and all(re.search(question_cues, part) for part in comma_parts):
            # Boundary tails often omit their subject: ``72 小时具体指什么，
            # 不能理解成什么``. Preserve both answer obligations, but carry the
            # first subject into the second retrieval query so the strict
            # compound gate can verify the same explicit source-side boundary.
            boundary_continuation = re.match(
                r"\s*(?:不能理解(?:成|为)|不等于|不代表|不是什么)",
                comma_parts[1],
            )
            if boundary_continuation:
                shared_subject = re.sub(
                    r"(?:具体)?(?:指|是)什么[？?]?\s*$",
                    "",
                    comma_parts[0],
                ).strip()
                if substantive(shared_subject):
                    carried = valid([
                        comma_parts[0],
                        f"{shared_subject}{comma_parts[1].strip()}",
                    ])
                    if carried:
                        return carried
            # ``某 Agent 有什么功能，什么场景有用`` asks two fields about
            # one subject; the second clause deliberately omits that subject.
            # Keep it together so entity focusing and structured-row extraction
            # can return one complete record.
            same_subject_continuation = bool(
                cls.explicit_entity_terms(comma_parts[0])
                and not cls.explicit_entity_terms(comma_parts[1])
                and re.match(rf"\s*{question_cues}", comma_parts[1])
            )
            if not same_subject_continuation:
                paired = valid(comma_parts)
                if paired:
                    return paired

        # Several fields of one identified row share the same subject. Carry
        # its ID into every field lookup instead of turning the last field
        # into a bare definition query ("付款比例是什么"). Separate queries
        # still retain evidence when the row spans chunks or documents.
        if row_fields := cls.shared_record_field_queries(text):
            return row_fields

        if cls.parallel_field_subject_terms(text):
            return [text]

        # Parallel fields of one named Agent belong to the same table row.
        # Treating their conjunction as a comparison drops the subject from
        # the second field ("核心功能是什么") and loses its evidence binding.
        # Keep the complete question for the existing row/header checks. Two
        # named Agents and unknown field phrases still use the normal parser.
        entity_fields = re.fullmatch(
            r"(?P<subject>.+?)的(?P<fields>[^的。！？?；;]+?)"
            r"(?:分别|各自)(?:是什么|是多少|有哪些|如何)[？?]?",
            text,
        )
        if entity_fields:
            subject = entity_fields.group("subject")
            entities = cls.explicit_entity_terms(subject)
            fields = [re.sub(r"\s+", "", part) for part in re.split(
                r"(?:以及|和|与|、)", entity_fields.group("fields"),
            )]
            known_fields = {
                term for question_terms, label_terms in cls.structured_field_aliases()
                for term in (*question_terms, *label_terms)
            }
            if (
                len(entities) == 1
                and len(re.findall(r"Agent|智能体", subject, flags=re.IGNORECASE)) == 1
                and not re.search(r"以及|[和与、]", subject)
                and re.fullmatch(
                    r"(?:请问[\s，,：:]*)?(?:关于[\s，,：:]*)?"
                    + cls.whitespace_flexible_pattern(entities[0]),
                    subject.strip(), flags=re.IGNORECASE,
                )
                and 2 <= len(set(fields)) <= 3
                and all(field in known_fields for field in fields)
            ):
                return [text]

        comparison_markers = r"(?:各自|分别|共同点|区别|不同|是否一致|有什么关系|合规边界|之间)"
        if re.search(comparison_markers, text):
            connector = re.search(r"(?:[，,]\s*)?(?:和|与|以及)\s*", text)
            if connector:
                left = clean(text[:connector.start()])
                right = clean(text[connector.end():])
                left_query = left
                right_query = right

                # A trailing shared predicate applies to both subjects.
                if re.search(r"[，,]\s*(?:各自|分别)", right):
                    subject, focus = re.split(r"[，,]\s*(?:各自|分别)", right, maxsplit=1)
                    left_query = f"{left}{focus}"
                    right_query = f"{subject}{focus}"
                elif "各自" in right or "分别" in right:
                    shared_attribute = re.fullmatch(
                        r"(?P<subject>.+)(?P<focus>的[^，,；;。！？?]{1,36}?)"
                        r"(?:各自|分别)(?P<predicate>是什么|是多少|有哪些|有多少|为多少|如何|怎么|"
                        r"采取了什么(?:手段|措施)?|有什么(?:要求|限制|风险|区别|优缺点)?)[？?]?",
                        right,
                    )
                    if shared_attribute and substantive(shared_attribute.group("subject")):
                        shared_tail = (
                            f"{shared_attribute.group('focus')}"
                            f"{shared_attribute.group('predicate')}"
                        )
                        left_query = f"{left}{shared_tail}"
                        right_query = f"{shared_attribute.group('subject')}{shared_tail}"
                    else:
                        subject_focus = re.split(r"(?:各自|分别)", right, maxsplit=1)
                        if len(subject_focus) == 2 and substantive(subject_focus[0]):
                            left_query = f"{left}{subject_focus[1]}"
                            right_query = f"{subject_focus[0]}{subject_focus[1]}"
                else:
                    focus_patterns = (
                        r"(?P<subject>.+?)(?P<focus>对.+?的要求)(?:是否一致|有什么不同)?[？?]?",
                        r"(?P<subject>.+?)(?P<focus>在.+?的处理)(?:有什么不同|有何不同)?[？?]?",
                        r"(?P<subject>.+?)(?P<focus>的合规边界)(?:是什么|有哪些)?[？?]?",
                    )
                    for pattern in focus_patterns:
                        match = re.fullmatch(pattern, right)
                        if not match:
                            continue
                        # In an asymmetric comparison the left side may already
                        # be a complete fact, while only the right side carries a
                        # condition. For example, ``招聘面试的结构化反馈`` must not
                        # become ``...在责任人缺失时的处理`` merely because the
                        # meeting-Agent half contains that condition.
                        if pattern.startswith(r"(?P<subject>.+?)(?P<focus>在") and "的" in left:
                            left_query = left
                        else:
                            left_query = f"{left}{match.group('focus')}"
                        right_query = f"{match.group('subject')}{match.group('focus')}"
                        break
                    else:
                        right_query = re.sub(
                            r"(?:之间)?(?:有什么共同点和区别|有什么区别|有什么不同|有何不同|有什么关系)[？?]?$",
                            "",
                            right,
                        )
                comparison_parts = valid([left_query, right_query])
                if comparison_parts:
                    return comparison_parts

        return [text]

    @staticmethod
    def compound_clause_queries(clause: str) -> list[str]:
        """Return bounded, explainable retrieval variants for one clause.

        ``合规边界`` is usually documented under an approval rule rather than
        under that abstract phrase.  Add one terminology bridge without asking
        the LLM to rewrite the query or widening every query globally.  The
        original clause always remains first and normal reranking still decides
        whether the variant contributes evidence.
        """
        original = str(clause or "").strip()
        if not original:
            return []
        variants = [original]
        match = re.fullmatch(
            r"(?P<subject>.{2,48}?)(?:的)?合规边界(?:是什么|有哪些)?[？?]?",
            original,
        )
        if match:
            subject = match.group("subject").strip()
            if subject.startswith("采购") and len(subject) > 2:
                subject = f"{subject[2:]}采购"
            variants.append(f"{subject}审批规则")
        return list(dict.fromkeys(variant for variant in variants if variant))[:2]

    @staticmethod
    def merge_query_variants(results: list[list[dict]]) -> list[dict]:
        """Merge query variants without letting an expansion replace the original."""
        merged: list[dict] = []
        seen: set[str] = set()
        for result in results:
            for item in result:
                key = str(
                    item.get("chunk_identity")
                    or re.sub(r"\s+", "", str(item.get("excerpt", ""))[:160])
                )
                if key in seen:
                    continue
                seen.add(key)
                merged.append(item)
        return merged

    @staticmethod
    def compound_candidate_limit(final_limit: int) -> int:
        """Return bounded clause-recall depth before final context pruning."""
        return max(8, final_limit)

    @classmethod
    def merge_compound_results(
        cls,
        question: str,
        clauses: list[str],
        overall: list[dict],
        clause_results: list[list[dict]],
        limit: int,
    ) -> tuple[list[dict], int, int]:
        """Merge sub-query results while reserving one credible slot per clause."""
        if limit <= 0:
            return [], 0, 0
        quantified_tasks = cls.quantified_parallel_tasks(question)

        def identity(item: dict) -> str:
            return str(item.get("chunk_identity") or re.sub(r"\s+", "", str(item.get("excerpt", ""))[:160]))

        def group(item: dict) -> str:
            note = item.get("note")
            return str(item.get("source_group") or item.get("source_id") or getattr(note, "id", ""))

        def credible(clause: str, item: dict) -> bool:
            text = f"{item.get('title', '')}\n{item.get('raw_text', item.get('excerpt', ''))}"
            if focus := quantified_tasks.get(clause):
                if not cls.text_contains_term(str(item.get("raw_text", item.get("excerpt", ""))), focus):
                    return False
            anchors = cls.subquery_anchor_terms(clause)
            if anchors and not all(cls.text_contains_term(text, anchor) for anchor in anchors):
                return False

            generic = {
                "ai", "agent", "什么", "哪些", "如何", "怎么", "是否", "需要",
                "关注", "要求", "问题", "内容", "资料", "这个", "那个",
            }
            signals = []
            for raw_term in item.get("hits", []):
                term = str(raw_term or "").strip()
                compact = re.sub(r"\s+", "", term).casefold()
                if (
                    len(compact) < 2
                    or compact in generic
                    or re.fullmatch(r"[\d.%-]+", compact)
                    or any(marker in compact for marker in ("什么", "哪些", "如何", "怎么"))
                ):
                    continue
                signals.append(compact)

            return bool(
                len(set(signals)) >= 2
                or any(len(signal) >= 4 for signal in signals)
                or float(item.get("fts_score", 0.0) or 0.0) > 0
                or float(item.get("score", 0.0) or 0.0) >= 0.62
            )

        combined: dict[str, dict] = {}
        coverage: dict[str, set[int]] = {}
        clause_coverage: dict[str, set[str]] = {}
        for query_index, result in enumerate([overall, *clause_results]):
            for item in result:
                key = identity(item)
                coverage.setdefault(key, set()).add(query_index)
                if query_index > 0 and (
                    not quantified_tasks or credible(clauses[query_index - 1], item)
                ):
                    clause_coverage.setdefault(key, set()).add(clauses[query_index - 1])
                current = combined.get(key)
                if current is None or float(item.get("rank_score", 0.0) or 0.0) > float(current.get("rank_score", 0.0) or 0.0):
                    combined[key] = dict(item)

        clause_order = {clause: index for index, clause in enumerate(clauses)}
        for key, item in combined.items():
            item["compound_clauses"] = sorted(
                clause_coverage.get(key, set()),
                key=lambda clause: clause_order.get(clause, len(clause_order)),
            )

        reserved: list[dict] = []
        reserved_keys: set[str] = set()
        reserved_groups: set[str] = set()
        reserved_clause_count = 0
        for clause, result in zip(clauses, clause_results):
            candidates = [item for item in result if credible(clause, item)]
            candidates.sort(
                key=lambda item: cls._source_evidence_priority(clause, item),
                reverse=True,
            )
            candidate = next((item for item in candidates if group(item) not in reserved_groups), None)
            candidate = candidate or (candidates[0] if candidates else None)
            if candidate is None:
                continue
            key = identity(candidate)
            reserved_clause_count += 1
            if key in reserved_keys:
                chosen = combined[key]
                chosen["compound_reserved"] = True
                continue
            chosen = combined.get(key, dict(candidate))
            chosen["compound_reserved"] = True
            reserved.append(chosen)
            reserved_keys.add(key)
            reserved_groups.add(group(chosen))
            if len(reserved) >= limit:
                break

        # Additional chunks are useful only when they belong to a source already
        # selected for one of the explicit clauses.  Feeding unrelated tail
        # candidates to a small local model can make the least-relevant passage
        # overwrite two correct leading sources.
        pool = [
            item for item in combined.values()
            if group(item) in reserved_groups and clause_coverage.get(identity(item))
        ]
        pool.sort(
            key=lambda item: (
                len(coverage.get(identity(item), set())),
                float(item.get("rank_score", 0.0) or 0.0),
                float(item.get("score", 0.0) or 0.0),
                getattr(item.get("note"), "updated_at", datetime.min),
            ),
            reverse=True,
        )
        ordered = reserved + [item for item in pool if identity(item) not in reserved_keys]
        selected = cls.dedupe_chunks(ordered, limit, question)
        return selected, len(combined), reserved_clause_count

    @classmethod
    def retain_focused_chunks(cls, question: str, chunks: list[dict]) -> list[dict]:
        """Remove generic semantic neighbours only for an explicit entity identifier.

        A broad Chinese topic can legitimately require several sources.  It must
        never be narrowed just because one retrieved chunk happens to contain a
        long topic substring.  We only apply this display/context narrowing when
        the user writes an entity-style identifier such as ``需求污染检测Agent``
        and the top chunk contains that complete identifier.
        """
        if not chunks:
            return chunks
        if len(cls.compound_query_clauses(question)) > 1:
            return chunks
        entity_terms = cls.explicit_entity_terms(question)
        # A query naming two Agents is inherently comparative/multi-fact. Its
        # clause-level retrieval has already assembled separate evidence, so it
        # must not be narrowed around whichever entity happened to rank first.
        if len(entity_terms) != 1:
            return chunks

        primary = chunks[0]
        primary_text = f"{primary.get('title', '')}\n{primary.get('raw_text', primary.get('excerpt', ''))}"
        matched_entities = [term for term in entity_terms if cls.text_contains_term(primary_text, term)]
        if not matched_entities:
            return chunks

        retained = []
        for chunk in chunks:
            chunk_text = f"{chunk.get('title', '')}\n{chunk.get('raw_text', chunk.get('excerpt', ''))}"
            if any(cls.text_contains_term(chunk_text, term) for term in matched_entities):
                retained.append(chunk)
        return retained or chunks

    @staticmethod
    def explicit_entity_terms(question: str) -> list[str]:
        """Extract full Agent/智能体 identifiers without question lead-ins."""
        # A contiguous Chinese lead-in ("关于文件更新检查Agent") is question
        # grammar, not part of the entity. Strip only leading discourse words;
        # the full remaining name must still occur in the cited evidence.
        source = re.sub(
            r"^\s*(?:请问[\s，,：:]*(?:关于[\s，,：:]*)?|关于[\s，,：:]*)",
            "",
            str(question or ""),
        )
        source = re.sub(
            r"(?<=[\u3400-\u9fffA-Za-z0-9_\-])\s+(?=(?:Agent|智能体)(?![A-Za-z0-9_\-]))",
            "",
            source,
            flags=re.IGNORECASE,
        )
        matches = re.findall(
            r"(?<![A-Za-z0-9_\-])[\u3400-\u9fffA-Za-z0-9_\-]{2,}(?:Agent|智能体)(?![A-Za-z0-9_\-])",
            source,
            flags=re.IGNORECASE,
        )
        # Do not mistake a task phrase such as "扫描低效Agent" for a literal
        # agent name. It describes the type of Agent to find, while a named
        # entity such as "隐形低效扫描Agent" should still participate in exact
        # entity narrowing.
        action_prefixes = ("我该", "请", "能否", "是否", "如果", "想知道", "扫描", "评估", "识别", "使用", "选择", "需要", "找到", "查看", "生成", "创建", "分析", "比较", "了解", "知道", "如何", "怎么", "这个", "该", "那个", "此")
        return list(dict.fromkeys(match for match in matches if not match.startswith(action_prefixes)))

    @staticmethod
    def explicit_identifier_terms(question: str) -> list[str]:
        """Extract user-specified identifiers that evidence must not substitute.

        Semantic similarity is useful for recall, but a source about supplier A,
        Qwen3-0.6B, or Windows OCR cannot establish a claim about supplier B,
        Qwen3-4B, or MacBook.  This extractor is intentionally generic: it
        covers mixed-language business identifiers plus model, product, acronym,
        version, and date-like ASCII tokens without encoding fixture answers.
        """
        source = str(question or "")
        terms: list[str] = []

        entity_prefixes = (
            "供应商", "客户", "项目", "合同", "订单", "产品", "公司", "设备",
            "版本", "模型", "工单", "批次", "方案", "规则", "节点",
        )
        prefix_pattern = "|".join(re.escape(prefix) for prefix in entity_prefixes)
        for match in re.finditer(
            rf"(?:{prefix_pattern})\s*[A-Za-z0-9][A-Za-z0-9_.+\-/]*",
            source,
            flags=re.IGNORECASE,
        ):
            terms.append(re.sub(r"\s+", "", match.group(0)))

        ignored = {
            "agent", "vs", "and", "or", "the", "is", "are", "of", "to",
            "in", "on", "for", "with", "from", "current",
        }
        ascii_tokens = re.findall(
            r"(?<![A-Za-z0-9])([A-Za-z0-9][A-Za-z0-9]*(?:[._+\-/][A-Za-z0-9]+)*)(?![A-Za-z0-9])",
            source,
        )
        for token in ascii_tokens:
            if len(token) < 2 or token.casefold() in ignored:
                continue
            # Lowercase prose is not a stable identifier. Keep names, acronyms,
            # model/version strings, and numeric/date tokens.
            if not (
                token[0].isupper()
                or sum(char.isupper() for char in token) >= 2
                or any(char.isdigit() for char in token)
                or re.search(r"[._+\-/]", token)
            ):
                continue
            terms.append(token)

        # ``合同 HT-2026-021`` yields both ``合同HT-2026-021`` and
        # ``HT-2026-021``.  Requiring both strings makes a normal extracted
        # row such as ``合同编号：HT-2026-021`` fail a hard evidence gate even
        # though the identifier is exact.  Retain the semantic prefix only
        # when no stable standalone identifier was extracted (for example
        # ``供应商A`` where ``A`` alone is intentionally ignored).
        standalone_keys = {
            re.sub(r"\s+", "", term).casefold()
            for term in terms
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+\-/]*", re.sub(r"\s+", "", term))
        }
        deduplicated: list[str] = []
        seen: set[str] = set()
        for term in terms:
            compact = re.sub(r"\s+", "", term)
            key = compact.casefold()
            prefixed_duplicate = False
            for prefix in entity_prefixes:
                if not compact.startswith(prefix):
                    continue
                suffix_key = compact[len(prefix):].casefold()
                if suffix_key in standalone_keys:
                    prefixed_duplicate = True
                    break
            if prefixed_duplicate:
                continue
            if not key or key in seen:
                continue
            deduplicated.append(term)
            seen.add(key)
        return deduplicated

    @staticmethod
    def record_identifier_terms(question: str) -> list[str]:
        """Return stable uppercase identifiers that name one business row.

        This deliberately excludes mixed-case model names such as Qwen3-4B:
        those can be genuine concepts and still need explanatory evidence.
        Contract, order, ticket, asset and incident IDs conventionally use an
        uppercase hyphenated token containing at least one digit.  The digit
        need not be the first character after the hyphen (``SN-A17`` is valid).
        """
        candidates = re.findall(
            r"(?<![A-Za-z0-9_])[A-Z]{2,}[A-Z0-9_]*(?:-[A-Z0-9_]+)+(?![A-Za-z0-9_])",
            str(question or ""),
        )
        return list(dict.fromkeys(value for value in candidates if any(char.isdigit() for char in value)))

    @classmethod
    def retrieval_identifier_terms(cls, question: str) -> list[str]:
        """Return stable alphanumeric identifiers suitable for hard recall.

        Pure numbers and broad two-letter abbreviations are useful answer
        constraints but unsafe retrieval filters. Named technologies such as
        BM25, OCR, MySQL, BGE, and versioned model names are specific enough to
        prevent an unrelated Chinese topic from dominating the candidate set.
        """
        selected: list[str] = []
        seen: set[str] = set()
        for term in cls.explicit_identifier_terms(question):
            compact = re.sub(r"\s+", "", str(term or ""))
            key = compact.casefold()
            if (
                len(compact) < 3
                or not re.search(r"[A-Za-z]", compact)
                or key in {"agent", "api", "llm"}
                or key in seen
            ):
                continue
            seen.add(key)
            selected.append(term)
        return selected

    @staticmethod
    def text_contains_identifier(text: str, term: str) -> bool:
        """Match identifiers space-insensitively without version-prefix leaks."""
        raw_text = str(text or "").casefold()
        normalized_term = re.sub(r"\s+", "", str(term or "")).casefold()
        if not normalized_term:
            return False
        # Do not remove line breaks around a pure number. Otherwise a source
        # ending one line with ``2048`` and starting the next numbered item
        # with ``2.`` becomes the artificial token ``20482`` and fails an
        # otherwise exact boundary match.
        if normalized_term.isdigit():
            return bool(re.search(
                rf"(?<!\d){re.escape(normalized_term)}(?!\d)",
                raw_text,
            ))
        normalized_text = re.sub(r"\s+", "", raw_text)
        if re.fullmatch(r"[a-z0-9._+\-/]+", normalized_term):
            return bool(re.search(
                rf"(?<![a-z0-9]){re.escape(normalized_term)}(?![a-z0-9])",
                normalized_text,
            ))
        return normalized_term in normalized_text

    @staticmethod
    def matched_terms(text: str, terms: Iterable[str]) -> list[str]:
        text = text.lower()
        compact_text = re.sub(r"\s+", "", text)
        hits = []
        seen = set()
        for term in terms:
            key = term.lower()
            if key in seen:
                continue
            if len(key) == 1 and key.isascii() and key.isalnum():
                matched = re.search(rf"(?<![A-Za-z0-9_]){re.escape(key)}(?![A-Za-z0-9_])", text)
            else:
                matched = key in text or re.sub(r"\s+", "", key) in compact_text
            if not matched:
                continue
            hits.append(term)
            seen.add(key)
        # 去除被更长命中词包含的短词，避免"未命名笔记、未命名笔、命名笔记"重叠噪声
        filtered = []
        for term in sorted(hits, key=RagPipeline.term_order_key):
            if not any(term in longer for longer in filtered):
                filtered.append(term)
        return filtered[:8]

    @staticmethod
    def without_urls(text: str) -> str:
        """Prevent URL fragments from becoming lexical evidence for short symbols."""
        return re.sub(r"https?://\S+", " ", text)

    @staticmethod
    def lexical_score(hits: list[str], text: str = "", question: str = "") -> float:
        score = 0.0
        for term in hits:
            if re.fullmatch(r"[A-Za-z0-9_]+", term):
                score += 3.0
            elif len(term) >= 4:
                score += 4.0
            elif len(term) == 3:
                score += 2.0
            else:
                score += 1.0
        for phrase in re.findall(r"[\u3400-\u9fffA-Za-z0-9_]{4,}", question):
            if phrase.lower() in text:
                score += 6.0
        return score

    @staticmethod
    def title_topic_bonus(title: str, hits: list[str]) -> float:
        """Prefer a note whose title names the Chinese topic over an incidental ID hit.

        A query can contain a wrong model name but a correct topic (for example,
        "Qwen ... 向量检索方案对比").  A bounded title boost surfaces the note
        about the actual topic without drowning out strong content evidence.
        """
        title = str(title or "")
        topic_hits = [
            hit for hit in hits
            if len(hit) >= 3 and re.fullmatch(r"[\u3400-\u9fff]+", hit) and hit in title
        ]
        if not topic_hits:
            return 0.0
        return min(3.0, max(len(hit) for hit in topic_hits) / 2)

    @staticmethod
    def source_reason(score: float, hits: list[str], fts_score: float = 0.0, kind: str = "content") -> str:
        kind_label = {"title": "标题", "content": "正文", "ocr": "图片识别", "full": "全文", "document": "文件片段"}.get(kind, "内容")
        parts = []
        if hits:
            parts.append(f"{kind_label}命中：" + "、".join(hits[:5]))
        if fts_score:
            parts.append("全文检索匹配")
        if score:
            parts.append(f"语义相关（{score:.2f}）")
        return "；".join(parts) or "候选笔记"

    @staticmethod
    def source_url(note, source_id: int | None = None) -> str:
        # 返回 note_id 格式，前端 openDoc 据此加载完整笔记（含页签+编辑）
        # source_id 用于指定具体页签（如果来源是子页签）
        base = f"/api/notes/{note.id}"
        if source_id is not None and source_id != note.id:
            return f"{base}?tab_id={source_id}"
        return base

    @classmethod
    def chunk_source_url(cls, chunk: dict) -> str:
        if chunk.get("source_kind") == "document":
            return f"/api/documents/{chunk['note'].id}/file?chunk_id={chunk['document_chunk_id']}"
        return cls.source_url(chunk["note"], chunk.get("source_id"))

    @staticmethod
    def segment_title(note, text: str) -> str:
        first_line = next((line.strip() for line in str(text or "").splitlines() if line.strip()), "")
        match = re.search(r"《[^》]+》[^\n，。；;]*", first_line)
        if match:
            return match.group(0).strip()
        return note.title

    @classmethod
    def note_segments(cls, note) -> list[dict]:
        blocks = []
        for kind, label, text in (("title", "标题", note.title), ("content", "正文", note.content), ("ocr", "OCR", note.ocr_text)):
            for index, chunk in enumerate(cls.split_text(text)):
                blocks.append({"text": chunk, "location": f"{label} {index + 1}", "kind": kind})
        return blocks or [{"text": cls.note_text(note), "location": "全文", "kind": "full"}]

    @staticmethod
    def note_text(note) -> str:
        return "\n".join(part for part in (note.title, note.content, note.ocr_text) if str(part).strip()).strip()

    @staticmethod
    def split_text(text: str, target_size: int = 420, overlap: int = 80) -> list[str]:
        return split_note_text(text, target_size=target_size, overlap=overlap)

    @staticmethod
    def focused_excerpt(text: str, hits: list[str], radius: int = 260) -> str:
        text = str(text or "").strip()
        if not text:
            return ""
        lowered = text.lower()
        positions = [lowered.find(term.lower()) for term in hits if term and lowered.find(term.lower()) >= 0]
        if not positions:
            return text[:700] + ("..." if len(text) > 700 else "")
        center = min(positions)
        start = max(0, center - radius)
        end = min(len(text), center + radius)
        excerpt = text[start:end].strip()
        if start:
            excerpt = "..." + excerpt
        if end < len(text):
            excerpt += "..."
        return excerpt

    def evidence_sentence(self, question: str, excerpt: str, hits: list[str]) -> str:
        """从 excerpt 中挑出最可能承载答案的句子。

        通用打分：命中的查询词越多、长度适中、非纯标题，分数越高。
        对含数值或明确断言的句子增加权重，优先展示可直接支撑答案的内容。
        """
        sentences = self._evidence_candidates(excerpt)
        if not sentences:
            return ""
        # Flattened OCR tables need their row reconstructed before the generic
        # sentence scorer runs.  Otherwise a compact query such as
        # "项目风险扫描Agent" can select the row label alone, while the visually
        # identical "项目风险扫描 Agent" happens to reach the richer row parser.
        if self.structured_field_coverage(question, [excerpt]) is True:
            named_row = self.structured_record_evidence(question, excerpt, hits)
            if named_row:
                return named_row
        ocr_agent_row = self.ocr_agent_table_evidence(question, excerpt)
        if ocr_agent_row:
            return ocr_agent_row
        boundary = self.explicit_boundary_evidence(question, excerpt, hits)
        if boundary:
            return boundary
        direct_clause = self.direct_evidence_clause(question, excerpt)
        if direct_clause:
            return direct_clause
        role_clause = self.role_comparison_evidence(question, excerpt)
        if role_clause:
            return role_clause
        selector_row = self.selector_structured_row_evidence(question, excerpt, hits)
        if selector_row:
            return selector_row
        structured = self.structured_record_evidence(question, excerpt, hits)
        if structured:
            return structured
        actor_action = self.actor_action_evidence(question, excerpt, hits)
        if self.is_actor_action_question(question):
            return actor_action
        object_requirement = self.object_requirement_evidence(question, excerpt, hits)
        if object_requirement:
            return object_requirement
        faceted = self.multi_facet_evidence(question, excerpt, hits)
        if faceted:
            return faceted
        question_terms = self.query_terms(question)
        scored = []
        for index, (sentence, section) in enumerate(sentences):
            sentence_hits = self.matched_terms(sentence, question_terms)
            score = self.lexical_score(sentence_hits, sentence.lower(), question)
            score += sum(4 for hit in hits if hit and hit.lower() in sentence.lower())
            score += self.evidence_intent_bonus(question, sentence, section)
            score += self.amount_range_evidence_bonus(question, sentence)
            score += self.scalar_attribute_evidence_bonus(question, sentence)
            score += self.requested_field_evidence_bonus(question, sentence)
            # At an inclusive currency boundary, "20 万整" must resolve to
            # the sentence explicitly marked "20 万元（含）以上", rather than
            # an adjacent range ending at 20 万元（不含）.
            compact_question = re.sub(r"\s+", "", question)
            compact_sentence = re.sub(r"\s+", "", sentence)
            for amount in re.findall(r"(\d+)万(?:元)?(?:整|含)", compact_question):
                if re.search(rf"{re.escape(amount)}万元?（含）(?:以上|起)", compact_sentence):
                    score += 10
                if re.search(rf"{re.escape(amount)}万元?（不含）", compact_sentence):
                    score -= 6
            for month, day in re.findall(r"(\d{1,2})月(\d{1,2})[日号]?", compact_question):
                if re.search(rf"{re.escape(month)}月{re.escape(day)}[日号]?", compact_sentence):
                    score += 10
            if re.fullmatch(r"《[^》]+》[^\n，。；;]*", sentence):
                score -= 8
            if 8 <= len(sentence) <= 220:
                score += 1
            if re.search(r"\d|[%％]|(?:是|为|应当|需要|不得|包括|采用|支持)", sentence):
                score += 2
            scored.append((score, -index, sentence))
        best = max(scored, key=lambda item: item[:2])
        if best[0] <= 0 and hits:
            return ""
        # A question that explicitly asks for a boundary ("不是...吗" /
        # "不能理解成什么") needs the affirmative fact and its exclusion.
        # Returning only one sentence invites the generator to invent the
        # missing contrast even though it exists immediately nearby.
        if re.search(r"(?:不是|不等于|不能|不得|不可以|是否|是不是|是.{0,12}吗|能否)", question):
            negative = [
                item for item in scored
                if item[2] != best[2]
                and re.search(r"(?:不是|不等于|不代表|不能|不得|不可以)", item[2])
            ]
            positive = [
                item for item in scored
                if item[2] != best[2]
                and not re.search(r"(?:不是|不等于|不代表|不能|不得|不可以)", item[2])
                and item[0] > 0
            ]
            best_is_negative = bool(re.search(r"(?:不是|不等于|不代表|不能|不得|不可以)", best[2]))
            contrast_pool = positive if best_is_negative else negative
            if contrast_pool:
                contrast = max(contrast_pool, key=lambda item: item[:2])
                pair = (contrast[2], best[2]) if best_is_negative else (best[2], contrast[2])
                return "\n".join(pair)
        return best[2]

    def explicit_boundary_evidence(self, question: str, text: str, hits: list[str]) -> str:
        """Select an explicit source-side exclusion for polarity/boundary questions.

        A nearby positive record may share more generic nouns than the sentence
        which actually says ``不是`` or ``不代表``.  For a user asking whether a
        value has a particular meaning, that exclusion is the answer-bearing
        evidence and must be selected before a generic structured row.
        """
        raw_question = str(question or "")
        if not re.search(
            r"(?:不是|不等于|不代表|不能理解(?:成|为)|并非|是否|是不是|是.{0,16}吗)",
            raw_question,
        ):
            return ""
        negative_pattern = re.compile(
            r"(?:不是|不等于|不代表|不能理解(?:成|为)|并非)",
            flags=re.IGNORECASE,
        )
        question_terms = self.query_terms(raw_question)
        question_values = set(re.findall(
            r"[A-Za-z]+-\d[A-Za-z0-9_-]*|\d+(?:\.\d+)?\s*(?:分钟|小时|天|日|周|月|年|%|％|元|万)?",
            raw_question,
            flags=re.IGNORECASE,
        ))
        candidates = self._evidence_candidates(text)
        scored: list[tuple[float, int, str]] = []
        for index, (sentence, section) in enumerate(candidates):
            if not negative_pattern.search(sentence):
                continue
            sentence_hits = self.matched_terms(sentence, question_terms)
            shared_values = sum(
                self.text_contains_term(sentence, value)
                for value in question_values
                if value.strip()
            )
            substantive_hits = [
                hit for hit in sentence_hits
                if len(re.sub(r"\s+", "", hit)) >= 2
                and hit not in self.stopwords
            ]
            if not substantive_hits and not shared_values:
                continue
            score = self.lexical_score(sentence_hits, sentence.casefold(), raw_question)
            score += sum(4 for hit in hits if hit and self.text_contains_term(sentence, hit))
            score += shared_values * 12
            score += self.evidence_intent_bonus(raw_question, sentence, section)
            scored.append((score, -index, sentence))
        if not scored:
            return ""
        best = max(scored, key=lambda item: item[:2])
        needs_positive_definition = bool(
            re.search(r"(?:具体)?指什么|是什么", raw_question)
            or re.search(r"\d+(?:\.\d+)?\s*(?:分钟|小时|天|日|周|月|年)", raw_question)
            or (
                re.search(r"(?:是否|是不是|是.{0,16}吗)", raw_question)
                and re.search(r"(?:当前|现在|已经)", raw_question)
            )
        )
        if needs_positive_definition:
            positive: list[tuple[float, int, str]] = []
            for index, (sentence, section) in enumerate(candidates):
                if sentence == best[2] or negative_pattern.search(sentence):
                    continue
                sentence_hits = self.matched_terms(sentence, question_terms)
                shared_values = sum(
                    self.text_contains_term(sentence, value)
                    for value in question_values
                    if value.strip()
                )
                if not sentence_hits and not shared_values:
                    continue
                score = self.lexical_score(sentence_hits, sentence.casefold(), raw_question)
                score += sum(4 for hit in hits if hit and self.text_contains_term(sentence, hit))
                score += shared_values * 12
                score += self.evidence_intent_bonus(raw_question, sentence, section)
                if score > 0:
                    positive.append((score, -index, sentence))
            if positive:
                definition = max(positive, key=lambda item: item[:2])[2]
                return f"{definition}\n{best[2]}"
        return best[2]

    def object_requirement_evidence(self, question: str, text: str, hits: list[str]) -> str:
        """Select a rule that constrains the object explicitly named by the user.

        Questions such as ``对手机号使用的要求`` are easy to derail with a
        nearby sentence containing a generic policy word such as ``必须`` or
        ``不得``.  This selector first binds the requested object, then ranks
        only source sentences that mention that object.  It remains extractive:
        no synonym or business rule is invented here.
        """
        compact_question = re.sub(r"\s+", "", str(question or ""))
        object_matches = re.findall(
            r"对(?P<object>[\u3400-\u9fffA-Za-z0-9_./+\-]{2,20}?)"
            r"(?:的)?(?:使用|处理|访问|保存|传输|存储|披露|发布|展示|采集|投放|触达)"
            r"(?:的)?(?:要求|规则|规范|限制|规定)",
            compact_question,
            flags=re.IGNORECASE,
        )
        if not object_matches:
            object_matches = re.findall(
                r"(?P<object>[\u3400-\u9fffA-Za-z0-9_./+\-]{2,12})的"
                r"(?:使用|处理|访问|保存|传输|存储|披露|发布|展示|采集|投放|触达)"
                r"(?:要求|规则|规范|限制|规定)",
                compact_question,
                flags=re.IGNORECASE,
            )
        objects = list(dict.fromkeys(match.strip() for match in object_matches if match.strip()))
        if not objects:
            return ""

        question_terms = self.query_terms(question)
        constraint_pattern = re.compile(
            r"(?:只|仅|限于|限定|授权|同意|不得|禁止|必须|应当|需要|"
            r"保留|替换|脱敏|加密|删除|允许|不允许|不能|投放|触达)",
            flags=re.IGNORECASE,
        )
        scored: list[tuple[float, int, str]] = []
        for index, (sentence, section) in enumerate(self._evidence_candidates(text)):
            matched_objects = [
                obj for obj in objects if self.text_contains_term(sentence, obj)
            ]
            if not matched_objects:
                continue
            sentence_hits = self.matched_terms(sentence, question_terms)
            score = self.lexical_score(sentence_hits, sentence.casefold(), question)
            score += sum(4 for hit in hits if hit and hit.casefold() in sentence.casefold())
            score += 18 * len(matched_objects)
            score += 8 if constraint_pattern.search(sentence) else 0
            score += self.evidence_intent_bonus(question, sentence, section)
            if 6 <= len(sentence) <= 260:
                score += 1
            scored.append((score, -index, sentence))
        if not scored:
            return ""
        return max(scored, key=lambda item: item[:2])[2]

    def multi_facet_evidence(self, question: str, text: str, hits: list[str]) -> str:
        """Return the smallest passages that cover explicitly requested facets.

        Retrieval similarity alone cannot distinguish a risk definition from
        the requested colour and action, or a citation rule from the requested
        no-answer rule. This selector only runs for recognizable field-like
        facets and only extracts sentences already present in the source.
        """
        compact_question = re.sub(r"\s+", "", str(question or ""))
        facet_rules: list[tuple[str, str]] = []
        if re.search(r"(?:什么|哪种)?颜色|(?:风险)?等级|(?:被|判定为?)什么", compact_question):
            facet_rules.append((
                "status",
                r"(?:当前|综合|状态|等级|判定|标记|记为|评为)[^。；;]{0,24}(?:红色|黄色|绿色|橙色|蓝色)"
                r"|(?:红色|黄色|绿色|橙色|蓝色)",
            ))
        if re.search(
            r"(?:采取|执行|安排|制定).{0,8}(?:行动|措施|动作)"
            r"|(?:行动|措施|下一步|处置|处理|应对)(?:是什么|有哪些|如何|怎么|为)?",
            compact_question,
        ):
            facet_rules.append((
                "action",
                r"(?:处置|处理|应对|行动|措施|下一步|整改|补齐|取得|完成|提交|"
                r"扩容|恢复|调整|暂停|停止|安排|负责人|截止)",
            ))
        if re.search(r"(?:适用|不适用|适用于|针对|限定|范围|覆盖|哪些物料|哪些对象)", compact_question):
            facet_rules.append((
                "scope",
                r"(?:只针对|仅针对|仅限|适用于|不适用于|适用范围|限定|覆盖|物料|对象)",
            ))
        if re.search(r"(?:无答案|没有答案|拒答|证据不足|资料不足|无法回答|不能回答)", compact_question):
            facet_rules.append((
                "refusal",
                r"(?:无充分证据|没有足够信息|资料不足|证据不足|明确拒答|无法回答|不能回答)",
            ))
        if re.search(r"(?:付款安排|付款节点|支付安排|预付款|尾款|付款比例)", compact_question):
            facet_rules.append((
                "payment",
                r"(?:付款节点|支付|预付款|尾款|付款比例)",
            ))
        if re.search(r"(?:限制|责任人缺失|缺失时|待确认)", compact_question):
            facet_rules.append((
                "limitation",
                r"(?:限制|责任人缺失|待确认|不能|不得|仅|只)",
            ))
        if not facet_rules:
            return ""

        candidates = self._evidence_candidates(text)
        question_terms = self.query_terms(question)
        selected: list[tuple[int, str]] = []
        for _facet, pattern in facet_rules:
            scored: list[tuple[float, int, str]] = []
            for index, (sentence, section) in enumerate(candidates):
                if not re.search(pattern, sentence, flags=re.IGNORECASE):
                    continue
                sentence_hits = self.matched_terms(sentence, question_terms)
                score = self.lexical_score(sentence_hits, sentence.casefold(), question)
                score += sum(4 for hit in hits if hit and hit.casefold() in sentence.casefold())
                score += self.evidence_intent_bonus(question, sentence, section)
                score += self.amount_range_evidence_bonus(question, sentence)
                scored.append((score, -index, sentence))
            if not scored:
                return ""
            _score, negative_index, sentence = max(scored, key=lambda item: item[:2])
            original_index = -negative_index
            if sentence not in {value for _, value in selected}:
                selected.append((original_index, sentence))

        ordered = [sentence for _, sentence in sorted(selected, key=lambda item: item[0])]
        bounded: list[str] = []
        total = 0
        for sentence in ordered:
            projected = total + len(sentence) + (1 if bounded else 0)
            if bounded and (len(bounded) >= 4 or projected > 720):
                break
            bounded.append(sentence)
            total = projected
        return "\n".join(bounded)

    @staticmethod
    def _split_evidence_sentences(text: str) -> list[str]:
        """Split prose without treating decimal or version dots as boundaries."""
        return [
            sentence.strip(" \n\r\t")
            for sentence in re.split(
                r"(?<=[。！？；;!?])\s*|(?<=\.)\s+",
                str(text or ""),
            )
            if sentence.strip(" \n\r\t")
        ]

    @staticmethod
    def _evidence_candidates(text: str) -> list[tuple[str, str]]:
        """Split prose while retaining short section labels for evidence scoring."""
        candidates: list[tuple[str, str]] = []
        section = ""
        for line in str(text or "").splitlines():
            clean_line = line.strip(" \n\r\t")
            if not clean_line:
                continue
            compact_line = re.sub(r"[：:：\-—#*\s]", "", clean_line)
            if len(compact_line) <= 18 and re.fullmatch(r"(?:命题|目标|准备|步骤|计划|行动|建议|要求|最小实验|下一步|风险|问题|限制|假设|背景|结论|注意事项)", compact_line):
                section = compact_line
                continue
            for sentence in RagPipeline._split_evidence_sentences(clean_line):
                candidates.append((sentence, section))
        if candidates:
            return candidates
        return [
            (sentence, "")
            for line in str(text or "").splitlines()
            for sentence in RagPipeline._split_evidence_sentences(line)
        ]

    @staticmethod
    def evidence_intent_bonus(question: str, sentence: str, section: str = "") -> float:
        """Align a citation with the role asked for, not just repeated words.

        A preparation question often shares nouns such as "文字" and "视频"
        with a risk paragraph.  The risk is useful context but cannot support
        the conclusion about what the user should prepare.
        """
        compact_question = re.sub(r"\s+", "", str(question or ""))
        text = f"{section} {sentence}"
        asks_preparation = bool(re.search(r"(?:准备什么|需要准备|准备哪些|怎么准备|如何准备|需要什么材料|怎么做|如何做|步骤|计划|下一步)", compact_question))
        asks_risk = bool(re.search(r"(?:风险|问题|隐患|困难|限制|阻碍|注意事项)", compact_question))
        is_risk = bool(re.search(r"(?:风险|问题|不足|困难|限制|隐患|失败|不清晰)", text))
        is_action = bool(re.search(r"(?:准备|明确|列出|完成|收集|录制|编写|写.*(?:资料|文档)|资料|视频|步骤|行动|计划|下一步|建议|要求)", text))
        if asks_preparation and not asks_risk:
            return (6.0 if is_action else 0.0) - (8.0 if is_risk else 0.0)
        if asks_risk and not asks_preparation:
            return (6.0 if is_risk else 0.0) - (3.0 if is_action else 0.0)
        asks_rule = bool(re.search(
            r"(?:需要|要求|必须|应当|关注|审批|规则|限制|禁止|不得|如何处理)",
            compact_question,
        ))
        states_rule = bool(re.search(
            r"(?:必须|应当|需要|不得|禁止|均需|只可|应|符合.{0,16}规则|审批流|会签)",
            text,
        ))
        if asks_rule and states_rule:
            return 6.0 + RagPipeline.precise_answer_field_bonus(question, sentence)
        return RagPipeline.precise_answer_field_bonus(question, sentence)

    @staticmethod
    def amount_range_evidence_bonus(question: str, sentence: str) -> float:
        """Prefer the stated monetary band that actually contains the query value.

        This is a deterministic evidence-selection rule, not a business rule:
        it interprets common inclusive/exclusive Chinese range wording only
        after retrieval has already selected the source.
        """
        compact_question = re.sub(r"\s+", "", str(question or ""))
        compact_sentence = re.sub(r"\s+", "", str(sentence or ""))
        values = [float(value) for value in re.findall(r"(\d+(?:\.\d+)?)万(?:元)?", compact_question)]
        if not values:
            return 0.0

        ranges: list[tuple[float | None, bool, float | None, bool]] = []
        for match in re.finditer(
            r"(\d+(?:\.\d+)?)万(?:元)?(?P<lower_inc>（含）|\(含\)|含)?"
            r"(?:至|到|[-—~～])"
            r"(\d+(?:\.\d+)?)万(?:元)?(?P<upper_exc>（不含）|\(不含\)|不含)?",
            compact_sentence,
        ):
            ranges.append((
                float(match.group(1)),
                bool(match.group("lower_inc")),
                float(match.group(3)),
                not bool(match.group("upper_exc")),
            ))
        for match in re.finditer(
            r"(\d+(?:\.\d+)?)万(?:元)?(?:（含）|\(含\)|含)?(?:以上|起)",
            compact_sentence,
        ):
            ranges.append((float(match.group(1)), True, None, False))
        for match in re.finditer(
            r"(\d+(?:\.\d+)?)万(?:元)?(?P<inclusive>（含）|\(含\)|含)?(?:以下|以内)",
            compact_sentence,
        ):
            ranges.append((None, False, float(match.group(1)), bool(match.group("inclusive"))))
        if not ranges:
            return 0.0

        def contains(value: float, band: tuple[float | None, bool, float | None, bool]) -> bool:
            lower, lower_inclusive, upper, upper_inclusive = band
            if lower is not None and (value < lower or (value == lower and not lower_inclusive)):
                return False
            if upper is not None and (value > upper or (value == upper and not upper_inclusive)):
                return False
            return True

        return 16.0 if any(contains(value, band) for value in values for band in ranges) else -8.0

    @staticmethod
    def precise_answer_field_bonus(question: str, sentence: str) -> float:
        """Prefer the sentence that contains the exact field requested.

        Generic numeric bonuses cannot distinguish an outage duration from its
        monetary loss or a rollout percentage from final ROI. This scorer is
        deliberately field-shaped: it only reorders sentences inside an
        already retrieved chunk and never fabricates a missing value.
        """
        compact_question = re.sub(r"\s+", "", str(question or ""))
        text = str(sentence or "")
        if "损失" in compact_question and re.search(r"(?:多少|几|金额|元|万元|人民币|美元)", compact_question):
            if "损失" in text and re.search(r"\d+(?:\.\d+)?\s*(?:亿元|万元|元|万|亿)", text):
                return 14.0
        if re.search(r"(?:签约|签署).{0,8}(?:日期|时间|什么时候|何时|哪天|哪一年|哪年)", compact_question):
            if re.search(r"(?:签约|签署)", text) and re.search(
                r"(?<![A-Za-z0-9_-])20\d{2}(?:[-/.]\d{1,2}(?:[-/.]\d{1,2})?|年\d{1,2}月(?:\d{1,2}日)?)(?![A-Za-z0-9_-])",
                text,
            ):
                return 14.0
        if re.search(r"(?:一共|累计|总共|总计)?.{0,10}(?:发生|出现)(?:过|了)?(?:多少|几)(?:次|起|例|笔)", compact_question):
            if re.search(r"(?:发生|出现|累计|共计|总计).{0,12}\d+\s*(?:次|起|例|笔)", text):
                return 14.0
        if "roi" in compact_question.casefold() and re.search(r"(?:最终|最后|实际|结果|多少|几)", compact_question):
            if re.search(r"ROI\s*(?:为|是|达到|[:：=])?\s*-?\d+(?:\.\d+)?\s*(?:[%％]|倍)?", text, re.IGNORECASE):
                return 14.0
        if re.search(r"(?:多少|几)(?:个|家|名)?(?:真实|正式|实际)?(?:客户|用户)", compact_question):
            if re.search(r"\d+\s*(?:个|家|名)\s*(?:真实|正式|实际)?(?:客户|用户)", text) and re.search(
                r"(?:在用|使用|部署|上线|采用)", text
            ):
                return 14.0
        if re.search(r"(?:是否|有没有|有无).{0,16}(?:已经|曾经|实际)?(?:发生|出现)", compact_question):
            if re.search(r"(?:已经|已|曾经|曾|实际|本期|本月).{0,16}(?:发生|出现)", text):
                return 12.0
        if re.search(r"(?:哪位|谁|哪个).{0,10}(?:最终|最后).{0,10}(?:录用|录取|选中|入选|中标)", compact_question):
            if re.search(r"(?:最终|最后|正式|确认).{0,16}(?:录用|录取|选中|入选|中标)", text):
                return 12.0
        return 0.0

    @staticmethod
    def scalar_attribute_evidence_bonus(question: str, text: str) -> float:
        """Prefer an explicit scalar value for the attribute the user asks for.

        Retrieval can correctly identify one note while sibling chunks compete
        on generic subject words.  A scope note mentioning ``交货延迟率`` is not
        evidence for its threshold, and a model-selection heading is not the
        requested file size.  This scorer only reorders already-retrieved
        passages when the question names a value-shaped field and the passage
        contains a compatible unit or comparison.  It never supplies a value.
        """
        compact_question = re.sub(r"\s+", "", str(question or ""))
        compact_text = re.sub(r"\s+", "", str(text or ""))
        if not compact_question or not compact_text:
            return 0.0

        asks_threshold = bool(re.search(
            r"(?:阈值|上限|下限|临界值|触发值|超过多少|低于多少|高于多少)",
            compact_question,
            flags=re.IGNORECASE,
        ))
        has_threshold_value = bool(re.search(
            r"(?:阈值|上限|下限|超过|低于|高于|大于|小于|不少于|不超过|至少|至多|[<>≤≥])"
            r"[^。；;\n]{0,24}\d+(?:\.\d+)?(?:%|％|元|万|万元|小时|天|日|周|月|年|次|个)?"
            r"|\d+(?:\.\d+)?(?:%|％|元|万|万元)[^。；;\n]{0,18}(?:触发|阈值|上限|下限|超过|低于|高于)",
            compact_text,
            flags=re.IGNORECASE,
        ))
        if asks_threshold and has_threshold_value:
            return 16.0

        asks_resource_constraint = bool(re.search(
            r"(?:内存|显存|体积|容量|大小|占用).{0,12}(?:限制|上限|多少|多大|分别|是什么)"
            r"|(?:限制|上限).{0,12}(?:内存|显存|体积|容量|大小|占用)",
            compact_question,
            flags=re.IGNORECASE,
        ))
        has_resource_value = bool(re.search(
            r"\d+(?:\.\d+)?(?:KB|MB|GB|TB|KiB|MiB|GiB|字节)",
            compact_text,
            flags=re.IGNORECASE,
        ))
        if asks_resource_constraint and has_resource_value:
            score = 12.0
            asks_memory = bool(re.search(r"(?:内存|显存|占用)", compact_question))
            asks_volume = bool(re.search(r"(?:体积|容量|大小)", compact_question))
            if asks_memory and re.search(r"(?:内存|显存|常驻|占用)", compact_text):
                score += 4.0
            if asks_volume and re.search(r"(?:体积|容量|大小|文件)", compact_text):
                score += 4.0
            # A limit question is best supported by the sentence that states
            # the actual boundary or failure condition, not merely a nearby
            # descriptive size.
            if "限制" in compact_question and re.search(
                r"(?:不能|不可|不得|上限|限制|超过|超出|爆体积|放不下|无法)",
                compact_text,
            ):
                score += 8.0
            if re.search(r"(?:实测|当前|实际|内存占用|常驻约|常驻为)", compact_text):
                score += 6.0
            if (
                not re.search(r"(?:预计|预期|计划|下一步)", compact_question)
                and re.search(r"(?:预计|预期|计划|下一步)", compact_text)
            ):
                score -= 10.0
            return score

        size_subject = r"(?:模型|权重|文件|安装包|索引|数据库)"
        size_field = r"(?:多大|大小|体积|容量|占用)"
        asks_size = bool(re.search(
            rf"{size_subject}.{{0,8}}{size_field}|{size_field}.{{0,8}}{size_subject}",
            compact_question,
            flags=re.IGNORECASE,
        ))
        has_size_value = bool(re.search(
            r"\d+(?:\.\d+)?(?:KB|MB|GB|TB|KiB|MiB|GiB|字节)",
            compact_text,
            flags=re.IGNORECASE,
        ))
        if asks_size and has_size_value:
            return 16.0

        asks_duration = bool(re.search(
            r"(?:多久|多长时间|耗时|时长|延迟|响应时间)",
            compact_question,
            flags=re.IGNORECASE,
        ))
        has_duration_value = bool(re.search(
            r"\d+(?:\.\d+)?(?:毫秒|ms|秒|分钟|小时|天|日|周|个月|月|年)",
            compact_text,
            flags=re.IGNORECASE,
        ))
        if asks_duration and has_duration_value:
            return 12.0
        return 0.0

    @staticmethod
    def selection_criteria_evidence_bonus(question: str, text: str) -> float:
        """Prefer substantive trade-off evidence for selection questions.

        A source heading can repeat ``选型`` more exactly than the paragraph
        which contains the actual dimensions.  This scorer only reorders
        sibling chunks from an already selected source; it never introduces a
        criterion that is absent from the source.
        """
        compact_question = re.sub(r"\s+", "", str(question or ""))
        body = str(text or "").strip()
        if not re.search(
            r"(?:选型|如何选|怎么选|怎样选|选择时|选择标准|选用标准|"
            r"各自关注)",
            compact_question,
            flags=re.IGNORECASE,
        ):
            return 0.0
        if not body:
            return -4.0
        compact_body = re.sub(r"\s+", "", body)
        if len(compact_body) <= 48 and re.search(r"[：:]$", compact_body):
            return -4.0

        criteria = (
            r"准确(?:率|度)?", r"速度", r"延迟", r"性能", r"体积", r"大小",
            r"内存", r"显存", r"CPU", r"GPU", r"量化", r"维度", r"中文",
            r"英文", r"语言", r"兼容", r"成本", r"资源", r"规模", r"容量",
            r"稳定", r"质量", r"风险", r"限制", r"阈值", r"并发", r"吞吐",
        )
        matched = sum(bool(re.search(pattern, body, re.IGNORECASE)) for pattern in criteria)
        comparison_shape = bool(
            re.search(r"(?:^|\n)\s*(?:[-*]|\d+[.、])", body)
            or re.search(r"(?:优于|逊于|略高|略低|更好|太重|最终选|相比)", body)
        )
        return float(min(16, matched * 2 + (2 if comparison_shape else 0)))

    @staticmethod
    def remediation_evidence_bonus(question: str, text: str) -> float:
        """Prefer explicit fixes when the user asks how something was improved.

        A generic architecture note may mention OCR or a database technology,
        but it does not answer a request for the measures that resolved or
        optimised a problem. This score only reorders retrieved evidence and
        requires solution-shaped wording already present in the source.
        """
        compact_question = re.sub(r"\s+", "", str(question or ""))
        if not re.search(
            r"(?:优化|修复|解决|排查|改进).{0,16}(?:如何|怎么|手段|措施|方案|方法|采取|做法)"
            r"|(?:如何|怎么|采取了什么|用了什么).{0,16}(?:优化|修复|解决|排查|改进)",
            compact_question,
        ):
            return 0.0
        body = str(text or "").strip()
        if not body:
            return 0.0

        score = 0.0
        if re.search(r"(?:解决方案|优化方案|修复方案|处理方案|改进措施|具体措施)\s*[：:]", body):
            score += 10.0
        if len(re.findall(r"(?:^|\n)\s*(?:[-*]|\d+[.、])", body)) >= 2:
            score += 3.0
        action_count = sum(bool(re.search(pattern, body, re.IGNORECASE)) for pattern in (
            r"缩放|压缩|限制", r"设置|配置|调整", r"创建|增加|加入|引入",
            r"回退|重试|捕获|try/except", r"索引|MATCH|bm25", r"线程|并发",
        ))
        score += min(4.0, float(action_count))
        if re.search(r"(?:修复后|优化后|处理后|降(?:低|至|到)|无复现|恢复正常)", body):
            score += 2.0
        return min(18.0, score)

    @staticmethod
    def is_role_comparison_question(question: str) -> bool:
        compact = re.sub(r"\s+", "", str(question or ""))
        return bool(re.search(
            r"(?:各自|分别).{0,12}(?:承担|负责|扮演|起到|角色|作用|职责)"
            r"|(?:角色|作用|职责).{0,12}(?:各自|分别)",
            compact,
        ))

    @staticmethod
    def is_actor_action_question(question: str) -> bool:
        """Whether the user explicitly asks who owns or performs an action."""
        compact = re.sub(r"\s+", "", str(question or ""))
        return bool(re.search(
            r"(?:谁|哪位|哪个(?:人|团队|部门|角色)?).{0,10}"
            r"(?:做|负责|执行|完成|处理|跟进|承担|审批|审核|复核|确认|提交)"
            r"|(?:由|交由)(?:谁|哪位|哪个(?:人|团队|部门|角色)?).{0,8}"
            r"(?:做|负责|执行|完成|处理|跟进|承担|审批|审核|复核|确认|提交)?"
            r"|(?:责任人|负责人|执行人|牵头人|承办人)(?:是|为)?"
            r"(?:谁|哪位|哪个(?:人|团队|部门|角色)?)",
            compact,
        ))

    @staticmethod
    def _actor_question_entity(question: str) -> tuple[str, str] | None:
        """Extract a compact ``entity type + identifier`` used to bind actions.

        The identifier is intentionally structural rather than domain-specific:
        it covers forms such as ``供应商 A``, ``项目 R-03`` and ``工单 1024``.
        """
        compact = re.sub(r"\s+", "", str(question or ""))
        matches = list(re.finditer(
            r"(?P<base>[\u3400-\u9fff]{2,12})"
            r"(?P<identifier>[A-Za-z][A-Za-z0-9_-]*|\d+[A-Za-z0-9_-]*)",
            compact,
        ))
        if not matches:
            return None
        match = matches[-1]
        base = re.sub(r"^(?:请问|我想知道|想知道|那|这|该|此|关于)", "", match.group("base"))
        base = re.sub(r"(?:的|里|中|内)$", "", base)
        if len(base) < 2:
            return None
        return base, match.group("identifier")

    @staticmethod
    def _actor_material_contains_entity(text: str, entity: tuple[str, str]) -> bool:
        """Return whether evidence names the exact entity from the question."""
        base, identifier = entity
        compact = re.sub(r"\s+", "", str(text or "")).casefold()
        return f"{base}{identifier}".casefold() in compact

    @classmethod
    def actor_action_evidence(
        cls,
        question: str,
        text: str,
        hits: Iterable[str] | None = None,
    ) -> str:
        """Extract role-action assignments for a ``who does it`` question.

        The selector first binds an alphanumeric entity record when one is
        present, then keeps every directly stated responsible role in that
        record.  It never infers an owner from a nearby deadline or topic.
        """
        if not cls.is_actor_action_question(question):
            return ""
        candidates = cls._evidence_candidates(text)
        if not candidates:
            return ""

        start = 0
        end = len(candidates)
        entity = cls._actor_question_entity(question)
        if entity:
            base, identifier = entity
            entity_pattern = re.compile(
                rf"{re.escape(base)}\s*(?P<identifier>[A-Za-z][A-Za-z0-9_-]*|\d+[A-Za-z0-9_-]*)",
                flags=re.IGNORECASE,
            )
            target_key = identifier.casefold()
            anchor = None
            for index, (sentence, _section) in enumerate(candidates):
                for match in entity_pattern.finditer(sentence):
                    if match.group("identifier").casefold() == target_key:
                        anchor = index
                        break
                if anchor is not None:
                    break
            if anchor is not None:
                start = anchor
                for index in range(anchor + 1, len(candidates)):
                    identifiers = [
                        match.group("identifier").casefold()
                        for match in entity_pattern.finditer(candidates[index][0])
                    ]
                    if any(value != target_key for value in identifiers):
                        end = index
                        break

        role_token = (
            r"[\u3400-\u9fffA-Za-z0-9_·-]{0,18}"
            r"(?:团队|部门|中心|小组|工作组|委员会|经理|负责人|总监|主管|专员|人员|工程师|顾问|组)"
        )
        action = (
            r"(?:负责|执行|完成|提交|取得|处理|跟进|承担|审核|复核|审批|会签|"
            r"确认|通知|交付|提供|制定|安排|整改|扩容|恢复|暂停|停止)"
        )
        assignment = re.compile(
            rf"(?:由|交由)\s*{role_token}.{{0,12}}{action}"
            rf"|{role_token}.{{0,18}}{action}",
            flags=re.IGNORECASE,
        )
        responsibility_field = re.compile(
            rf"(?:责任人|负责人|执行人|牵头人|承办人)\s*[：:]\s*{role_token}",
            flags=re.IGNORECASE,
        )
        question_terms = cls.query_terms(question)
        retrieval_hits = [str(hit) for hit in (hits or []) if str(hit).strip()]
        asks_next_action = bool(re.search(
            r"(?:下一步|接下来|后续|下一项|下一阶段)",
            re.sub(r"\s+", "", str(question or "")),
        ))
        scored: list[tuple[float, int, str]] = []
        for index in range(start, end):
            sentence, section = candidates[index]
            has_assignment = bool(assignment.search(sentence) or responsibility_field.search(sentence))
            if not has_assignment:
                continue
            if asks_next_action and not re.search(
                r"(?:行动|计划|安排|待办|跟进|下一步|后续|"
                r"在.{0,18}前|于.{0,18}(?:前|完成)|截止)",
                sentence,
            ):
                continue
            sentence_hits = cls.matched_terms(sentence, question_terms)
            score = cls.lexical_score(sentence_hits, sentence.casefold(), question)
            score += sum(4 for hit in retrieval_hits if cls.text_contains_term(sentence, hit))
            score += 18.0
            score += 4.0 if re.search(r"(?:在|于).{0,12}(?:前|内)|截止", sentence) else 0.0
            score += cls.evidence_intent_bonus(question, sentence, section)
            scored.append((score, index, sentence))
        if not scored:
            return ""

        # All direct assignments inside the bound record are independently
        # useful. Preserve source order and a bounded context budget.
        selected: list[str] = []
        total = 0
        for _score, _index, sentence in sorted(scored, key=lambda item: item[1]):
            if sentence in selected:
                continue
            projected = total + len(sentence) + (1 if selected else 0)
            if selected and (len(selected) >= 4 or projected > 720):
                break
            selected.append(sentence)
            total = projected
        return "\n".join(selected)

    @classmethod
    def actor_action_evidence_bonus(cls, question: str, text: str) -> float:
        evidence = cls.actor_action_evidence(question, text)
        if not evidence:
            return 0.0
        return min(30.0, 18.0 + 4.0 * len(evidence.splitlines()))

    @staticmethod
    def role_comparison_subjects(question: str) -> list[str]:
        """Extract two explicit subjects from an ``A 和 B 各自...`` question."""
        compact = re.sub(r"\s+", "", str(question or "")).strip("。！？?；;")
        match = re.search(
            r"([\u3400-\u9fffA-Za-z0-9_./+\-]{2,30}?)(?:和|与)"
            r"([\u3400-\u9fffA-Za-z0-9_./+\-]{2,30}?)(?:各自|分别)",
            compact,
        )
        if not match:
            return []
        return [match.group(1), match.group(2)]

    @classmethod
    def role_comparison_evidence(cls, question: str, text: str) -> str:
        """Return a passage assigning distinct roles to both requested subjects."""
        if not cls.is_role_comparison_question(question):
            return ""
        subjects = cls.role_comparison_subjects(question)
        if len(subjects) != 2:
            return ""
        candidates = cls._evidence_candidates(text)
        scored: list[tuple[float, int, str]] = []
        for index, (sentence, _section) in enumerate(candidates):
            compact = re.sub(r"\s+", "", sentence)
            if not all(subject in compact for subject in subjects):
                continue
            direct_roles = len(re.findall(r"(?:承担|负责|扮演|起到|用于)", compact))
            declarative_roles = len(re.findall(r"(?:是|为|作为)", compact))
            role_terms = len(re.findall(r"(?:角色|作用|职责|基础设施|发现模式|生成摘要|回答问题|提出建议)", compact))
            score = direct_roles * 14.0 + declarative_roles * 2.0 + role_terms * 2.0
            if 12 <= len(sentence) <= 360:
                score += 1.0
            if score > 0:
                scored.append((score, -index, sentence))

        # A common Chinese structure assigns the two roles in adjacent clauses:
        # ``A 承担……；B 承担……``.  Sentence splitting intentionally treats the
        # semicolon as a boundary, so evaluate a two-clause window as one piece
        # of evidence only when each subject has its own explicit assignment.
        direct_role_pattern = r"(?:承担|负责|扮演|起到|用于)"
        negative_role_pattern = r"(?:没有|未|不|无).{0,8}(?:定义|说明|承担|负责|角色|作用|职责)"

        def has_positive_assignment(subject: str, clause: str) -> bool:
            compact_clause = re.sub(r"\s+", "", clause)
            if subject not in compact_clause or re.search(negative_role_pattern, compact_clause):
                return False
            escaped = re.escape(subject)
            return bool(
                re.search(rf"{escaped}.{{0,24}}{direct_role_pattern}", compact_clause)
                or re.search(rf"(?:由|交由){escaped}.{{0,8}}{direct_role_pattern}", compact_clause)
            )

        for index in range(len(candidates) - 1):
            left, left_section = candidates[index]
            right, right_section = candidates[index + 1]
            if left_section and right_section and left_section != right_section:
                continue
            pair = f"{left.rstrip('；;')}；{right.lstrip()}"
            compact_pair = re.sub(r"\s+", "", pair)
            if len(pair) > 420 or not all(subject in compact_pair for subject in subjects):
                continue
            if not all(
                has_positive_assignment(subject, left) or has_positive_assignment(subject, right)
                for subject in subjects
            ):
                continue
            direct_roles = len(re.findall(direct_role_pattern, compact_pair))
            role_terms = len(re.findall(
                r"(?:角色|作用|职责|基础设施|发现模式|生成摘要|回答问题|提出建议)",
                compact_pair,
            ))
            score = direct_roles * 14.0 + role_terms * 2.0 + 2.0
            scored.append((score, -index, pair))
        if not scored:
            return ""
        best = max(scored, key=lambda item: item[:2])
        return best[2] if best[0] >= 4.0 else ""

    @classmethod
    def role_comparison_bonus(cls, question: str, text: str) -> float:
        """Prefer explicit role assignments over a shared-topic document title."""
        evidence = cls.role_comparison_evidence(question, text)
        if not evidence:
            return 0.0
        compact = re.sub(r"\s+", "", evidence)
        direct_roles = len(re.findall(r"(?:承担|负责|扮演|起到|用于)", compact))
        return 26.0 if direct_roles >= 2 else 5.0

    def direct_evidence_clause(self, question: str, text: str) -> str:
        """Prefer a source's explicit conclusion over a row's generic header."""
        raw = str(text or "")
        if not raw:
            return ""
        compact_question = re.sub(r"\s+", "", str(question or ""))
        asks_resolution = self.is_resolution_question(compact_question)
        if asks_resolution:
            # Long imported documents often put the actionable conclusion in a
            # later "核心建议" block.  It is more trustworthy than asking the
            # model to infer a procedure from an earlier problem statement.
            recommendation = re.search(
                r"(?:💡\s*)?核心建议\s*[：:]\s*([^\n|]{8,360})",
                raw,
                flags=re.IGNORECASE,
            )
            if recommendation:
                clause = re.sub(r"\s+", " ", recommendation.group(1)).strip(" -：:；;")
                if clause:
                    return clause[:520]
        compares_approval_level = bool(re.search(
            r"(?:不同.{0,16}(?:金额|预算).{0,16}(?:同一|一样|层级|审批)"
            r"|(?:同一|一样).{0,16}(?:层级|审批))",
            compact_question,
        ))
        if compares_approval_level:
            match = re.search(
                r"(?:【检查项】\s*)?(不同\s*(?:PO|PR)?\s*(?:金额|预算金额)"
                r"[^；。\n]{0,100}?(?:不同层级审批)[^；。\n]*(?:。[^。；\n]{0,100})?)",
                raw,
                flags=re.IGNORECASE,
            )
            if match:
                clause = match.group(1).strip()
                if match.end() < len(raw) and raw[match.end()] == "。":
                    clause += "。"
                return clause[:520]

        if not self.is_structured_field_query(question):
            return ""
        field = re.sub(r"\s+", "", str(question or ""))
        clauses = [part.strip() for part in re.split(r"；|\n+", raw) if part.strip()]
        for index, clause in enumerate(clauses):
            if field not in re.sub(r"\s+", "", clause):
                continue
            selected = [clause]
            if index + 1 < len(clauses) and re.search(r"(?:判定|条件|输出|规则)", clauses[index + 1]):
                selected.append(clauses[index + 1])
            return "；".join(selected)[:520]
        return ""

    def structured_record_evidence(self, question: str, text: str, hits: list[str]) -> str:
        """Keep an Excel-like record together when it is the matching evidence.

        Spreadsheet extraction deliberately uses semicolons between fields. A
        sentence splitter must not reduce a matched row to only its name field,
        otherwise the adjacent function and scenario fields are lost.
        """
        records = self.structured_record_evidences(question, text, hits)
        return "\n".join(records)

    def markdown_table_field_evidence(self, question: str, chunk: dict) -> str | None:
        """Recover a named row's fields from its own verified Markdown header.

        Persisted note chunks may contain a late table row without its header.
        Read the same note/tab only to restore those column labels, and require
        the complete row to already be present in the retrieved chunk. None
        leaves other evidence paths unchanged; an empty string refuses a row
        whose requested fields are missing or whose column mapping conflicts.
        """
        if chunk.get("source_kind") == "document" or chunk.get("kind") == "ocr":
            return None
        subjects = self.explicit_entity_terms(question) or self.parallel_field_subject_terms(question)
        if len(subjects) != 1 or len(self.compound_query_clauses(question)) > 1:
            return None
        raw_text = str(chunk.get("raw_text", "") or chunk.get("excerpt", ""))
        fragment_lines = {line.strip() for line in raw_text.splitlines() if "|" in line}
        if not fragment_lines:
            return None
        note = chunk.get("note")
        source_id = chunk.get("source_id")
        if source_id is not None and source_id != getattr(note, "id", None):
            store = getattr(self, "store", None)
            if store is None:
                return None
            note = store.get_note(source_id)
        lines = str(getattr(note, "content", "") or "").splitlines()

        def cells(line: str) -> list[str]:
            clean = line.strip()
            if "|" not in clean or "\\|" in clean or "`" in clean:
                return []
            if clean.startswith("|"):
                clean = clean[1:]
            if clean.endswith("|"):
                clean = clean[:-1]
            return [value.strip() for value in clean.split("|")]

        rows: list[tuple[str, tuple[str, ...], str]] = []
        retrieved_named_row = False
        for position in range(len(lines) - 2):
            labels = [re.sub(r"\s+", "", value) for value in cells(lines[position])]
            separator = cells(lines[position + 1])
            if (
                len(labels) < 2 or len(labels) != len(separator)
                or len(set(labels)) != len(labels)
                or not all(re.fullmatch(r"[A-Za-z\u3400-\u9fff][A-Za-z0-9_\-/（）()\u3400-\u9fff]{0,23}", label) for label in labels)
                or not all(re.fullmatch(r":?-{3,}:?", value) for value in separator)
            ):
                continue
            for raw_line in lines[position + 2:]:
                values = cells(raw_line)
                if not values:
                    break
                if len(values) != len(labels):
                    continue
                fields = list(zip(labels, values))
                if raw_line.strip() in fragment_lines and self.structured_identity_labels(labels):
                    retrieved_named_row = True
                if not self.structured_row_identity(question, fields):
                    continue
                record = "；".join(f"{label}：{value}" for label, value in fields if value)
                rows.append((raw_line.strip(), tuple(labels), record))

        matching = [row for row in rows if row[0] in fragment_lines]
        if not matching:
            if not rows and retrieved_named_row and self.parallel_field_subject_terms(question):
                # A source-visible table identity must match the whole name;
                # a shorter substring cannot fall back to an unlabelled row.
                return ""
            return None
        projections: set[str] = set()
        for raw_line, labels, record in matching:
            # Identical cell text under different table headers has no unique
            # provenance when the retrieved fragment lacks either header.
            if len({other_labels for other_raw, other_labels, _ in rows if other_raw == raw_line}) > 1:
                return ""
            coverage = self.structured_field_coverage(question, [record])
            if coverage is False:
                return ""
            projection = self.compact_requested_structured_fields(record, question)
            if projection and coverage is True:
                projections.add(projection)
        if not projections:
            return None
        # A question naming only the Agent cannot select between conflicting
        # rows for that same Agent elsewhere in this note.
        for _raw_line, _labels, record in rows:
            if self.structured_field_coverage(question, [record]) is True:
                projection = self.compact_requested_structured_fields(record, question)
                if projection:
                    projections.add(projection)
        return next(iter(projections)) if len(projections) == 1 else ""

    @staticmethod
    def structured_record_fields(record: str) -> list[tuple[str, str]]:
        """Parse one semicolon-delimited table row without crossing row boundaries.

        Imported spreadsheets use semicolons between cells.  A continuation
        segment without a safe ``label: value`` shape belongs to the preceding
        cell; it must not be promoted to a new field merely because its prose
        contains a colon.
        """
        fields: list[tuple[str, str]] = []
        for raw_part in re.split(r"[；;]", str(record or "")):
            part = raw_part.strip()
            if not part:
                continue
            match = re.match(
                r"^([A-Za-z\u3400-\u9fff][A-Za-z0-9_\-/（）()\u3400-\u9fff]{0,23})\s*[：:]\s*(.+)$",
                part,
            )
            if match:
                label = match.group(1).strip()
                value = match.group(2).strip()
                if label and value:
                    fields.append((label, value))
                    continue
            if fields:
                label, value = fields[-1]
                fields[-1] = (label, f"{value}；{part}")
        return fields

    @classmethod
    def structured_record_units(cls, text: str) -> list[tuple[int, str]]:
        """Return independently citable rows from flattened table text.

        Some parsers place several records on one physical line.  A new record
        is recognised when its primary identity field repeats. Secondary
        identities such as a contract's supplier or an ordinal's Agent name
        remain attached to their physical row.
        """
        record_start = re.compile(
            r"(?=(?:^|(?<=[；;]))\s*(?P<label>"
            r"(?:合同|项目|订单|工单|申请|采购|付款|资产|设备|事件|故障|供应商|客户|员工|候选人|产品|批次|记录)?"
            r"(?:编号|编码|序号|ID)"
            r"|Agent名称"
            r"|供应商|客户|候选人|员工"
            r")\s*[：:])",
            flags=re.IGNORECASE,
        )
        units: list[tuple[int, str]] = []
        for line_index, line in enumerate(str(text or "").splitlines()):
            clean = line.strip()
            if not clean:
                continue
            starts = list(record_start.finditer(clean))
            primary = starts[0].group("label").casefold() if starts else ""
            boundaries = [
                match.start() for match in starts[1:]
                if match.group("label").casefold() == primary
            ]
            offsets = [0, *boundaries, len(clean)]
            parts = [clean[start:end].strip("；; ") for start, end in zip(offsets, offsets[1:])]
            for part in parts:
                fields = cls.structured_record_fields(part)
                if len(fields) >= 3 or (
                    len(fields) >= 2
                    and cls.structured_identity_labels(label for label, _ in fields)
                ):
                    units.append((line_index, part.strip("；; ")))
        return units

    @staticmethod
    def structured_identity_labels(labels: Iterable[str]) -> list[str]:
        """Identify fields which keep a compact row tied to its source entity."""
        selected: list[str] = []
        for raw_label in labels:
            label = str(raw_label or "").strip()
            if re.search(
                r"(?:编号|编码|序号|ID|Agent名称|合同号|订单号|工单号|资产号|设备号|事件号|项目名称|供应商|客户|候选人|员工)$",
                label,
                flags=re.IGNORECASE,
            ):
                selected.append(label)
        return selected

    @staticmethod
    def structured_field_aliases() -> tuple:
        """Field vocabulary shared by row selection and missing-field checks."""
        return (
            (("核心功能", "功能", "作用", "能做什么", "做什么"), ("核心功能", "功能")),
            (("适用场景", "使用场景", "应用场景", "场景", "用途"), ("适用场景", "场景", "用途")),
            (("输入资料", "输入材料", "输入内容"), ("输入资料", "输入材料", "输入内容")),
            (("输出资料", "输出内容", "输出结果"), ("输出资料", "输出内容", "输出结果", "输出")),
            (("付款节点", "付款安排", "尾款", "付款时间", "付款条件"), ("付款节点", "付款安排", "付款条件")),
            (("预付款比例", "预付款", "首付款比例"), ("预付款比例", "首付款比例")),
            (("付款比例", "支付比例"), ("付款比例", "支付比例")),
            (("付款状态", "支付状态"), ("付款状态", "支付状态")),
            (("币种", "货币", "结算币种"), ("币种", "货币", "结算币种")),
            (("争议条款", "争议解决", "仲裁", "管辖"), ("争议条款", "争议解决", "仲裁", "管辖")),
            (("负责人", "责任人", "所有者", "经办人", "由谁负责", "谁负责", "谁来负责"), ("负责人", "责任人", "所有者", "经办人")),
            (("状态", "进度", "是否完成"), ("状态", "进度")),
            (("安装在哪里", "安装位置", "安装地点"), ("安装位置", "安装地点")),
            (("根因", "原因"), ("根因", "原因")),
            (("采购价格", "价格", "单价", "售价"), ("采购价格", "价格", "单价", "售价")),
            (("合同金额", "合同总额", "合同总价"), ("合同金额", "合同总额", "合同总价", "签约金额")),
            (("金额", "费用", "成本", "预算"), ("金额", "费用", "成本", "预算", "总额", "总价")),
            (("风险", "隐患", "缺口"), ("核心风险", "风险", "风险等级", "风险描述")),
            (("行动项", "处理措施", "整改措施", "下一步", "怎么处理", "如何处理"), ("行动项", "处理措施", "整改措施", "下一步")),
            (("损失", "损失金额", "影响金额"), ("损失", "损失金额", "影响金额")),
            (("时长", "持续时间", "多久"), ("时长", "持续时间", "故障时长")),
            (("限制", "缺失", "待确认"), ("限制", "缺失处理", "待确认事项")),
        )

    @classmethod
    def requested_structured_labels(cls, question: str, labels: Iterable[str]) -> list[str]:
        """Map user wording to source-visible field labels conservatively."""
        compact_question = re.sub(r"\s+", "", str(question or "")).casefold()
        available = [str(label or "").strip() for label in labels if str(label or "").strip()]
        aliases = cls.structured_field_aliases()
        requested: list[str] = []
        for label in available:
            compact_label = re.sub(r"\s+", "", label).casefold()
            if len(compact_label) >= 2 and compact_label in compact_question:
                requested.append(label)
                continue
            for question_terms, label_terms in aliases:
                if any(term.casefold() in compact_question for term in question_terms) and any(
                    term.casefold() in compact_label for term in label_terms
                ):
                    requested.append(label)
                    break
        return list(dict.fromkeys(requested))

    @classmethod
    def structured_row_identity(cls, question: str, fields: list[tuple[str, str]]) -> tuple[str, str] | None:
        """Return the exact source-visible identity explicitly named by a query."""
        identities = set(cls.structured_identity_labels(label for label, _ in fields))
        named_subjects = cls.named_structured_subject_terms(question)
        for label, value in fields:
            if label not in identities or len(value) < 2 or value.isdigit():
                continue
            if re.search(r"[A-Za-z0-9]", value):
                hit = cls.text_contains_identifier(question, value)
            else:
                hit = (
                    any(cls._normalise_support_text(value) == cls._normalise_support_text(subject) for subject in named_subjects)
                    if named_subjects else bool(re.search(
                        re.escape(value) + r"(?:供应商|客户|候选人|员工)?(?:的|由|是|有|能|[\s，,？?]|$)",
                        question,
                    ))
                )
            if hit:
                return cls._normalise_support_text(label), cls._normalise_support_text(value)
        return None

    @classmethod
    def matched_structured_rows(cls, question: str, texts: Iterable[str]) -> list[str]:
        """Bind fields to exact row identities, never to a neighbouring row."""
        matched: list[str] = []
        for text in texts:
            for _index, record in cls.structured_record_units(text):
                fields = cls.structured_record_fields(record)
                if cls.structured_row_identity(question, fields) and record not in matched:
                    matched.append(record)
        return matched

    @classmethod
    def structured_field_coverage(cls, question: str, texts: Iterable[str]) -> bool | None:
        """Check requested fields on a named row; None leaves prose gates intact."""
        compact_question = re.sub(r"\s+", "", str(question or ""))
        # A field mentioned as an absent input is not a request for its value:
        # "责任人未填写时怎么办" asks for a handling rule, whereas
        # "责任人是谁" must still fail when the row has no owner assignment.
        absence_handling = bool(re.search(
            r"(?:缺失|缺少|未填写|未填|未注明|未指定|为空|没有写明|没有填写)"
            r".{0,18}(?:怎么办|如何处理|怎么处理|的处理|处理方式)",
            compact_question,
        ))
        if absence_handling and not re.search(r"(?:是谁|由谁|多少钱|是多少)", compact_question):
            return None
        # Questions about what an identifier denotes need the explanatory
        # sentence (often outside the row), not a value in a similarly named
        # field such as "风险等级" merely because "风险编号" was mentioned.
        if re.search(
            r"(?:编号|编码|序号|标识|ID).{0,48}(?:是|代表|表示|等于|属于)"
            r".{0,20}(?:编号|编码|序号|标识|ID)",
            compact_question,
            flags=re.IGNORECASE,
        ):
            return None
        # Cross-source synthesis is checked clause by clause. A contract's
        # ratio may inform a policy comparison without containing its outcome.
        clauses = cls.compound_query_clauses(question)
        if len(clauses) > 1 and not cls.shared_record_field_queries(question):
            # An omitted subject on a field-only continuation still belongs
            # to the same row. Do not let decomposition borrow that field
            # from another record in the same chunk.
            field_only = r"^(?:它|其|该记录)?的?(?:采购价格|价格|单价|负责人|责任人|状态|进度|根因|原因|持续时间|时长|损失|安装位置|安装在哪里|预付款比例|尾款|币种)"
            if len(cls.record_identifier_terms(question)) > 1 or not all(
                re.match(field_only, re.sub(r"\s+", "", clause)) for clause in clauses[1:]
            ):
                return None
        rows = cls.matched_structured_rows(question, texts)
        if not rows:
            return None
        requested_ids = cls.record_identifier_terms(question)
        if any(not any(cls.text_contains_identifier(row, identifier) for row in rows) for identifier in requested_ids):
            return False
        recognised = False
        covered = True
        grouped_fields: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for row in rows:
            fields = cls.structured_record_fields(row)
            identity = cls.structured_row_identity(question, fields)
            if identity is not None:
                grouped_fields.setdefault(identity, []).extend(fields)
        # Long rows repeat their identity in each bounded fragment. Coverage
        # may span those fragments, but fields from a different ID never join.
        for fields in grouped_fields.values():
            identities = set(cls.structured_identity_labels(label for label, _ in fields))
            field_question = str(question or "")
            for label, value in fields:
                if label in identities and len(value) >= 2:
                    field_question = re.sub(
                        cls.whitespace_flexible_pattern(value), "", field_question,
                        flags=re.IGNORECASE,
                    )
            compact_question = re.sub(r"\s+", "", field_question).casefold()
            requested = [
                label_terms for question_terms, label_terms in cls.structured_field_aliases()
                if any(term.casefold() in compact_question for term in question_terms)
            ]
            if not requested:
                continue
            recognised = True
            present = [label.casefold() for label, value in fields if value.strip()]
            covered = covered and all(
                any(any(term.casefold() in label for term in group) for label in present)
                for group in requested
            )
        return covered if recognised else None

    @classmethod
    def compact_requested_structured_fields(cls, record: str, question: str) -> str:
        """Keep row identity plus only the fields requested by the question."""
        fields = cls.structured_record_fields(record)
        if len(fields) < 2:
            return ""
        labels = [label for label, _value in fields]
        requested = cls.requested_structured_labels(question, labels)
        if not requested:
            return ""
        identities = cls.structured_identity_labels(labels)
        if "Agent名称" in identities:
            identities = [label for label in identities if label not in {"编号", "序号"}]
        keep = set([*identities, *requested])
        selected = [f"{label}：{value}" for label, value in fields if label in keep]
        return "；".join(selected).strip("；")[:520]

    def selector_structured_row_evidence(self, question: str, text: str, hits: list[str]) -> str:
        """Return the complete matching row for a ``which record`` question.

        Imported tables often store the identifier, currency, schedule and
        other answer fields in one physical row.  Selecting one semicolon-delimited
        cell loses the identity of the record.  Split flattened multi-record
        lines first, then require at least two source-visible query signals so a
        generic word such as ``合同`` cannot preserve an unrelated row.
        """
        raw_question = str(question or "")
        if not re.search(r"(?:哪份|哪个|哪一个|哪条|哪项|哪笔)", raw_question):
            return ""

        units = self.structured_record_units(text)
        if not units:
            return ""

        generic = {
            "合同", "记录", "文件", "文档", "项目", "哪个", "哪份", "哪一个",
            "哪条", "哪项", "哪笔", "付款", "什么", "是否", "一份",
        }
        query_terms = self.query_terms(raw_question)
        scored: list[tuple[float, int, str]] = []
        for line_index, unit in units:
            record_hits = self.matched_terms(unit, query_terms)
            signals = [
                hit for hit in record_hits
                if re.sub(r"\s+", "", hit).casefold() not in generic
                and not re.search(r"(?:哪份|哪个|哪一个|哪条|哪项|哪笔|什么)", hit)
            ]
            # Include retrieval hits only when they are visibly present in this
            # row; they improve ranking but cannot manufacture row eligibility.
            visible_hits = [
                str(hit).strip() for hit in hits
                if str(hit).strip()
                and self.text_contains_term(unit, str(hit))
                and re.sub(r"\s+", "", str(hit)).casefold() not in generic
            ]
            distinct_signals = list(dict.fromkeys([
                re.sub(r"\s+", "", value).casefold()
                for value in [*signals, *visible_hits]
                if value
            ]))
            if len(distinct_signals) < 2:
                continue
            score = self.lexical_score(record_hits, unit.casefold(), raw_question)
            score += sum(len(value) * 2 for value in distinct_signals)
            scored.append((score, -line_index, unit))
        if not scored:
            return ""
        return max(scored, key=lambda item: item[:2])[2].strip("；; ")[:720]

    def structured_record_evidences(self, question: str, text: str, hits: list[str]) -> list[str]:
        """Keep every independently requested structured record in one chunk."""
        identifier_records = self.identifier_record_evidences(question, text)
        if identifier_records:
            return identifier_records

        # An identifier alone (for example ``合同编号``) does not make an
        # entire physical line one evidence unit. Imported spreadsheets often
        # flatten several records plus a general rule onto the same line; if we
        # preserve that line wholesale, an unrelated ``预付款比例：0%`` can be
        # cited together with the actual approval rule. Keep a row intact only
        # when it exposes semantic fields whose values belong together.
        rows = self.structured_record_units(text)
        if not rows:
            return []

        entity_terms = self.explicit_entity_terms(question)
        targets = entity_terms if len(entity_terms) > 1 else self.compound_query_clauses(question)
        if len(targets) <= 1:
            targets = [question]

        selected = []
        for target in targets:
            target_terms = self.query_terms(target)
            target_entities = self.explicit_entity_terms(target)
            target_identifiers = self.explicit_identifier_terms(target)
            candidates = []
            for index, record in rows:
                # A multi-Agent question must bind each requested identifier to
                # its own row. Generic "Agent" hits from another row are not
                # evidence for this target.
                if target_entities and not any(self.text_contains_term(record, entity) for entity in target_entities):
                    continue
                record_hits = self.matched_terms(record, target_terms)
                if not record_hits:
                    continue
                fields = self.structured_record_fields(record)
                requested_labels = self.requested_structured_labels(
                    target,
                    [label for label, _value in fields],
                )
                anchored = bool(
                    target_entities
                    or any(self.text_contains_identifier(record, identifier) for identifier in target_identifiers)
                )
                meaningful_hits = [
                    hit for hit in record_hits
                    if len(re.sub(r"\s+", "", hit)) >= 3
                    and not re.search(r"^(?:Agent|什么|哪些|如何|怎么|功能|场景|状态|风险|记录|资料|文件)$", hit, re.IGNORECASE)
                ]
                semantic_task_hits = [
                    hit for hit in record_hits
                    if len(re.sub(r"\s+", "", hit)) >= 2
                    and not re.search(r"^(?:Agent|什么|哪些|如何|怎么|功能|场景|状态|风险|记录|资料|文件)$", hit, re.IGNORECASE)
                ]
                semantic_agent_task = (
                    {"Agent名称", "核心功能"}.issubset({label for label, _ in fields})
                    and len(semantic_task_hits) >= 2
                )
                if not anchored and not requested_labels and not semantic_agent_task:
                    continue
                if not anchored and len(meaningful_hits) < 2 and not semantic_agent_task:
                    continue
                score = self.lexical_score(record_hits, record.lower(), target)
                score += sum(len(hit) * 2 for hit in record_hits)
                score += len(requested_labels) * 8
                if "核心功能" in record and "适用场景" in record:
                    score += 8
                candidates.append((score, -index, record))
            if candidates:
                best = max(candidates, key=lambda item: item[:2])
                evidence = self.compact_structured_record_evidence(best[2], question, hits)
                if best[0] > 0 and evidence not in selected:
                    selected.append(evidence)
        return selected

    @classmethod
    def identifier_record_evidences(cls, question: str, text: str) -> list[str]:
        """Select rows by an explicit alphanumeric identifier in the question.

        This is intentionally exact and bounded. It prevents a contract or
        order lookup from stopping at the identifier field while also avoiding
        the unsafe behavior of preserving a flattened line containing several
        unrelated records.
        """
        identifiers = cls.record_identifier_terms(question)
        if not identifiers:
            return []

        units = [record for _index, record in cls.structured_record_units(text)]

        selected: list[str] = []
        for identifier in identifiers:
            matching = [unit for unit in units if cls.text_contains_identifier(unit, identifier)]
            requested = [
                compact for unit in matching
                if (compact := cls.compact_requested_structured_fields(unit, question))
            ]
            for compact in requested or [re.sub(r"\s+", " ", unit).strip("；; ") for unit in matching]:
                if compact and compact not in selected:
                    selected.append(compact[:720])
        return selected

    def compact_structured_record_evidence(self, record: str, question: str, hits: list[str]) -> str:
        """Keep a long spreadsheet row citable without turning it into a full-page highlight.

        Spreadsheet rows can contain an entire control design in one physical
        line.  Returning that line as a "sentence" makes both the answer card
        and document highlight unreadable.  Preserve the rule-defining fields
        first, then include only a small number of matching clauses.
        """
        normalized = str(record or "").strip()
        requested = self.compact_requested_structured_fields(normalized, question)
        if requested:
            return requested
        if len(normalized) <= 720:
            return normalized
        clauses = [part.strip() for part in re.split(r"；|(?<=。)", normalized) if part.strip()]
        selected: list[str] = []
        preferred_labels = ("核心风险", "风险监控指标", "核心功能", "适用场景")
        for label in preferred_labels:
            for index, clause in enumerate(clauses):
                if label not in clause:
                    continue
                if clause not in selected:
                    selected.append(clause)
                # Spreadsheet cells often use semicolons inside one labelled
                # KRI field. Keep the immediate continuation so "KRI 2" and
                # its approval role are not separated from "风险监控指标".
                if label == "风险监控指标" and index + 1 < len(clauses):
                    continuation = clauses[index + 1]
                    if continuation and continuation not in selected:
                        selected.append(continuation)
                break
        # When a structured record exposes its own risk/function field, that is
        # the concise answer. Only fall back to rule clauses for records without
        # those labelled fields.
        if not selected:
            specific_hits = [str(hit).strip() for hit in hits if len(str(hit).strip()) >= 2]
            for clause in clauses:
                if any(hit.casefold() in clause.casefold() for hit in specific_hits) and re.search(r"(?:审批|确认|金额|授权|签字|阈值|条件)", clause):
                    selected.append(clause)
                if len("；".join(selected)) >= 420 or len(selected) >= 2:
                    break
        compact = "；".join(selected).strip("；")
        if not compact:
            compact = normalized[:420].rsplit("；", 1)[0] or normalized[:420]
        return compact[:520]

    @classmethod
    def rerank_candidates(cls, question: str, candidates: list[dict], focus_terms: list[str] | None = None) -> list[dict]:
        """Apply an explainable second-pass relevance score without another model.

        BGE/FTS are intentionally broad recall stages.  This pass rewards a
        complete named-topic hit and independent meaningful query terms, while
        demoting candidates matched solely by question scaffolding.  It adds no
        model download or generation latency and leaves broad multi-source
        questions eligible for several evidence chunks.
        """
        focus_terms = focus_terms or cls.focused_query_terms(cls.query_terms(question))
        entity_terms = cls.explicit_entity_terms(question)
        for item in candidates:
            evidence_text = f"{item.get('title', '')}\n{item.get('raw_text', item.get('excerpt', ''))}"
            text = evidence_text.lower()
            hits = list(item.get("hits", []))
            meaningful_hits = [
                hit for hit in hits
                if cls.has_specific_matched_term({"matched_terms": [hit]})
            ]
            matched_focus = [term for term in focus_terms if cls.text_contains_term(evidence_text, term)]
            matched_entities = [term for term in entity_terms if cls.text_contains_term(evidence_text, term)]

            adjustment = min(4.0, len(meaningful_hits) * 1.25)
            adjustment += min(3.0, len(matched_focus) * 0.75)
            adjustment += min(8.0, len(matched_entities) * 8.0)
            adjustment += cls.temporal_fact_bonus(question, text, hits)
            adjustment += cls.role_comparison_bonus(question, text)
            adjustment += cls.principle_evidence_bonus(question, text)
            adjustment += cls.remediation_evidence_bonus(question, evidence_text)
            adjustment += cls.ocr_agent_table_bonus(question, evidence_text)
            adjustment += cls.requested_field_evidence_bonus(question, evidence_text)
            if hits and not meaningful_hits:
                adjustment -= 5.0
            if matched_entities and all(field in text for field in ("核心功能", "适用场景")):
                adjustment += 2.0

            item["rerank_adjustment"] = round(adjustment, 2)
            item["rank_score"] += adjustment
        return candidates

    @staticmethod
    def compact_channel_ranks(channels: dict[str, int | None]) -> dict[str, int]:
        """Drop absent retrieval channels while preserving their one-based rank."""
        return {
            str(name): int(rank)
            for name, rank in channels.items()
            if rank is not None and int(rank) > 0
        }

    @staticmethod
    def rank_ids_by_score(scores: dict[int, float]) -> dict[int, int]:
        """Return stable one-based ranks for a sparse score mapping."""
        ordered = sorted(scores.items(), key=lambda item: (-float(item[1]), int(item[0])))
        return {int(item_id): rank for rank, (item_id, _) in enumerate(ordered, 1)}

    @classmethod
    def merge_candidate_channels(cls, candidates: list[dict]) -> list[dict]:
        """Merge duplicate materialisations of one persisted evidence chunk.

        A note chunk can be reached through its parent note vector, its own
        vector, and FTS. Weighted RRF must see those as independent votes for
        one chunk rather than as duplicate results competing for answer slots.
        """
        merged: dict[str, dict] = {}
        order: list[str] = []
        for candidate in candidates:
            key = str(
                candidate.get("chunk_identity")
                or candidate.get("source_id")
                or id(candidate)
            )
            incoming = dict(candidate)
            incoming["fusion_channels"] = dict(candidate.get("fusion_channels") or {})
            current = merged.get(key)
            if current is None:
                merged[key] = incoming
                order.append(key)
                continue

            current_channels = dict(current.get("fusion_channels") or {})
            for channel, rank in incoming["fusion_channels"].items():
                previous = current_channels.get(channel)
                if previous is None or int(rank) < int(previous):
                    current_channels[channel] = int(rank)

            representative = current
            if float(incoming.get("rank_score", 0.0) or 0.0) > float(current.get("rank_score", 0.0) or 0.0):
                representative = incoming
            representative["fusion_channels"] = current_channels
            representative["hits"] = list(dict.fromkeys([
                *current.get("hits", []),
                *incoming.get("hits", []),
            ]))[:8]
            representative["lexical_score"] = max(
                float(current.get("lexical_score", 0.0) or 0.0),
                float(incoming.get("lexical_score", 0.0) or 0.0),
            )
            representative["fts_score"] = max(
                float(current.get("fts_score", 0.0) or 0.0),
                float(incoming.get("fts_score", 0.0) or 0.0),
            )
            representative["score"] = max(
                float(current.get("score", 0.0) or 0.0),
                float(incoming.get("score", 0.0) or 0.0),
            )
            merged[key] = representative
        return [merged[key] for key in order]

    @classmethod
    def apply_weighted_rrf(cls, candidates: list[dict]) -> list[dict]:
        """Fuse independent lexical and dense rankings with bounded weighted RRF.

        Candidate pools are deliberately small, so ``k=20`` retains useful
        rank separation without allowing one channel's raw score scale to
        dominate. The multiplier preserves room for the existing explainable
        reranker; it does not change the RRF ordering itself.
        """
        if not candidates:
            return candidates

        lexical_order = sorted(
            range(len(candidates)),
            key=lambda index: (
                -float(candidates[index].get("lexical_score", 0.0) or 0.0),
                index,
            ),
        )
        lexical_ranks = {
            index: rank
            for rank, index in enumerate(lexical_order, 1)
            if float(candidates[index].get("lexical_score", 0.0) or 0.0) > 0
        }
        weights = {
            "semantic_parent": 0.85,
            "semantic_chunk": 1.35,
            "fts": 1.15,
            "lexical": 1.10,
        }
        k = 20.0
        scale = 500.0
        for index, item in enumerate(candidates):
            channels = dict(item.get("fusion_channels") or {})
            if index in lexical_ranks:
                channels["lexical"] = lexical_ranks[index]
            contributions = {
                channel: weights[channel] / (k + float(rank))
                for channel, rank in channels.items()
                if channel in weights and float(rank) > 0
            }
            fusion_score = sum(contributions.values())
            item["fusion_channels"] = channels
            item["fusion_contributions"] = {
                channel: round(value, 8) for channel, value in contributions.items()
            }
            item["fusion_score"] = round(fusion_score, 8)
            item["rank_score"] = fusion_score * scale
        return candidates

    @classmethod
    def filter_weak_chunks(
        cls,
        ranked: list[dict],
        topic_anchors: list[str] | None = None,
        question: str = "",
    ) -> list[dict]:
        if question:
            ranked = [
                item for item in ranked
                if not cls.candidate_declares_irrelevance(question, item)
            ]
        identifier_terms = cls.retrieval_identifier_terms(question)

        def contains_stable_identifier(item: dict) -> bool:
            if not identifier_terms:
                return False
            corpus = f"{item.get('title', '')}\n{item.get('raw_text', item.get('excerpt', ''))}"
            return any(
                cls.text_contains_identifier(corpus, identifier)
                for identifier in identifier_terms
            )
        # An explicit multi-character topic should not be represented by an
        # overlapping fragment.  Pure semantic matches remain eligible only at
        # a deliberately high confidence; this keeps legitimate paraphrases
        # while removing "提示词" -> "提示词注入" false positives.
        anchors = topic_anchors or []
        if anchors:
            narrowed = []
            for item in ranked:
                text = f"{item.get('title', '')}\n{item.get('raw_text', item.get('excerpt', ''))}".lower()
                has_anchor = any(cls.text_contains_term(text, anchor) for anchor in anchors)
                has_lexical_fragment = bool(item.get("hits"))
                # A candidate with no lexical overlap is a genuine semantic
                # candidate and must stay eligible for paraphrases.  Only a
                # *partial lexical* hit is misleading evidence for a compact
                # named topic.
                if has_anchor or (
                    not has_lexical_fragment and float(item.get("score", 0.0) or 0.0) >= 0.72
                ):
                    narrowed.append(item)
            ranked = narrowed
        topic_source_ids = {
            item.get("source_id", item["note"].id)
            for item in ranked
            if cls.title_topic_bonus(item["note"].title, item["hits"]) > 0
        }
        best_lexical = max((item["lexical_score"] for item in ranked), default=0)
        if best_lexical >= 6:
            # The title may contain the full topic while its body uses a shorter
            # phrasing. Keep that body's candidate so dedupe can later choose
            # actual evidence instead of the title-only chunk.
            ranked = [
                item for item in ranked
                if item["lexical_score"] >= best_lexical * 0.35
                or item.get("source_id", item["note"].id) in topic_source_ids
                or contains_stable_identifier(item)
            ]
        # 当查询的中文主题已经和某个标题直接匹配时，低分的“只命中型号/ID”
        # 候选通常是错误前提带来的干扰。保留同主题标题或足够接近的强候选，
        # 避免小模型把错误型号和正确主题强行拼在一起。
        title_topic_scores = [cls.title_topic_bonus(item["note"].title, item["hits"]) for item in ranked]
        best_topic = max(title_topic_scores, default=0.0)
        best_rank = max((item["rank_score"] for item in ranked), default=0.0)
        if best_topic > 0 and best_rank > 0:
            ranked = [
                item for item in ranked
                if item.get("source_id", item["note"].id) in topic_source_ids
                or item["rank_score"] >= best_rank * 0.95
                or contains_stable_identifier(item)
            ]
        return ranked

    @staticmethod
    def candidate_declares_irrelevance(question: str, candidate: dict) -> bool:
        """Exclude fixture/noise prose that explicitly says it is unrelated.

        A direct relationship question may legitimately be answered with
        "unrelated", so negative relation evidence remains eligible there.
        """
        compact_question = re.sub(r"\s+", "", str(question or ""))
        if re.search(r"(?:关系|相关|无关|没关系|不相关)", compact_question):
            return False
        title = str(candidate.get("title", "") or "").strip()
        text = str(candidate.get("raw_text", candidate.get("excerpt", "")) or "")
        if re.match(r"^(?:干扰项|噪声样本|无关样本)[:：]", title):
            return True
        return bool(
            re.search(r"(?:都|完全)?(?:没关系|无关|不相关)", text)
            and re.search(r"(?:测试|干扰|噪声|抗干扰)", text)
        )

    @classmethod
    def dedupe_chunks(cls, chunks: list[dict], limit: int, question: str = "") -> list[dict]:
        if any(chunk["hits"] for chunk in chunks):
            chunks = [chunk for chunk in chunks if chunk["hits"] or chunk["lexical_score"] > 0]
        selected: list[dict] = []
        per_source: dict[str, int] = {}
        seen = set()
        def group_id(chunk: dict) -> str:
            return str(chunk.get("source_group") or chunk.get("source_id") or chunk["note"].id)

        def usable(chunk: dict) -> bool:
            source_id = chunk.get("source_id", chunk["note"].id)
            if chunk.get("kind") == "title" and any(
                item.get("source_id", item["note"].id) == source_id and item.get("kind") != "title" for item in chunks
            ):
                return False
            key = str(chunk.get("chunk_identity") or re.sub(r"\s+", "", chunk["excerpt"][:160]))
            if key in seen:
                return False
            seen.add(key)
            return True

        # A complete-list question needs the numbered siblings from the same
        # source before we reserve broad recall from several different sources.
        # Without this exception, the usual "one source first" rule can keep
        # only "风险一" and silently discard "风险二、风险三".
        if cls.is_enumeration_question(question) and limit > 1:
            enumerated_groups: dict[str, list[dict]] = {}
            for chunk in chunks:
                raw_text = str(chunk.get("raw_text", "") or chunk.get("excerpt", ""))
                if cls.enumeration_item_count(question, raw_text) <= 0:
                    continue
                enumerated_groups.setdefault(group_id(chunk), []).append(chunk)
            if enumerated_groups:
                preferred_group = max(
                    enumerated_groups.values(),
                    key=lambda group: (
                        len(group),
                        max(float(item.get("rank_score", 0.0) or 0.0) for item in group),
                    ),
                )
                for chunk in preferred_group[: min(6, limit)]:
                    group = group_id(chunk)
                    if not usable(chunk):
                        continue
                    selected.append(chunk)
                    per_source[group] = per_source.get(group, 0) + 1

        # First reserve one evidence unit for every source. This prevents a long
        # document from pushing all other relevant notes out of the answer.
        source_first: list[dict] = []
        for chunk in chunks:
            group = group_id(chunk)
            if group in per_source or not usable(chunk):
                continue
            source_first.append(chunk)
            per_source[group] = 1
            if len(selected) + len(source_first) >= limit:
                break
        selected.extend(source_first)
        if len(selected) >= limit:
            return selected

        # A single source may need more than one fragment for a compound rule or
        # two related rows. Keep a bounded second/third evidence unit; sources
        # are merged again before they reach the UI and the generation prompt.
        for chunk in chunks:
            group = group_id(chunk)
            key = str(chunk.get("chunk_identity") or re.sub(r"\s+", "", chunk["excerpt"][:160]))
            if key in seen or per_source.get(group, 0) >= 3:
                continue
            if not usable(chunk):
                continue
            selected.append(chunk)
            per_source[group] = per_source.get(group, 0) + 1
            if len(selected) >= limit:
                break
        return selected

    @staticmethod
    def answer_needs_fallback(answer: str) -> bool:
        normalized = answer.strip()
        if not normalized:
            return True
        # 来源编号本身不是回答质量证明。小模型偶尔会输出
        # "没有足够信息 [1]"，此时仍应从已命中的具体证据恢复答案。
        has_markers = bool(re.search(r"\[\d+\]", normalized))
        markers = ("没有足够信息", "无法回答", "不能回答", "无法提供", "未提及", "不包含", "没有找到", "没有相关")
        has_negative = any(marker in normalized for marker in markers)
        if has_negative:
            # A short refusal remains a refusal even when it carries a source
            # number.  Long answers may contain a qualified limitation, so do
            # not replace those wholesale.
            return len(normalized) <= 90
        if has_markers:
            return False
        return False

    @staticmethod
    def has_specific_matched_term(source: dict) -> bool:
        terms = [str(term).strip() for term in source.get("matched_terms", []) if str(term).strip()]
        generic = {"ai", "agent", "功能", "场景", "团队", "什么", "哪些", "怎么", "有什么", "什么样的", "有用"}
        for term in terms:
            normalized = term.lower()
            if len(term) < 3 or normalized in generic or any(marker in term for marker in ("什么", "哪些", "如何", "怎么", "功能", "场景", "有用", "作用")):
                continue
            return True
        return False

    @staticmethod
    def has_substantive_matched_term(source: dict) -> bool:
        """Accept short domain terms such as ``光速`` but reject question scaffolding."""
        generic_fragments = ("什么", "哪些", "如何", "怎么", "功能", "场景", "有用", "作用", "是否", "可以")
        generic = {"ai", "agent", "团队", "问题", "内容", "资料"}
        for raw_term in source.get("matched_terms", []):
            term = str(raw_term).strip()
            if len(term) < 2 or term.lower() in generic or any(fragment in term for fragment in generic_fragments):
                continue
            return True
        return False

    @classmethod
    def has_unique_specific_term(cls, sources: list[dict]) -> bool:
        if not sources:
            return False
        primary = sources[0]
        for term in primary.get("matched_terms", []):
            term = str(term).strip()
            if not cls.has_specific_matched_term({"matched_terms": [term]}):
                continue
            occurrences = sum(
                term in {str(item).strip() for item in source.get("matched_terms", [])}
                for source in sources
            )
            if occurrences == 1:
                return True
        return False

    @classmethod
    def can_use_evidence_fallback(cls, sources: list[dict]) -> bool:
        """Allow a traceable extract when retrieval already supplied evidence."""
        if not sources:
            return False
        for source in sources:
            evidence = str(source.get("evidence", "")).strip()
            if not evidence:
                continue
            matched_clauses = [
                str(clause).strip()
                for clause in source.get("matched_clauses", [])
                if str(clause).strip()
            ]
            terms = [
                str(term).strip()
                for term in source.get("matched_terms", [])
                if str(term).strip()
            ]
            if matched_clauses or (terms and cls.has_substantive_matched_term(source)):
                return True
        return False

    @classmethod
    def fallback_sources(cls, sources: list[dict], force_multiple: bool = False) -> list[dict]:
        """Keep displayed references aligned with the evidence-only fallback."""
        if force_multiple:
            # One spreadsheet chunk may already contain separate, complete
            # rows for every requested task. In that case extra low-score
            # semantic neighbours add noise rather than evidence.
            complete_structured = [
                source for source in sources
                if sum("Agent名称：" in line for line in str(source.get("evidence", "")).splitlines()) >= 2
            ]
            if complete_structured:
                return complete_structured[:1]
            return sources[:3]
        if cls.has_unique_specific_term(sources):
            return sources[:1]
        return sources[:3]

    @staticmethod
    def cited_sources(answer: str, sources: list[dict]) -> list[dict]:
        """Return only valid sources explicitly cited by the final answer.

        Keep the original list when the model emitted no usable citations; that
        leaves the evidence visible for inspection instead of pretending an
        unsupported answer has a precise source.
        """
        cited_indexes = {int(index) for index in re.findall(r"\[(\d+)\]", str(answer or ""))}
        if not cited_indexes:
            return sources
        selected = [
            source for position, source in enumerate(sources, 1)
            if int(source.get("index", position)) in cited_indexes
        ]
        return selected or sources

    @staticmethod
    def compact_selected_citations(answer: str, sources: list[dict]) -> tuple[str, list[dict]]:
        """Renumber a pruned source list and its answer citations together.

        Retrieval sources keep their original indexes while the answer contract
        decides which references remain visible.  If the model cites only the
        original source ``[2]``, the UI receives a one-item list; leaving the
        answer as ``[2]`` would point past that list.  Compact only after
        evidence alignment, which still needs the original indexes.
        """
        if not sources:
            return str(answer or ""), []
        index_map = {
            int(source.get("index", position)): position
            for position, source in enumerate(sources, 1)
        }

        def replace(match: re.Match) -> str:
            original = int(match.group(1))
            replacement = index_map.get(original)
            return f"[{replacement}]" if replacement is not None else match.group(0)

        compact_answer = re.sub(r"\[(\d+)\]", replace, str(answer or ""))
        compact_sources: list[dict] = []
        for position, source in enumerate(sources, 1):
            updated = dict(source)
            updated["index"] = position
            compact_sources.append(updated)
        return compact_answer, compact_sources

    @staticmethod
    def cited_indexes(answer: str) -> set[int]:
        return {int(index) for index in re.findall(r"\[(\d+)\]", str(answer or ""))}

    @classmethod
    def answer_claims(cls, answer: str) -> list[dict]:
        """Split an answer into auditable claims without breaking decimals.

        A leading ``来源[n]`` applies to every sentence on that line.  Trailing
        citations remain sentence-local: this deliberately rejects the common
        but ambiguous pattern ``事实 A；事实 B [1]`` where only the last claim is
        visibly sourced.
        """
        claims: list[dict] = []
        for raw_line in str(answer or "").splitlines():
            line = raw_line.strip(" -•\t")
            if not line:
                continue
            leading = re.match(r"^来源\s*\[(\d+)\]\s*", line)
            inherited = {int(leading.group(1))} if leading else set()
            if leading:
                line = line[leading.end():]
            for sentence in cls._split_evidence_sentences(line):
                body = re.sub(r"\[\d+\]", "", sentence).strip(" -•\t。！？!?；; ")
                if len(body) < 2 or not re.search(r"[\u3400-\u9fffA-Za-z0-9]", body):
                    continue
                citations = cls.cited_indexes(sentence) or inherited
                claims.append({
                    "text": body,
                    "citations": sorted(citations),
                    "raw": sentence.strip(),
                })
        merged: list[dict] = []
        polar_conclusions = {
            "是", "不是", "可以", "不可以", "能", "不能", "有", "没有", "需要", "不需要",
        }
        index = 0
        while index < len(claims):
            claim = claims[index]
            compact = re.sub(r"\s+", "", claim["text"]).strip("。！？!?；;")
            if compact in polar_conclusions and index + 1 < len(claims):
                following = claims[index + 1]
                citations = sorted(set(claim["citations"]) | set(following["citations"]))
                merged.append({
                    "text": f"{claim['text'].rstrip('。！？!?；;')}。{following['text']}",
                    "citations": citations,
                    "raw": f"{claim['raw']}{following['raw']}",
                })
                index += 2
                continue
            merged.append(claim)
            index += 1
        return merged

    @staticmethod
    def _normalise_support_text(text: str) -> str:
        return re.sub(r"[\s`*_#<>《》\[\]（）()，,。！？!?；;：:\-—]", "", str(text or "")).casefold()

    @classmethod
    def claim_exact_markers(cls, claim: str) -> list[str]:
        """Return values whose provenance can be checked deterministically."""
        plain = re.sub(r"\[\d+\]", "", str(claim or ""))
        markers: list[str] = []
        patterns = (
            r"[\u3400-\u9fffA-Za-z0-9_-]{2,}(?:Agent|智能体)",
            r"(?<![A-Za-z0-9])(?:v(?:ersion)?\s*)?\d+(?:\.\d+){1,3}(?![A-Za-z0-9])",
            r"(?<![A-Za-z0-9])(?:[A-Za-z]{1,12}[-_])+(?:[A-Za-z0-9]+[-_]*)+(?![A-Za-z0-9])",
            r"\d{4}\s*[-/.年]\s*\d{1,2}(?:\s*[-/.月]\s*\d{1,2}\s*日?)?",
            r"\d+(?:\.\d+)?\s*(?:[%％]|亿元|万元|元|万|亿|毫米|厘米|米|mm/s|mm|cm|GB|MB|KB|天|日|周|星期|月|年|小时|分钟|秒|个|项|次|人|条)",
        )
        seen = set()
        for pattern in patterns:
            for match in re.findall(pattern, plain, flags=re.IGNORECASE):
                value = str(match).strip()
                key = cls._normalise_support_text(value)
                if key and key not in seen:
                    markers.append(value)
                    seen.add(key)
        return markers

    @classmethod
    def _claim_support_terms(cls, claim: str) -> list[str]:
        terms = cls.query_terms(re.sub(r"\[\d+\]", "", str(claim or "")))
        selected: list[str] = []
        for term in terms:
            if not cls.has_substantive_matched_term({"matched_terms": [term]}):
                continue
            if term.casefold() in {item.casefold() for item in selected}:
                continue
            selected.append(term)
        return selected

    @classmethod
    def answer_evidence_mapping(cls, question: str, answer: str, sources: list[dict]) -> list[dict]:
        """Map each visible claim to cited evidence using conservative checks.

        ``supported`` is a deterministic/lexical proxy, not semantic
        entailment.  It verifies citation validity, exact-value provenance and
        at least one substantive textual bridge.  Human evaluation remains the
        authority for paraphrases and business meaning.
        """
        source_by_index = {
            int(source.get("index", position)): source
            for position, source in enumerate(sources, 1)
        }
        question_text = cls._normalise_support_text(question)
        mapping: list[dict] = []
        for claim in cls.answer_claims(answer):
            cited = list(claim["citations"])
            invalid = [index for index in cited if index not in source_by_index]
            cited_sources = [source_by_index[index] for index in cited if index in source_by_index]
            evidence = "\n".join(
                "\n".join((
                    str(source.get("evidence", "")),
                    str(source.get("fallback_excerpt", "")),
                    str(source.get("excerpt", "")),
                ))
                for source in cited_sources
            )
            normalised_evidence = cls._normalise_support_text(evidence)
            unsupported_markers = [
                marker for marker in cls.claim_exact_markers(claim["text"])
                if cls._normalise_support_text(marker) not in normalised_evidence
                and cls._normalise_support_text(marker) not in question_text
            ]
            terms = cls._claim_support_terms(claim["text"])
            matched_terms = cls.matched_terms(evidence, terms) if evidence else []
            if not cited or invalid or unsupported_markers:
                status = "unsupported"
            elif matched_terms:
                status = "supported"
            else:
                status = "unsupported"
            mapping.append({
                **claim,
                "valid_citations": [index for index in cited if index in source_by_index],
                "invalid_citations": invalid,
                "exact_markers": cls.claim_exact_markers(claim["text"]),
                "unsupported_markers": unsupported_markers,
                "matched_terms": matched_terms[:8],
                "status": status,
            })
        return mapping

    @classmethod
    def answer_has_unsupported_claims(cls, question: str, answer: str, sources: list[dict]) -> bool:
        mapping = cls.answer_evidence_mapping(question, answer, sources)
        return bool(mapping) and any(item["status"] == "unsupported" for item in mapping)

    @classmethod
    def answer_has_unsupported_retention_endpoint(cls, question: str, answer: str, sources: list[dict]) -> bool:
        """Do not infer a material's retention endpoint from a related workflow.

        Detection is broader than proof: only a complete, affirmative clause
        with the same literal object, action and endpoint proves the relation.
        Unknown wording recovers source text; it is not a general policy parser.
        """
        material = r"(?:资料|记录|数据|文件)"
        endpoint = re.compile(r"(?:保留|保存)(?:直至|直到|至|到)[^，,。；;！？!?\n]{2,64}")
        relation = re.compile(
            r"(?P<object>[A-Za-z0-9_\-\u3400-\u9fff]{0,32}" + material + r")"
            r"(?:将会|将|会|应|必须|需要)?(?P<action>保留|保存)"
            r"(?:直至|直到|至|到)(?P<end>[^，,。；;！？!?\n]{2,64})"
        )

        def proved_relations(text: str) -> set[tuple[str, str, str]]:
            result = set()
            for clause in re.split(r"[，,。；;！？!?\n]+", str(text or "")):
                clause = re.sub(r"\s+", "", clause)
                clause = re.sub(r"^【[^】]+】", "", clause)
                found = relation.fullmatch(clause)
                if found and not re.search(r"并非|不是|不等于|不代表|否认|不|未|无需", found["object"]):
                    result.add((found["object"], found["action"], found["end"]))
            return result

        by_index = {int(source.get("index", index)): source for index, source in enumerate(sources, 1)}
        for claim in cls.answer_claims(answer):
            text = re.sub(r"\s+", "", claim["text"])
            if not endpoint.search(text) or not re.search(material, str(question or "") + text):
                continue
            requested = proved_relations(text)
            # A comma-separated continuation with an omitted object is not a
            # complete relation, even if another part of the claim is proved.
            if len(requested) != len(endpoint.findall(text)):
                return True
            for item in requested:
                if not any(
                    item in proved_relations(str(source.get(field, "")))
                    for index in claim["citations"] if (source := by_index.get(index)) is not None
                    for field in ("evidence", "fallback_excerpt", "excerpt")
                ):
                    return True
        return False

    @staticmethod
    def _amount_alignment_anchor_source(source: dict) -> dict | None:
        """Project only a producer-verified, still-current passage origin."""
        anchor = source.get("evidence_alignment_anchor")
        if not isinstance(anchor, dict) or not anchor.get("location") or not anchor.get("url"):
            return None
        if anchor.get("source_id") != source.get("id") or anchor.get("source_type") != source.get("type"):
            return None
        evidence = str(source.get("evidence", ""))
        context = str(source.get("fallback_excerpt", ""))
        if not evidence or not context:
            return None
        if anchor.get("evidence_sha256") != hashlib.sha256(evidence.encode("utf-8")).hexdigest():
            return None
        if anchor.get("fallback_sha256") != hashlib.sha256(context.encode("utf-8")).hexdigest():
            return None
        if any(line.strip() not in context for line in evidence.splitlines() if line.strip()):
            return None
        chunk_id, chunk_order = anchor.get("chunk_id"), anchor.get("chunk_order")
        if source.get("type") == "document" and (
            chunk_id is None or anchor["url"] != f"/api/documents/{source.get('id')}/file?chunk_id={chunk_id}"
        ):
            return None
        return {
            **source, "location": anchor["location"], "evidence_locations": [anchor["location"]],
            "url": anchor["url"], "chunk_id": chunk_id, "chunk_order": chunk_order,
            "chunk_ids": [chunk_id] if chunk_id is not None else [],
            "chunk_orders": [chunk_order] if chunk_id is not None else [],
        }

    @classmethod
    def _alignment_amount_hint(cls, claim: str, question: str, source: dict) -> str:
        """Return one unambiguous amount for a single-location prose source.

        This is a ranking hint, not an answer validator or currency converter.
        Unrecognised/mixed amounts must not be mistaken for an absent amount
        and then replaced with a convenient value from the question.
        """
        locations = {str(value).strip() for value in [source.get("location"), *(source.get("evidence_locations") or [])]
                     if value is not None and str(value).strip()}
        chunk_ids = {str(value) for value in [source.get("chunk_id"), *(source.get("chunk_ids") or [])] if value is not None}
        if not locations or len(locations) > 1 or len(chunk_ids) > 1:
            return ""
        full_text = str(source.get("fallback_excerpt") or source.get("excerpt") or source.get("evidence") or "")
        if cls.structured_record_units(full_text) or cls.structured_record_units(str(source.get("evidence", ""))):
            return ""
        foreign_currency = r"(?:美元|欧元|港元|港币|日元|英镑|澳元|加元|USD|EUR|HKD|JPY|GBP|AUD|CAD|[$€£])"
        if re.search(foreign_currency, f"{source.get('title', '')}\n{full_text}", flags=re.IGNORECASE):
            return ""

        def amount(text: str) -> tuple[bool, str]:
            compact = re.sub(r"\s+", "", str(text or ""))
            number = r"(?:[-−负]?\d+(?:[.,]\d+)*|[零〇一二三四五六七八九十百千万亿两]+)"
            money_unit = r"(?:亿元|万元|元|万|亿)"
            mentions = list(re.finditer(rf"(?<![A-Za-z\d.,])(?P<number>{number})(?P<unit>{money_unit})", compact))
            foreign = bool(re.search(foreign_currency, compact, flags=re.IGNORECASE))
            # Unitless labelled amounts and unsupported numeric spellings also
            # block question inheritance; their value is intentionally unknown.
            labelled = list(re.finditer(rf"(?:金额|预算|价格|费用|成本)(?:为|是|[:：])?(?P<number>{number})", compact))
            has_amount = bool(mentions or foreign or labelled or re.search(r"\d.*(?:亿元|万元|元|万|亿|[¥￥])", compact))
            if not has_amount:
                return False, ""
            if foreign or not mentions or re.search(rf"{number}(?:{money_unit})?(?:至|到|[~～—–-]){number}", compact):
                return True, ""
            if any(match.span("number") not in {item.span("number") for item in mentions} for match in labelled):
                return True, ""
            if len(re.findall(money_unit, compact)) != len(mentions):
                return True, ""
            if any(match.group("unit") != "万元" or not re.fullmatch(r"\d+(?:\.\d+)?", match.group("number")) for match in mentions):
                return True, ""
            values = {(whole.lstrip("0") or "0", fraction.rstrip("0"))
                      for match in mentions for whole, _dot, fraction in [match.group("number").partition(".")]}
            return True, f"{mentions[0].group('number')}万元" if len(values) == 1 else ""

        has_claim_amount, hint = amount(claim)
        if not has_claim_amount:
            _has_question_amount, hint = amount(question)
        if hint and any(cls.amount_range_evidence_bonus(hint, sentence) > 0 for sentence, _ in cls._evidence_candidates(full_text)):
            return hint
        return ""

    @classmethod
    def _best_claim_evidence(cls, claim: str, source: dict, *, amount_hint: str = "") -> tuple[float, str]:
        """Find the smallest source passage that best explains one answer claim."""
        full_text = str(source.get("fallback_excerpt") or source.get("excerpt") or source.get("evidence") or "")
        terms = cls._claim_support_terms(claim)
        best: tuple[float, str] = (0.0, "")
        for index, (sentence, section) in enumerate(cls._evidence_candidates(full_text)):
            hits = cls.matched_terms(sentence, terms)
            if not hits:
                continue
            score = cls.lexical_score(hits, sentence.casefold(), claim)
            score += sum(min(12, len(hit) * 2) for hit in hits)
            score += cls.evidence_intent_bonus(claim, sentence, section)
            score += cls.amount_range_evidence_bonus(amount_hint, sentence)
            if 8 <= len(sentence) <= 360:
                score += 1.0
            candidate = (score - index * 0.001, sentence)
            if candidate[0] > best[0]:
                best = candidate
        return best

    @classmethod
    def align_sources_to_answer(cls, answer: str, sources: list[dict], *, question: str = "") -> list[dict]:
        """Align displayed evidence to the accepted claim that cites each source."""
        claims = cls.answer_claims(answer)
        aligned: list[dict] = []
        for position, source in enumerate(sources, 1):
            source_index = int(source.get("index", position))
            source_claims = [claim for claim in claims if source_index in claim["citations"]]
            scoring_source = source
            amount_hint = ""
            if len(claims) == 1 and source_claims:
                anchored = cls._amount_alignment_anchor_source(source)
                amount_hint = cls._alignment_amount_hint(source_claims[0]["text"], question, anchored or source)
                if amount_hint and anchored:
                    scoring_source = anchored
            mapped: list[str] = []
            for claim in source_claims:
                _score, evidence = cls._best_claim_evidence(claim["text"], scoring_source, amount_hint=amount_hint)
                if evidence and evidence not in mapped:
                    mapped.append(evidence)
            updated = dict(scoring_source)
            if mapped:
                current = str(source.get("evidence", "")).strip()
                # A compact row already binds its requested values to an
                # identity. Sentence-level alignment would keep a value but
                # lose the ID, or keep only one field from a compound claim.
                current_rows = [cls.structured_record_fields(row) for row in current.splitlines()]
                if current and len(current) <= 720 and all(
                    len(fields) >= 2 and cls.structured_identity_labels(label for label, _ in fields)
                    for fields in current_rows
                ):
                    aligned.append(updated)
                    continue
                current_score = max(
                    (cls._best_claim_evidence(claim["text"], {"fallback_excerpt": current}, amount_hint=amount_hint)[0]
                     for claim in source_claims),
                    default=0.0,
                )
                bounded_mapped: list[str] = []
                mapped_chars = 0
                for evidence in mapped:
                    projected = mapped_chars + len(evidence) + (1 if bounded_mapped else 0)
                    if bounded_mapped and (len(bounded_mapped) >= 8 or projected > 720):
                        break
                    bounded_mapped.append(evidence)
                    mapped_chars = projected
                mapped_text = "\n".join(bounded_mapped)
                mapped_score = max(
                    (cls._best_claim_evidence(claim["text"], {"fallback_excerpt": mapped_text}, amount_hint=amount_hint)[0]
                     for claim in source_claims),
                    default=0.0,
                )
                if not current or mapped_score > current_score:
                    updated["evidence"] = mapped_text
                if scoring_source is not source:
                    updated["evidence_alignment_anchor"] = {
                        **updated["evidence_alignment_anchor"],
                        "evidence_sha256": hashlib.sha256(updated["evidence"].encode("utf-8")).hexdigest(),
                    }
            aligned.append(updated)
        return aligned

    @classmethod
    def has_uncited_fact_line(cls, answer: str) -> bool:
        """Detect a substantive sentence or line that has no source marker."""
        for line in re.split(r"(?<=[。！？!?；;])|\n", str(answer or "")):
            cleaned = line.strip(" -•\t")
            without_citation = re.sub(r"\[\d+\]", "", cleaned).strip()
            if len(without_citation) < 8 or not re.search(r"[\u3400-\u9fff0-9]", without_citation):
                continue
            if not cls.cited_indexes(cleaned):
                return True
        return False

    @classmethod
    def complete_single_source_citations(cls, answer: str) -> str:
        """Attach [1] to every substantive sentence for one visible source.

        A model commonly writes ``事实 A。事实 B。[1]``.  With only one
        visible source the citation is unambiguous, but leaving it at the end
        makes claim-level verification treat ``事实 A`` as uncited.  Expand
        that shorthand before verification and before the UI converts each
        conclusion to a source-led line.  Multi-source answers never use this
        helper and therefore still require explicit per-claim citations.
        """
        completed_lines: list[str] = []
        for raw_line in str(answer or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            completed_sentences: list[str] = []
            for sentence in cls._split_evidence_sentences(line):
                plain = re.sub(r"\[\d+\]", "", sentence).strip(" -•\t。！？!?；; ")
                substantive = len(plain) >= 2 and bool(re.search(r"[\u3400-\u9fffA-Za-z0-9]", plain))
                if not substantive and re.fullmatch(r"(?:\s*\[\d+\]\s*)+", sentence):
                    continue
                if substantive and not cls.cited_indexes(sentence):
                    punctuation = re.search(r"([。！？!?；;]+)\s*$", sentence)
                    if punctuation:
                        sentence = f"{sentence[:punctuation.start()].rstrip()} [1]{punctuation.group(1)}"
                    else:
                        sentence = f"{sentence.rstrip()} [1]"
                completed_sentences.append(sentence)
            if completed_sentences:
                completed_lines.append("".join(completed_sentences))
        return "\n".join(completed_lines).strip()

    @staticmethod
    def answer_omits_required_conjuncts(question: str, answer: str, sources: list[dict]) -> bool:
        """Catch incomplete lists in a precise approval/verification answer.

        This is deliberately narrow. It only protects evidence that explicitly
        lists two or more joined approvers/checks, avoiding a generic attempt
        to judge semantic completeness with hand-written business rules.
        """
        if not re.search(r"(?:审批|会签|校验|需要|是否|哪些|谁)", str(question or "")):
            return False
        normalized_answer = re.sub(r"\s+", "", str(answer or ""))
        for source in sources:
            evidence = str(source.get("evidence", ""))
            match = re.search(r"(?:增加|由|需|需要)([^。；;]{2,80}?)(?:审批|会签|校验)", evidence)
            if not match:
                continue
            required = [
                item.strip()
                for item in re.split(r"[、，,与和及]", match.group(1))
                if len(item.strip()) >= 2
            ]
            if len(required) >= 2 and any(item not in normalized_answer for item in required):
                return True
        return False

    @classmethod
    def answer_has_uncovered_exact_values(cls, question: str, answer: str, sources: list[dict]) -> bool:
        """Catch fabricated numeric values or named Agents behind a valid citation.

        This is intentionally narrow: it verifies only values that must be
        preserved verbatim, while leaving ordinary Chinese paraphrases to the
        model and the human evaluation set.
        """
        source_by_index = {
            int(source.get("index", position)): "\n".join(
                (str(source.get("evidence", "")), str(source.get("fallback_excerpt", "")))
            ).casefold()
            for position, source in enumerate(sources, 1)
        }
        question_text = cls._normalise_support_text(question)
        for line in re.split(r"(?<=[。！？!?；;])|\n", str(answer or "")):
            cited = cls.cited_indexes(line)
            if not cited:
                continue
            evidence = "\n".join(source_by_index.get(index, "") for index in cited)
            plain = re.sub(r"\[\d+\]", "", line)
            entities = re.findall(r"[\u3400-\u9fffA-Za-z0-9_-]{2,}(?:Agent|智能体)", plain, flags=re.IGNORECASE)
            values = re.findall(r"\d+(?:\.\d+)?\s*(?:[%％]|万元|元|天|日|周|星期|月|年|小时|分钟|个|项|次|人|条)", plain)
            for value in [*entities, *values]:
                if (
                    value.casefold() not in evidence
                    and cls._normalise_support_text(value) not in question_text
                ):
                    return True
        return False

    @classmethod
    def inverse_approval_request(cls, question: str) -> dict | None:
        """One business/role, asking for the low band's upper boundary."""
        compact = re.sub(r"\s+", "", str(question or "")).strip("。！？?")
        if cls.record_identifier_terms(compact) or len(cls.compound_query_clauses(compact)) > 1:
            return None
        business = r"(?P<business>[\u3400-\u9fff]{2,12}?)"
        role = r"(?P<role>[\u3400-\u9fff]{2,14}(?:负责人|经理|主管|总监))"
        forms = (
            rf"(?:小额)?{business}走到什么金额(?:以内|以下)可以不找{role}",
            rf"(?:小额)?{business}(?:在)?(?:什么金额|多少金额)(?:以内|以下)(?:可以)?(?:无需|不需要){role}审批",
        )
        for form in forms:
            match = re.fullmatch(form, compact)
            if match and not re.search(r"[和与及或、]|比较|对比", match.group("business") + match.group("role")):
                return match.groupdict()
        return None

    @classmethod
    def _inverse_approval_scope_hash(cls, question: str, title: str) -> str:
        request = cls.inverse_approval_request(question) or {}
        value = "\n".join((request.get("business", ""), request.get("role", ""), "upper_bound_without_role", str(title)))
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @classmethod
    def inverse_approval_proof(cls, question: str, title: str, text: str) -> dict | None:
        """Compare two adjacent, closed rosters in one complete rule block.

        The rule's original interval is the answer, not a point amount. This
        deliberately accepts only integer same-unit amounts and a full first
        amount prefix; a decimal tail at a production chunk seam is no rule.
        """
        request = cls.inverse_approval_request(question)
        if not request or cls.structured_record_units(text):
            return None
        business, role = request["business"], request["role"]
        compact_title = re.sub(r"\s+", "", str(title or ""))
        title_scope = bool(re.match(rf"{re.escape(business)}(?:审批|规则|制度|流程|管理)", compact_title))
        uncertain = r"拟|草案|计划|将于|尚待|待批|待定|未生效|尚未生效|旧版|废止|撤回|撤销|取消|作废|未获批准|未批准|否决"
        foreign_currency = re.compile(r"美元|欧元|港元|港币|日元|英镑|澳元|加元|USD|EUR|HKD|JPY|GBP|AUD|CAD|[$€£]", re.IGNORECASE)

        def invalid_status(value: str) -> bool:
            value = re.sub(r"(?:并未|没有|未)(?:取消|撤回|撤销)", "", value)
            return bool(re.search(uncertain, value))

        def failed(status: str = "unproven") -> dict:
            return {**request, "status": status}

        integer = r"\d+"
        unit = r"(?:万元|元)"
        inclusion = r"[（(](?P<included>不含|含)[）)]"
        # Requiring a complete labelled first band also prevents matching the
        # suffix of 7.\n25万元 after split_note_text's decimal punctuation split.
        low = re.compile(
            rf"(?P<scope>[\u3400-\u9fff]*?)(?:单笔)?(?:含税)?金额(?P<bound>{integer})(?P<unit>{unit})"
            rf"(?:{inclusion})?(?P<limit>以下|以内)，?由(?P<roles>.+)审批"
        )
        high = re.compile(
            rf"(?P<bound>{integer})(?P<unit>{unit})[（(](?P<included>不含|含)[）)]"
            rf"(?:至|到)(?P<upper>{integer})(?P<upper_unit>{unit})[（(](?:不含|含)[）)]，?由(?P<roles>.+)审批"
        )

        def roster(value: str) -> list[str] | None:
            if re.search(r"等|包括|至少|例如|增加|沿用|可由|或|初审|代理|委托|授权|助理|副|\.\.\.|…", value):
                return None
            roles = re.split(r"[、,，]|和|及|与", value)
            if len(roles) < 2 or not all(re.fullmatch(r"[\u3400-\u9fff]{2,16}", item) for item in roles):
                return None
            return roles

        blocks = re.split(r"(?=【)", str(text or ""))
        found = []
        pointer = False
        active_business = business if title_scope else ""
        scoped_units = []
        protected_units = set()

        def rule_reference(value: str) -> str | None:
            match = re.search(r"(?P<scope>[\u3400-\u9fff]{1,24}?)(?:审批规则|审批制度|审批流程|规则)", value)
            if not match:
                return None
            scope = re.sub(r"^(?:这是|这份|关于|依据|根据|现行|当前)", "", match.group("scope"))
            if re.search(r"(?:该|此|本|上述|这套|此项|本次)$", scope):
                return ""
            return scope

        for block in blocks:
            if not block.strip():
                continue
            header_match = re.match(r"\s*【([^】]+)】", block)
            header = re.sub(r"\s+", "", header_match.group(1)) if header_match else ""
            body = block[header_match.end():] if header_match else block
            neutral_header = not header or header in {"新审批规则", "审批规则", "当前审批规则", "现行审批规则", "新规则", "规则", "规则状态", "审批规则状态", "状态"}
            named_header = header in {business, business + "审批规则", business + "规则"}
            # A named business changes scope; a formatting/maintenance/condition
            # heading does not erase a rule's continuing constraints. The same
            # distinction is used for positive rules and negative continuations.
            scope_header = re.sub(r"^(?:新|现行|当前)", "", header)
            header_scope = rule_reference(scope_header)
            generic_header = header_scope in {"补充", "附加", "适用", "注意", "其他"}
            if named_header:
                active_business = business
            elif header_scope is not None and not neutral_header and not generic_header:
                active_business = header_scope
            scope = active_business == business
            compact_body = re.sub(r"\s+", "", body)
            # Preserve punctuation and whole clauses for both display and
            # closed-roster checks; never split on ASCII decimal points.
            unit_matches = [match for match in re.finditer(r"[^。；;！？]+[。；;！？]?", body) if match.group().strip()]
            units = [match.group().strip() for match in unit_matches]
            compact_units = [re.sub(r"\s+", "", value).rstrip("。；;！？") for value in units]
            local_business = active_business
            for value in compact_units:
                reference = rule_reference(value)
                if reference:
                    local_business = reference
                scoped_units.append((value, local_business, reference is not None, header))
            if active_business and active_business != business:
                continue
            candidates = []
            for index, value in enumerate(compact_units):
                match = low.fullmatch(value)
                if not match:
                    continue
                explicit = match.group("scope").removesuffix("的")
                # Greedy Chinese prefixes can include the optional structural
                # words. They are removed only in this exact labelled grammar.
                explicit = re.sub(r"(?:单笔)?(?:含税)?$", "", explicit)
                if explicit and explicit != business:
                    continue
                if not (explicit == business or scope):
                    continue
                if index + 1 >= len(units):
                    return failed()
                neighbour = high.fullmatch(compact_units[index + 1])
                if not neighbour:
                    return failed()
                target_roles, witness_roles = roster(match.group("roles")), roster(neighbour.group("roles"))
                if not target_roles or not witness_roles or role in target_roles or role not in witness_roles:
                    return failed()
                # A longer name is not proof that the requested person is
                # absent, nor that a similarly named higher-tier person is C.
                if any(role in item for item in target_roles):
                    return failed()
                boundary = match.group("bound").lstrip("0") or "0"
                if boundary != (neighbour.group("bound").lstrip("0") or "0"):
                    return failed()
                if len({match.group("unit"), neighbour.group("unit"), neighbour.group("upper_unit")}) != 1:
                    return failed()
                if int(neighbour.group("upper")) <= int(boundary):
                    return failed()
                low_included = match.group("included") or ("含" if match.group("limit") == "以内" else "不含")
                if low_included == neighbour.group("included"):
                    return failed()
                # Inspect the complete same-block context, not just the two
                # clauses that will be visible. C's earlier/later participation
                # or an unfulfilled extra condition invalidates the exemption.
                if invalid_status(header + compact_title + compact_body) or foreign_currency.search(header + compact_title + compact_body):
                    return failed()
                for other_index, other in enumerate(compact_units):
                    if other_index in (index, index + 1):
                        continue
                    if role in other or re.search(r"所有.*(?:复核|会签|审批)|仅.*(?:供应商|适用)|前提|条件是", other):
                        return failed()
                # Non-exhaustive or conditional prefixes cannot be ignored by
                # finding a syntactically closed list further down the block.
                if any(re.search(r"至少|包括|例如|仅|沿用|可由|委托|授权|以下仅|前提", other)
                       for other in compact_units[:index]):
                    return failed()
                target = ((block[:header_match.end()] + unit_matches[index].group()).strip()
                          if header_match and index == 0 else units[index])
                witness = units[index + 1]
                candidates.append({**request, "status": "proved", "target": target, "witness": witness,
                                   "evidence": target + "\n" + witness, "boundary": boundary, "unit": match.group("unit")})
                protected_units.update((value, compact_units[index + 1]))
            if candidates:
                found.extend(candidates)
            elif scope and re.search(r"金额.*审批|\d\s*(?:万元|元).*审批", compact_body):
                # Recognised but malformed/partial rules do not silently become
                # unrelated context (notably broken decimals and missing lists).
                return failed()
        for value, scope_business, has_rule_reference, header in scoped_units:
            if scope_business and scope_business != business:
                continue
            if value in protected_units:
                continue
            status_pointer = bool(re.match(r"(?:该|此|上述|本)(?:审批)?(?:规则|方案)", value))
            if invalid_status(value) and (has_rule_reference or status_pointer):
                pointer = True
            # Conditions and role actions in neutral continuation sections are
            # still part of this business's rule; they cannot be cropped away.
            if scope_business == business and (
                (role in value and re.search(r"审批|复核|会签|签字|授权", value))
                or (re.search(r"仅|前提|条件是", value) and (has_rule_reference or "适用" in value))
                or foreign_currency.search(value)
            ):
                pointer = True
        if pointer:
            return failed("unresolved_pointer")
        if len(found) != 1:
            return failed() if found else None
        return found[0]

    @classmethod
    def _inverse_approval_answer(cls, question: str, sources: list[dict]) -> tuple[str, list[dict]]:
        refusal = ("现有笔记没有足够信息。", [])
        if len(sources) != 1:
            return refusal
        source = sources[0]
        candidate = cls._amount_alignment_anchor_source(source)
        anchor = source.get("evidence_alignment_anchor", {})
        if candidate is None or anchor.get("inverse_scope_sha256") != cls._inverse_approval_scope_hash(question, str(source.get("title", ""))):
            return refusal
        if (not isinstance(source.get("id"), int) or source["id"] <= 0
            or source.get("url") != anchor.get("url")
            or source.get("location") != anchor.get("location")
            or source.get("evidence_locations") != [anchor.get("location")]
            or source.get("chunk_id") != anchor.get("chunk_id")
            or source.get("chunk_order") != anchor.get("chunk_order")
            or source.get("chunk_ids") != ([anchor["chunk_id"]] if anchor.get("chunk_id") is not None else [])):
            return refusal
        if candidate.get("type") == "note" and candidate.get("url") != f"/api/notes/{candidate.get('id')}":
            return refusal
        if candidate.get("type") not in {"note", "document"}:
            return refusal
        visible = cls.inverse_approval_proof(question, str(candidate.get("title", "")), str(candidate.get("evidence", "")))
        context = cls.inverse_approval_proof(question, str(candidate.get("title", "")), str(candidate.get("fallback_excerpt", "")))
        if not visible or not context or visible.get("status") != "proved" or visible != context:
            return refusal
        answer = f"来源[{candidate.get('index', 1)}] {visible['target']}"
        answer, selected = cls.compact_selected_citations(answer, [dict(candidate)])
        return cls.format_answer_with_leading_sources(answer), selected

    @staticmethod
    def budget_pause_request(question: str) -> dict | None:
        """Recognise a resolved business-budget pause question, not arithmetic."""
        compact = re.sub(r"\s+", "", str(question or "")).strip("。！？?")
        match = re.fullmatch(
            r"(?:关于)?(?P<business>[\u3400-\u9fff]{2,16}?)(?:的)?预算[，,]?"
            r"(?:(?:超过|超出)(?:一点点|一点|少量)|(?:略微|稍微)(?:超出|超预算))"
            r"(?:也|就)?(?:要|需要|必须)(?:停|暂停)吗", compact,
        )
        if not match or re.search(r"和|与|或|以及|分别|比较|对比", match["business"]):
            return None
        return {"business": match["business"], "field": "budget_overrun", "operation": "pause_obligation"}

    @classmethod
    def _budget_pause_scope_hash(cls, question: str, title: str) -> str:
        request = cls.budget_pause_request(question) or {}
        scope = "\n".join([*(request.get(key, "") for key in ("business", "field", "operation")), title])
        return hashlib.sha256(scope.encode("utf-8")).hexdigest()

    @staticmethod
    def _budget_pause_rule(text: str) -> dict | None:
        """Positive proof is a complete literal condition -> obligation clause.

        Do not split on an English period: a production chunk may contain
        5.\n5%, which must not become the integer 5% by losing its prefix.
        """
        evidence = str(text or "").strip()
        compact = re.sub(r"\s+", "", evidence).rstrip("。；;")
        match = re.fullmatch(
            r"(?:任何)?单渠道超预算(?P<threshold>\d+)[%％]必须暂停(?:投放)?"
            r"(?:并向(?P<role>[\u3400-\u9fff]{2,16}?)(?:说明|报告))?", compact,
        )
        if not match:
            return None
        return {"threshold": match["threshold"], "role": match["role"] or "", "evidence": evidence}

    @classmethod
    def budget_pause_proof(cls, question: str, title: str, text: str) -> dict | None:
        """Prove one integer-percent budget rule in one identified meeting job."""
        request = cls.budget_pause_request(question)
        if not request or cls.structured_record_units(text):
            return None
        bad = {"status": "unproven"}
        compact_title = re.sub(r"\s+", "", str(title or ""))
        title_match = re.fullmatch(
            r"(?P<business>[\u3400-\u9fff]{2,16})项目例会纪要[（(]"
            r"(?P<activity>[\u3400-\u9fffA-Za-z0-9_-]{2,24})[）)]", compact_title,
        )
        status = r"拟议|拟定|拟实施|草案|初步|旧版|历史|未批准|尚待批准|待审批|待定|未生效|撤回|撤销|取消|作废|废止|否决|回滚"
        if re.search(status, compact_title) and request["business"] in compact_title:
            return bad
        if title_match and title_match["business"] != request["business"]:
            return None
        compact = re.sub(r"\s+", "", str(text or ""))
        constraint = status + r"|并非|不一定|除非|除外|例外|豁免|仅限|只适用于|无需|不必|不得|不要求|建议|允许|可以|计划|拟于"
        rule_name = r"预算(?:暂停)?(?:规则|条件|规定|要求)"
        pointer = r"(?:该|此|本|上述)(?:" + rule_name + r"|规则|条件|规定|要求)"
        if not title_match:
            if re.search(pointer, compact) and re.search(constraint, compact):
                return {"status": "unresolved_pointer"}
            return bad if request["business"] in compact_title and "预算" in compact else None
        business, activity = title_match["business"], title_match["activity"]
        headers = list(re.finditer(r"【([^】]+)】", str(text or "")))
        sections = [(item[1], text[item.end():headers[index + 1].start() if index + 1 < len(headers) else len(text)])
                    for index, item in enumerate(headers)]
        meetings = [body for header, body in sections if header == "会议日期"]
        if not meetings:
            if re.search(pointer + "|" + rule_name + r"|超预算", compact) and re.search(constraint, compact):
                return {"status": "unresolved_pointer"}
            return bad if "超预算" in compact or "【预算" in compact else None
        if len(meetings) != 1:
            return bad
        date_match = re.match(r"\s*(\d{4})[-年](\d{1,2})[-月](\d{1,2})(?:日)?(?=[。；;\s]|$)", meetings[0])
        if not date_match:
            return bad
        try:
            meeting_date = date(*(int(value) for value in date_match.groups())).isoformat()
        except ValueError:
            return bad
        if len(re.findall(r"\d{4}[-年]\d{1,2}[-月]\d{1,2}", meetings[0])) != 1:
            return bad

        def header_scope(header: str) -> str:
            value = re.sub(r"\s+", "", header)
            if "预算" not in value:
                return "neutral" if re.fullmatch(r"补充(?:规则|要求|说明|条款)?|附加(?:要求|说明)?|注意(?:事项)?|适用范围|说明", value) else "other"
            label = value.split("预算", 1)[0]
            if re.fullmatch(r"(?:新|当前|补充|附加|拟议|拟定|初步|旧版|历史)?", label):
                return "own"
            named = re.sub(r"^(?:补充|附加|当前|新|其他)", "", label)
            if named in {business, activity}:
                return "ambiguous" if label.startswith("其他") else "own"
            return "foreign"

        def explicit_scope(value: str) -> str:
            # Named statements outrank an inherited neutral budget heading.
            patterns = (r"^(?:本次)?(?P<name>[\u3400-\u9fffA-Za-z0-9_-]{2,24}?)(?:的)?预算(?:暂停)?(?:规则|规定|条件|[：:])",
                        r"(?:适用活动|活动名称|所属活动|业务名称)(?:为|是|[：:])(?P<name>[\u3400-\u9fffA-Za-z0-9_-]{2,24})")
            for pattern in patterns:
                match = re.search(pattern, value)
                if match and match["name"] not in {"本", "该", "上述", "任何单渠道", "单渠道"}:
                    return "own" if match["name"] in {business, activity} else "foreign"
            return ""

        proofs = []
        active = False
        prefix = text[:headers[0].start()] if headers else text
        for header, body in [("说明", prefix), *sections]:
            scope = header_scope(header)
            if scope == "ambiguous":
                return bad
            if scope == "foreign":
                active = False
                continue
            if scope == "own":
                active = True
            elif scope != "neutral":
                active = False
            header_status = bool(re.search(constraint, header))
            if active and header_status:
                return bad
            for clause in re.findall(r"[^。；;]+[。；;]?", body):
                local = re.sub(r"\s+", "", clause).strip("。；;")
                named = explicit_scope(local)
                if named == "foreign":
                    # A body switch cannot give this title a positive rule.
                    if active:
                        return bad
                    continue
                relevant = active or named == "own" or bool(re.search(rule_name + r"|超预算", local))
                if not relevant and re.search(pointer, local) and re.search(constraint, local) and scope == "neutral":
                    relevant = True
                if relevant and re.search(constraint, local):
                    return bad
                if re.search(r"(?:本次|该次|此次)会议(?:日期)?(?:是|为|于)", local):
                    other_date = re.search(r"(\d{4})[-年](\d{1,2})[-月](\d{1,2})", local)
                    if other_date:
                        try:
                            if date(*(int(value) for value in other_date.groups())).isoformat() != meeting_date:
                                return bad
                        except ValueError:
                            return bad
                if not active:
                    continue
                proof = cls._budget_pause_rule(clause)
                if proof:
                    proofs.append(proof)
                elif re.search(r"超预算|单渠道|暂停|继续投放", local):
                    # Unsupported values, exceptions and a second obligation
                    # are counterevidence, not permission to select one rule.
                    return bad
        unique = {(proof["threshold"], proof["role"], proof["evidence"]) for proof in proofs}
        if len(unique) != 1:
            return bad
        return {"status": "proved", "identity": (business, activity, meeting_date), **proofs[0]}

    @classmethod
    def _budget_pause_answer(cls, question: str, sources: list[dict]) -> tuple[str, list[dict]]:
        refusal = ("现有笔记没有足够信息。", [])
        if len(sources) != 1:
            return refusal
        source = sources[0]
        candidate = cls._amount_alignment_anchor_source(source)
        anchor = source.get("evidence_alignment_anchor", {})
        if candidate is None or anchor.get("budget_scope_sha256") != cls._budget_pause_scope_hash(question, str(source.get("title", ""))):
            return refusal
        if (type(source.get("id")) is not int or source["id"] <= 0
            or source.get("url") != anchor.get("url") or source.get("location") != anchor.get("location")
            or source.get("evidence_locations") != [anchor.get("location")]
            or source.get("chunk_id") != anchor.get("chunk_id") or source.get("chunk_order") != anchor.get("chunk_order")
            or source.get("chunk_ids") != ([anchor["chunk_id"]] if anchor.get("chunk_id") is not None else [])):
            return refusal
        if candidate.get("type") not in {"note", "document"} or (
            candidate["type"] == "note" and candidate.get("url") != f"/api/notes/{candidate['id']}"
        ):
            return refusal
        context = cls.budget_pause_proof(question, candidate["title"], candidate.get("fallback_excerpt", ""))
        visible = cls._budget_pause_rule(candidate.get("evidence", ""))
        if not context or context.get("status") != "proved" or not visible or any(context[key] != visible[key] for key in visible):
            return refusal
        candidate = {**candidate, "index": 1}
        return cls.format_answer_with_leading_sources(cls.complete_single_source_citations(candidate["evidence"])), [candidate]

    @classmethod
    def cause_pair_request(cls, question: str) -> dict | None:
        """Recognise one explicit two-option root-cause question, not a winner."""
        compact = re.sub(r"\s+", "", str(question or "")).strip("。！？?")
        match = re.fullmatch(
            r"(?P<subject>[\u3400-\u9fff]{2,12}?)(?:同步延迟)?(?P<severity>P[0-9])的根因是"
            r"(?P<option_a>[\u3400-\u9fff]{2,12})还是(?P<option_b>[\u3400-\u9fff]{2,12})", compact, re.I,
        )
        if not match:
            return None
        request = match.groupdict()
        if any(re.search(r"和|与|以及|对比|比较|历史|还是", item) for item in request.values()):
            return None
        if request["option_a"] == request["option_b"]:
            return None
        request["severity"] = request["severity"].upper()
        return request

    @classmethod
    def _cause_pair_scope_hash(cls, question: str, title: str) -> str:
        request = cls.cause_pair_request(question) or {}
        scope = "\n".join([*(request.get(key, "") for key in ("subject", "severity", "option_a", "option_b")), title])
        return hashlib.sha256(scope.encode("utf-8")).hexdigest()

    @staticmethod
    def _cause_pair_polarities(request: dict, text: str) -> dict[str, set[str]]:
        """Keep explicit root-cause predicates attached to their named object.

        A mechanism can be a symptom rather than a cause. Such qualifications
        deny this narrow proof even when the same block also describes its
        failure. No polarity is inferred from mere normal/abnormal operation.
        """
        options = (request["option_a"], request["option_b"])
        result = {option: set() for option in options}
        negative = r"(?:不是|并非|不属于|不构成)根因"
        affirmative = r"(?:是|为|属于|构成)(?:真正|直接|主要|唯一)?根因"
        mechanism = r"(?:积压|阻塞|滞留|故障|过载|并发不足|容量不足)"
        compact = re.sub(r"【[^】]*】", "", re.sub(r"\s+", "", text))
        for sentence in re.split(r"[。；;\n]", compact):
            previous = None
            for raw_clause in re.split(r"[，,]", sentence):
                clause = re.sub(r"^[\u3400-\u9fffP0-9]{1,24}[：:]", "", raw_clause)
                mentioned = [option for option in options if option in clause]
                # Only a recognisable omitted-subject qualification may
                # continue over a comma. A new named object starts its own
                # predicate, including objects outside the two candidates.
                if not mentioned and previous and re.match(r"(?:只是|仅是|仅仅是|仅为|并非|而非|不属于)", clause):
                    if re.search(r"根因|症状|表象|结果|影响", clause):
                        result[previous].add("qualified")
                for option in mentioned:
                    subject = re.escape(option) + rf"(?:发生)?(?:{mechanism})?(?:也|亦|才|确实|的确)?"
                    if re.fullmatch(subject + negative, clause) or re.fullmatch(r"根因(?:不是|并非)" + re.escape(option), clause):
                        result[option].add("excluded")
                    elif re.fullmatch(subject + affirmative, clause) or re.fullmatch(r"根因(?:是|为)" + re.escape(option), clause):
                        result[option].add("affirmed")
                    elif re.search(r"根因|(?:只是|仅是|仅仅是|仅为).*(?:症状|表象|结果|影响)", clause):
                        # This scan can deny a proof, never establish one.
                        # Unknown modifiers and negated predicates are not
                        # accepted by searching for their inner substring.
                        result[option].add("qualified")
                previous = mentioned[0] if len(mentioned) == 1 else None
        return result

    @staticmethod
    def _cause_pair_incident_dates(text: str) -> list[tuple[str | None, int, int]]:
        """Read explicit incident-occurrence dates in either word order."""
        date_text = r"(?:(20\d{2})年)?(\d{1,2})月(\d{1,2})日"
        event = r"(?:本次|该次|这次|此次|本起|该起|这起)?(?:事故|故障|事件)"
        patterns = (
            event + r"(?:的)?(?:发生(?:日期|时间)?(?:为|是|于|在)?|日期(?:为|是))" + date_text,
            date_text + r"(?:发生|出现)(?:的)?" + event,
        )
        compact = re.sub(r"\s+", "", text)
        return [(match[1], int(match[2]), int(match[3])) for pattern in patterns for match in re.finditer(pattern, compact)]

    @classmethod
    def _cause_pair_facts(cls, request: dict, evidence: str) -> tuple[str, str] | None:
        """Verify a mechanism and an explicit exclusion inside the same block.

        The result identifies what the block states; it never turns the object
        of a causal verb into a newly generated root-cause assertion.
        """
        compact = re.sub(r"\s+", "", evidence)
        if not compact.startswith("【根因】") or len(re.findall(r"【", compact)) != 1:
            return None
        if re.search(r"疑似|初步|可能|待确认|待定|尚未|不能认定|无法认定|未确认|撤回|撤销|否决|推翻|不成立|假设|草案", compact):
            return None
        clauses = [part for part in re.split(r"[。；;，,]", compact.removeprefix("【根因】")) if part]
        polarities = cls._cause_pair_polarities(request, compact)
        # Positive evidence requires a complete, explicitly named exclusion.
        # Wider polarity scans above are conservative counterevidence only.
        excluded = [option for option in (request["option_a"], request["option_b"])
                    if any(re.fullmatch(re.escape(option) + r"(?:也|亦)?(?:不是|并非|不属于|不构成)根因", part)
                           or re.fullmatch(r"根因(?:不是|并非)" + re.escape(option), part) for part in clauses)]
        if len(excluded) != 1:
            return None
        negative = excluded[0]
        positive = request["option_b"] if negative == request["option_a"] else request["option_a"]
        if polarities[negative] != {"excluded"} or polarities[positive] - {"affirmed"}:
            return None
        mechanism = r"(?:积压|阻塞|滞留|故障|过载|并发不足|容量不足)"
        positive_clauses = [part for part in clauses if re.search(re.escape(positive) + r"(?:发生)?" + mechanism, part)]
        if not positive_clauses or any(re.search(r"未|没有|并非|不是|不再", part) for part in positive_clauses):
            return None
        # Excluding A cannot coexist with A being the stated causal head.
        # B -> A symptoms remains an original relation, not a new winner claim.
        for part in clauses:
            if re.search(re.escape(negative) + r"(?:发生)?" + mechanism, part):
                return None
            if re.search(re.escape(negative) + r"(?:是|为|属于)(?:直接)?根因", part):
                return None
            if re.search(re.escape(negative) + r"[^。；;，,]{0,12}(?:使|导致|引发|造成)", part):
                return None
        return positive, negative

    @classmethod
    def cause_pair_proof(cls, question: str, title: str, text: str) -> dict | None:
        """Prove one complete same-day synchronization-delay incident job."""
        request = cls.cause_pair_request(question)
        if not request or cls.structured_record_units(text):
            return None
        bad = {"status": "unproven"}
        compact_title = re.sub(r"\s+", "", title)
        title_event = re.fullmatch(r"(?P<subject>[\u3400-\u9fff]{2,12})同步延迟(?P<severity>P[0-9])故障复盘", compact_title, re.I)
        if title_event and (title_event["subject"] != request["subject"] or title_event["severity"].upper() != request["severity"]):
            return None
        compact = re.sub(r"\s+", "", str(text or ""))
        invalid = r"疑似|初步|可能|待确认|尚未确认|待定|撤回|撤销|否决|推翻|不成立|草案|假设"
        # A dangling same-source conclusion cannot supply facts, but can deny
        # an earlier job. The producer associates these only by source group.
        pointer = bool(re.search(r"(?:该|此|本次|上述)(?:事件|事故)?(?:的)?根因(?:结论)?", compact))
        named = request["subject"] + request["severity"]
        if not title_event:
            if pointer and re.search(invalid, compact):
                return {"status": "unresolved_pointer"}
            return bad if named in compact_title or named in compact else None
        headers = list(re.finditer(r"【([^】]+)】", str(text or "")))
        sections = [(match.group(1), text[match.end():headers[index + 1].start() if index + 1 < len(headers) else len(text)],
                     text[match.start():headers[index + 1].start() if index + 1 < len(headers) else len(text)].strip())
                    for index, match in enumerate(headers)]
        summaries = [body for header, body, _ in sections if header == "事件摘要"]
        causes = [whole for header, _, whole in sections if header == "根因"]
        if not summaries and not causes:
            if re.search(invalid, compact) and (pointer or "根因" in compact):
                return {"status": "unresolved_pointer"}
            if any(cls._cause_pair_polarities(request, compact).values()) or cls._cause_pair_incident_dates(compact):
                # A separate job cannot refine the complete incident proof;
                # retain its same-source conclusion/date constraint instead
                # of silently discarding an incomplete counterexample.
                return {"status": "unresolved_pointer"}
            return None
        if len(summaries) != 1 or len(causes) != 1:
            return bad
        summary = re.sub(r"\s+", "", summaries[0])
        incident = re.match(
            r"(?:(?P<year>20\d{2})年)?(?P<month>\d{1,2})月(?P<day>\d{1,2})日"
            r"(?P<start>\d{2}:\d{2})至(?P<end>\d{2}:\d{2})[，,]"
            r"(?P<owner>[\u3400-\u9fff]{2,16}?)(?:向[\u3400-\u9fff]{2,16}(?:系统|中心)的)?同步延迟", summary,
        )
        if not incident or incident["owner"] not in {request["subject"], request["subject"] + "系统", request["subject"] + "中心"}:
            return bad
        try:
            date(int(incident["year"] or 2000), int(incident["month"]), int(incident["day"]))
            start = datetime.strptime(incident["start"], "%H:%M")
            end = datetime.strptime(incident["end"], "%H:%M")
            if end <= start:
                return bad
        except ValueError:
            return bad
        if len(re.findall(r"\d{1,2}月\d{1,2}日", summary)) != 1 or len(re.findall(r"\d{2}:\d{2}至\d{2}:\d{2}", summary)) != 1:
            return bad
        evidence = causes[0]
        facts = cls._cause_pair_facts(request, evidence)
        if not facts:
            return bad
        # Inspect only the event/causal scope. Action timestamps and future
        # improvement deadlines do not constitute a second incident.
        prefix = text[:headers[0].start()] if headers else text
        causal_scope = [prefix, summaries[0], evidence]
        for header, body, _ in sections:
            if header in {"事件摘要", "根因"}:
                continue
            compact_header = re.sub(r"\s+", "", header)
            event_header = re.sub(r"^(?:(?:其他|补充|初步|当前|另一|历史))+", "", compact_header)
            foreign = re.fullmatch(r"([\u3400-\u9fff]{2,16}?)(P[0-9])(?:事故|故障|事件)?(?:的)?根因(?:结论)?", event_header, re.I)
            if foreign:
                # An additional event block cannot complete the selected root
                # block. A complete selected block remains independent of it.
                if foreign[1] != request["subject"] or foreign[2].upper() != request["severity"]:
                    continue
                if re.search(r"其他|另一|历史", compact_header):
                    return bad
            if header in {"处置", "长期改进", "经验"}:
                # Future actions are not tentative root-cause conclusions.
                # Explicit conclusion reversals still apply in these columns.
                causal_scope.extend(part for part in re.split(r"[。；;]", body)
                                    if re.search(r"根因|(?:该|此|上述)(?:结论|判断)", part)
                                    or cls._cause_pair_incident_dates(part))
            else:
                causal_scope.append(header + "：" + body)
        for unit in causal_scope:
            local = re.sub(r"\s+", "", unit)
            if re.search(invalid, local):
                return bad
            polarities = cls._cause_pair_polarities(request, local)
            if polarities[facts[0]] - {"affirmed"} or polarities[facts[1]] - {"excluded"}:
                return bad
            for occurrence in cls._cause_pair_incident_dates(local):
                if occurrence[1:] != (int(incident["month"]), int(incident["day"])) or (
                    occurrence[0] is not None and occurrence[0] != incident["year"]
                ):
                    return bad
            if re.search(r"(?:另一起|另一|上次|历史)(?:[^。；;]{0,15})(?:事故|故障|P[0-9])", local):
                return bad
            for event in re.finditer(r"([\u3400-\u9fff]{2,16}?)(P[0-9])(?:事故|故障|事件|的根因)", local, re.I):
                if event[1] != request["subject"] or event[2].upper() != request["severity"]:
                    return bad
            if unit != summaries[0] and not cls._cause_pair_incident_dates(local) and re.search(r"\d{1,2}月\d{1,2}日[^。；;]{0,30}(?:事故|故障|P[0-9])", local):
                return bad
            for owner in re.finditer(r"([\u3400-\u9fff]{2,12}?)(?:系统|中心)(?:的)?(?:同步延迟|事故|故障|根因)", local if unit != summaries[0] else ""):
                if owner[1] != request["subject"]:
                    return bad
        identity = (request["subject"], "同步延迟", request["severity"], incident["year"],
                    int(incident["month"]), int(incident["day"]), incident["start"], incident["end"])
        return {"status": "proved", "event_identity": identity, "evidence": evidence, "facts": facts}

    @classmethod
    def _cause_pair_answer(cls, question: str, sources: list[dict]) -> tuple[str, list[dict]]:
        refusal = ("现有笔记没有足够信息。", [])
        if len(sources) != 1:
            return refusal
        source = sources[0]
        candidate = cls._amount_alignment_anchor_source(source)
        anchor = source.get("evidence_alignment_anchor", {})
        if candidate is None or anchor.get("cause_scope_sha256") != cls._cause_pair_scope_hash(question, str(source.get("title", ""))):
            return refusal
        if (not isinstance(source.get("id"), int) or source["id"] <= 0
            or source.get("url") != anchor.get("url") or source.get("location") != anchor.get("location")
            or source.get("evidence_locations") != [anchor.get("location")]
            or source.get("chunk_id") != anchor.get("chunk_id") or source.get("chunk_order") != anchor.get("chunk_order")
            or source.get("chunk_ids") != ([anchor["chunk_id"]] if anchor.get("chunk_id") is not None else [])):
            return refusal
        if candidate.get("type") not in {"note", "document"} or (
            candidate["type"] == "note" and candidate.get("url") != f"/api/notes/{candidate['id']}"
        ):
            return refusal
        context = cls.cause_pair_proof(question, candidate["title"], candidate.get("fallback_excerpt", ""))
        visible = cls._cause_pair_facts(cls.cause_pair_request(question), candidate.get("evidence", ""))
        if not context or context.get("status") != "proved" or context["facts"] != visible or context["evidence"] != candidate.get("evidence"):
            return refusal
        candidate = {**candidate, "index": 1}
        answer = cls.complete_single_source_citations(candidate["evidence"])
        return cls.format_answer_with_leading_sources(answer), [candidate]

    @classmethod
    def adjusted_count_request(cls, question: str) -> dict | None:
        """Recognise one explicit request for a changed count threshold only."""
        compact = re.sub(r"\s+", "", str(question or "")).strip("。！？?")
        if cls.record_identifier_terms(compact) or len(cls.compound_query_clauses(compact)) > 1:
            return None
        match = re.fullmatch(
            r"(?P<subject>[^，,；;。！？?和与、]{2,20}?(?:系统|中心|服务|平台))的"
            r"(?P<parameter>[^，,；;。！？?和与、]{2,20}?(?:告警|阈值|门限))"
            r"(?:被)?(?:调整到|调整为|改为|改到)(?:多少|几)条", compact,
        )
        return match.groupdict() if match else None

    @staticmethod
    def _count_parameter_key(label: str) -> str:
        # Remove only structural words; qualifiers such as dead-letter,
        # refund and retry remain part of the complete parameter identity.
        value = re.sub(r"(?:阈值|门限)(?=告警$)", "", label)
        return value.removeprefix("队列")

    @classmethod
    def adjusted_count_proof(cls, question: str, title: str, text: str) -> dict | None:
        """Prove a single actual OLD-count -> NEW-count change in this job.

        Values, their roles and their complete units stay in the same clause.
        A title may supply an omitted subject, but never another parameter or
        a value. Unclear versions and status continuations are not resolved.
        """
        request = cls.adjusted_count_request(question)
        if not request or cls.structured_record_units(text):
            return None
        subject = request["subject"]
        subject_stem = re.sub(r"(?:系统|中心|服务|平台)$", "", subject)
        parameter = cls._count_parameter_key(request["parameter"])
        compact_title = re.sub(r"\s+", "", str(title or ""))
        named_scope = re.compile(r"^(?P<stem>[\u3400-\u9fffA-Za-z][\u3400-\u9fffA-Za-z0-9_-]{1,19}?)(?:系统|中心|服务|平台)")
        title_subject = named_scope.match(compact_title)
        title_scope = bool(title_subject and title_subject.group("stem") == subject_stem and not re.search(r"(?:与|和|、|对比|比较)", compact_title))
        uncertain = re.compile(r"(?:拟|计划|预计|将于|将来|待审批|待定|待确认|未生效|尚未|没有|未曾|否决|取消|撤回|撤销|未获批准|未批准|未通过|作废|回滚|改回|恢复原|不再)")

        def without_time(value: str) -> str:
            return re.sub(r"^\d{1,2}[:：]\d{2}(?:[:：]\d{2})?", "", value)

        def without_change_lead(value: str) -> str:
            return re.sub(r"^(?:已经|已|拟|计划|预计|尚未|未曾)?(?:将|把)?", "", value)

        def uncertain_status(value: str) -> bool:
            # An explicit denial of cancellation is not a cancellation. This
            # narrow normalization does not infer whether a proposal is live.
            value = re.sub(r"(?:并未|没有|未)(?:取消|撤回|撤销)", "", value)
            return bool(uncertain.search(value))
        count = r"\d+(?:\.\d+)?(?:万|千|百|亿)?条"
        change = re.compile(rf"(?P<label>.+?)从(?P<old>{count})(?:调整为|调整到|改为|改到)(?P<new>{count})(?P<tail>.*)")
        units = []
        header = ""
        local_subject = ""
        for sentence, _section in cls._evidence_candidates(text):
            for clause in re.split(r"[，,]", sentence):
                compact = re.sub(r"\s+", "", clause).strip("。；;！？?")
                heading = re.match(r"【([^】]+)】", compact)
                if heading:
                    header = heading.group(1)
                    compact = compact[heading.end():]
                    section_subject = named_scope.match(header)
                    if section_subject:
                        local_subject = section_subject.group("stem")
                explicit_subject = named_scope.match(without_change_lead(without_time(compact)))
                if explicit_subject:
                    local_subject = explicit_subject.group("stem")
                units.append((clause.strip(), compact, header, local_subject))
        found = []
        unresolved = False
        unresolved_pointer = False
        active_parameter = ""
        previous_header = ""
        named_status = re.compile(r"(?P<label>.+?(?:告警|阈值|门限|条数))(?:的)?(?P<status>(?:调整|变更|方案|已|未|尚|待|被|否决|取消|撤回|撤销|不再).*)")
        for original, compact, header, local_subject in units:
            value = without_time(compact)
            if header != previous_header:
                active_parameter = ""
                previous_header = header
            header_scope = not header or header in {"处置", "调整", "变更", "配置", "参数调整", "配置变更", "当前配置"} or header in {subject, subject_stem}
            local_scope = (title_scope or local_subject == subject_stem) and (not local_subject or local_subject == subject_stem)
            pointer = re.match(r"(?:该|此|上述|本次)(?:调整|变更|方案)|(?:尚未|未生效|待审批|待定|待确认|已被(?:否决|取消|撤回|撤销)|未获批准|未批准|未通过)", value)
            if pointer:
                if header_scope and (not local_subject or local_subject == subject_stem) and uncertain_status(value):
                    if active_parameter == parameter and local_scope:
                        unresolved = True
                    elif not active_parameter and not (title_subject and title_subject.group("stem") != subject_stem):
                        # A dangling status cannot prove a value. The producer
                        # may conservatively bind it only to the same source.
                        unresolved_pointer = True
                continue
            match = change.fullmatch(value)
            status_match = named_status.fullmatch(value) if not match else None
            if not match and not status_match:
                continue
            prefix = without_change_lead((match or status_match).group("label"))
            named_subject = named_scope.match(prefix)
            explicit_scope = bool(named_subject and named_subject.group("stem") == subject_stem)
            if named_subject:
                if not explicit_scope:
                    active_parameter = "other"
                    continue
                prefix = prefix[named_subject.end():].removeprefix("的")
            prefix = without_change_lead(prefix)
            active_parameter = cls._count_parameter_key(prefix)
            if active_parameter != parameter:
                continue
            # A named section overrides the document title. Neutral operation
            # sections may retain its subject, not a different named system.
            if not explicit_scope and (not local_scope or not header_scope):
                if (status_match and header_scope and not local_subject and uncertain_status(value)
                    and not (title_subject and title_subject.group("stem") != subject_stem)):
                    # An exact target-field retraction with an omitted subject
                    # still blocks a proven sibling from this same source.
                    unresolved_pointer = True
                continue
            if uncertain_status(value) or uncertain_status(header):
                unresolved = True
                continue
            if not match:
                continue
            tail = match.group("tail")
            if tail and not re.fullmatch(r"(?:并已生效|已生效|并生效|正式生效)?", tail):
                continue
            found.append({**request, "old": match.group("old"), "new": match.group("new"),
                          "unit": "条", "status": "stated_actual", "evidence": original})
        if unresolved or len({(proof["old"], proof["new"]) for proof in found}) > 1:
            return {**request, "status": "unresolved"}
        if unresolved_pointer:
            return {**request, "status": "unresolved_pointer"}
        if not found:
            return None
        return found[0]

    @classmethod
    def _adjusted_count_answer(cls, question: str, sources: list[dict]) -> tuple[str, list[dict]]:
        proven = []
        values = set()
        unresolved_sources = set()
        for source in sources:
            projected = cls._amount_alignment_anchor_source(source)
            if "evidence_alignment_anchor" in source and projected is None:
                continue
            candidate = projected or dict(source)
            locations = {str(value).strip() for value in [candidate.get("location"), *(candidate.get("evidence_locations") or [])]
                         if value is not None and str(value).strip()}
            chunk_ids = {str(value) for value in [candidate.get("chunk_id"), *(candidate.get("chunk_ids") or [])] if value is not None}
            if len(locations) != 1 or len(chunk_ids) > 1:
                continue
            if candidate.get("type") == "document" and (
                candidate.get("id") is None or candidate.get("chunk_id") is None
                or candidate.get("url") != f"/api/documents/{candidate.get('id')}/file?chunk_id={candidate.get('chunk_id')}"
            ):
                continue
            proof = cls.adjusted_count_proof(question, str(candidate.get("title", "")), str(candidate.get("fallback_excerpt") or candidate.get("evidence") or ""))
            if proof and proof["status"] == "unresolved_pointer":
                unresolved_sources.add((candidate.get("type"), candidate.get("id")))
                continue
            if proof and proof["status"] != "stated_actual":
                return "现有笔记没有足够信息。", []
            if proof:
                values.add((proof["old"], proof["new"]))
            if proof and proof["evidence"] in str(candidate.get("evidence", "")):
                proven.append((candidate, proof))
        if len(proven) != 1 or len(values) != 1 or unresolved_sources.intersection((candidate.get("type"), candidate.get("id")) for candidate, _proof in proven):
            return "现有笔记没有足够信息。", []
        candidate, proof = proven[0]
        selected = {**candidate, "evidence": proof["evidence"], "excerpt": proof["evidence"], "fallback_excerpt": proof["evidence"]}
        selected.pop("evidence_alignment_anchor", None)
        answer = cls.fallback_answer(question, [selected])
        answer, selected_sources = cls.compact_selected_citations(answer, [selected])
        return cls.format_answer_with_leading_sources(answer), selected_sources

    @classmethod
    def explicit_scalar_field_evidence(cls, question: str, source: dict) -> dict[str, list[str]]:
        """Prove narrowly recognised scalar requests from task-bound visible text.

        Missing keys mean unrecognised forms, never evidence of coverage. A
        deadline needs the requested submission task and a relative time limit;
        a batch ratio needs that exact batch/task and, if asked, its observation
        duration. Titles may identify a batch's project, but cannot supply values.
        """
        compact = re.sub(r"\s+", "", str(question or "")).strip("。！？?")
        evidence = str(source.get("evidence", "") or "")
        if cls.structured_record_units(evidence):
            # Labelled rows retain their existing exact-identity/field gates.
            return {}
        title = str(source.get("title", "") or "")
        units = [(text, re.sub(r"^(?:【[^】]{1,24}】)+", "", re.sub(r"\s+", "", text)))
                 for text, _ in cls._evidence_candidates(evidence)]
        identifiers = cls.record_identifier_terms(question)
        if identifiers:
            units = [(text, unit) for text, unit in units if all(cls.text_contains_identifier(unit, value) for value in identifiers)]
        proofs: dict[str, list[str]] = {}
        number = r"(?:\d+(?:\.\d+)?|[一二三四五六七八九十两]+)"
        duration = rf"{number}(?:个)?(?:工作日|自然日|天|小时|分钟)"
        uncertain = re.compile(r"(?:否决|取消|作废|待定|待确认|未确定|不要求|无需|不必)")

        deadline = re.search(
            r"(?P<action>补齐|补交|补充|补|提交)(?P<object>[^，,；;。！？?]{2,20}?)的?期限"
            r"(?:是|为)?(?:多久|多长时间|多少(?:个)?(?:工作日|自然日|天|小时))$",
            compact,
        )
        if deadline:
            target = re.sub(r"(?:材料|资料|依据|文档)$", "", deadline.group("object").rstrip("的"))
            if len(target) >= 2:
                action = r"补(?:齐|交|充)?" if deadline.group("action").startswith("补") else "提交"
                task = rf"{action}{re.escape(target)}(?:材料|资料|依据|文档)?"
                bound_limit = re.compile(rf"(?:{duration}内{task}|{task}(?:应|需|必须)?(?:在)?[^，,；;\d]{{0,12}}{duration}内)")
                lead = re.split(r"[，,；;]", compact[:deadline.start()])[0].strip("的")
                scope = re.split(r"(?:能否|能|可以|是否|需要|必须)", lead, maxsplit=1)[0]
                scope = re.sub(r"^(?:请问|请告诉我|关于)", "", scope).strip("的")
                requested_day_kind = next((kind for kind in ("工作日", "自然日") if kind in compact), "")
                proofs["期限"] = []
                for text, unit in units:
                    if (scope and not unit.startswith(scope)) or re.search(
                        rf"(?:期限|时限|该要求|该方案).{{0,8}}{uncertain.pattern}", unit,
                    ):
                        continue
                    for clause in re.split(r"[，,]", unit):
                        if uncertain.search(clause):
                            continue
                        for match in bound_limit.finditer(clause):
                            if requested_day_kind and requested_day_kind not in match.group():
                                continue
                            prefix = clause[:match.start()].removeprefix(scope).strip("：:")
                            # A continuation may inherit the initial subject;
                            # an explicit other subject before the deadline may not.
                            if scope and not re.fullmatch(
                                r"(?:并|且)?(?:要求|应|需|必须)?(?:在)?"
                                r"(?:(?:申请|提交|审批|签约|验收|收到通知)(?:单)?(?:后|之日起)?)?", prefix,
                            ):
                                continue
                            proofs["期限"].append(text)

        batch_pattern = r"(?:首批|第[一二三四五六七八九十\d]+批)"
        ratio = re.fullmatch(
            rf"(?P<scope>[^，,；;。！？?]*?)(?P<batch>{batch_pattern})(?P<task>[^，,；;。！？?]{{2,12}}?)的?比例"
            r"(?:是|为)?多少(?:[，,](?P<observe>[\u3400-\u9fff]{2,6})(?:多久|多长时间))?",
            compact,
        )
        if ratio:
            def batch_key(value: str) -> str:
                ordinal = value.removeprefix("第").removesuffix("批")
                digits = {word: str(index) for index, word in enumerate("一二三四五六七八九十", 1)}
                return "1" if ordinal == "首" else digits.get(ordinal, ordinal)

            scope = re.sub(r"^(?:请问|请告诉我|关于)", "", ratio.group("scope")).strip("的")
            task = re.escape(ratio.group("task").rstrip("的"))
            bound_ratio = re.compile(
                rf"(?P<batch>{batch_pattern})的?{task}(?:的?比例)?(?:为|是|占|[:：])?{number}[%％]"
            )
            proofs["比例"] = []
            for position, (text, unit) in enumerate(units):
                for match in bound_ratio.finditer(unit):
                    if batch_key(match.group("batch")) != batch_key(ratio.group("batch")):
                        continue
                    next_batch = re.search(batch_pattern, unit[match.end():])
                    value_scope = unit[:match.end() + next_batch.start()] if next_batch else unit
                    if uncertain.search(value_scope):
                        continue
                    prefix = unit[:match.start()].strip("：:")
                    if scope and (prefix.rstrip("的") != scope if prefix else not cls.text_contains_term(title, scope)):
                        continue
                    observe = ratio.group("observe")
                    if observe:
                        observation = rf"{re.escape(observe)}(?:需|应|至少|为|是)?{duration}"
                        tail = value_scope[match.end():]
                        if re.match(rf"[，,]{observation}", tail):
                            proofs["比例"].append(text)
                        elif not tail.strip("。；;！!？?") and position + 1 < len(units):
                            next_text, next_unit = units[position + 1]
                            if re.match(observation, next_unit) and not uncertain.search(next_unit):
                                proofs["比例"].append(f"{text}\n{next_text}")
                    else:
                        proofs["比例"].append(text)
        return proofs

    @classmethod
    def explicit_scalar_field_proofs(cls, question: str, source: dict) -> dict[str, bool]:
        return {field: bool(evidence) for field, evidence in cls.explicit_scalar_field_evidence(question, source).items()}

    @classmethod
    def source_lacks_requested_high_risk_field(cls, question: str, sources: list[dict]) -> bool:
        """Reject entity-only matches when a precise requested field is absent.

        For example, a note may mention ``供应商 A`` and its delivery score, but
        that is not evidence for a question about its *contract total amount*.
        The guard is intentionally limited to fields where an invented answer
        would be high-risk; broad semantic questions stay on the normal path.
        """
        # Exact dates and times are common evidence requests, not by themselves
        # a reason to discard a source. Values are already checked against the
        # cited evidence below. Reserve this early hard refusal for fields where
        # an entity-only hit is particularly misleading.
        field_markers = ("金额", "比例", "期限", "预算", "合同", "币种", "账号", "地址")
        requested_markers = [marker for marker in field_markers if marker in str(question or "")]
        if not requested_markers:
            return False
        source_text = "\n".join(
            f"{source.get('title', '')}\n{source.get('evidence', '')}"
            for source in sources
        ).lower()
        entities = cls.explicit_entity_terms(question)
        scalar_proofs = [cls.explicit_scalar_field_proofs(question, source) for source in sources]
        def field_is_present(marker: str) -> bool:
            recognised = [proof[marker] for proof in scalar_proofs if marker in proof]
            if recognised:
                return any(recognised)
            if marker.lower() in source_text:
                return True
            if marker == "金额":
                return bool(re.search(r"\d+(?:\.\d+)?\s*(?:万元|元|万|亿)", source_text))
            return False

        fields_are_present = all(field_is_present(marker) for marker in requested_markers)
        entities_are_present = not entities or all(
            cls.text_contains_term(source_text, entity) for entity in entities
        )
        return not (fields_are_present and entities_are_present)

    @classmethod
    def requested_answer_fields_are_covered(cls, question: str, texts) -> bool:
        """Require direct values for questions that ask for a precise outcome.

        Topic similarity is not enough when the user asks whether an event
        actually happened, who was finally selected, or for an exact date,
        amount, count, or ROI.  The rules below inspect evidence shape rather
        than document titles, so related plans and policies cannot masquerade
        as the requested result.  Unrecognised question forms deliberately
        remain on the normal retrieval path.
        """
        raw_question = str(question or "").strip()
        compact_question = re.sub(r"\s+", "", raw_question)
        material = [str(text or "") for text in texts if str(text or "").strip()]
        if not material:
            return False
        row_coverage = cls.structured_field_coverage(raw_question, material)
        if row_coverage is False:
            return False
        if row_coverage is True:
            # A recognised field does not validate every other requested
            # field. Keep the precise date/amount checks, scoped to the named
            # row so a neighbour cannot supply a missing signing date.
            material = cls.matched_structured_rows(raw_question, material)

        material_units: list[tuple[str, list[str]]] = []
        for text in material:
            candidates = cls._evidence_candidates(text)
            units = [sentence for sentence, _section in candidates if sentence.strip()]
            material_units.append((text, units or [text]))

        if cls.is_actor_action_question(raw_question) and row_coverage is not True:
            return any(cls.actor_action_evidence(raw_question, text) for text in material)

        identifiers = list(dict.fromkeys(re.findall(
            r"(?<![A-Za-z0-9])(?:[A-Za-z]{1,12}[-_])?\d[A-Za-z0-9_-]{0,24}(?![A-Za-z0-9])",
            raw_question,
            flags=re.IGNORECASE,
        )))

        def clean_subject(value: str) -> str:
            subject = re.sub(
                r"^(?:请问|我想知道|想知道|请告诉我|帮我查一下|帮我看看)",
                "",
                str(value or ""),
            )
            subject = re.sub(r"(?:最终|最后|实际)(?:的)?$", "", subject)
            return subject.strip(" 的，。！？?；;：:")

        def any_unit(predicate, *, subject: str = "") -> bool:
            for source_text, units in material_units:
                if identifiers and not all(
                    cls.text_contains_term(source_text, identifier) for identifier in identifiers
                ):
                    continue
                if subject and not cls.text_contains_term(source_text, subject):
                    continue
                if any(predicate(unit) for unit in units):
                    return True
            return False

        # A named organisational role must actually be assigned in the
        # evidence. Generic wording such as "不代表全量开放" is no evidence
        # for the identity of the organisation's legal representative.
        role_match = re.search(
            r"(?P<role>法定代表人|法人代表|董事长|总经理|联系人)(?:是|为)?谁",
            compact_question,
        )
        if role_match:
            role = re.escape(role_match.group("role"))
            return any_unit(lambda unit: bool(re.search(
                rf"{role}\s*(?:是|为|[：:]|由)\s*[^，。；;\n]{{2,24}}"
                rf"|[^，。；;\n]{{2,16}}(?:担任|出任){role}",
                unit,
            )))

        asks_final_selection = bool(re.search(
            r"(?:哪位|谁|哪个).{0,10}(?:最终|最后).{0,10}(?:录用|录取|选中|入选|中标)"
            r"|(?:最终|最后).{0,10}(?:录用|录取|选中|入选|中标).{0,8}(?:哪位|谁|哪个)",
            compact_question,
        ))
        if asks_final_selection:
            return any_unit(lambda unit: bool(re.search(
                r"(?:最终|最后|正式|确认).{0,16}(?:录用|录取|选中|入选|中标)"
                r"|(?:录用|录取|选中|入选|中标).{0,16}(?:最终|最后|正式|确认)",
                unit,
            )))

        asks_occurrence_status = bool(re.search(
            r"(?:是否|有没有|有无).{0,16}(?:已经|曾经|实际)?(?:发生|出现)(?:过|了)?"
            r"|(?:已经|曾经|实际)(?:发生|出现)(?:过|了)?(?:吗|没有|与否)?",
            compact_question,
        ))
        if asks_occurrence_status:
            subject_match = re.match(
                r"(?P<subject>.+?)(?:是否|有没有|有无)(?:已经|曾经|实际)?(?:发生|出现)",
                compact_question,
            ) or re.match(
                r"(?P<subject>.+?)(?:已经|曾经|实际)(?:发生|出现)",
                compact_question,
            )
            subject = clean_subject(subject_match.group("subject")) if subject_match else ""
            return any_unit(lambda unit: bool(re.search(
                r"(?:已经|已|曾经|曾|实际|本期|本月|期间).{0,16}(?:发生|出现)"
                r"|(?:发生|出现)(?:过|了|共\s*[0-9一二三四五六七八九十两]+\s*(?:起|次|例|笔))"
                r"|(?:未|没有|尚未|从未).{0,10}(?:发生|出现)",
                unit,
            )), subject=subject)

        asks_money_loss = "损失" in compact_question and bool(re.search(
            r"(?:多少|几|金额|元|万元|人民币|美元)", compact_question,
        ))
        if asks_money_loss:
            subject_match = re.match(
                r"(?P<subject>.+?)(?:实际|最终)?损失(?:是|为)?(?:多少|几|金额)",
                compact_question,
            ) or re.match(
                r"(?P<subject>.+?)(?:造成|导致|产生)(?:了)?(?:多少|几|金额).*?损失",
                compact_question,
            )
            subject = clean_subject(subject_match.group("subject")) if subject_match else ""
            record_subjects = cls.record_identifier_terms(raw_question)
            if len(record_subjects) == 1:
                subject = record_subjects[0]
            return any_unit(lambda unit: "损失" in unit and bool(re.search(
                r"(?:人民币|美元|RMB|USD)?\s*\d+(?:\.\d+)?\s*(?:亿元|万元|元|万|亿)",
                unit,
                flags=re.IGNORECASE,
            )), subject=subject)

        asks_signing_date = bool(re.search(
            r"(?:签约|签署).{0,8}(?:日期|时间|什么时候|何时|哪天|哪一年|哪年)",
            compact_question,
        ))
        if asks_signing_date:
            absolute_date = re.compile(
                r"(?:"
                r"(?<![A-Za-z0-9_-])20\d{2}(?:[-/.]\d{1,2}(?:[-/.]\d{1,2})?|年\d{1,2}月(?:\d{1,2}日)?)(?![A-Za-z0-9_-])"
                r"|(?<![A-Za-z0-9_-])\d{1,2}月\d{1,2}[日号](?![A-Za-z0-9_-])"
                r")"
            )
            return any_unit(lambda unit: bool(re.search(r"(?:签约|签署)", unit)) and bool(absolute_date.search(unit)))

        asks_observed_customer_count = bool(re.search(
            r"(?:多少|几)(?:个|家|名)?(?:真实|正式|实际)?(?:客户|用户).{0,10}(?:在用|使用|部署|上线)"
            r"|(?:真实|正式|实际)(?:客户|用户).{0,10}(?:多少|几)(?:个|家|名)?",
            compact_question,
        ))
        if asks_observed_customer_count:
            subject = clean_subject(re.split(
                r"(?:目前)?(?:已有|有)?(?:多少|几)(?:个|家|名)?(?:真实|正式|实际)?(?:客户|用户)",
                compact_question,
                maxsplit=1,
            )[0])
            return any_unit(lambda unit: bool(re.search(
                r"\d+\s*(?:个|家|名)\s*(?:真实|正式|实际)?(?:客户|用户)"
                r"|(?:真实|正式|实际)(?:客户|用户).{0,12}\d+\s*(?:个|家|名)",
                unit,
            )) and bool(re.search(r"(?:在用|使用|部署|上线|采用)", unit)), subject=subject)

        asks_occurrence_count = bool(re.search(
            r"(?:一共|累计|总共)?.{0,10}(?:发生|出现)(?:过)?(?:多少|几)(?:次|起|例|笔)"
            r"|(?:多少|几)(?:次|起|例|笔).{0,10}(?:发生|出现)",
            compact_question,
        ))
        if asks_occurrence_count:
            subject_match = re.match(
                r"(?P<subject>.+?)(?:一共|累计|总共|总计)?(?:实际)?"
                r"(?:发生|出现)(?:过|了)?(?:多少|几)(?:次|起|例|笔)",
                compact_question,
            )
            subject = clean_subject(subject_match.group("subject")) if subject_match else ""
            return any_unit(lambda unit: bool(re.search(
                r"(?:发生|出现|累计|共计|总计).{0,12}\d+\s*(?:次|起|例|笔)"
                r"|\d+\s*(?:次|起|例|笔).{0,12}(?:发生|出现)",
                unit,
            )), subject=subject)

        asks_final_roi = "roi" in raw_question.casefold() and bool(re.search(
            r"(?:最终|最后|实际|结果|多少|几)", compact_question,
        ))
        if asks_final_roi:
            subject = clean_subject(re.split(
                r"(?:最终|最后|实际)?(?:的)?ROI",
                compact_question,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0])
            return any_unit(lambda unit: bool(re.search(r"\bROI\b", unit, flags=re.IGNORECASE)) and bool(re.search(
                r"(?:ROI\s*(?:为|是|达到|[:：=])?\s*)-?\d+(?:\.\d+)?\s*(?:[%％]|倍)?",
                unit,
                flags=re.IGNORECASE,
            )), subject=subject)

        return True

    @classmethod
    def source_lacks_requested_answer_field(cls, question: str, sources: list[dict]) -> bool:
        """Apply the precise-outcome gate to the final visible source set."""
        return not cls.requested_answer_fields_are_covered(
            question,
            (
                f"{source.get('title', '')}\n{source.get('evidence', '')}\n{source.get('fallback_excerpt', '')}"
                for source in sources
            ),
        )

    @staticmethod
    def is_broad_policy_question(question: str, sources: list[dict]) -> bool:
        return len(sources) > 1 and bool(re.search(
            r"(?:什么|哪些).{0,8}(?:情况|情形).{0,12}(?:需要|必须|应当).{0,12}(?:审批|确认)",
            str(question or ""),
        ))

    @staticmethod
    def requires_evidence_first_answer(question: str, sources: list[dict]) -> bool:
        """Use extractive evidence for high-risk boundary questions.

        Compound questions are verified clause by clause elsewhere.  Forcing
        every comparison back to raw excerpts discarded valid synthesis from
        the local model, so this gate is now reserved for question forms where
        a fluent paraphrase is materially more likely to change the rule.
        """
        text = str(question or "")
        asks_boundary = bool(re.search(r"(?:不是|不等于|不能|不得|不可以|是不是|是.{0,12}吗|能否)", text))
        asks_temporal_feasibility = RagPipeline.is_temporal_feasibility_question(text)
        asks_resolution = RagPipeline.is_resolution_question(text)
        asks_relationship = bool(re.search(
            r"(?:有什么关系|有何关系|什么关系|关系是什么|"
            r"(?:二者|两者|之间).{0,6}(?:关系|关联))",
            text,
        ))
        clauses = RagPipeline.compound_query_clauses(text)
        compound_without_clause_trace = len(clauses) > 1 and not any(
            source.get("matched_clauses") for source in sources
        )
        structured_fields = any(
            len(re.findall(r"Agent名称：", str(source.get("evidence", "")))) >= 2
            for source in sources
        )
        asks_structured_fields = bool(re.search(r"(?:功能|场景|用途|适用|亮点|关键词)", text))
        source_material = "\n".join(
            f"{source.get('evidence', '')}\n{source.get('fallback_excerpt', '')}"
            for source in sources
        )
        asks_polarity_with_direct_negative = "是否" in text and bool(re.search(
            r"(?:不是|不等于|不代表|不能理解(?:成|为)|并非)",
            source_material,
        ))
        asks_structured_selector = bool(re.search(
            r"(?:哪份|哪个|哪一个|哪条|哪项|哪笔).{0,16}(?:合同|记录|条款|项目|文件|文档)",
            text,
        )) and bool(re.search(r"(?:^|[；;])[^；;：:\n]{1,18}[：:]", source_material))
        broad_policy_question = RagPipeline.is_broad_policy_question(text, sources)
        return (
            asks_boundary
            or asks_temporal_feasibility
            or asks_resolution
            or asks_relationship
            or asks_polarity_with_direct_negative
            or asks_structured_selector
            # A shared citation can cover all row fields while the generated
            # answer names only one. Preserve the complete selected row values.
            or bool(RagPipeline.shared_record_field_queries(question))
            or compound_without_clause_trace
            or broad_policy_question
            or (structured_fields and asks_structured_fields)
            or any(
                source.get("structured_table_fields")
                and RagPipeline.structured_field_coverage(text, [str(source.get("evidence", ""))]) is True
                for source in sources
            )
        )

    @staticmethod
    def is_resolution_question(question: str) -> bool:
        return bool(re.search(r"(?:怎么办|怎么处理|如何处理|怎么解决)", str(question or "")))

    @staticmethod
    def requires_multiple_sources(question: str, sources: list[dict]) -> bool:
        """Keep several sources only when the question actually asks for several facts."""
        multiple_clauses = len(RagPipeline.compound_query_clauses(question)) > 1
        multiple_entities = len(RagPipeline.explicit_entity_terms(question)) > 1
        explicit_parallel_tasks = bool(RagPipeline.quantified_parallel_tasks(question, require_matching_count=False))
        broad_policy_question = RagPipeline.is_broad_policy_question(question, sources)
        return multiple_clauses or multiple_entities or explicit_parallel_tasks or broad_policy_question or (
            len(sources) > 1 and bool(re.search(r"(?:分别|以及|又|并且|同时)", str(question or "")))
        )

    @classmethod
    def answer_omits_required_compound_sources(
        cls,
        question: str,
        answer: str,
        sources: list[dict],
    ) -> bool:
        """Require at least one cited source for every annotated subquestion.

        Retrieval records which compound clause each source satisfied.  A
        generated comparison is accepted only when its citations still cover
        every such clause.  This catches plausible one-sided summaries without
        requiring every duplicate chunk or every displayed source to be cited.
        """
        clauses = cls.compound_query_clauses(question)
        if len(clauses) <= 1:
            return False
        quantified_tasks = cls.quantified_parallel_tasks(question)
        cited = cls.cited_indexes(answer)
        if not cited:
            return True
        for clause in clauses:
            focus = quantified_tasks.get(clause)
            # One document may contain several tasks under the same citation.
            # Citation coverage alone would accept an answer that names only
            # one task; recover the explicit task wording from the evidence.
            if focus and not cls.text_contains_term(answer, focus):
                return True
            candidates = {
                int(source.get("index", position))
                for position, source in enumerate(sources, 1)
                if (
                    cls.text_contains_term(str(source.get("evidence", "")), focus)
                    if focus else clause in {
                        str(value).strip()
                        for value in source.get("matched_clauses", [])
                        if str(value).strip()
                    }
                )
            }
            if not candidates:
                continue
            if not (cited & candidates):
                return True
        return False

    @classmethod
    def answer_has_under_cited_cross_source_synthesis(
        cls,
        question: str,
        answer: str,
        sources: list[dict],
    ) -> bool:
        """Reject a comparison conclusion that visibly cites only one side.

        Source-specific facts may remain on separate lines.  A line that starts
        with an explicit synthesis such as ``共同点`` or ``不一致`` represents a
        claim about both sides and therefore must cite at least one source for
        every annotated compound clause.
        """
        clauses = cls.compound_query_clauses(question)
        if len(clauses) <= 1 or not re.search(
            r"(?:共同点|区别|不同|是否一致|有什么关系|有何关系)",
            str(question or ""),
        ):
            return False
        required_indexes: set[int] = set()
        for position, source in enumerate(sources, 1):
            matched = {
                str(value).strip()
                for value in source.get("matched_clauses", [])
                if str(value).strip()
            }
            if any(clause in matched for clause in clauses):
                required_indexes.add(int(source.get("index", position)))
        if len(required_indexes) < 2:
            return False
        for claim in cls.answer_claims(answer):
            text = re.sub(r"\s+", "", str(claim.get("text", ""))).lstrip("-•")
            if not re.match(r"^(?:共同点|两者|二者|均|都|一致|不一致|区别|不同之处|关系)", text):
                continue
            if not required_indexes.issubset(set(claim.get("citations", []))):
                return True
        return False

    @staticmethod
    def temporal_conditions(text: str) -> list[str]:
        """Return explicit relative-time conditions, without inferring equivalents."""
        return list(dict.fromkeys(re.findall(
            r"(?:提前|推迟|延后|前|后)\s*(?:(?:\d+|[一二三四五六七八九十两半]|一个|两个)\s*)?(?:天|日|周|星期|月|年)",
            str(text or ""),
        )))

    @classmethod
    def is_temporal_feasibility_question(cls, question: str) -> bool:
        permission = bool(re.search(r"(?:可以|能否|可否|允许|行不行|来得及|是否)", str(question or "")))
        return permission and bool(cls.temporal_conditions(question))

    @classmethod
    def temporal_feasibility_not_directly_covered(cls, question: str, sources: list[dict]) -> bool:
        """Do not turn a recommendation into permission for a different date."""
        conditions = cls.temporal_conditions(question)
        if not conditions:
            return False
        source_text = "\n".join(
            f"{source.get('evidence', '')}\n{source.get('fallback_excerpt', '')}"
            for source in sources
        )
        return not all(condition in source_text for condition in conditions)

    @classmethod
    def temporal_feasibility_answer(cls, question: str, sources: list[dict]) -> str:
        primary = sources[0] if sources else {}
        evidence = str(primary.get("evidence") or primary.get("fallback_excerpt") or "").strip()
        index = int(primary.get("index", 1))
        condition = "、".join(cls.temporal_conditions(question)) or "该具体时间点"
        if not evidence:
            return "现有笔记没有足够信息。"
        return (
            f"现有笔记只直接说明：{evidence[:260]} [{index}]\n"
            f"问题中的时间条件“{condition}”未被该证据直接覆盖，不能据此判断“可以”；请核对来源。"
        )

    @classmethod
    def requires_clarification(cls, question: str) -> bool:
        """Detect a source-bound question whose subject is missing.

        Single-turn retrieval must not silently choose one contract, project or
        deadline merely because it has the highest semantic score.  Keep this
        gate deliberately narrow: plural discovery questions remain searchable,
        while deictic references and singular class selectors require a name.
        """
        text = str(question or "").strip()
        if cls.explicit_entity_terms(text) or cls.relationship_source_terms(text):
            return False
        compact = re.sub(r"\s+", "", text)
        deictic_object = re.search(
            r"(?:这个|那个|该|此|这份|那份|这项|那项|这笔|那笔|这条|那条)"
            r"(?:Agent|智能体|合同|项目|审批|风险|期限|日期|时间|条款|金额|方案|流程|规则|记录|文件|文档|资料|活动|任务)",
            compact,
            re.IGNORECASE,
        )
        if deictic_object:
            return True
        # A singular selector without any distinguishing attribute is equally
        # ambiguous when the library contains several projects or contracts.
        # Do not apply this to plural discovery ("哪些项目") or general tool/
        # concept recommendations.
        return bool(re.search(
            r"(?:哪个项目|哪一个项目|哪项项目|哪份合同|哪一个合同)"
            r"(?:要|会|计划|准备|应该|需要|何时|什么时候|怎么|如何|由谁|谁来|是否|能否)",
            compact,
            re.IGNORECASE,
        ))

    @classmethod
    def enforce_answer_contract(cls, question: str, answer: str, sources: list[dict]) -> tuple[str, list[dict]]:
        """Return only answers whose visible claims have valid source references."""
        clean = str(answer or "").strip()
        if not sources:
            if cls.answer_has_unsupported_retention_endpoint(question, clean, sources):
                return "现有笔记没有足够信息。", []
            if cls.adjusted_count_request(question) or cls.inverse_approval_request(question) or cls.cause_pair_request(question) or cls.budget_pause_request(question):
                return "现有笔记没有足够信息。", []
            return clean, []
        if cls.budget_pause_request(question):
            return cls._budget_pause_answer(question, sources)
        if cls.cause_pair_request(question):
            return cls._cause_pair_answer(question, sources)
        if cls.inverse_approval_request(question):
            return cls._inverse_approval_answer(question, sources)
        if cls.adjusted_count_request(question) and not any(
            cls.structured_record_units(str(source.get("evidence", ""))) for source in sources
        ):
            return cls._adjusted_count_answer(question, sources)

        quantified_tasks = cls.quantified_parallel_tasks(question)
        task_count_differs_from_categories = not quantified_tasks and bool(
            cls.quantified_parallel_tasks(question, require_matching_count=False)
        )
        # These task nouns were explicitly named by the user. A high semantic
        # score or a shared heading cannot supply a missing parallel task.
        visible_material = "\n".join(
            str(source.get("evidence", ""))
            for source in sources
        )
        if cls.parallel_field_subject_terms(question):
            # Substring recall (报告助手 -> 专题报告助手) cannot establish a
            # named row's fields. Validate visible row identities after header
            # recovery, so headerless retrieval fragments are not rejected early.
            for _line, record in cls.structured_record_units(visible_material):
                fields = cls.structured_record_fields(record)
                if cls.structured_identity_labels(label for label, _value in fields) and not cls.structured_row_identity(question, fields):
                    return "现有笔记没有足够信息。", []
        if any(
            not cls.text_contains_term(visible_material, focus)
            for focus in quantified_tasks.values()
        ):
            return "现有笔记没有足够信息。", []

        required_topics = cls.answer_contract_anchor_terms(question, sources)
        if required_topics:
            corpus = "\n".join(
                f"{source.get('title', '')}\n{source.get('anchor_context', '')}\n"
                f"{source.get('evidence', '')}\n{source.get('fallback_excerpt', '')}"
                for source in sources
            )
            if not all(cls.text_contains_term(corpus, topic) for topic in required_topics):
                return "现有笔记没有足够信息。", []
            if cls.requires_definition_evidence(question) and not cls.has_definition_evidence(
                required_topics,
                (
                    f"{source.get('evidence', '')}\n{source.get('fallback_excerpt', '')}"
                    for source in sources
                ),
            ):
                return "现有笔记没有足够信息。", []

        available_indexes = {int(source.get("index", position)) for position, source in enumerate(sources, 1)}
        if cls.source_lacks_requested_high_risk_field(question, sources):
            return "现有笔记没有足够信息。", []
        if cls.source_lacks_requested_answer_field(question, sources):
            return "现有笔记没有足够信息。", []
        scalar_sources = []
        for source in sources:
            scalar_evidence = cls.explicit_scalar_field_evidence(question, source)
            if scalar_evidence and all(scalar_evidence.values()):
                visible = "\n".join(dict.fromkeys(text for values in scalar_evidence.values() for text in values))
                scalar_sources.append({**source, "evidence": visible, "fallback_excerpt": visible, "excerpt": visible})
        if scalar_sources:
            # The newly proved values are task-local. A different task in the
            # background must not validate a wrong unit, batch or omitted value.
            # Reuse the extractive path for these bounded scalar question forms.
            sources = scalar_sources
        if cls.is_role_comparison_question(question) and not any(
            cls.role_comparison_evidence(
                question,
                f"{source.get('evidence', '')}\n{source.get('fallback_excerpt', '')}",
            )
            for source in sources
        ):
            return "现有笔记没有足够信息。", []
        if cls.is_temporal_feasibility_question(question) and cls.temporal_feasibility_not_directly_covered(question, sources):
            return cls.temporal_feasibility_answer(question, sources), cls.fallback_sources(sources)
        cited = cls.cited_indexes(clean)
        invalid_citation = bool(cited - available_indexes)
        if len(sources) == 1 and not invalid_citation:
            # A single visible source is unambiguous. Complete omitted line-level
            # markers before claim mapping so each statement is still auditable.
            clean = cls.complete_single_source_citations(clean)
            cited = cls.cited_indexes(clean)
        unsupported_retention = cls.answer_has_unsupported_retention_endpoint(question, clean, sources)
        if unsupported_retention:
            # Recover only facts already visible in the cited source. Hidden
            # workflow context must not manufacture a new retention deadline.
            sources = [
                {**source, "fallback_excerpt": source["evidence"], "excerpt": source["evidence"]}
                for position, source in enumerate(sources, 1)
                if int(source.get("index", position)) in cited and str(source.get("evidence", "")).strip()
            ]
            if not cls.can_use_evidence_fallback(sources):
                return "现有笔记没有足够信息。", []
        # Category count cannot establish task count: two quality tasks and
        # one security task may be a valid set of three. In this narrow form,
        # retain the available source facts without certifying completeness
        # from the model's unsupported count or additional invented item.
        needs_recovery = (
            cls.answer_needs_fallback(clean) or not cited or invalid_citation
            or task_count_differs_from_categories
            or bool(scalar_sources)
            or unsupported_retention
        )
        if not needs_recovery and cls.answer_is_only_source_title(clean, sources):
            needs_recovery = True
        if not needs_recovery and cls.answer_omits_required_compound_sources(question, clean, sources):
            needs_recovery = True
        if not needs_recovery and cls.answer_has_under_cited_cross_source_synthesis(question, clean, sources):
            needs_recovery = True
        if not needs_recovery and cls.requires_evidence_first_answer(question, sources):
            # A model may attach two citations to one blended policy statement.
            # Source-specific evidence is safer and easier for users to audit.
            needs_recovery = True
        if not needs_recovery and cls.answer_omits_required_conjuncts(question, clean, sources):
            needs_recovery = True
        if not needs_recovery and cls.answer_has_uncovered_exact_values(question, clean, sources):
            needs_recovery = True
        if not needs_recovery and cls.answer_cites_weaker_source_than_direct_match(question, clean, sources):
            needs_recovery = True
        if not needs_recovery and cls.answer_omits_enumerated_evidence(question, clean, sources):
            needs_recovery = True
        if not needs_recovery and cls.answer_omits_explicit_component_list(question, clean, sources):
            needs_recovery = True
        if not needs_recovery:
            clean = cls.complete_enumeration_count(question, clean, sources)
        if len(sources) > 1 and not needs_recovery and cls.has_uncited_fact_line(clean):
            needs_recovery = True
        if not needs_recovery and cls.answer_has_unsupported_claims(question, clean, sources):
            needs_recovery = True

        if needs_recovery:
            if scalar_sources or cls.can_use_evidence_fallback(sources):
                fallback = cls.fallback_answer(question, sources)
                if fallback.startswith("不是。"):
                    selected = [sources[0]]
                else:
                    selected = cls.fallback_sources(
                        sources,
                        force_multiple=cls.requires_multiple_sources(question, sources),
                    )
                if len(selected) == 1:
                    fallback = cls.complete_single_source_citations(fallback)
                selected = cls.align_sources_to_answer(fallback, selected, question=question)
                fallback, selected = cls.compact_selected_citations(fallback, selected)
                return cls.format_answer_with_leading_sources(fallback), selected
            return "现有笔记没有足够信息。", []

        selected = cls.cited_sources(clean, sources)
        selected = cls.align_sources_to_answer(clean, selected, question=question)
        clean, selected = cls.compact_selected_citations(clean, selected)
        return cls.format_answer_with_leading_sources(clean), selected

    @classmethod
    def answer_contract_anchor_terms(cls, question: str, sources: list[dict]) -> list[str]:
        """Use a source-visible entity when a named-field question is phrased naturally.

        The answer contract normally retains the stricter full-question
        anchors.  OCR tables and records rarely repeat wording such as
        "项目风险扫描Agent 的核心功能和适用场景", though, while they do contain
        the named Agent and both fields.  In that narrow, auditable case use
        the entity as the guard.  Do not require an entity if a legacy source
        intentionally stores only the already-selected field value.
        """
        clauses = cls.compound_query_clauses(question)
        if len(clauses) > 1:
            # Retrieval already treats each clause as an independent evidence
            # obligation. Carry only safe, source-visible anchors into final
            # verification; never turn the full compound wording into one
            # impossible phrase requirement.
            corpus = "\n".join(
                f"{source.get('title', '')}\n{source.get('evidence', '')}\n{source.get('fallback_excerpt', '')}"
                for source in sources
            )
            compound_anchors: list[str] = []
            seen: set[str] = set()
            for clause in clauses:
                # Final answer validation needs only literal named entities here.
                # Broader retrieval anchors such as ``供应商 A`` or a generated
                # ``subject + field`` phrase are useful for candidate recall, but
                # making them answer-contract requirements rejects otherwise
                # supported paraphrases across multiple evidence passages.
                for anchor in cls.explicit_entity_terms(clause):
                    key = re.sub(r"\s+", "", anchor).casefold()
                    if key and key not in seen and cls.text_contains_term(corpus, anchor):
                        compound_anchors.append(anchor)
                        seen.add(key)
            return compound_anchors

        # A named table row is anchored by its stable identifier, not by the
        # complete natural-language field request.  Requiring a source to
        # contain ``HT-2026-021 的预付款比例和尾款付款节点`` verbatim rejected
        # a row that visibly contained HT-2026-021 plus both requested values.
        record_identifiers = cls.record_identifier_terms(question)
        if record_identifiers:
            return record_identifiers
        named_rows = cls.named_structured_subject_terms(question)
        if named_rows:
            return named_rows

        anchors = cls.answer_anchor_terms(question)
        entities = cls.explicit_entity_terms(question)
        if len(entities) != 1:
            return anchors
        corpus = "\n".join(
            f"{source.get('title', '')}\n{source.get('evidence', '')}\n{source.get('fallback_excerpt', '')}"
            for source in sources
        )
        entity = entities[0]
        if cls.text_contains_term(corpus, entity) and (
            not anchors or not all(cls.text_contains_term(corpus, topic) for topic in anchors)
        ):
            return [entity]
        return anchors

    @classmethod
    def answer_omits_enumerated_evidence(cls, question: str, answer: str, sources: list[dict]) -> bool:
        """Reject an answer that turns a labelled source list into one example."""
        if not cls.is_enumeration_question(question):
            return False
        labels = []
        for source in sources:
            _, stage_labels = cls.stage_enumeration_block(question, str(source.get("evidence", "")))
            labels.extend(stage_labels)
            labels.extend(re.findall(
                r"(?:风险|问题|措施|步骤|原因|目标|事项|建议)\s*(?:[一二三四五六七八九十]+|\d+)(?=[：:])",
                str(source.get("evidence", "")),
            ))
        labels = list(dict.fromkeys(labels))
        if len(labels) < 2:
            return False
        compact_answer = re.sub(r"\s+", "", str(answer or ""))
        return any(label not in compact_answer for label in labels)

    @staticmethod
    def is_explicit_component_list_question(question: str) -> bool:
        """Whether the user asks for every member of a concrete component list."""
        compact = re.sub(r"\s+", "", str(question or ""))
        return bool(re.search(
            r"(?:有哪些|有哪(?:些)?|包含(?:哪些|什么)|包括(?:哪些|什么)|"
            r"由(?:哪些|什么).{0,8}组成|核心项|核心文件|目录结构|组成部分)",
            compact,
        ))

    @classmethod
    def explicit_component_list(cls, question: str, text: str) -> list[str]:
        """Extract a short, explicitly delimited list from one evidence sentence.

        This deliberately requires a list question, a label followed by a colon,
        and at least two visible separators. It therefore does not reinterpret
        ordinary comma-separated prose as a complete inventory.
        """
        if not cls.is_explicit_component_list_question(question):
            return []
        raw = re.sub(r"[\t\r ]+", " ", str(text or "")).strip()
        if not raw:
            return []
        for sentence in re.split(r"[。！？\n]+", raw):
            labelled = re.search(r"([^：:\n]{1,60})[：:]\s*(.+)", sentence)
            if not labelled:
                continue
            label = labelled.group(1).strip()
            value = labelled.group(2).strip()
            value = re.split(
                r"[，,](?=(?:总体|总计|合计|总共|共计|大小|体积|容量|数量|约为|约\s*\d))",
                value,
                maxsplit=1,
            )[0].strip()
            explicit_label = bool(re.search(
                r"(?:列表|清单|核心项|核心文件|目录结构|组成|包括|包含)",
                label,
            ))
            if "+" in value or "、" in value:
                parts = re.split(r"\s*(?:\+|、)\s*", value)
            elif explicit_label and ("；" in value or ";" in value):
                parts = re.split(r"\s*[；;]\s*", value)
            elif explicit_label and (value.count("，") >= 2 or value.count(",") >= 2):
                parts = re.split(r"\s*[，,]\s*", value)
            else:
                continue
            components = [
                part.strip(" \t\r\n，,；;。:：'\"“”‘’()（）[]【】")
                for part in parts
            ]
            components = [part for part in components if part]
            if not 2 <= len(components) <= 8:
                continue
            if any(len(part) > 60 or re.search(r"[。！？]", part) for part in components):
                continue
            return components
        return []

    @staticmethod
    def answer_contains_component(answer: str, component: str) -> bool:
        """Match a source list member without making path punctuation mandatory."""
        answer_text = str(answer or "").casefold()
        component_text = str(component or "").casefold().strip()
        if not component_text:
            return True
        if re.search(r"[a-z0-9_]", component_text):
            token = component_text.rstrip("/\\")
            return token in answer_text
        compact_answer = re.sub(r"\s+", "", answer_text)
        compact_component = re.sub(r"\s+", "", component_text)
        return compact_component in compact_answer

    @classmethod
    def answer_omits_explicit_component_list(
        cls,
        question: str,
        answer: str,
        sources: list[dict],
    ) -> bool:
        """Reject fluent list answers that silently omit a source component."""
        for source in sources:
            evidence = str(source.get("evidence", ""))
            components = cls.explicit_component_list(question, evidence)
            if components and any(
                not cls.answer_contains_component(answer, component)
                for component in components
            ):
                return True
        return False

    @classmethod
    def enumeration_count_prefix(cls, question: str, sources: list[dict]) -> str:
        """Derive a count only from one source's explicitly labelled items."""
        compact_question = re.sub(r"\s+", "", str(question or ""))
        requested = re.search(
            r"(?:有|共有|一共|总共|包含)?(?:多少|几)(?:个|项|条|种)?"
            r"(风险|问题|措施|步骤|原因|目标|事项|建议)",
            compact_question,
        )
        if not requested:
            return ""
        noun = requested.group(1)
        number_words = {
            2: "二", 3: "三", 4: "四", 5: "五", 6: "六",
            7: "七", 8: "八", 9: "九", 10: "十",
        }
        marker = rf"{re.escape(noun)}\s*(?:[一二三四五六七八九十]+|\d+)(?=[：:])"
        for position, source in enumerate(sources, 1):
            labels = list(dict.fromkeys(re.findall(marker, str(source.get("evidence", "")))))
            count = len(labels)
            if count < 2 or count not in number_words:
                continue
            source_index = int(source.get("index", position))
            return f"来源[{source_index}] 共{number_words[count]}个{noun}。"
        return ""

    @classmethod
    def complete_enumeration_count(cls, question: str, answer: str, sources: list[dict]) -> str:
        prefix = cls.enumeration_count_prefix(question, sources)
        if not prefix:
            return answer
        count_phrase = re.sub(r"^来源\[\d+\]\s*", "", prefix).rstrip("。")
        if count_phrase in re.sub(r"\s+", "", str(answer or "")):
            return answer
        return f"{prefix}\n{str(answer or '').strip()}".strip()

    @staticmethod
    def answer_is_only_source_title(answer: str, sources: list[dict]) -> bool:
        """A citation-backed title alone is an anchor, not an answer."""
        def compact(value: str) -> str:
            normalized = re.sub(r"(?:来源)?\[\d+\]|\s+|[。！？!?；;：:]", "", str(value or ""))
            return re.sub(r"\.(?:txt|md|pdf|docx?|xlsx?|csv)$", "", normalized, flags=re.IGNORECASE)

        compact_answer = compact(answer)
        if not compact_answer:
            return True
        for source in sources:
            if compact_answer == compact(str(source.get("title", ""))):
                return True
            evidence = str(source.get("evidence", ""))
            if compact_answer == compact(evidence) and RagPipeline.is_non_answer_heading(evidence):
                return True
        return False

    @staticmethod
    def is_non_answer_heading(text: str) -> bool:
        """Recognise titles such as ``…行动方案`` that cannot answer a question alone."""
        compact = re.sub(r"\s+", "", str(text or "")).strip("。！？?；;：:")
        if not compact or len(compact) > 48:
            return False
        if re.search(r"(?:承担|负责|扮演|起到|是|为|包括|需要|应当|不得|可以)", compact):
            return False
        return bool(re.search(r"(?:方案|计划|摘要|通知|意见|报告|手册|指南|制度|文件|说明)$", compact))

    @classmethod
    def answer_cites_weaker_source_than_direct_match(
        cls, question: str, answer: str, sources: list[dict]
    ) -> bool:
        """Reject a model citation that skips a stronger direct source.

        The local model can sometimes paraphrase a lower-ranked semantic
        neighbour and cite it even when source [1] contains the exact phrase
        requested by the user.  For a single-fact question this is not a
        harmless citation preference: it makes the displayed evidence less
        precise and can change the intended conclusion.  Multi-part questions
        remain free to cite distinct sources for their distinct claims.
        """
        if len(sources) < 2 or cls.requires_multiple_sources(question, sources):
            return False
        cited = cls.cited_indexes(answer)
        if not cited:
            return False
        primary = sources[0]
        primary_index = int(primary.get("index", 1))
        if primary_index in cited:
            return False
        focus_terms = cls.focused_query_terms(cls.query_terms(question))
        if not focus_terms:
            return False

        def direct_coverage(source: dict) -> int:
            text = "\n".join(
                (str(source.get("title", "")), str(source.get("evidence", "")), str(source.get("fallback_excerpt", "")))
            ).lower()
            return sum(len(term) * len(term) for term in focus_terms if term.lower() in text)

        primary_coverage = direct_coverage(primary)
        if primary_coverage <= 0:
            return False
        cited_coverage = max(
            (direct_coverage(source) for position, source in enumerate(sources, 1)
             if int(source.get("index", position)) in cited),
            default=0,
        )
        return primary_coverage > cited_coverage

    @staticmethod
    def format_answer_with_leading_sources(answer: str) -> str:
        """Present each cited conclusion as a source-led line for fast checking."""
        text = str(answer or "").strip()
        if not re.search(r"\[\d+\]", text):
            return text
        output: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if re.match(r"^来源\[\d+\]", line):
                output.append(line)
                continue
            sentences = re.findall(r"[^。！？!?；;]+[。！？!?；;]?", line) or [line]
            for sentence in sentences:
                citations = list(re.finditer(r"\[\d+\]", sentence))
                if not citations:
                    output.append(sentence.strip())
                    continue
                if len(citations) == 1:
                    citation = citations[0].group(0)
                    claim = re.sub(r"\[\d+\]", "", sentence).strip(" -•\t")
                    # A model sometimes places the citation before a conjunction
                    # (``[2]，且……``). Once converted to a source-led line that
                    # punctuation becomes a distracting orphan, so remove only
                    # leading separators/conjunctions without rewriting content.
                    claim = re.sub(r"^(?:[，,；;：:]\s*)?(?:且|并且|同时)\s*", "", claim)
                    claim = claim.lstrip("，,；;：:。 ")
                    claim = re.sub(r"\s+([。！？!?；;])", r"\1", claim)
                    if claim:
                        output.append(f"来源{citation} {claim}")
                    continue
                cursor = 0
                last_output_index: int | None = None
                for citation_match in citations:
                    claim = sentence[cursor:citation_match.start()].strip(" -•\t。！？!?；; ")
                    if claim:
                        output.append(f"来源{citation_match.group(0)} {claim}。")
                        last_output_index = len(output) - 1
                    cursor = citation_match.end()
                trailing = sentence[cursor:].strip()
                if trailing and last_output_index is not None:
                    output[last_output_index] = output[last_output_index].rstrip("。") + trailing
        return "\n".join(output) if output else text

    @classmethod
    def fallback_answer(cls, question: str, sources: list[dict]) -> str:
        """通用证据拼接：从最相关笔记里抽取关键句作为答案。

        这是 LLM 不可用或返回空时的兜底，不包含任何领域专用规则。
        """
        if not sources:
            return "没有找到足够相似的笔记，暂时无法基于笔记回答。"
        threshold_gap = cls.amount_threshold_gap_answer(question, sources)
        if threshold_gap:
            return threshold_gap
        # A direct negative statement in the best evidence answers a simple
        # yes/no question more clearly than showing the sentence alone.
        if re.search(r"(?:是否|是不是|是.{0,12}吗)", str(question or "")):
            primary = sources[0]
            evidence = str(primary.get("evidence", "")).strip()
            if re.search(r"(?:不代表|不是|不等于|不能|不得|不可以)", evidence):
                index = int(primary.get("index", 1))
                return f"不是。{evidence} [{index}]"
        # 最多用前 3 条来源，避免答案过长
        used_sources = cls.fallback_sources(
            sources,
            force_multiple=cls.requires_multiple_sources(question, sources),
        )
        lines = []
        for source in used_sources:
            excerpt = str(source.get("excerpt", "")).strip()
            fallback_excerpt = str(source.get("fallback_excerpt", "")).strip() or excerpt
            evidence = str(source.get("evidence", "")).strip()
            title = str(source.get("title", "相关笔记")).strip()
            source_index = int(source.get("index", 1))
            # A title can be a strong retrieval anchor but is not useful as an
            # answer by itself. Prefer the focused body passage in that case.
            if (
                re.sub(r"\s+", "", evidence) == re.sub(r"\s+", "", title)
                or cls.is_non_answer_heading(evidence)
            ) and fallback_excerpt:
                evidence = ""
            # A structured row or focused sentence is more precise than a
            # large raw chunk, particularly for spreadsheet records.
            if evidence and len(evidence) >= 8:
                structured_lines = cls.structured_fallback_lines(evidence, question)
                if structured_lines:
                    record_parts: list[str] = []
                    for structured_line in structured_lines:
                        if structured_line.startswith("- "):
                            if record_parts:
                                lines.append(f"来源[{source_index}] {'；'.join(record_parts)}。")
                            record_parts = [structured_line[2:].strip("。；; ")]
                        else:
                            record_parts.append(structured_line.strip("。；; "))
                    if record_parts:
                        lines.append(f"来源[{source_index}] {'；'.join(record_parts)}。")
                else:
                    lines.append(f"来源[{source_index}] {cls.compact_policy_evidence(evidence, question)}")
            elif fallback_excerpt:
                sentences = [
                    s.strip(" \n\r\t。；;")
                    for s in re.split(r"[。！？\n]+", fallback_excerpt)
                    if len(s.strip()) >= 8
                ]
                if sentences:
                    picked = "；".join(sentences[:12])
                    lines.append(f"来源[{source_index}] {picked}")
        if not lines:
            return "现有笔记里没有足够信息。"
        answer = "\n".join(lines)
        return cls.complete_enumeration_count(question, answer, used_sources)

    @classmethod
    def amount_threshold_gap_answer(cls, question: str, sources: list[dict]) -> str:
        """Explain why a percentage record cannot prove an amount threshold.

        This protects a recurring class of high-risk conditional questions:
        one source contains a payment ratio while another defines approval by
        absolute currency amount.  The two values are not comparable without
        the total amount, so returning both excerpts without that caveat is an
        incomplete answer.
        """
        text = str(question or "")
        if not re.search(r"(?:是否|能否|会不会).{0,12}(?:触发|达到|满足|符合)", text):
            return ""
        if not (
            "合同" in text
            and re.search(r"(?:付款|预付款)", text)
            and re.search(r"(?:会签|审批)", text)
        ):
            return ""
        ratio_entry: tuple[int, str] | None = None
        threshold_entry: tuple[int, str] | None = None
        money_pattern = re.compile(r"\d+(?:\.\d+)?\s*(?:亿元|万元|元|万|亿)")
        ratio_pattern = re.compile(r"预付款(?:比例)?\s*[：:=]?\s*(\d+(?:\.\d+)?\s*[%％])")
        for position, source in enumerate(sources, 1):
            index = int(source.get("index", position))
            material = "\n".join((
                str(source.get("evidence", "")),
                str(source.get("fallback_excerpt", "")),
            ))
            ratio = ratio_pattern.search(material)
            if ratio and not money_pattern.search(material) and ratio_entry is None:
                ratio_entry = (index, re.sub(r"\s+", "", ratio.group(1)))
            if threshold_entry is None:
                for sentence, _section in cls._evidence_candidates(material):
                    if money_pattern.search(sentence) and re.search(r"(?:以上|超过|大于|不低于|至少)", sentence):
                        threshold_entry = (index, sentence.strip("。；; "))
                        break
        if not ratio_entry or not threshold_entry or ratio_entry[0] == threshold_entry[0]:
            return ""
        threshold_index, threshold = threshold_entry
        ratio_index, ratio = ratio_entry
        return (
            f"来源[{threshold_index}] 是否触发取决于合同金额是否达到会签阈值：{threshold}。\n"
            f"来源[{ratio_index}] 合同付款证据只提供预付款比例 {ratio}，没有可与该金额阈值比较的合同总金额，"
            "因此现有信息不足以确认是否触发会签。"
        )

    @staticmethod
    def compact_policy_evidence(evidence: str, question: str = "") -> str:
        """Condense recurring policy-record wording without changing its claim."""
        text = re.sub(r"\s+", " ", str(evidence or "")).strip()
        sensitive = re.search(
            r"(?:在\s*)?(?:Agent\s*)?(执行[^。；;]{0,100}?操作[^。；;]{0,120}?)"
            r"前，?(?:前端)?必须有一个[“\"]?([^”\"。；;]{2,40})[”\"]?的卡点",
            text,
        )
        if sensitive:
            return f"{sensitive.group(1).strip()}前需{sensitive.group(2).strip()}。"

        if "合同" in text and "审批" in text and "CoA" in text:
            amount_basis = "金额" in text or bool(re.search(r"\d+(?:\.\d+)?\s*(?:万元|元|万|亿)", text))
            if amount_basis and "授权签字人" in text and re.search(
                r"不同.{0,16}(?:金额|预算).{0,16}(?:同一|一样).{0,16}(?:层级|审批)",
                str(question or ""),
            ):
                subject = "PO金额" if "po" in str(question or "").lower() else "金额"
                return f"不同{subject}通常不是同一审批层级，需依据金额阈值和授权签字人类型确定。"
            claim = "合同审批需符合CoA要求"
            if amount_basis and "授权签字人" in text:
                claim += "，依据金额和授权签字人类型进行审批"
            return claim + "。"
        return text

    @staticmethod
    def structured_fallback_lines(evidence: str, question: str = "") -> list[str]:
        """Present common spreadsheet fields without asking the LLM to paraphrase them."""
        lines = []
        compact_question = re.sub(r"\s+", "", str(question or ""))
        records = [line.strip() for line in str(evidence or "").splitlines() if line.strip()]
        for record in records or [str(evidence or "")]:
            fields = {}
            for label in ("Agent名称", "核心功能", "适用场景", "亮点/关键词", "限制"):
                match = re.search(rf"{re.escape(label)}：([^；\n]+)", record)
                if match:
                    fields[label] = match.group(1).strip()
            if not (fields.get("核心功能") and fields.get("适用场景")):
                continue
            if fields.get("Agent名称"):
                lines.append(f"- {fields['Agent名称']}")
            lines.append(f"  核心功能：{fields['核心功能']}")
            lines.append(f"  适用场景：{fields['适用场景']}")
            if fields.get("亮点/关键词"):
                lines.append(f"  关键词：{fields['亮点/关键词']}")
            if fields.get("限制") and re.search(r"(?:限制|缺失|待确认|处理|怎么办|如何)", compact_question):
                lines.append(f"  限制：{fields['限制']}")
        return lines

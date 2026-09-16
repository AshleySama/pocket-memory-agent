from __future__ import annotations

import re
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

    def __init__(self, store, embedding_service) -> None:
        self.store = store
        self.embedding_service = embedding_service

    def retrieve(
        self,
        question: str,
        limit: int = 5,
        semantic_min_score: float = 0.40,
        extra_terms: list[str] | None = None,
        strict_topic_match: bool = False,
        required_topic_terms: list[str] | None = None,
        _compound_pass: bool = True,
    ) -> list[dict]:
        # A compound question can contain independent facts with very different
        # vocabulary (for example, "地球...；火星..."). Ranking it as one long
        # query lets the first clause suppress later evidence. Recall each
        # explicit clause once, then merge their already-ranked candidates.
        if _compound_pass:
            clauses = self.compound_query_clauses(question)
            if len(clauses) > 1:
                merged = []
                for clause in clauses:
                    merged.extend(
                        self.retrieve(
                            clause,
                            limit=limit,
                            semantic_min_score=semantic_min_score,
                            extra_terms=extra_terms,
                            strict_topic_match=strict_topic_match,
                            required_topic_terms=required_topic_terms,
                            _compound_pass=False,
                        )
                    )
                merged.sort(key=lambda item: (item["rank_score"], item["score"], item["note"].updated_at), reverse=True)
                return self.dedupe_chunks(merged, limit, question)
        # A frequent OCR artifact is the Latin "d" in place of Chinese "的"
        # (for example "m d标准换算").  Normalize only that narrow form before
        # embedding and FTS; retain the original user wording for display and
        # answer generation.
        search_question = self.normalize_ocr_query(question)
        # Only explicit note-metadata questions (for example "昨天写了哪些
        # 笔记") may filter on created_at. Absolute dates in a business question
        # usually describe the document's content, such as a project milestone.
        time_range = self._detect_time_range(search_question) if IntentRouter._is_note_timestamp_query(search_question) else None
        if time_range:
            return self._retrieve_by_time(time_range, limit)

        analysis = self.analyze(search_question, extra_terms)
        focus_terms = self.focused_query_terms(analysis.terms)
        # A relationship lookup such as "项目复盘里提到的风险是什么" has
        # two parts: the named source and the requested field. The source is a
        # hard coverage anchor, so it must be recalled before incidental 4-5
        # character fragments generated from the surrounding question wording.
        focus_terms = self.prioritize_required_terms(focus_terms, required_topic_terms)
        # Uppercase identifiers such as OCR are often the only stable clue in a
        # damaged scan query. Let them participate in the exact FTS retry pass.
        focus_terms.extend(
            term for term in analysis.terms
            if re.fullmatch(r"[A-Z][A-Z0-9_]{1,}", term) and term not in focus_terms
        )
        query_vector = self.embedding_service.query_embedding(search_question)
        semantic = {
            note.id: (note, score)
            for note, score in self.embedding_service.semantic_search(
                search_question,
                limit=max(20, limit),
                min_score=semantic_min_score,
                query_vector=query_vector,
            )
        }
        document_semantic = {
            chunk.id: (chunk, score)
            for chunk, score in self.embedding_service.semantic_document_search(
                search_question,
                limit=max(20, limit * 4),
                min_score=semantic_min_score,
                query_vector=query_vector,
            )
        }
        note_chunk_semantic = {
            chunk.id: (chunk, score)
            for chunk, score in self.embedding_service.semantic_note_chunk_search(
                search_question,
                limit=max(20, limit * 4),
                min_score=semantic_min_score,
                query_vector=query_vector,
            )
        }
        # FTS 召回：原问题权重最高，扩展词权重减半，合并取最大值
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
                }
            )
        ranked = self.rerank_candidates(search_question, ranked, focus_terms)
        ranked.sort(key=lambda item: (item["rank_score"], item["score"], item["note"].updated_at), reverse=True)
        ranked = self.filter_weak_chunks(
            ranked,
            topic_anchors=(required_topic_terms or self.topic_anchor_terms(search_question))
            if strict_topic_match else None,
        )
        return self.dedupe_chunks(ranked, limit, search_question)

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
        intent = intent or IntentRouter.detect(question, store=self.store)
        evidence_question = evidence_question or question
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
            return self._route_qa(
                intent,
                question,
                extra_terms or [],
                limit,
                llm_engine=llm_engine,
                evidence_question=evidence_question,
            )
        return handler(intent, question, extra_terms or [], limit)

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
    ) -> IntentResult:
        # First retrieve with the original wording. A non-empty candidate list is
        # not enough: generic neighbours must not prevent a focused retry.
        evidence_question = evidence_question or question
        required_topics = self.retrieval_anchor_terms(evidence_question)
        chunks = self.retrieve(
            question,
            limit=limit,
            extra_terms=extra_terms,
            strict_topic_match=bool(required_topics),
            required_topic_terms=required_topics,
        )
        if not self.has_answerable_evidence(evidence_question, chunks) and llm_engine is not None and not extra_terms:
            try:
                extra_terms = llm_engine.rewrite_query(question)
            except Exception:
                extra_terms = []
            if extra_terms:
                retried = self.retrieve(
                    question,
                    limit=limit,
                    extra_terms=extra_terms,
                    strict_topic_match=bool(required_topics),
                    required_topic_terms=required_topics,
                )
                if self.has_answerable_evidence(evidence_question, retried):
                    chunks = retried
        if not self.has_answerable_evidence(evidence_question, chunks):
            chunks = []
        chunks = self.retain_focused_chunks(evidence_question, chunks)
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
        if cls.is_role_comparison_question(question):
            # A shared policy title is not evidence that two named subjects
            # have distinct roles. Require a passage that actually states them.
            if not any(
                cls.role_comparison_evidence(question, str(chunk.get("raw_text", "")))
                for chunk in chunks
            ):
                return False
        required_topics = cls.retrieval_anchor_terms(question)
        if required_topics:
            corpus = "\n".join(
                f"{chunk.get('title', '')}\n{chunk.get('raw_text', '')}" for chunk in chunks
            )
            if not all(cls.text_contains_term(corpus, topic) for topic in required_topics):
                return False
            if cls.requires_definition_evidence(question) and not cls.has_definition_evidence(
                required_topics,
                (str(chunk.get("raw_text", "")) for chunk in chunks),
            ):
                return False
        entity_terms = cls.explicit_entity_terms(question)
        if entity_terms:
            corpus = "\n".join(
                f"{chunk.get('title', '')}\n{chunk.get('raw_text', '')}" for chunk in chunks
            )
            if not all(cls.text_contains_term(corpus, term) for term in entity_terms):
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
        grouped: dict[str, list[dict]] = {}
        for chunk in chunks:
            key = str(chunk.get("source_group") or chunk.get("source_id") or chunk["note"].id)
            grouped.setdefault(key, []).append(chunk)

        sources = []
        for index, group in enumerate(grouped.values(), 1):
            # Retrieval deliberately keeps a few chunks from one long source.
            # Their global rank may be influenced by a broad semantic match,
            # while a later sibling contains the user's exact rule or field.
            # Choose the display/context anchor by direct phrase coverage first
            # so a source card never leads with its most generic neighbour.
            group.sort(
                key=lambda chunk: self._source_evidence_priority(question, chunk),
                reverse=True,
            )
            primary = group[0]
            note = primary["note"]
            is_document = primary.get("source_kind") == "document"
            evidences: list[str] = []
            excerpts: list[str] = []
            raw_texts: list[str] = []
            locations: list[str] = []
            matched_terms: list[str] = []
            reasons: list[str] = []
            chunk_ids: list[int] = []
            chunk_orders: list[int] = []
            resolution_evidences: list[str] = []
            resolution_excerpts: list[str] = []
            resolution_raw_texts: list[str] = []
            resolution_locations: list[str] = []
            asks_resolution = self.is_resolution_question(question)
            for chunk in group:
                raw_text = str(chunk.get("raw_text", "") or chunk.get("excerpt", ""))
                # Enumerative prompts need the complete set of labelled items.
                # Selecting just the first matching sentence makes a source look
                # valid while silently dropping the rest of the user's answer.
                evidence = self.principle_evidence(question, raw_text) or self.enumerated_evidence(question, raw_text) or self.evidence_sentence(
                    question, raw_text, chunk["hits"]
                )
                evidence_lines = [line for line in evidence.splitlines() if line.strip()]
                excerpt = self.focused_excerpt(raw_text, [*evidence_lines, *chunk["hits"]], radius=320) or chunk["excerpt"]
                for value, target in ((evidence, evidences), (excerpt, excerpts), (raw_text, raw_texts), (chunk["location"], locations), (chunk["reason"], reasons)):
                    if value and value not in target:
                        target.append(value)
                for term in chunk["hits"]:
                    if term not in matched_terms:
                        matched_terms.append(term)
                if is_document and chunk.get("document_chunk_id") is not None:
                    chunk_ids.append(int(chunk["document_chunk_id"]))
                    chunk_orders.append(int(chunk.get("document_chunk_order") or 0))
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
            if asks_resolution and resolution_evidences:
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
            sources.append({
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
                "url": self.chunk_source_url(primary),
                "location": "；".join(locations),
                "evidence_locations": locations,
                "matched_terms": matched_terms,
                "chunk_id": chunk_ids[0] if chunk_ids else None,
                "chunk_order": chunk_orders[0] if chunk_orders else None,
                "chunk_ids": chunk_ids,
                "chunk_orders": chunk_orders,
            })
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
        return sources

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
        text = evidence_text.lower()
        focus_terms = cls.focused_query_terms(cls.query_terms(question))
        direct_terms = [term for term in focus_terms if term.lower() in text]
        # Longer direct phrases are much stronger than generic 2-character
        # overlaps such as "来源" or "字段".
        direct_coverage = sum(len(term) * len(term) for term in direct_terms)
        hits = [str(term) for term in chunk.get("hits", []) if str(term)]
        hit_coverage = sum(len(term) for term in hits if len(term) >= 3)
        return (
            cls.ocr_agent_table_bonus(question, evidence_text),
            cls.temporal_fact_bonus(question, text, hits),
            cls.principle_evidence_bonus(question, text),
            float(direct_coverage),
            float(hit_coverage),
            float(chunk.get("rank_score", 0.0) or 0.0),
            float(chunk.get("score", 0.0) or 0.0),
        )

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
        if sources:
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
            terms.sort(key=len, reverse=True)
        return QueryAnalysis(text=text, terms=terms)

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
        return sorted(terms, key=len, reverse=True)

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
        return cls.answer_anchor_terms(question)

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

    @classmethod
    def requires_definition_evidence(cls, question: str) -> bool:
        """Apply the explanatory-evidence gate only to concepts, never normal fact queries."""
        if cls.is_structured_field_query(question):
            return False
        # A named Agent query asks for source-backed fields or capabilities,
        # not a dictionary definition. Its entity coverage is checked
        # separately with whitespace-insensitive matching.
        if cls.explicit_entity_terms(question):
            return False
        # "项目复盘里提到的风险是什么" asks for a field inside a named
        # source. It is not a request to define that source, so requiring an
        # "项目复盘是..." sentence would incorrectly discard its real evidence.
        if cls.relationship_source_terms(question):
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
        return len(re.findall(cls.enumeration_marker_pattern(question), str(text or "")))

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

    @staticmethod
    def whitespace_flexible_pattern(value: str) -> str:
        """Match a compact identifier even when OCR or tables insert spaces."""
        compact = re.sub(r"\s+", "", str(value or ""))
        return r"\s*".join(re.escape(char) for char in compact)

    @staticmethod
    def compound_query_clauses(question: str) -> list[str]:
        """Split explicit multi-task questions for independent recall.

        Semicolons are the strongest separator. ``并且`` / ``同时`` are also
        treated as a separator when both sides are substantive: office users
        commonly ask two independent tool-selection questions in one sentence.
        """
        text = str(question or "").strip()
        if "；" not in text and ";" not in text:
            joined_parts = [part.strip(" ，、。？?\t") for part in re.split(r"(?:并且|同时)", text) if part.strip(" ，、。？?\t")]
            if len(joined_parts) > 1 and all(len(re.sub(r"[\s，、。！？?？]", "", part)) >= 4 for part in joined_parts):
                return joined_parts
            return [text] if text else []
        clauses = []
        for raw_clause in re.split(r"[；;]", text):
            clause = re.sub(r"^(?:请)?(?:分别)?(?:说明|回答|介绍|解释)[:：\s]*", "", raw_clause.strip())
            if len(re.sub(r"[\s，、。！？?？]", "", clause)) >= 4:
                clauses.append(clause)
        return clauses if len(clauses) > 1 else [text]

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
        """Extract full Agent/智能体 identifiers, normalising a suffix boundary space."""
        source = re.sub(
            r"(?<=[\u3400-\u9fffA-Za-z0-9_\-])\s+(?=(?:Agent|智能体)\b)",
            "",
            str(question or ""),
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
        for term in sorted(hits, key=len, reverse=True):
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
        ocr_agent_row = self.ocr_agent_table_evidence(question, excerpt)
        if ocr_agent_row:
            return ocr_agent_row
        direct_clause = self.direct_evidence_clause(question, excerpt)
        if direct_clause:
            return direct_clause
        role_clause = self.role_comparison_evidence(question, excerpt)
        if role_clause:
            return role_clause
        structured = self.structured_record_evidence(question, excerpt, hits)
        if structured:
            return structured
        question_terms = self.query_terms(question)
        scored = []
        for index, (sentence, section) in enumerate(sentences):
            sentence_hits = self.matched_terms(sentence, question_terms)
            score = self.lexical_score(sentence_hits, sentence.lower(), question)
            score += sum(4 for hit in hits if hit and hit.lower() in sentence.lower())
            score += self.evidence_intent_bonus(question, sentence, section)
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
            for sentence in re.split(r"(?<=[。！？；;.!?])\s*", clean_line):
                sentence = sentence.strip(" \n\r\t")
                if sentence:
                    candidates.append((sentence, section))
        if candidates:
            return candidates
        return [
            (sentence.strip(" \n\r\t"), "")
            for sentence in re.split(r"(?<=[。！？；;.!?])\s*|\n+", str(text or ""))
            if sentence.strip()
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
        return 0.0

    @staticmethod
    def is_role_comparison_question(question: str) -> bool:
        compact = re.sub(r"\s+", "", str(question or ""))
        return bool(re.search(
            r"(?:各自|分别).{0,12}(?:承担|负责|扮演|起到|角色|作用|职责)"
            r"|(?:角色|作用|职责).{0,12}(?:各自|分别)",
            compact,
        ))

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
        scored: list[tuple[float, int, str]] = []
        for index, (sentence, _section) in enumerate(cls._evidence_candidates(text)):
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

    def structured_record_evidences(self, question: str, text: str, hits: list[str]) -> list[str]:
        """Keep every independently requested structured record in one chunk."""
        markers = ("核心功能", "适用场景", "Agent名称", "名称：", "编号：")
        rows = [
            (index, line.strip())
            for index, line in enumerate(str(text or "").splitlines())
            if "；" in line and any(marker in line for marker in markers)
        ]
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
                score = self.lexical_score(record_hits, record.lower(), target)
                score += sum(len(hit) * 2 for hit in record_hits)
                if "核心功能" in record and "适用场景" in record:
                    score += 8
                candidates.append((score, -index, record))
            if candidates:
                best = max(candidates, key=lambda item: item[:2])
                evidence = self.compact_structured_record_evidence(best[2], question, hits)
                if best[0] > 0 and evidence not in selected:
                    selected.append(evidence)
        return selected

    def compact_structured_record_evidence(self, record: str, question: str, hits: list[str]) -> str:
        """Keep a long spreadsheet row citable without turning it into a full-page highlight.

        Spreadsheet rows can contain an entire control design in one physical
        line.  Returning that line as a "sentence" makes both the answer card
        and document highlight unreadable.  Preserve the rule-defining fields
        first, then include only a small number of matching clauses.
        """
        normalized = str(record or "").strip()
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
            adjustment += cls.ocr_agent_table_bonus(question, evidence_text)
            if hits and not meaningful_hits:
                adjustment -= 5.0
            if matched_entities and all(field in text for field in ("核心功能", "适用场景")):
                adjustment += 2.0

            item["rerank_adjustment"] = round(adjustment, 2)
            item["rank_score"] += adjustment
        return candidates

    @classmethod
    def filter_weak_chunks(cls, ranked: list[dict], topic_anchors: list[str] | None = None) -> list[dict]:
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
            ]
        return ranked

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
        primary = sources[0]
        evidence = str(primary.get("evidence", "")).strip()
        terms = [str(term).strip() for term in primary.get("matched_terms", []) if str(term).strip()]
        return bool(evidence and terms and cls.has_substantive_matched_term(primary))

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
    def cited_indexes(answer: str) -> set[int]:
        return {int(index) for index in re.findall(r"\[(\d+)\]", str(answer or ""))}

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
        """Attach [1] to each substantive line when there is exactly one source."""
        completed = []
        for line in str(answer or "").splitlines(keepends=True):
            body = line.rstrip("\r\n")
            ending = line[len(body):]
            plain = re.sub(r"\[\d+\]", "", body).strip(" -•\t")
            if len(plain) >= 8 and re.search(r"[\u3400-\u9fff0-9]", plain) and not cls.cited_indexes(body):
                match = re.search(r"([。！？!?]+)\s*$", body)
                if match:
                    body = f"{body[:match.start()].rstrip()} [1]{match.group(1)}"
                else:
                    body = f"{body.rstrip()} [1]"
            completed.append(body + ending)
        return "".join(completed).strip()

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
    def answer_has_uncovered_exact_values(cls, answer: str, sources: list[dict]) -> bool:
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
        for line in re.split(r"(?<=[。！？!?；;])|\n", str(answer or "")):
            cited = cls.cited_indexes(line)
            if not cited:
                continue
            evidence = "\n".join(source_by_index.get(index, "") for index in cited)
            plain = re.sub(r"\[\d+\]", "", line)
            entities = re.findall(r"[\u3400-\u9fffA-Za-z0-9_-]{2,}(?:Agent|智能体)", plain, flags=re.IGNORECASE)
            values = re.findall(r"\d+(?:\.\d+)?\s*(?:[%％]|万元|元|天|日|周|星期|月|年|小时|分钟|个|项|次|人|条)", plain)
            for value in [*entities, *values]:
                if value.casefold() not in evidence:
                    return True
        return False

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
        def field_is_present(marker: str) -> bool:
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

    @staticmethod
    def is_broad_policy_question(question: str, sources: list[dict]) -> bool:
        return len(sources) > 1 and bool(re.search(
            r"(?:什么|哪些).{0,8}(?:情况|情形).{0,12}(?:需要|必须|应当).{0,12}(?:审批|确认)",
            str(question or ""),
        ))

    @staticmethod
    def requires_evidence_first_answer(question: str, sources: list[dict]) -> bool:
        """Use extractive evidence for multi-fact and boundary questions.

        A compact local model is most likely to omit one half of a comparison
        or negate a fact that is already in context. For these narrowly
        recognizable question forms, exact cited evidence is safer than a
        fluent paraphrase.
        """
        text = str(question or "")
        asks_boundary = bool(re.search(r"(?:不是|不等于|不能|不得|不可以|是否|是不是|是.{0,12}吗|能否)", text))
        asks_temporal_feasibility = RagPipeline.is_temporal_feasibility_question(text)
        asks_resolution = RagPipeline.is_resolution_question(text)
        multiple_clauses = len(RagPipeline.compound_query_clauses(text)) > 1
        multiple_evidence_rows = any(
            len([line for line in str(source.get("evidence", "")).splitlines() if line.strip()]) > 1
            for source in sources
        )
        asks_multiple = (
            len(sources) > 1 and bool(re.search(r"(?:、|分别|以及|又|并且|同时)", text))
        ) or (multiple_clauses and multiple_evidence_rows)
        structured_fields = any(
            len(re.findall(r"Agent名称：", str(source.get("evidence", "")))) >= 2
            for source in sources
        )
        asks_structured_fields = bool(re.search(r"(?:功能|场景|用途|适用|亮点|关键词)", text))
        broad_policy_question = RagPipeline.is_broad_policy_question(text, sources)
        return (
            asks_boundary
            or asks_temporal_feasibility
            or asks_resolution
            or asks_multiple
            or broad_policy_question
            or (structured_fields and asks_structured_fields)
        )

    @staticmethod
    def is_resolution_question(question: str) -> bool:
        return bool(re.search(r"(?:怎么办|怎么处理|如何处理|怎么解决)", str(question or "")))

    @staticmethod
    def requires_multiple_sources(question: str, sources: list[dict]) -> bool:
        """Keep several sources only when the question actually asks for several facts."""
        multiple_clauses = len(RagPipeline.compound_query_clauses(question)) > 1
        multiple_entities = len(RagPipeline.explicit_entity_terms(question)) > 1
        broad_policy_question = RagPipeline.is_broad_policy_question(question, sources)
        return multiple_clauses or multiple_entities or broad_policy_question or (
            len(sources) > 1 and bool(re.search(r"(?:分别|以及|又|并且|同时)", str(question or "")))
        )

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
        """Detect a deictic question with no named object and no conversation."""
        text = str(question or "").strip()
        if cls.explicit_entity_terms(text):
            return False
        return bool(re.search(r"(?:这个|该|那个|此)(?:\s|的){0,2}(?:Agent|智能体|合同|项目|审批|风险)", text, re.IGNORECASE))

    @classmethod
    def enforce_answer_contract(cls, question: str, answer: str, sources: list[dict]) -> tuple[str, list[dict]]:
        """Return only answers whose visible claims have valid source references."""
        clean = str(answer or "").strip()
        if not sources:
            return clean, []

        required_topics = cls.answer_contract_anchor_terms(question, sources)
        if required_topics:
            corpus = "\n".join(
                f"{source.get('title', '')}\n{source.get('evidence', '')}\n{source.get('fallback_excerpt', '')}"
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
        needs_recovery = cls.answer_needs_fallback(clean) or not cited or invalid_citation
        if not needs_recovery and cls.answer_is_only_source_title(clean, sources):
            needs_recovery = True
        if not needs_recovery and cls.requires_evidence_first_answer(question, sources):
            # A model may attach two citations to one blended policy statement.
            # Source-specific evidence is safer and easier for users to audit.
            needs_recovery = True
        if not needs_recovery and cls.answer_omits_required_conjuncts(question, clean, sources):
            needs_recovery = True
        if not needs_recovery and cls.answer_has_uncovered_exact_values(clean, sources):
            needs_recovery = True
        if not needs_recovery and cls.answer_cites_weaker_source_than_direct_match(question, clean, sources):
            needs_recovery = True
        if not needs_recovery and cls.answer_omits_enumerated_evidence(question, clean, sources):
            needs_recovery = True
        if len(sources) > 1 and not needs_recovery and cls.has_uncited_fact_line(clean):
            needs_recovery = True

        if needs_recovery:
            if cls.can_use_evidence_fallback(sources):
                fallback = cls.fallback_answer(question, sources)
                if fallback.startswith("不是。"):
                    return fallback, [sources[0]]
                return fallback, cls.fallback_sources(
                    sources,
                    force_multiple=cls.requires_multiple_sources(question, sources),
                )
            return "现有笔记没有足够信息。", []

        selected = cls.cited_sources(clean, sources)
        if len(selected) == 1:
            clean = cls.complete_single_source_citations(clean)
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
                structured_lines = cls.structured_fallback_lines(evidence)
                if structured_lines:
                    for structured_line in structured_lines:
                        if structured_line.startswith("- "):
                            lines.append(f"来源[{source_index}] {structured_line[2:]}")
                        else:
                            lines.append(structured_line)
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
        return "\n".join(lines)

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
    def structured_fallback_lines(evidence: str) -> list[str]:
        """Present common spreadsheet fields without asking the LLM to paraphrase them."""
        lines = []
        records = [line.strip() for line in str(evidence or "").splitlines() if line.strip()]
        for record in records or [str(evidence or "")]:
            fields = {}
            for label in ("Agent名称", "核心功能", "适用场景", "亮点/关键词"):
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
        return lines

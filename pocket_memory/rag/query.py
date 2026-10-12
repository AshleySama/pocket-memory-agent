# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class QueryUnderstanding:
    """A bounded, inspectable interpretation of one user question.

    Conversation history is deliberately used only to recover an omitted
    subject.  Assistant answers never become retrieval evidence and a complete
    new question never inherits the previous topic.
    """

    original_query: str
    normalized_query: str
    retrieval_query: str
    evidence_question: str
    contract_question: str
    mode: str
    subject: str
    confidence: float
    reason: str
    entities: tuple[str, ...]
    requested_fields: tuple[str, ...]
    operation: str
    time_expressions: tuple[str, ...]
    aliases: tuple[str, ...]
    prior_user_question: str = ""

    @property
    def history_used(self) -> bool:
        return self.mode == "contextual_followup"

    def conversation_payload(self) -> dict:
        questions = [self.normalized_query]
        if self.history_used and self.prior_user_question:
            questions.insert(0, self.prior_user_question)
        return {
            "mode": self.mode,
            "subject": self.subject or self.normalized_query,
            "questions": questions,
            "confidence": round(self.confidence, 2),
        }

    def diagnostics(self) -> dict:
        """Return content-light diagnostics suitable for local eval reports."""
        return {
            "mode": self.mode,
            "history_used": self.history_used,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "entity_count": len(self.entities),
            "requested_fields": list(self.requested_fields),
            "operation": self.operation,
            "time_expression_count": len(self.time_expressions),
            "alias_count": len(self.aliases),
        }


class QueryResolver:
    """Resolve only explicit or strongly elliptical follow-up questions.

    This is intentionally deterministic and conservative.  It costs no model
    call on a CPU-only laptop and gives regression tests a stable decision
    boundary.  Broader model-based rewriting belongs to the later, gated query
    expansion stage rather than this safety-critical subject resolver.
    """

    _AMBIGUOUS_ACTION = re.compile(
        r"^(?:为什么|为何|怎么)(?:会)?(?:不能|无法|不可以|没法)"
        r"(?:回退|删除|移除|清空|保存|上传|下载|导出|合并|还原|打开)[?？。！!]*$",
        re.IGNORECASE,
    )
    _DEICTIC_START = re.compile(
        r"^(?:那(?:么)?|它|其|这个|那个|这家|那家|该|上述|前述|这里|其中|"
        r"这笔|那笔|这条|那条|这份|那份|这项|那项)(?!次演示)",
        re.IGNORECASE,
    )
    _ELLIPTICAL_START = re.compile(
        r"^(?:超过|低于|高于|少于|多于|不到|达到|正式上线前|上线前|发布前|"
        r"签约前|签约后|审批后|到期前|发生后|完成后)",
        re.IGNORECASE,
    )
    _FIELD_ONLY = re.compile(
        r"^(?:那)?(?:注册信息|资质|金额|阈值|期限|日期|时间|负责人|场景|功能|"
        r"风险|状态|下一步|模型大小|多大|原因|依据|结论|范围|流程|审批|"
        r"适用场景|核心功能)(?:呢|是什么|有哪些|有多少|怎么做|如何处理)?[?？。！!]*$",
        re.IGNORECASE,
    )
    _GENERIC_SUBJECTS = {
        "项目", "合同", "风险", "问题", "资料", "文件", "文档", "记录", "方案",
        "流程", "规则", "agent", "智能体", "模型", "这个", "那个", "它", "其",
    }

    _COUNT_CLASSIFIERS = (
        r"(?:个|项|条|种|次|起|例|笔|家|名|位|人|份|章|页|"
        r"风险|问题|功能|步骤|文件|笔记|文档|记录|资料|来源|证据|标签|分类)"
    )

    _FIELD_PATTERNS = (
        ("date", r"(?:日期|时间|何时|什么时候|哪一年|哪天|期限|截止|上线)"),
        ("amount", r"(?:金额|预算|价格|费用|成本|万|元|%|比例|阈值)"),
        (
            "size",
            r"(?:模型|权重|文件|安装包|索引|数据库).{0,8}(?:多大|大小|体积|容量|占用)"
            r"|(?:多大|大小|体积|容量|占用).{0,8}(?:模型|权重|文件|安装包|索引|数据库)",
        ),
        ("owner", r"(?:谁|负责人|责任人|角色|由谁|谁来)"),
        ("function", r"(?:核心功能|功能|作用|能做什么|做什么)"),
        ("scenario", r"(?:适用场景|使用场景|什么场景|何时使用)"),
        ("risk", r"(?:风险|缺口|问题|隐患)"),
        ("status", r"(?:状态|进度|是否完成|有没有)"),
        ("action", r"(?:下一步|怎么办|怎么做|如何处理|措施|行动项)"),
        ("definition", r"(?:是什么|是啥|什么意思|定义|怎么回事)"),
    )

    @classmethod
    def resolve(cls, question: str, history: Iterable[object] | None = None) -> QueryUnderstanding:
        original = str(question or "").strip()
        normalized = cls.normalize(original)
        prior = cls._recent_user_question(history, normalized)
        continuation_reason = cls._continuation_reason(normalized)
        subject = cls._subject_from_question(prior) if prior and continuation_reason else ""
        qualifier = cls._contextual_state_qualifier(prior) if subject else ""
        if qualifier and not re.search(r"(?:红色|橙色|黄色|蓝色|绿色)", normalized):
            subject = f"{subject}{qualifier}"

        confidence = 0.0
        mode = "single_turn"
        reason = "complete_current_question"
        if prior and continuation_reason and cls._usable_subject(subject):
            confidence = 0.95 if continuation_reason == "deictic_reference" else 0.9
            mode = "contextual_followup"
            reason = continuation_reason
        elif continuation_reason and not prior:
            reason = "followup_without_history"
        elif continuation_reason:
            reason = "history_subject_not_reliable"

        if mode == "contextual_followup":
            # Express the recovered subject as one natural question.  A
            # semicolon or synthetic "previous/current" label is interpreted
            # by the downstream multi-intent splitter as two independent
            # obligations, which can incorrectly turn a valid follow-up into
            # an insufficient-evidence refusal.
            retrieval = f"关于{subject}，{normalized}"
            evidence_question = retrieval
            contract_question = retrieval
        else:
            subject = ""
            retrieval = normalized
            evidence_question = normalized
            contract_question = normalized

        entities = cls._entities(normalized, subject)
        aliases = cls._aliases(normalized, entities)
        return QueryUnderstanding(
            original_query=original,
            normalized_query=normalized,
            retrieval_query=retrieval,
            evidence_question=evidence_question,
            contract_question=contract_question,
            mode=mode,
            subject=subject,
            confidence=confidence,
            reason=reason,
            entities=entities,
            requested_fields=cls._requested_fields(normalized),
            operation=cls._operation(normalized),
            time_expressions=cls._time_expressions(normalized),
            aliases=aliases,
            prior_user_question=prior if mode == "contextual_followup" else "",
        )

    @staticmethod
    def normalize(text: str) -> str:
        value = unicodedata.normalize("NFKC", str(text or "")).replace("\u00a0", " ")
        value = re.sub(r"\s+", " ", value).strip()
        # A boundary space before Agent/AI/RAG/OCR is a presentation choice,
        # not a semantic distinction.  Canonicalising it prevents users from
        # needing to remember whether the imported table contained a space.
        # Chinese field words may follow directly; Unicode \b would reject
        # that boundary, while ASCII continuations still belong to the name.
        value = re.sub(
            r"(?<=[\u3400-\u9fffA-Za-z0-9_\-])\s+(?=(?:Agent|AI|RAG|OCR)(?![A-Za-z0-9_\-]))",
            "",
            value,
            flags=re.IGNORECASE,
        )
        return value

    @classmethod
    def _recent_user_question(cls, history: Iterable[object] | None, current: str) -> str:
        items = list(history or [])[-8:]
        for item in reversed(items):
            if isinstance(item, dict):
                if str(item.get("role", "user")).lower() != "user":
                    continue
                content = item.get("content", "")
            elif isinstance(item, str):
                content = item
            else:
                continue
            candidate = cls.normalize(str(content))[:300]
            if candidate and candidate != current:
                return candidate
        return ""

    @classmethod
    def _continuation_reason(cls, question: str) -> str:
        compact = re.sub(r"\s+", "", question)
        if not compact or cls._AMBIGUOUS_ACTION.fullmatch(compact):
            return ""
        if cls._DEICTIC_START.search(compact):
            return "deictic_reference"
        if cls._ELLIPTICAL_START.search(compact):
            return "elliptical_continuation"
        if len(compact) <= 18 and cls._FIELD_ONLY.fullmatch(compact):
            return "field_ellipsis"
        return ""

    @classmethod
    def _subject_from_question(cls, question: str) -> str:
        text = cls.normalize(question).strip(" ?？。!！,，;；:：")
        if not text:
            return ""

        entity_match = re.search(
            r"(?<![A-Za-z0-9_\-])([\u3400-\u9fffA-Za-z0-9_.+\-]{2,40}(?:Agent|智能体))"
            r"(?![A-Za-z0-9_\-])",
            text,
            flags=re.IGNORECASE,
        )
        if entity_match:
            return entity_match.group(1)

        special = re.match(r"^(.{1,30}?)是(?:距离|位于|属于).+?(?:第几|哪里|什么)", text)
        if special:
            text = special.group(1)
        else:
            marker = re.search(
                r"(?:是什么意思|是多少|为多少|是什么|是啥|啥意思|什么意思|有何含义|能做什么|"
                r"可以做什么|适合什么场景|有什么特点|什么时候|何时|需要哪些|"
                r"有哪些|有几个|有多少|怎么|如何|为什么|为何|是否|能否|"
                r"什么座位|什么角色|什么功能|什么场景|第几)",
                text,
                flags=re.IGNORECASE,
            )
            if marker:
                text = text[:marker.start()]

        text = re.sub(r"^(?:请问|请介绍|请说明|请解释|帮我看看|我想知道)", "", text)
        # Keep discriminating business fields such as "风险".  Removing them
        # turns "供应商 A 风险" into the overly broad "供应商 A" and can select
        # an unrelated OCR record for the same supplier.  Only discard the
        # temporal filler that precedes the field.
        text = re.sub(r"(?:现在|当前)(?=(?:的)?(?:风险|情况|状态|信息)$)", "", text)
        # A colour-coded state is the value being queried, not part of the
        # stable subject.  Keeping it would make a follow-up such as "那绿色
        # 呢" search for both red and green states at once.
        text = re.sub(
            r"(?:红色|橙色|黄色|蓝色|绿色)(?:预警|状态|等级|阈值)?$",
            "",
            text,
        )
        text = re.sub(r"\b\d+(?:\.\d+)?\s*(?:万|万元|元|%|小时|天|日|年)\b", "", text)
        text = re.sub(r"\s+", " ", text).strip(" 的?？。!！,，;；:：")
        return text[:80]

    @classmethod
    def _contextual_state_qualifier(cls, question: str) -> str:
        """Keep a prior colour state unless the current turn replaces it.

        ``红色预警`` can be the condition that identifies a requested threshold,
        while ``那绿色呢`` explicitly replaces that condition.  The stable
        subject extractor removes colour states, so this helper restores only
        the bounded qualifier needed by an otherwise elliptical continuation.
        """
        matches = re.findall(
            r"(?:红色|橙色|黄色|蓝色|绿色)(?:预警|状态|等级|阈值)?",
            cls.normalize(question),
        )
        return matches[-1] if matches else ""

    @classmethod
    def _usable_subject(cls, subject: str) -> bool:
        compact = re.sub(r"\s+", "", subject).strip().casefold()
        return len(compact) >= 2 and compact not in cls._GENERIC_SUBJECTS

    @classmethod
    def _entities(cls, question: str, subject: str) -> tuple[str, ...]:
        text = f"{subject} {question}".strip()
        matches = re.findall(
            r"(?<![A-Za-z0-9_\-])[\u3400-\u9fffA-Za-z0-9_.+\-]{2,40}(?:Agent|智能体)"
            r"(?![A-Za-z0-9_\-])",
            text,
            flags=re.IGNORECASE,
        )
        matches += re.findall(r"\b[A-Z][A-Z0-9_-]{1,31}\b", text)
        return tuple(dict.fromkeys(item.strip() for item in matches if item.strip()))

    @classmethod
    def _requested_fields(cls, question: str) -> tuple[str, ...]:
        fields = [
            name
            for name, pattern in cls._FIELD_PATTERNS
            if re.search(pattern, question, re.IGNORECASE)
        ]
        if cls._is_count_question(question):
            fields.insert(0, "count")
        return tuple(fields)

    @classmethod
    def _operation(cls, question: str) -> str:
        if re.search(r"(?:区别|不同|相同|相比|比较|一样吗|各自)", question):
            return "compare"
        if cls._is_count_question(question):
            return "count"
        if re.search(r"(?:哪些|列出|清单|分别)", question):
            return "list"
        if re.search(r"(?:是否|是不是|能否|可以吗|吗[?？。！!]*)$", question):
            return "boolean"
        if re.search(r"(?:是什么|是啥|什么意思|定义|怎么回事)", question):
            return "definition"
        if re.search(r"(?:为什么|为何)", question):
            return "why"
        return "lookup"

    @classmethod
    def _is_count_question(cls, question: str) -> bool:
        """Distinguish enumeration counts from scalar ``多少`` questions.

        ``多少`` is also used for durations, amounts, percentages, thresholds,
        and sizes. Treating every occurrence as a count weakens evidence
        selection because the downstream contract starts looking for a list
        cardinality instead of the requested scalar value.
        """
        compact = re.sub(r"\s+", "", str(question or ""))
        if not compact:
            return False
        if re.search(rf"(?:几{cls._COUNT_CLASSIFIERS}|多少{cls._COUNT_CLASSIFIERS})", compact):
            return True
        if "数量" in compact:
            return True
        if re.search(r"(?:一共|总共)(?:有)?(?:多少)?", compact):
            scalar_markers = re.search(
                r"(?:耗时|时长|延迟|响应时间|期限|金额|预算|价格|费用|成本|"
                r"比例|比率|率|阈值|上限|下限|大小|体积|容量|占用|"
                r"毫秒|秒|分钟|小时|天|日|周|月|年|元|万|%|％|GB|MB)",
                compact,
                flags=re.IGNORECASE,
            )
            return scalar_markers is None
        return False

    @staticmethod
    def _time_expressions(question: str) -> tuple[str, ...]:
        patterns = (
            r"\d{4}\s*年(?:\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*[日号])?)?",
            # Keep a date token separate from business identifiers such as
            # ``HT-2026-021``.  The old permissive pattern backtracked to
            # ``2026-02`` inside that identifier, which incorrectly routed a
            # contract-row lookup through temporal/definition safeguards.
            r"(?<![A-Za-z0-9_-])\d{4}[-/](?:0?[1-9]|1[0-2])"
            r"(?:[-/](?:0?[1-9]|[12]\d|3[01]))?(?![A-Za-z0-9_-])",
            r"\d+(?:\.\d+)?\s*(?:分钟|小时|天|周|个月|月|年)",
            r"(?:今天|昨天|明天|本周|上周|下周|本月|上月|下月|最近\d+天|上线前|发布前|到期前)",
        )
        found: list[str] = []
        for pattern in patterns:
            found.extend(re.findall(pattern, question))
        return tuple(dict.fromkeys(found))

    @classmethod
    def _aliases(cls, question: str, entities: tuple[str, ...]) -> tuple[str, ...]:
        aliases: list[str] = []
        for entity in entities:
            compact = re.sub(r"\s+", "", entity)
            spaced = re.sub(r"(?i)(?<!\s)(Agent|AI|RAG|OCR)$", r" \1", compact)
            for value in (compact, spaced):
                if value and value.casefold() != question.casefold() and value not in aliases:
                    aliases.append(value)
        return tuple(aliases[:4])

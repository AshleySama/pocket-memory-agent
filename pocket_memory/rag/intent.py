"""查询意图路由器。

业内个人知识库 RAG 常见查询意图分三大族：
- 结构化查询族（返回数据）：时间 / 列表 / 统计 / 元数据 / 分类标签
- 内容理解族（生成文本）：摘要 / 比较 / 实体定位 / 关联推荐
- 默认兜底：通用问答

策略：规则正则优先（零延迟、零 token），未命中时由 LLM 语义兜底分类。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta


@dataclass
class Intent:
    """识别出的查询意图。

    type: time/time_period/list/stats/metadata/taxonomy/summary/compare/entity/related/qa
    params: 意图参数（time_range / topic / entities / filter 等）
    source: "rule"（正则）或 "llm"（模型分类）
    """
    type: str
    params: dict
    source: str = "rule"


@dataclass
class IntentResult:
    """意图路由结果。"""
    kind: str
    chunks: list[dict] = field(default_factory=list)
    intent: Intent | None = None
    answer: str = ""               # 预生成答案（stats/taxonomy 无需 LLM）
    data: dict = field(default_factory=dict)
    expanded_terms: list[str] = field(default_factory=list)
    prompt_override: str = ""      # summary/compare 时传给引擎的改写问题
    empty_answer: str = ""         # 无命中时的兜底文案


class IntentRouter:
    """意图检测：规则优先 + LLM 兜底。"""

    # 统计类关键词（计数 / 聚合 / 排名 / 长度 / 字数）
    STATS_PATTERNS = (
        "有多少", "几条", "几篇", "几个笔记", "多少条", "多少篇", "多少笔记",
        "总数", "总共", "统计", "占比", "比例", "分布",
        "最多", "最少", "哪类最多", "哪个分类最多", "哪个最多",
        "最长", "最短", "最大", "最小",
        "多少字", "总字数", "平均字数", "平均多少字", "一共多少字",
        "最早", "第一篇", "最新", "最近一篇", "最后一篇",
        "最久", "多久没", "最旧", "什么时候写的",
        "通常", "一般", "习惯",
    )
    # 长度/极值类查询：识别后按内容长度排序返回笔记
    LENGTH_PATTERNS = ("最长", "最短", "内容最多", "内容最少", "字数最多", "字数最少")

    # 分类体系类关键词（列出标签 / 分类）
    TAXONOMY_PATTERNS = (
        "有哪些标签", "标签列表", "标签都有什么", "都有哪些标签", "标签云",
        "有哪些分类", "分类列表", "分类都有什么", "都有哪些分类", "分类体系",
        "有哪些类别", "类别都有什么",
    )

    # 元数据过滤类（固定规则匹配）
    METADATA_RULES = (
        ("untitled", ("未命名", "没标题", "无标题", "没有标题")),
        ("no_tags", ("没有标签", "无标签", "没打标签", "没有标签的", "未打标签")),
        ("has_image", ("带图片", "有图片", "图片笔记", "带附件", "有附件")),
        ("pinned", ("置顶的", "置顶笔记", "已置顶", " pinned")),
        ("empty", ("空笔记", "空的笔记", "无内容", "空白笔记")),
        ("has_tabs", ("有页签", "有子页签", "有标签页", "多个页签", "多页签", "有tab", "有Tab")),
    )

    # 按分类/标签筛选的提示词
    CATEGORY_FILTER_HINTS = ("类的笔记", "类的有哪些", "分类的笔记", "分类的有哪些",
                             "类下的笔记", "类别里的", "分类里", "类别下")
    TAG_FILTER_HINTS = ("标签的笔记", "标签的有哪些", "带X标签", "打了标签",
                        "有标签叫", "标签是", "标签为", "标记了")

    # 最近修改类（基于 updated_at 而非 created_at）
    RECENT_EDIT_PATTERNS = (
        "最近改", "最近修改", "最近编辑", "最近更新", "最近动过",
        "今天改", "今天编辑", "今天修改", "今天更新",
        "昨天改", "昨天编辑", "昨天修改", "昨天更新",
        "刚改", "刚编辑", "刚修改", "刚更新", "刚动过",
    )

    @staticmethod
    def detect_time_range(question: str) -> tuple[date, date, str] | None:
        """识别"昨天/今天/本周/最近N天"等时间查询，返回 (start, end, label) 或 None。"""
        q = question.strip()
        today = date.today()

        m = re.search(r"最近\s*(\d+)\s*(天|小时|周|个月|月)", q)
        if m:
            n = int(m.group(1))
            unit = m.group(2)
            if unit == "天":
                start = today - timedelta(days=n)
            elif unit == "小时":
                start = today - timedelta(hours=n)
            elif unit == "周":
                start = today - timedelta(weeks=n)
            elif unit in ("个月", "月"):
                start = today - timedelta(days=n * 30)
            else:
                return None
            return (start, today + timedelta(days=1), f"最近{n}{unit}")

        m = re.search(r"(\d+)\s*天前", q)
        if m:
            n = int(m.group(1))
            target = today - timedelta(days=n)
            return (target, target + timedelta(days=1), f"{n}天前")

        # 这两天 / 这几天 / 近几天 / 近3天
        m = re.search(r"(?:这|近|最近)?\s*(\d+)\s*天(?:内|里|来)?", q)
        if m and ("这" in q or "近" in q):
            n = int(m.group(1))
            start = today - timedelta(days=n - 1)  # "这两天"含今天和昨天
            return (start, today + timedelta(days=1), f"这{n}天")
        if "这两天" in q or "这几天" in q or "近几天" in q:
            start = today - timedelta(days=1)  # 含今天和昨天
            return (start, today + timedelta(days=1), "这两天")

        # 具体日期：8月1日 / 8月1号 / 2026年8月1日
        m = re.search(r"(\d{4})?\s*年?\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", q)
        if m:
            year = int(m.group(1)) if m.group(1) else today.year
            month = int(m.group(2))
            day = int(m.group(3))
            try:
                target = date(year, month, day)
                return (target, target + timedelta(days=1), f"{year}年{month}月{day}日")
            except ValueError:
                pass

        # 具体月份：2026年8月 / 8月（当月用"本月"已覆盖，这里处理其他月）
        m = re.search(r"(\d{4})?\s*年?\s*(\d{1,2})\s*月(?!份的笔记|$)", q)
        if m and "本" not in q and "上" not in q:
            year = int(m.group(1)) if m.group(1) else today.year
            month = int(m.group(2))
            if month != today.month or year != today.year:
                try:
                    start = date(year, month, 1)
                    if month == 12:
                        end = date(year + 1, 1, 1)
                    else:
                        end = date(year, month + 1, 1)
                    return (start, end, f"{year}年{month}月")
                except ValueError:
                    pass

        if "今天" in q or "今日" in q:
            return (today, today + timedelta(days=1), "今天")
        if "昨天" in q:
            y = today - timedelta(days=1)
            return (y, y + timedelta(days=1), "昨天")
        if "前天" in q:
            d = today - timedelta(days=2)
            return (d, d + timedelta(days=1), "前天")
        if "本周" in q or "这周" in q:
            start = today - timedelta(days=today.weekday())
            return (start, today + timedelta(days=1), "本周")
        if "上周" in q:
            start = today - timedelta(days=today.weekday() + 7)
            end = start + timedelta(days=7)
            return (start, end, "上周")
        if "本月" in q or "这个月" in q:
            start = today.replace(day=1)
            return (start, today + timedelta(days=1), "本月")
        if "上月" in q or "上个月" in q:
            if today.month == 1:
                start = today.replace(year=today.year - 1, month=12, day=1)
            else:
                start = today.replace(month=today.month - 1, day=1)
            end = today.replace(day=1)
            return (start, end, "上月")
        return None

    # 时段识别：基于 created_at 的小时范围过滤
    # (hour_start, hour_end, label) — hour_end 是开区间
    TIME_PERIODS = {
        "凌晨": (0, 6, "凌晨（0-6点）"),
        "后半夜": (0, 6, "后半夜（0-6点）"),
        "早上": (6, 9, "早上（6-9点）"),
        "早晨": (6, 9, "早晨（6-9点）"),
        "上午": (9, 12, "上午（9-12点）"),
        "中午": (12, 14, "中午（12-14点）"),
        "下午": (14, 18, "下午（14-18点）"),
        "傍晚": (18, 20, "傍晚（18-20点）"),
        "晚上": (18, 24, "晚上（18-24点）"),
        "夜里": (18, 24, "夜里（18-24点）"),
        "夜间": (18, 24, "夜间（18-24点）"),
    }

    @classmethod
    def detect_time_period(cls, question: str) -> tuple[int, int, str] | None:
        """识别"上午/下午/晚上/后半夜"等时段查询，返回 (hour_start, hour_end, label) 或 None。"""
        q = question.strip()
        for keyword, (h_start, h_end, label) in cls.TIME_PERIODS.items():
            if keyword in q:
                return (h_start, h_end, label)
        return None

    @classmethod
    def _has_stats(cls, question: str) -> bool:
        """Return true only for an explicit request about this notebook library.

        A precomputed dashboard answer has no source evidence, so a false
        positive is much more harmful than asking RAG to answer a legitimate
        statistics request.  The old implementation started from generic words
        such as ``多少`` and then tried to list exceptions.  That inevitably
        turns an unfamiliar knowledge question into a notebook count.  Here the
        direction is reversed: the caller must explicitly name a personal
        library object *and* ask for an aggregation of that object.  Anything
        else, including colloquial questions such as "提示词注入是啥", remains QA.
        """
        q = re.sub(r"\s+", "", question)
        # These forms refer to a fact recorded *inside* a note, rather than a
        # property of the note collection itself.
        if any(marker in q for marker in ("笔记里", "笔记中", "记录里", "记录中", "资料里", "资料中")):
            return False

        library_object = r"(?:我的?(?:笔记|记录|日记|资料|资料库|知识库)|笔记库|随手记)"
        aggregation = r"(?:有多少|多少(?:条|篇|个|字)|几(?:条|篇|个)|总数|总共|统计|占比|比例|分布|最多|最少|最长|最短|最早|最新|最旧)"
        if re.search(rf"{library_object}.{{0,10}}{aggregation}", q):
            return True
        if re.search(rf"{aggregation}.{{0,10}}{library_object}", q):
            return True

        # "一共几个笔记" / "有几篇记录" are equally explicit collection
        # counts even without "我的". Keep this narrow to note-like objects;
        # generic "几个" questions must continue through evidence retrieval.
        note_object = r"(?:笔记|记录|日记)"
        count_phrase = r"(?:一共|总共|共有|总计)?(?:有多少|多少(?:条|篇|个)?|几(?:条|篇|个))"
        # "FTS5 在多少条笔记时性能异常" describes a threshold inside a
        # document. It is not an aggregation request about the user's library.
        if re.search(rf"{count_phrase}.{{0,4}}{note_object}(?:时|后|前|以内|以上|以下)", q):
            return False
        if re.search(rf"{count_phrase}.{{0,4}}{note_object}", q):
            return True
        if re.search(rf"{note_object}.{{0,4}}{count_phrase}", q):
            return True

        # A first-person creation count is also unambiguously collection
        # management, even when the user omits the word "笔记".
        return bool(re.search(
            r"(?:我|本周|今天|昨天|最近).{0,8}(?:写了|创建了|记录了).{0,8}(?:多少|几(?:条|篇)|多少字|总字数)",
            q,
        ))

    @staticmethod
    def _is_note_timestamp_query(question: str) -> bool:
        """Whether a relative time refers to note metadata, not its content."""
        return any(marker in question for marker in (
            "笔记", "写的", "写过", "创建", "记录", "改过", "修改", "编辑", "更新",
        ))

    @classmethod
    def _is_taxonomy(cls, question: str) -> bool:
        return any(p in question for p in cls.TAXONOMY_PATTERNS)

    @classmethod
    def _is_recent_edit(cls, question: str) -> bool:
        """识别"最近修改/今天编辑"等基于 updated_at 的查询。"""
        return any(p in question for p in cls.RECENT_EDIT_PATTERNS)

    @classmethod
    def _detect_metadata(cls, question: str) -> dict | None:
        # 固定规则元数据
        for filter_type, patterns in cls.METADATA_RULES:
            for p in patterns:
                if p in question:
                    return {"filter": filter_type}
        return None

    @classmethod
    def _detect_category_filter(cls, question: str, store) -> dict | None:
        """识别"技术类的笔记"等按分类筛选。需查 store 确认分类名存在。"""
        if not store:
            return None
        try:
            taxonomy = store.get_taxonomy()
            categories = taxonomy.get("categories", [])
            for cat in categories:
                name = cat.get("name", "")
                if not name:
                    continue
                # "技术类的笔记" / "技术类有哪些" / "技术分类的" / "关于技术的"
                patterns = [
                    f"{name}类的笔记", f"{name}类的有哪些", f"{name}分类的",
                    f"{name}类下的", f"{name}类别里", f"{name}分类里",
                    f"{name}类别下", f"关于{name}的笔记", f"{name}相关的笔记",
                ]
                for p in patterns:
                    if p in question:
                        return {"filter": "category", "category": name}
        except Exception:
            pass
        return None

    @classmethod
    def _detect_tag_filter(cls, question: str, store) -> dict | None:
        """识别"带RAG标签的笔记"等按标签筛选。需查 store 确认标签名存在。"""
        if not store:
            return None
        try:
            taxonomy = store.get_taxonomy()
            tags = taxonomy.get("tags", [])
            for tag in tags:
                name = tag.get("name", "")
                if not name:
                    continue
                patterns = [
                    f"{name}标签的笔记", f"{name}标签的有哪些", f"带{name}标签",
                    f"打了{name}标签", f"有标签叫{name}", f"标签是{name}",
                    f"标签为{name}", f"标记了{name}", f"#{name}",
                ]
                for p in patterns:
                    if p in question:
                        return {"filter": "tag", "tag": name}
        except Exception:
            pass
        return None

    @classmethod
    def _detect_word_count_filter(cls, question: str) -> dict | None:
        """识别"超过500字的笔记" / "短笔记"等字数范围筛选。"""
        # 超过N字 / 大于N字 / N字以上
        m = re.search(r"(?:超过|大于|不少于|最少)\s*(\d+)\s*字", question)
        if m:
            n = int(m.group(1))
            return {"filter": "min_words", "min": n}
        m = re.search(r"(\d+)\s*字(?:以上|及以上|起步)", question)
        if m:
            n = int(m.group(1))
            return {"filter": "min_words", "min": n}
        # 少于N字 / 小于N字 / N字以下
        m = re.search(r"(?:少于|小于|最多|不超过)\s*(\d+)\s*字", question)
        if m:
            n = int(m.group(1))
            return {"filter": "max_words", "max": n}
        m = re.search(r"(\d+)\s*字(?:以下|以内)", question)
        if m:
            n = int(m.group(1))
            return {"filter": "max_words", "max": n}
        # 短笔记 / 长笔记（无具体数字）
        if "短笔记" in question or "简短的笔记" in question:
            return {"filter": "max_words", "max": 100}
        if "长笔记" in question or "长篇笔记" in question:
            return {"filter": "min_words", "min": 500}
        return None

    # 用于提取时间+主题组合中的主题词
    _TIME_NOISE_WORDS = (
        "的", "了", "写", "创建", "创建过", "创建的", "写的", "有", "哪些", "哪些笔记",
        "关于", "相关", "内容", "笔记", "记", "过", "的笔记", "的内容",
    )

    @classmethod
    def _extract_topic_after_time(cls, question: str, time_label: str) -> str:
        """从"昨天关于RAG的笔记"中提取主题词"RAG"。

        策略：去掉时间词和常见噪声词，剩余内容若非纯噪声则作为主题。
        返回空字符串表示无明确主题（纯时间查询）。
        """
        q = question
        # 去掉时间词（label 可能含括号说明，只取关键词部分）
        time_keywords = re.sub(r"[（）()]", "", time_label)
        for kw in time_keywords.split():
            q = q.replace(kw, "")
        # 也去掉 detect_time_range 识别的其他时间词
        for kw in ("今天", "今日", "昨天", "前天", "本周", "这周", "上周",
                   "本月", "这个月", "上月", "上个月", "最近"):
            q = q.replace(kw, "")
        # 去掉具体日期
        q = re.sub(r"\d{4}\s*年?\s*\d{1,2}\s*月\s*\d{1,2}\s*[日号]", "", q)
        q = re.sub(r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]", "", q)
        q = re.sub(r"\d{4}\s*年\s*\d{1,2}\s*月", "", q)
        q = re.sub(r"\d{1,2}\s*月", "", q)
        # 去掉时段词
        for kw in cls.TIME_PERIODS:
            q = q.replace(kw, "")
        # 去掉噪声词
        for noise in cls._TIME_NOISE_WORDS:
            q = q.replace(noise, " ")
        # 清理多余空白
        q = re.sub(r"\s+", " ", q).strip()
        # 剩余内容长度>1且非纯标点，视为主题
        q = q.strip("，。、！？的了吗呢")
        if len(q) >= 2 and not all(c in " " for c in q):
            return q
        return ""

    @classmethod
    def detect(cls, question: str, llm_engine=None, store=None) -> Intent:
        """检测查询意图。规则优先，未命中时用 LLM 兜底分类。

        llm_engine: LlamaCppTextEngine 实例（其 classify_intent 方法会触发模型加载）。
                    为 None 时跳过 LLM 分类，直接返回 qa 兜底。
        store: NoteStore 实例，用于按分类/标签筛选时确认名称存在。
        """
        q = question.strip()

        # 1. 最近修改意图（基于 updated_at）—优先于时间范围，因为"最近改过"≠"最近创建"
        if cls._is_recent_edit(q):
            # 提取时间范围（今天改/昨天改/最近改）
            time_range = cls.detect_time_range(q)
            return Intent("recent_edit", {"time_range": time_range}, "rule")

        # 2. 时间意图（与统计可组合：本周写了多少 → 时间+统计）
        time_range = cls.detect_time_range(q)
        has_stats = cls._has_stats(q)
        if time_range and not cls._is_note_timestamp_query(q):
            # "本周 RAG 学习安排" and similar prompts ask about a date written
            # inside a note. Let normal retrieval handle them instead of looking
            # for notes created this week.
            time_range = None
        if time_range and has_stats:
            return Intent("stats", {"time_range": time_range, "subtype": "time_count"}, "rule")
        if time_range:
            # 时间+元数据组合优先：昨天未命名的笔记 / 昨天超过500字的
            meta = cls._detect_metadata(q) or cls._detect_word_count_filter(q)
            if meta:
                return Intent("time", {"time_range": time_range, "metadata": meta}, "rule")
            # 时间+统计组合：今天写了多少字 → 字数统计+时间过滤
            if has_stats:
                subtype = cls._stats_subtype(q)
                if subtype == "word_count":
                    return Intent("stats", {"time_range": time_range, "subtype": "word_count"}, "rule")
                return Intent("stats", {"time_range": time_range, "subtype": "time_count"}, "rule")
            # 时间+主题组合：昨天关于RAG的笔记 → 提取主题词做二次过滤
            topic = cls._extract_topic_after_time(q, time_range[2])
            if topic:
                return Intent("time", {"time_range": time_range, "topic": topic}, "rule")
            return Intent("time", {"time_range": time_range}, "rule")

        # 2b. 时段意图：上午/下午/晚上/后半夜等，基于 created_at 的小时过滤
        time_period = cls.detect_time_period(q)
        if time_period:
            # 时段+统计组合：我通常什么时候写笔记 → 写作时段分布
            if any(w in q for w in ("通常", "一般", "习惯", "什么时候写", "什么时段")):
                return Intent("stats", {"subtype": "hour_distribution"}, "rule")
            # 时段+主题组合：晚上写的RAG笔记 → 提取主题词
            topic = cls._extract_topic_after_time(q, time_period[2])
            if topic:
                return Intent("time_period", {"period": time_period, "topic": topic}, "rule")
            return Intent("time_period", {"period": time_period}, "rule")

        # 3. 纯统计意图
        if has_stats:
            return Intent("stats", {"subtype": cls._stats_subtype(q)}, "rule")

        # 4. 分类体系意图
        if cls._is_taxonomy(q):
            return Intent("taxonomy", {}, "rule")

        # 5. 元数据过滤意图
        # 5a. 字数范围筛选
        word_filter = cls._detect_word_count_filter(q)
        if word_filter:
            return Intent("metadata", word_filter, "rule")
        # 5b. 按分类筛选
        cat_filter = cls._detect_category_filter(q, store)
        if cat_filter:
            return Intent("metadata", cat_filter, "rule")
        # 5c. 按标签筛选
        tag_filter = cls._detect_tag_filter(q, store)
        if tag_filter:
            return Intent("metadata", tag_filter, "rule")
        # 5d. 固定规则元数据
        meta = cls._detect_metadata(q)
        if meta:
            return Intent("metadata", meta, "rule")

        # 6. LLM 语义兜底分类（list/summary/compare/entity/related/qa）
        if llm_engine is not None:
            try:
                result = llm_engine.classify_intent(q)
                intent_type = result.get("intent", "qa")
                if intent_type not in ("list", "summary", "compare", "entity", "related", "qa"):
                    intent_type = "qa"
                params = {k: v for k, v in result.items() if k != "intent"}
                return Intent(intent_type, params, "llm")
            except Exception:
                pass
        return Intent("qa", {}, "rule")

    @classmethod
    def _stats_subtype(cls, question: str) -> str:
        # 字数统计
        if any(w in question for w in ("多少字", "总字数", "平均字数", "平均多少字", "一共多少字")):
            return "word_count"
        # 最早的/第一篇/最新/最后一篇
        if any(w in question for w in ("最早", "第一篇", "最新", "最近一篇", "最后一篇")):
            return "time_order"
        # 最久没动
        if any(w in question for w in ("最久", "多久没", "最旧")):
            return "stalest"
        # 写作时段分布
        if any(w in question for w in ("通常", "一般", "习惯", "什么时候写", "什么时段")):
            return "hour_distribution"
        # 长度/极值：返回最长/最短的笔记
        if any(p in question for p in cls.LENGTH_PATTERNS):
            return "length"
        if any(w in question for w in ("分类", "类别", "哪类", "哪个最多")):
            return "category_count"
        if "标签" in question:
            return "tag_count"
        return "count"

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from pocket_memory.storage import NoteStore

logger = logging.getLogger(__name__)

ALLOWED_CATEGORIES = {"工作", "技术", "学习", "生活", "灵感", "其他"}


def resource_path(relative_path: str) -> Path:
    if hasattr(sys, "_MEIPASS"):
        # Green builds may put a large LLM beside PocketMemory.exe while keeping
        # BGE/OCR inside _internal. Check the exact external resource first,
        # then fall back per file instead of switching the whole models tree.
        external = Path(sys.executable).resolve().parent / relative_path
        if external.exists():
            return external
        return Path(sys._MEIPASS) / relative_path
    return Path(__file__).resolve().parent.parent / relative_path


def writable_models_path() -> Path:
    """Return the install-adjacent model directory suitable for large downloads."""
    if hasattr(sys, "_MEIPASS"):
        return Path(sys.executable).resolve().parent / "models"
    return Path(__file__).resolve().parent.parent / "models"


def _bundled_models_path() -> Path:
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "models"
    return writable_models_path()


# 默认模型相对路径（相对 models/ 目录）
DEFAULT_MODEL_REL = "Qwen3-4B/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
# 模型文件最小大小（10MB），过滤误放的小文件
MIN_MODEL_SIZE_BYTES = 10 * 1024 * 1024
DEFAULT_CONTEXT_TOKENS = 3072
DEFAULT_RESPONSE_TOKENS = 220


def list_available_models() -> list[dict]:
    """扫描 models/ 目录下所有 .gguf 文件，返回可用模型列表。

    返回: [{"path": "相对路径", "name": "文件名", "size_mb": 1234.5, "is_default": True}, ...]
    """
    # An online-model package has two roots: writable external Qwen weights and
    # bundled BGE/OCR resources. Deduplicate by relative model path, with the
    # external model taking precedence over a bundled copy.
    roots = [writable_models_path(), _bundled_models_path()]
    result_by_path: dict[str, dict] = {}
    seen_roots: set[Path] = set()
    for models_dir in roots:
        models_dir = models_dir.resolve()
        if models_dir in seen_roots or not models_dir.is_dir():
            continue
        seen_roots.add(models_dir)
        for gguf_path in sorted(models_dir.rglob("*.gguf")):
            try:
                size = gguf_path.stat().st_size
            except OSError:
                continue
            if size < MIN_MODEL_SIZE_BYTES:
                continue
            rel = str(gguf_path.relative_to(models_dir)).replace("\\", "/")
            result_by_path.setdefault(rel, {
                "path": rel,
                "name": gguf_path.name,
                "size_mb": round(size / 1024 / 1024, 1),
                "is_default": rel == DEFAULT_MODEL_REL,
            })
    return [result_by_path[key] for key in sorted(result_by_path)]


def resolve_model_path(model_rel: str | None) -> Path:
    """根据配置中的相对路径解析模型绝对路径，None 或无效时回退到默认。"""
    requested = model_rel or DEFAULT_MODEL_REL
    for models_dir in (writable_models_path(), _bundled_models_path()):
        candidate = models_dir / requested
        if candidate.is_file():
            return candidate.resolve()
    if model_rel:
        logger.warning("配置的模型不存在，回退默认: %s", model_rel)
        for models_dir in (writable_models_path(), _bundled_models_path()):
            candidate = models_dir / DEFAULT_MODEL_REL
            if candidate.is_file():
                return candidate.resolve()
    return (writable_models_path() / DEFAULT_MODEL_REL).resolve()


class LlamaCppTextEngine:
    """进程内常驻的 llama.cpp 文本理解引擎。

    通过 `llama-cpp-python` 加载 GGUF 模型一次，整个应用生命周期复用，
    避免每次调用都新起 subprocess 重新加载模型权重。

    支持空闲卸载：超过 idle_threshold 秒未使用时自动释放模型权重，
    下次调用时重新加载（约 10-15s），平衡内存占用与响应速度。
    """

    def __init__(
        self,
        model_path: Path | None = None,
        n_ctx: int = DEFAULT_CONTEXT_TOKENS,
        n_threads: int = 4,
        n_gpu_layers: int = 0,
    ) -> None:
        self.model_path = (model_path or resolve_model_path(None)).resolve()
        self.n_ctx = n_ctx
        self.n_threads = n_threads
        self.n_gpu_layers = n_gpu_layers
        self._llm = None
        self._error: str | None = None
        self._lock = threading.Lock()
        self._last_used_at: float = 0.0  # 模型最后使用时间（单调时钟）

    @property
    def available(self) -> bool:
        self._ensure_loaded()
        return self._llm is not None

    @property
    def model_loaded(self) -> bool:
        """模型是否已加载（不触发加载，仅查询状态）。"""
        return self._llm is not None

    def unload(self) -> None:
        """手动卸载模型，释放内存。下次调用会重新加载。"""
        with self._lock:
            if self._llm is not None:
                logger.info("卸载 LLM 模型，释放内存")
                self._llm = None
                self._last_used_at = 0.0

    def switch_model(self, model_path: Path) -> None:
        """切换到新模型：先卸载当前模型，再更新路径，下次调用时自动加载新模型。"""
        with self._lock:
            if self._llm is not None:
                logger.info("切换模型，卸载旧模型: %s", self.model_path.name)
                self._llm = None
                self._last_used_at = 0.0
            self.model_path = model_path.resolve()
            self._error = None  # 清除旧错误状态，允许重新加载
            logger.info("已切换模型路径: %s", self.model_path.name)

    def unload_if_idle(self, idle_threshold_seconds: float) -> bool:
        """若模型空闲超过阈值则卸载。返回是否执行了卸载。"""
        if self._llm is None or self._last_used_at == 0.0:
            return False
        idle = time.monotonic() - self._last_used_at
        if idle >= idle_threshold_seconds:
            logger.info("LLM 空闲 %.0f 分钟，自动卸载", idle / 60)
            self.unload()
            return True
        return False

    @property
    def unavailable_reason(self) -> str:
        self._ensure_loaded()
        return self._error or ""

    def runtime_status(self) -> dict:
        """Return availability without loading multi-gigabyte model weights."""
        if self._llm is not None:
            return {"available": True, "reason": "", "model_loaded": True}
        if self._error:
            return {"available": False, "reason": self._error, "model_loaded": False}
        if not self.model_path.is_file():
            return {
                "available": False,
                "reason": f"文本理解模型文件缺失: {self.model_path}",
                "model_loaded": False,
            }
        try:
            import llama_cpp  # noqa: F401
        except ImportError as exc:
            return {
                "available": False,
                "reason": f"未安装 llama-cpp-python: {exc}",
                "model_loaded": False,
            }
        return {"available": True, "reason": "", "model_loaded": False}

    def classify(
        self,
        text: str,
        existing_tags: list[str] | None = None,
        excluded_tags: list[str] | None = None,
    ) -> dict:
        if not self.available:
            raise RuntimeError(self.unavailable_reason)
        messages = [
            {
                "role": "system",
                "content": (
                    "你是一个本地笔记整理助手。请整理用户笔记。只输出一行 JSON，不要解释，不要 Markdown，不要思考过程。\n"
                    "字段规则：\n"
                    "- category 必须从 工作、技术、学习、生活、灵感、其他 中选择一个。\n"
                    "- subcategory 使用 2 到 8 个汉字概括主题。\n"
                    "- tags 是数组，生成 1 到 2 个能区分这篇笔记主题的简洁标签。只有与标题或正文主题直接一致时才复用已有标签，不能因为词表中出现频率高就复用。\n"
                    "- 禁止把证据链、智能问答、知识库、资料、笔记、项目、本地化等泛化产品词作为标签；优先使用具体主题词，例如 RAG 验收、采购审批、制造政策。\n"
                    "- 禁止生成用户明确移除过的标签。\n"
                    "- reason 用一句话说明为什么这样分类，20 字以内。\n"
                    "JSON 必须包含 category、subcategory、tags、reason 四个字段。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"已有标签词表（仅在语义匹配时复用）：{'、'.join(existing_tags or []) or '暂无'}\n"
                    f"用户明确移除、禁止恢复的标签：{'、'.join(excluded_tags or []) or '无'}\n\n"
                    f"用户笔记：\n{text[:4000]}"
                ),
            },
        ]
        try:
            output = self._chat(messages, max_tokens=200, temperature=0.0)
        except Exception as exc:
            raise RuntimeError("本地模型整理失败") from exc
        try:
            return self._parse_json(output)
        except ValueError as exc:
            raise ValueError("本地模型未返回有效整理结果") from exc

    def rewrite_query(self, question: str) -> list[str]:
        """用 LLM 改写用户问题，生成扩展查询词用于增强 FTS/词汇召回。

        仅在模型已加载时调用（不触发加载），避免阻塞首次问答。
        返回去重的扩展词列表（不含原问题中的词），失败时返回空列表。
        """
        if not self.model_loaded:
            return []
        messages = [
            {
                "role": "system",
                "content": (
                    "你是查询改写助手。用户会用一句话提问，请生成 3-5 个相关的扩展查询词或同义表达，"
                    "用于提高本地笔记检索的召回率。\n"
                    "规则：\n"
                    "- 每行一个扩展词，不要编号、不要解释、不要标点。\n"
                    "- 扩展词应该是用户可能在笔记里写过的关键词或短语。\n"
                    "- 不要重复原问题里已有的词。\n"
                    "- 中文扩展词 2-8 字，英文扩展词 1-3 词。"
                ),
            },
            {"role": "user", "content": f"用户问题：{question[:300]}"},
        ]
        try:
            output = self._chat(messages, max_tokens=80, temperature=0.0)
        except Exception:
            logger.debug("query rewrite failed", exc_info=True)
            return []
        output = re.sub(r"<think>[\s\S]*?</think>", "", output).strip()
        terms: list[str] = []
        seen: set[str] = set()
        for line in output.splitlines():
            term = line.strip(" \n\r\t•-·。；;，,")
            if not term or len(term) > 20 or term in question:
                continue
            key = term.lower()
            if key in seen:
                continue
            seen.add(key)
            terms.append(term)
            if len(terms) >= 5:
                break
        return terms

    def classify_intent(self, question: str) -> dict:
        """用 LLM 对查询进行意图分类，并提取关键实体。

        覆盖规则无法判定的灵活意图：list/summary/compare/entity/related/qa。
        模型未加载时会触发加载（强制分类，保证准确率）。
        返回 {"intent": "...", "topic": "...", "entities": [...]} 等。
        """
        if not self.available:
            return {"intent": "qa"}
        messages = [
            {
                "role": "system",
                "content": (
                    "你是查询意图分类器。判断用户对个人笔记库提问的意图类型，并提取关键实体。\n"
                    "意图类型：\n"
                    "- list：列出符合条件的笔记清单。如「有哪些关于X的笔记」「列出XX相关的」「关于X的笔记都有啥」。提取 topic。\n"
                    "- summary：总结/概括某主题或某篇笔记。如「总结一下XX」「概括XX」「XX的要点」「讲讲XX」。提取 topic。\n"
                    "- compare：比较两个实体的异同。如「X和Y的区别」「对比X和Y」「X与Y相比」。提取 entities（两个）。\n"
                    "- entity：查询某个具体实体是什么/在哪里。如「P2-3是什么」「XX在哪里」「XX指什么」。提取 entity。\n"
                    "- related：查找与某笔记/主题相关的其他笔记。如「和这篇相关的」「类似XX的」「XX相关推荐」。提取 reference。\n"
                    "- qa：通用问答（不属于以上任一类型）。\n"
                    "输出规则：\n"
                    "- 只输出一行 JSON，不要解释、不要 Markdown、不要思考过程。\n"
                    "- 必须包含 intent 字段，值为 list/summary/compare/entity/related/qa 之一。\n"
                    "- list/summary 提取 topic（字符串）；compare 提取 entities（两个字符串的数组）；entity 提取 entity（字符串）；related 提取 reference（字符串）。\n"
                    "- 没有对应实体时对应字段留空字符串或空数组。\n"
                    "- 示例：{\"intent\":\"summary\",\"topic\":\"P2-3\",\"entities\":[]}\n"
                    "- 示例：{\"intent\":\"compare\",\"topic\":\"\",\"entities\":[\"P2-3\",\"P2-22\"]}\n"
                    "- 示例：{\"intent\":\"entity\",\"entity\":\"P2-3\",\"entities\":[]}\n"
                    "- 示例：{\"intent\":\"list\",\"topic\":\"RAG\",\"entities\":[]}\n"
                    "- 示例：{\"intent\":\"qa\",\"topic\":\"\",\"entities\":[]}"
                ),
            },
            {"role": "user", "content": f"用户问题：{question[:300]}"},
        ]
        try:
            output = self._chat(messages, max_tokens=120, temperature=0.0)
        except Exception:
            logger.debug("intent classify failed", exc_info=True)
            return {"intent": "qa"}
        output = re.sub(r"<think>[\s\S]*?</think>", "", output).strip()
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            start = output.find("{")
            end = output.rfind("}")
            if start >= 0 and end > start:
                try:
                    parsed = json.loads(output[start:end + 1])
                except json.JSONDecodeError:
                    return {"intent": "qa"}
            else:
                return {"intent": "qa"}
        if not isinstance(parsed, dict) or parsed.get("intent") not in (
            "list", "summary", "compare", "entity", "related", "qa"
        ):
            return {"intent": "qa"}
        # 规整 entities 为 list[str]
        entities = parsed.get("entities", [])
        if not isinstance(entities, list):
            entities = []
        parsed["entities"] = [str(e).strip() for e in entities if str(e).strip()][:3]
        return parsed

    def answer(self, question: str, contexts: list[dict], history: list[dict] | None = None) -> str:
        if not self.available:
            raise RuntimeError(self.unavailable_reason)
        output = self._chat(
            self._answer_messages(question, contexts, history),
            max_tokens=DEFAULT_RESPONSE_TOKENS,
            temperature=0.0,
        )
        return self._clean_answer(output)

    def answer_stream(self, question: str, contexts: list[dict], history: list[dict] | None = None):
        """流式生成答案，yield 每个 token 片段。"""
        if not self.available:
            raise RuntimeError(self.unavailable_reason)
        messages = self._answer_messages(question, contexts, history)
        self._ensure_loaded()
        if self._llm is None:
            raise RuntimeError(self._error or "llama.cpp 模型未加载")
        with self._lock:
            self._last_used_at = time.monotonic()  # 更新使用时间，防止空闲卸载
            response = self._llm.create_chat_completion(
                messages=messages,
                max_tokens=DEFAULT_RESPONSE_TOKENS,
                temperature=0.0,
                stream=True,
            )
            for chunk in response:
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta", {}).get("content", "")
                if delta:
                    yield delta

    @staticmethod
    def _trim_text(text: str, limit: int) -> str:
        text = str(text or "").strip()
        if len(text) <= limit:
            return text
        cutoff = max(text.rfind(mark, 0, limit) for mark in ("。", "！", "？", "\n", ";", "；"))
        if cutoff >= limit // 2:
            return text[:cutoff + 1]
        return text[:limit].rstrip() + "..."

    @staticmethod
    def _evidence_aligned_question(question: str, contexts: list[dict]) -> str:
        """Hide only unmatched versioned identifiers from the generation prompt.

        Users sometimes remember the right topic but the wrong model or product
        version. Small local models otherwise tend to refuse despite directly
        relevant evidence. Retrieval, sources, and the visible question are not
        changed; this only keeps the generation request evidence-aligned.
        """
        evidence = "\n".join(
            f"{item.get('title', '')}\n{item.get('text', '')}" for item in contexts
        ).casefold()
        if not evidence:
            return question
        identifiers = re.findall(r"\b[A-Za-z][A-Za-z0-9_.-]*\d[A-Za-z0-9_.-]*\b", question)
        unmatched = [token for token in identifiers if token.casefold() not in evidence]
        if not unmatched:
            return question
        normalized = question
        for token in unmatched:
            normalized = re.sub(re.escape(token), "", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\s{2,}", " ", normalized).strip()
        return normalized or question

    @classmethod
    def _answer_messages(cls, question: str, contexts: list[dict], history: list[dict] | None) -> list[dict]:
        """Pack evidence and history conservatively so the answer remains grounded.

        Chinese usually consumes close to one token per character.  The old fixed
        6,000-character context could overflow a 2K context window and silently
        discard the evidence at the end.  Keep the prompt comfortably below the
        3K window while reserving room for a concise answer.
        """
        evidence_budget = 1200
        evidence_rows: list[str] = []
        remaining = evidence_budget
        for index, item in enumerate(contexts[:5], 1):
            header = f"[{index}] 标题：{item['title']}\n证据："
            if remaining <= len(header) + 80:
                break
            text_limit = min(360, remaining - len(header))
            text = cls._trim_text(item.get("text", ""), text_limit)
            if not text:
                continue
            row = header + text
            evidence_rows.append(row)
            remaining -= len(row) + 2

        messages = [
            {
                "role": "system",
                "content": (
                    "你是一个本地个人笔记问答助手，只能依据用户提供的证据回答。\n"
                    "回答规则：\n"
                    "- 每个事实性结论、数字、阈值、日期或比较结论都必须紧跟来源编号，如 [1]。\n"
                    "- 数字、比较符号、单位和范围必须按证据原样保留，例如「>10%」不能写成「10%」。\n"
                    "- 问题中的对象若与证据标题或内容冲突，但证据明确覆盖同一主题：先简短纠正错误前提，再依据正确证据继续回答；此时不能只回复信息不足。例如证据是 BGE 向量检索方案而问题误写 Qwen，应说明对象不符后给出 BGE 方案中的优化。纠正对象不是答案本身，仍须提取并回答用户所问的具体做法、参数或结论。\n"
                    "- 证据只提到相近对象、没有具体数值，或无法支持完整结论时，明确说明「现有笔记没有足够信息」，不要用常识补全。\n"
                    "- 不要猜测、不要扩写证据之外的原因，也不要引用不存在的编号。\n"
                    "- 用户问“有哪些/哪些风险/分别是什么”时，必须列出证据中全部编号项；不要只举第一项。\n"
                    "- 直接回答问题；默认不超过 120 个汉字或 5 个要点，用户明确要求全文时除外。\n"
                    "- 不要复述这些规则，也不要输出思考过程。"
                ),
            }
        ]
        history_rows = []
        # Keep two complete user/assistant rounds within about the same budget
        # as the old three long messages, leaving evidence dominant in n_ctx.
        for item in (history or [])[-4:]:
            role = item.get("role", "user")
            content = cls._trim_text(item.get("content", ""), 140)
            if role in ("user", "assistant") and content:
                history_rows.append({"role": role, "content": content})
        messages.extend(history_rows)
        context_text = "\n\n".join(evidence_rows) or "（没有可用笔记证据）"
        evidence_question = cls._evidence_aligned_question(question, contexts)
        messages.append({
            "role": "user",
            "content": f"问题：\n{cls._trim_text(evidence_question, 500)}\n\n可用笔记证据：\n{context_text}",
        })
        return messages

    def _chat(self, messages: list[dict], max_tokens: int, temperature: float) -> str:
        self._ensure_loaded()
        if self._llm is None:
            raise RuntimeError(self._error or "llama.cpp 模型未加载")
        with self._lock:
            self._last_used_at = time.monotonic()  # 更新使用时间，防止空闲卸载
            response = self._llm.create_chat_completion(
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                stream=False,
            )
            choices = response.get("choices") or []
            if not choices:
                return ""
            return choices[0].get("message", {}).get("content", "") or ""

    def generate_moc(self, folder_name: str, notes: list[dict]) -> str:
        """为文件夹生成 MOC（Map of Content）内容。

        入参 notes: [{title, category, excerpt, tags: [str]}]
        返回: Markdown 格式的 MOC 内容，用 [[标题]] 链接到各笔记。
        """
        if not self.available:
            raise RuntimeError(self.unavailable_reason)
        # 构造笔记摘要：每条一行，标题+摘要+标签
        # 注意：这里用纯文本「标题: xxx | 摘要: xxx」呈现，避免在输入里给 [[]]，
        # 防止小模型把输入原样回吐而不生成链接。链接必须由模型按指令生成。
        lines = []
        for i, n in enumerate(notes[:40], 1):  # 最多 40 条，避免超 n_ctx
            title = n.get("title", "").strip() or "未命名笔记"
            excerpt = (n.get("excerpt") or n.get("content") or "")[:80].replace("\n", " ")
            tags = ", ".join(n.get("tags", []) or [])
            tag_str = f" | 标签: {tags}" if tags else ""
            lines.append(f"{i}. 标题: {title} | 摘要: {excerpt}{tag_str}")
        notes_summary = "\n".join(lines)
        titles = [n.get("title", "").strip() or "未命名笔记" for n in notes[:40]]

        messages = [
            {
                "role": "system",
                "content": (
                    "/no_think\n"
                    "你为笔记文件夹生成 MOC 目录索引。\n"
                    f"必须引用下面全部 {len(notes[:40])} 条笔记，一条也不能漏。\n"
                    "每条笔记用 `[[完整标题]]` 引用，标题必须与给定列表完全一致，禁止改名、缩写、翻译。\n"
                    "禁止用书名号《》或其他符号代替双方括号。\n"
                    "按主题用 `## 主题名` 分组（主题名自行归纳，不要用占位词）。\n"
                    "每条引用后用一句话说明该笔记的核心。\n"
                    "第一行必须是 `# 🗺️ MOC: " + folder_name + "`。\n"
                    "结尾加 `## 概述` 段落，2-3 句话总结。\n"
                    "只输出 Markdown 正文，不要代码块、不要解释、不要 <think>。"
                ),
            },
            {
                "role": "user",
                "content": f"文件夹：{folder_name}\n笔记列表：\n{notes_summary}\n\n生成 MOC。",
            },
        ]
        try:
            output = self._chat(messages, max_tokens=1024, temperature=0.3)
            # 清理 <think> 标签（Qwen3 思考模式可能仍输出）
            output = re.sub(r"<think>[\s\S]*?</think>", "", output)
            output = re.sub(r"<think>.*$", "", output, flags=re.DOTALL)
            # 清理代码块包裹
            output = output.strip("`").strip()
            if output.startswith("markdown\n"):
                output = output[9:]
            # 后处理：把《标题》转成 [[标题]]（LLM 可能不遵循指令）
            output = re.sub(r"《([^》]+)》", r"[[\1]]", output)
            # 兜底校验：如果生成的链接数明显不足（少于笔记数的一半），
            # 说明小模型退化丢链接，直接用模板兜底保证至少有可点击链接。
            link_count = len(re.findall(r"\[\[[^\]]+\]\]", output))
            if link_count < max(1, len(titles) // 2):
                logger.warning(
                    "MOC 链接数不足（%d/%d），使用模板兜底", link_count, len(titles)
                )
                return self._fallback_moc(folder_name, notes)
            return output.strip()
        except Exception:
            logger.warning("MOC 生成失败，使用模板兜底", exc_info=True)
            return self._fallback_moc(folder_name, notes)

    @staticmethod
    def _fallback_moc(folder_name: str, notes: list[dict]) -> str:
        """LLM 不可用时的模板兜底：简单列表式 MOC。"""
        lines = [f"# 🗺️ MOC: {folder_name}\n"]
        for i, n in enumerate(notes[:40], 1):
            title = n.get("title", "").strip() or "未命名笔记"
            lines.append(f"- [[{title}]]")
        lines.append(f"\n## 概述\n本文件夹共 {len(notes)} 条笔记。")
        return "\n".join(lines)

    def generate_wiki_card(self, title: str, template: dict, evidence_blocks: list[dict]) -> dict:
        """Generate a constrained topic-card draft whose claims must cite supplied evidence keys."""
        if not self.available:
            raise RuntimeError(self.unavailable_reason)
        fields = list(template.get("fields") or [])
        evidence = "\n\n".join(
            f"[{block['key']}] {block['source_label']}｜{block['location']}\n{block['text']}"
            for block in evidence_blocks
        )
        field_rule = "字段只能是：" + "、".join(fields) if fields else "字段使用简短主题名"
        messages = [
            {
                "role": "system",
                "content": (
                    "你是信息抽取助手。只能复制用户提供的原文，禁止补充、改写数字、推测或判断真伪。"
                    "最多输出 6 行，每行严格使用：字段|原文|证据编号。原文必须是证据中的连续文字，长度 6-180 字。"
                    "没有明确证据时不要输出该行。不要输出 JSON、解释、标题或 Markdown。" + field_rule
                ),
            },
            {"role": "user", "content": f"专题：{title}\n\n证据：\n{evidence}"},
        ]
        output = self._chat(messages, max_tokens=500, temperature=0.1)
        facts = []
        for line in output.splitlines():
            parts = [part.strip() for part in line.strip().strip("-•").split("|")]
            if len(parts) != 3:
                continue
            field, claim, evidence_key = parts
            if field and claim and evidence_key:
                facts.append({"field": field, "claim": claim, "evidence": [evidence_key.strip("[] ")]})
        return {"facts": facts}

    def generate_report_claims(self, title: str, template: dict, evidence_blocks: list[dict]) -> list[dict]:
        """Generate short source-bound report claims for a deterministic renderer.

        Each generated claim must carry an exact quote from one supplied source.
        This lets the report service reject attractive but unsupported wording
        before it becomes visible to the user.
        """
        if not self.available:
            raise RuntimeError(self.unavailable_reason)
        writing_focus = str(template.get("writing_focus") or "按资料内容自然组织")
        evidence = "\n\n".join(
            f"[{block['key']}] {block['source_label']}｜{block['location']}\n{block['text']}"
            for block in evidence_blocks
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "你是严谨的中文专题报告编辑。只使用下方资料，不知道的内容绝不补充。"
                    "请提炼有信息量的短结论，而不是复制标题、罗列资料或写空泛套话。"
                    "每条结论必须由一个可逐字核对的原文引句支撑；引句必须完整连续地出现在同一条资料中。"
                    "每行严格使用：主题|结论|原文引句|证据编号。主题是你从资料中归纳的 2-18 字短语，不是固定模板栏目。"
                    "结论长度 18-100 个汉字，可以概括但不得增加资料中没有的实体、数字、日期、因果或承诺，也不得拼接或改变原文技术短语的修饰关系。"
                    "例如原文写“全文与向量索引”时，不得改写为“全文抽取”。"
                    "原文引句长度 12-100 个汉字，不要使用省略号。先在心中将资料归为 2-4 个主题，同一主题下的多条结论必须复用相同主题名，不要为每条结论新造主题。"
                    "资料提供至少 6 条证据时，优先输出 6-8 条不重复结论。"
                    "写作侧重："
                    + writing_focus
                    + "。不同结论必须使用不同的证据编号和原文引句。不要为了凑主题而错放资料；证据编号只能写一个。不要输出解释、标题、Markdown 或 JSON。"
                ),
            },
            {"role": "user", "content": f"专题：{title}\n\n资料：\n{evidence}"},
        ]
        output = self._chat(messages, max_tokens=1000, temperature=0.0)
        output = re.sub(r"<think>[\s\S]*?</think>", "", output)
        output = re.sub(r"<think>[\s\S]*$", "", output).strip()
        claims: list[dict] = []
        for line in output.splitlines():
            parts = [part.strip() for part in line.strip().strip("-•").split("|")]
            if len(parts) != 4:
                continue
            topic, claim, quote, evidence_key = parts
            if topic and claim and quote and evidence_key:
                claims.append({
                    "topic": topic,
                    "claim": claim,
                    "quote": quote,
                    "evidence": [evidence_key.strip("[] ")],
                })
        return claims

    def generate_report_structure(self, title: str, template: dict, claims: list[dict]) -> dict:
        """Arrange an already verified fact ledger into a readable report.

        This second, deliberately small prompt never sees the raw notes. It can
        decide the narrative order and headings, but can only cite fact IDs
        whose source quotations were validated in the first pass.
        """
        if not self.available:
            raise RuntimeError(self.unavailable_reason)
        facts = "\n".join(
            f"{item['id']}｜主题：{item.get('topic', '')}｜事实：{item.get('text', '')}"
            for item in claims
        )
        focus = str(template.get("writing_focus") or "按资料内容自然组织")
        messages = [
            {
                "role": "system",
                "content": (
                    "你是严谨的中文报告主编。只能使用下方已经核验过的事实，不得补充任何实体、数字、日期、因果、承诺或结论。"
                    "请让报告有自然的叙事脉络，不要照搬固定栏目，也不要将测试要求包装为产品亮点。"
                    "写作侧重：" + focus + "。\n"
                    "严格输出以下行格式，不要 Markdown、JSON 或解释：\n"
                    "摘要|50-160字的归纳摘要|C1,C2\n"
                    "章节|2-18字章节标题|30-130字章节导语|C1,C2\n"
                    "输出 2-3 行“章节”。每个章节应包含 2-3 个事实编号；每个事实编号必须恰好归入一个章节；摘要至少引用 2 个编号。"
                    "章节导语和摘要都只能概括所列编号的事实，不得出现没有编号支撑的信息。"
                ),
            },
            {"role": "user", "content": f"专题：{title}\n\n已核验事实：\n{facts}"},
        ]
        output = self._chat(messages, max_tokens=650, temperature=0.0)
        result = {"summary": {}, "sections": []}
        output = re.sub(r"<think>[\s\S]*?</think>", "", output)
        output = re.sub(r"<think>[\s\S]*$", "", output)
        for line in output.splitlines():
            parts = [part.strip() for part in line.strip().strip("-•").split("|")]
            if not parts:
                continue
            kind = parts[0].rstrip("：:").strip()
            if kind == "摘要" and len(parts) == 3 and not result["summary"]:
                result["summary"] = {"text": parts[1], "claim_ids": parts[2]}
            elif kind == "章节" and len(parts) == 4:
                result["sections"].append({"title": parts[1], "intro": parts[2], "claim_ids": parts[3]})
        return result

    def generate_report_outline(self, title: str, template: dict, evidence_blocks: list[dict]) -> list[dict]:
        """Compatibility shim for integrations using the former method name."""
        return self.generate_report_claims(title, template, evidence_blocks)

    def parse_tool_intent(self, user_input: str) -> dict:
        """解析用户输入的工具意图，返回 {tool, args} 或 {tool: "none"}。

        支持的工具：
        - set_timer: 倒计时。参数 minutes(数字), message(提醒内容)
        - pomodoro_start: 番茄钟。参数 work_minutes(默认25), break_minutes(默认5)
        - schedule: 日历提醒。参数 datetime_str(YYYY-MM-DD HH:MM), message, repeat(none/daily/weekly)
        """
        if not self.available:
            return {"tool": "none", "error": self.unavailable_reason}
        now_hint = datetime.now().strftime("%Y-%m-%d %H:%M")
        messages = [
            {
                "role": "system",
                "content": (
                    "/no_think\n"
                    "你是一个工具意图解析器，只负责判断用户是否想调用工具。\n"
                    "注意：你不是笔记分类器，不要输出 category/subcategory/tags。\n"
                    f"当前时间：{now_hint}\n"
                    "可用工具：\n"
                    "1. set_timer：设置倒计时。参数：days(天数,默认0), hours(小时,默认0), minutes(分钟,默认0), seconds(秒,默认0), message(提醒内容)。各字段可任意组合。\n"
                    "2. pomodoro_start：开始番茄钟。参数：work_minutes(默认25), break_minutes(默认5)\n"
                    "3. schedule：定时提醒。参数：datetime_str(格式 YYYY-MM-DD HH:MM), message(提醒内容), repeat(可选: none/daily/weekdays/weekly/monthly/quarterly/yearly，默认 none)\n"
                    "   - repeat 含义：none=不重复, daily=每天, weekdays=工作日, weekly=每周, monthly=每月, quarterly=每季度, yearly=每年\n"
                    "输出规则：\n"
                    "- 只输出一行 JSON，格式必须包含 tool 和 args 两个 key。\n"
                    "- tool 的值只能是 set_timer / pomodoro_start / schedule / none。\n"
                    "- 不要输出 category、subcategory、tags 等其他字段。\n"
                    "- 当用户说“N秒/分/小时/天”时，对应字段填 N，不要换算成其他单位的小数。可同时给出多个字段。\n"
                    "- 判断 set_timer vs schedule：相对时长（如“3小时后”“2天后”）用 set_timer；绝对时间点（如“明天下午3点”“每周一9点”）用 schedule。\n"
                    "- 示例：{\"tool\":\"set_timer\",\"args\":{\"minutes\":5,\"message\":\"开会\"}}\n"
                    "- 示例：{\"tool\":\"set_timer\",\"args\":{\"seconds\":50,\"message\":\"喝水\"}}\n"
                    "- 示例：{\"tool\":\"set_timer\",\"args\":{\"hours\":3,\"message\":\"下班\"}}\n"
                    "- 示例：{\"tool\":\"set_timer\",\"args\":{\"days\":2,\"hours\":6,\"message\":\"交报告\"}}\n"
                    "- 示例：{\"tool\":\"pomodoro_start\",\"args\":{}}\n"
                    "- 示例：{\"tool\":\"schedule\",\"args\":{\"datetime_str\":\"2026-07-29 15:00\",\"message\":\"交报告\",\"repeat\":\"none\"}}\n"
                    "- 示例：{\"tool\":\"schedule\",\"args\":{\"datetime_str\":\"2026-08-10 09:00\",\"message\":\"晨会\",\"repeat\":\"weekdays\"}}\n"
                    "- 示例：{\"tool\":\"schedule\",\"args\":{\"datetime_str\":\"2026-08-15 10:00\",\"message\":\"月度复盘\",\"repeat\":\"monthly\"}}\n"
                    "- 不是工具调用时输出：{\"tool\":\"none\",\"args\":{}}"
                ),
            },
            {"role": "user", "content": user_input[:500]},
        ]
        try:
            output = self._chat(messages, max_tokens=200, temperature=0.0)
        except Exception:
            logger.info("Tool intent parse failed.", exc_info=True)
            return {"tool": "none", "error": "意图解析失败"}
        # 过滤 <think> 标签内容
        output = re.sub(r"<think>[\s\S]*?</think>", "", output).strip()
        # 工具意图 JSON 可能嵌套（args 里也有 {}），不能用 _parse_json（它会取最后一个 {}）
        # 直接 json.loads 整个输出，失败再 fallback 找第一个 { 到匹配的 }
        parsed = None
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            # fallback：提取第一个 { 到最后一个 }（最外层完整 JSON）
            start = output.find("{")
            end = output.rfind("}")
            if start >= 0 and end > start:
                try:
                    parsed = json.loads(output[start:end + 1])
                except json.JSONDecodeError:
                    pass
        if not isinstance(parsed, dict) or "tool" not in parsed:
            return {"tool": "none", "error": "模型未返回工具意图"}
        if parsed["tool"] not in ("set_timer", "pomodoro_start", "schedule", "none"):
            return {"tool": "none", "error": "未知工具类型"}
        parsed.setdefault("args", {})
        return parsed

    def _ensure_loaded(self) -> None:
        if self._llm is not None or self._error is not None:
            return
        with self._lock:
            if self._llm is not None or self._error is not None:
                return
            if not self.model_path.is_file():
                self._error = f"文本理解模型文件缺失: {self.model_path}"
                logger.info("LlamaCpp unavailable: %s", self._error)
                return
            try:
                from llama_cpp import Llama
            except ImportError as exc:
                self._error = f"未安装 llama-cpp-python: {exc}"
                logger.info("LlamaCpp unavailable: %s", self._error)
                return
            def create_llm(*, use_mlock: bool, use_mmap: bool) -> Any:
                return Llama(
                    model_path=str(self.model_path),
                    n_ctx=self.n_ctx,
                    n_threads=self.n_threads,
                    n_gpu_layers=self.n_gpu_layers,
                    verbose=False,
                    use_mlock=use_mlock,
                    use_mmap=use_mmap,
                )

            # Some enterprise endpoint tools block memory-mapping a large GGUF in
            # a managed/download directory. Keep mmap for the fast path, then use
            # direct file reads as the final compatibility fallback.
            load_attempts = (
                (True, True, "锁页内存映射"),
                (False, True, "普通内存映射"),
                (False, False, "非内存映射"),
            )
            load_errors: list[Exception] = []
            for use_mlock, use_mmap, label in load_attempts:
                try:
                    self._llm = create_llm(use_mlock=use_mlock, use_mmap=use_mmap)
                    break
                except Exception as exc:
                    load_errors.append(exc)
                    logger.warning("%s加载模型失败: %s", label, exc)
            if self._llm is None:
                final_error = load_errors[-1]
                self._error = (
                    f"{type(final_error).__name__}: {final_error}"
                    "（已尝试锁页、普通内存及非内存映射加载）"
                )
                logger.info("LlamaCpp unavailable: %s", self._error)
                return

            self._last_used_at = time.monotonic()
            logger.info("LLM 模型已加载: %s", self.model_path.name)

    @staticmethod
    def _clean_answer(output: str) -> str:
        text = re.sub(r"<think>[\s\S]*?</think>", "", output)
        text = text.replace("[end of text]", "").strip()
        text = re.sub(r"^[：:\s]+", "", text).strip()
        return text or "现有笔记里没有足够信息。"

    @staticmethod
    def _parse_json(output: str) -> dict:
        decoder = json.JSONDecoder()
        payload = None
        for match in re.finditer(r"\{", output):
            try:
                candidate, _ = decoder.raw_decode(output[match.start():])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                payload = candidate
        if payload is None:
            raise ValueError("模型没有返回 JSON")
        return LlamaCppTextEngine._normalize_suggestions(payload)

    @staticmethod
    def _normalize_suggestions(payload: dict) -> dict:
        category = str(payload.get("category", "")).strip()
        subcategory = str(payload.get("subcategory", "")).strip()
        tags = payload.get("tags", [])
        if not isinstance(tags, list):
            raise ValueError("模型返回的 tags 不是数组")
        if category not in ALLOWED_CATEGORIES:
            category = "其他"
        result = {
            "category": category,
            "subcategory": subcategory[:8] or "随手记录",
            "tags": [str(tag).strip()[:12] for tag in tags if str(tag).strip()][:2],
        }
        reason = str(payload.get("reason", "")).strip()[:100]
        if reason:
            result["reason"] = reason
        return result

class IntelligenceService:
    # 空闲卸载配置
    IDLE_UNLOAD_SECONDS = 5 * 60  # 4B 模型空闲 5 分钟后释放办公内存
    IDLE_CHECK_INTERVAL = 60

    def __init__(self, store: NoteStore, engine: LlamaCppTextEngine | None = None, on_complete=None, auto_apply: bool = True) -> None:
        self.store = store
        self.engine = engine or LlamaCppTextEngine()
        self.on_complete = on_complete
        self.auto_apply = auto_apply
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pocket-memory-ai")
        self._idle_stop = threading.Event()
        self._idle_thread = threading.Thread(
            target=self._idle_watch_loop,
            name="pocket-memory-llm-idle-watch",
            daemon=True,
        )
        self._idle_thread.start()

    def status(self) -> dict:
        pending = len(self.store.pending_ai_note_ids())
        runtime_status = getattr(self.engine, "runtime_status", None)
        if callable(runtime_status):
            status = dict(runtime_status())
        else:
            status = {
                "available": self.engine.available,
                "reason": self.engine.unavailable_reason,
            }
            if hasattr(self.engine, "model_loaded"):
                status["model_loaded"] = self.engine.model_loaded
        status["pending"] = pending
        return status

    def enqueue(self, note_id: int, *, apply: bool | None = None) -> None:
        """Queue every organisation request for the configured LLM.

        Model loading happens inside the serial worker rather than the HTTP request,
        so creating a note stays responsive while the first 4B inference starts.
        """
        self.store.update_ai_status(note_id, "pending", "")
        self.executor.submit(self._process, note_id, apply)

    def recover(self) -> None:
        if not self.auto_apply:
            return
        for note_id in self.store.pending_ai_note_ids():
            self.enqueue(note_id)

    def unload_model(self) -> None:
        """手动卸载模型（供 API 调用）。"""
        self.engine.unload()

    def list_models(self) -> list[dict]:
        """列出所有可用模型。"""
        return list_available_models()

    def switch_model(self, model_rel: str) -> None:
        """切换到指定模型（相对路径）。"""
        model_path = resolve_model_path(model_rel)
        if not model_path.is_file():
            raise FileNotFoundError(f"模型文件不存在: {model_rel}")
        self.engine.switch_model(model_path)

    def rewrite_query(self, question: str) -> list[str]:
        """用 LLM 改写查询，返回扩展词列表。模型未加载或失败时返回空列表。"""
        try:
            return self.engine.rewrite_query(question)
        except Exception:
            logger.debug("rewrite_query failed", exc_info=True)
            return []

    def shutdown(self) -> None:
        self._idle_stop.set()
        self.executor.shutdown(wait=False, cancel_futures=True)

    def _idle_watch_loop(self) -> None:
        """后台线程：定期检查 LLM 是否空闲超时，超时则卸载释放内存。"""
        while not self._idle_stop.wait(self.IDLE_CHECK_INTERVAL):
            try:
                self.engine.unload_if_idle(self.IDLE_UNLOAD_SECONDS)
            except Exception:
                logger.debug("idle watch 检查异常", exc_info=True)

    def _process(self, note_id: int, apply: bool | None = None) -> None:
        try:
            note = self.store.update_ai_status(note_id, "processing")
            group = self.store.get_note_group(note_id)
            text = self._group_classification_text(group)
            if not text.strip():
                self.store.update_ai_status(note_id, "failed", "笔记没有可分析的文字")
                return
            if not self.engine.available:
                self.store.update_ai_status(note_id, "unavailable", self.engine.unavailable_reason)
                return
            excluded_tags = self.store.excluded_group_tags(note_id)
            try:
                suggestions = self.engine.classify(
                    text,
                    existing_tags=self.store.tag_vocabulary(),
                    excluded_tags=excluded_tags,
                )
            except TypeError:
                # Keep third-party/test engines that implement the historical
                # two-argument contract usable during an upgrade.
                suggestions = self.engine.classify(text, existing_tags=self.store.tag_vocabulary())
            should_apply = self.auto_apply if apply is None else apply
            if should_apply:
                self.store.apply_ai_suggestions(note_id, **suggestions)
            else:
                self.store.update_ai_status(note_id, "not_requested")
            if self.on_complete:
                self.on_complete(note_id)
        except KeyError:
            return
        except Exception as exc:
            logger.exception("Text understanding failed for note %s", note_id)
            self.store.update_ai_status(note_id, "failed", f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _group_classification_text(notes) -> str:
        """Create a balanced, bounded organisation view across all note tabs."""
        usable = [note for note in notes if any((note.title or "", note.content or "", note.ocr_text or ""))]
        if not usable:
            return ""
        total_budget = 3_800
        # There can be up to 20 tabs. Reserve a compact but real share for
        # every tab instead of letting earlier tabs consume the entire prompt.
        per_tab_budget = max(150, total_budget // len(usable))
        sections = []
        for index, note in enumerate(usable, 1):
            tab_name = LlamaCppTextEngine._trim_text(note.tab_name or f"页签{index}", 32)
            title = LlamaCppTextEngine._trim_text(note.title, 72)
            header = f"【{tab_name}】\n标题：{title}\n"
            body = "\n".join(part for part in (note.content, note.ocr_text) if part.strip())
            remaining = max(40, per_tab_budget - len(header))
            snippet = LlamaCppTextEngine._trim_text(body, remaining)
            sections.append(f"{header}内容：{snippet}".strip())
        return "\n\n".join(sections)[:4_000]

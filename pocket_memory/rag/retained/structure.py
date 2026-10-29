# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Conservative, coordinate-preserving section labels for saved original text.

This module never rewrites evidence or infers a heading from a question.  A
directory describes an explicitly parsed sibling group, not semantic coverage
of a document.  Presentation limits belong to the caller.
"""
from __future__ import annotations

import bisect
import copy
import re


_CN = "零〇一二三四五六七八九十百千两"
_ARTICLE = re.compile(r"文章[ \t]*([0-9]+)[ \t]*$")
_TITLE = re.compile(r"(?:标题|Title)[：:][ \t]*(\S.*)$")
_MARKDOWN = re.compile(r"(#{1,6})[ \t]+\S")
_CHINESE = re.compile(r"([" + _CN + r"]+)[、．.][ \t]*\S")
_PAREN = re.compile(r"[（(]([" + _CN + r"]+)[）)][ \t]*\S")
_CHAPTER = re.compile(r"第([" + _CN + r"0-9]+)([章篇部])[ \t]*\S")
_DECIMAL = re.compile(r"([1-9][0-9]{0,2}(?:\.[1-9][0-9]{0,2}){1,4})[ \t]+(\S.*)$")
_INLINE_DECIMAL = re.compile(r"(?<![\w.])([1-9][0-9]{0,2}(?:\.[1-9][0-9]{0,2}){1,4})[ \t]+(?=[\u4e00-\u9fffA-Za-z])")
_TOC_DOTS = re.compile(r"\.{3,}|…{2,}|·{3,}")
_EXPLICIT = {"markdown", "article", "title"}
_UNKNOWN_NUMBERED_BOUNDARY = re.compile(r"^第[" + _CN + r"0-9]+[章节節篇部条條款卷编編]")


def _number(value):
    if value.isascii() and value.isdigit():
        return int(value)
    digits = dict(zip("零〇一二三四五六七八九两", (0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 2)))
    total = digit = 0
    for char in value:
        if char in digits:
            digit = digits[char]
        elif char in "十百千":
            total += (digit or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            digit = 0
        else:
            return None
    return total + digit


class SectionIndex:
    """Parse explicit headings and describe one unambiguous original interval.

    ``headings`` contains JSON-compatible dictionaries. ``raw`` and ``title``
    both equal ``text[start:end]`` (including the original heading marker).
    ``section_end`` excludes the next heading at the same or a higher level.
    ``describe`` returns full nodes without truncating titles or sibling lists.
    """

    def __init__(self, text):
        if not isinstance(text, str):
            raise TypeError("SectionIndex requires saved original text")
        self.text = text
        self.headings = []
        self.skipped = []
        self._lines = []
        offset = 0
        for line in text.splitlines(keepends=True):
            value = line.rstrip("\r\n")
            self._lines.append(dict(start=offset, end=offset + len(value), text=value))
            offset += len(line)
        self._line_starts = [line["start"] for line in self._lines]
        self._scan()
        self._confirm_decimals()
        self._build_tree()
        self._starts = [h["start"] for h in self.headings]
        self._heading_lines = {self._line_index(h["start"]) for h in self.headings}

    def _skip(self, start, end, reason):
        self.skipped.append(dict(start=start, end=end, raw=self.text[start:end], reason=reason))

    def _scan(self):
        offset = 0
        boundary = 0
        article_open = False
        base_level = 0
        fence = None
        for original_line in self.text.splitlines(keepends=True):
            line = original_line.rstrip("\r\n")
            raw = line.strip()
            start = offset + len(line) - len(line.lstrip())
            line_end = offset + len(line.rstrip())
            offset += len(original_line)
            if not raw:
                continue
            fence_match = re.match(r"(`{3,}|~{3,})", raw)
            if fence_match:
                mark = fence_match[1]
                if fence is None:
                    fence = (mark[0], len(mark))
                elif mark[0] == fence[0] and len(mark) >= fence[1]:
                    fence = None
                continue
            if fence is not None:
                continue

            family = None
            number = None
            decimal_prefix = None
            end = line_end
            match = _ARTICLE.fullmatch(raw)
            if match:
                boundary = start
                article_open = True
                base_level = 1
                family, level, number = "article", 0, int(match[1])
            elif _TITLE.match(raw):
                # A title belongs to an article only when it immediately follows
                # its marker (apart from whitespace), never to the prior article.
                previous = self.headings[-1] if self.headings else None
                adjacent = (article_open and previous and previous["family"] == "article"
                            and not self.text[previous["end"]:start].strip())
                if adjacent:
                    family, level = "title", 1
                    base_level = 2
                else:
                    boundary = start
                    article_open = False
                    family, level = "title", 0
                    base_level = 1
            else:
                md = _MARKDOWN.match(raw)
                chinese = _CHINESE.match(raw)
                paren = _PAREN.match(raw)
                chapter = _CHAPTER.match(raw)
                decimal = _DECIMAL.match(raw)
                embedded = list(_INLINE_DECIMAL.finditer(raw))
                # PDF columns and contents pages can place several headings on
                # one line. Their body ownership cannot be recovered by labels.
                if _TOC_DOTS.search(raw) and (decimal or embedded):
                    self._skip(start, line_end, "contents_or_dot_leader_layout")
                    continue
                if len(embedded) > 1 or (embedded and not decimal and not md and not chinese and not paren):
                    self._skip(start, line_end, "embedded_or_multiple_decimal_headings")
                    continue
                if md:
                    family, level = "markdown", base_level + len(md[1])
                elif chinese or paren or chapter:
                    chosen = chinese or paren or chapter
                    family = "chinese" if chinese else "chinese_parenthetical" if paren else "chapter"
                    level = base_level + (2 if paren else 1)
                    number = _number(chosen[1])
                    # The first source sentence may be a heading followed by
                    # prose on the same line. Preserve its exact prefix only.
                    stop = re.search(r"[。！？!?；;]", raw)
                    if stop:
                        end = start + len(raw[:stop.start()].rstrip())
                elif decimal:
                    parts = tuple(int(p) for p in decimal[1].split("."))
                    label = decimal[2]
                    # Requiring prose and a confirmed sibling sequence below
                    # rejects isolated decimals, dates and scalar/unit rows.
                    prose = len(re.findall(r"[\u4e00-\u9fff]", label)) >= 2 or len(re.findall(r"[A-Za-z]{2,}", label)) >= 2
                    if not prose:
                        self._skip(start, line_end, "decimal_not_confirmed_as_heading")
                        continue
                    family, level, number = "decimal", base_level + len(parts), parts[-1]
                    decimal_prefix = parts[:-1]
                    stop = re.search(r"[。！？!?；;]", raw)
                    if stop:
                        end = start + len(raw[:stop.start()].rstrip())
                else:
                    continue
            if end <= start:
                continue
            raw_title = self.text[start:end]
            self.headings.append(dict(id=f"heading:{start}:{end}", raw=raw_title,
                title=raw_title, start=start, end=end, section_end=len(self.text),
                level=level, parent_id=None, children_ids=[], family=family,
                number=number, boundary=boundary, decimal_prefix=decimal_prefix))

    def _confirm_decimals(self):
        groups = {}
        for h in self.headings:
            if h["family"] == "decimal":
                # A non-decimal higher heading separates otherwise identical
                # numbered families repeated in different document sections.
                parent_start = max((p["start"] for p in self.headings
                    if p["start"] < h["start"] and p["boundary"] == h["boundary"]
                    and p["family"] != "decimal" and p["level"] < h["level"]), default=-1)
                key = (h["boundary"], parent_start, h["decimal_prefix"])
                groups.setdefault(key, []).append(h)
        accepted = set()
        for group in groups.values():
            numbers = [h["number"] for h in group]
            if len(group) >= 2 and numbers == list(range(1, len(group) + 1)):
                accepted.update(h["id"] for h in group)
        retained = []
        for h in self.headings:
            if h["family"] != "decimal" or h["id"] in accepted:
                retained.append(h)
            else:
                self._skip(h["start"], h["end"], "decimal_sibling_sequence_unconfirmed")
        self.headings = retained

    def _build_tree(self):
        stack = []
        for h in self.headings:
            while stack and (stack[-1]["boundary"] != h["boundary"] or stack[-1]["level"] >= h["level"]):
                stack.pop()["section_end"] = h["start"]
            if stack:
                h["parent_id"] = stack[-1]["id"]
                stack[-1]["children_ids"].append(h["id"])
            stack.append(h)
        self._by_id = {h["id"]: h for h in self.headings}

    def _line_index(self, position):
        return bisect.bisect_right(self._line_starts, position) - 1

    def _standalone(self, heading):
        line = self._lines[self._line_index(heading["start"])]
        tail = self.text[heading["end"]:line["end"]]
        # A heading-final sentence mark alone is not a body paragraph.
        return not tail.strip(" \t\u3000。！？!?；;")

    def _uncertain_line(self, index):
        """An unknown boundary vetoes ownership; it never becomes a new title."""
        line = self._lines[index]
        value = line["text"].strip()
        if not value or index in self._heading_lines:
            return False
        if any(line["start"] <= item["start"] <= line["end"] for item in self.skipped):
            return True
        if _UNKNOWN_NUMBERED_BOUNDARY.match(value) or re.match(r"^(?:`{3,}|~{3,})", value):
            return True
        before_blank = index == 0 or not self._lines[index - 1]["text"].strip()
        after_blank = index + 1 == len(self._lines) or not self._lines[index + 1]["text"].strip()
        # Unnumbered, isolated text without a sentence ending may be a section
        # boundary, a page label, or a caption. Do not choose between them.
        sentence_end = re.search(r"[。！？!?；;.]([”’\"']*)$", value)
        return bool(before_blank and after_blank and not sentence_end)

    def _first_body_line(self, heading):
        index = self._line_index(heading["start"])
        if not self._standalone(heading):
            line = self._lines[index]
            return line["start"], line["end"]
        for next_index in range(index + 1, len(self._lines)):
            line = self._lines[next_index]
            if not line["text"].strip():
                continue
            if next_index in self._heading_lines or self._uncertain_line(next_index):
                return None
            return line["start"], line["end"]
        return None

    def _siblings(self, current):
        return [h for h in self.headings if h["parent_id"] == current["parent_id"]
                and h["boundary"] == current["boundary"]]

    def _chinese_parent_reason(self, parent, siblings):
        if parent is None:
            return "no_explicit_parent_for_numbered_directory"
        if parent["family"] not in {"chinese", "markdown", "chapter"} or not self._standalone(parent):
            return "parent_not_a_standalone_explicit_heading"
        if len(siblings) < 3:
            return "fewer_than_three_certifying_children"
        if any(h["family"] != "chinese_parenthetical" or h["level"] != parent["level"] + 1 for h in siblings):
            return "children_not_one_next_level_parenthetical_family"
        if [h["number"] for h in siblings] != list(range(1, len(siblings) + 1)):
            return "non_contiguous_or_restarted_child_numbers"
        for index, line in enumerate(self._lines):
            if parent["end"] <= line["start"] < parent["section_end"] and self._uncertain_line(index):
                return "unconfirmed_structure_boundary_in_parent"
        return None

    def _certified_path(self, current):
        path = []
        node = current
        while node is not None:
            path.append(node)
            node = self._by_id.get(node["parent_id"])
        path.reverse()
        for parent, child in zip(path, path[1:]):
            if parent["boundary"] != child["boundary"]:
                return [current], False
            if parent["family"] in {"article", "title"}:
                continue
            if parent["family"] == "markdown" and child["family"] == "markdown":
                continue
            if child["family"] == "chinese_parenthetical" and self._chinese_parent_reason(parent, self._siblings(child)) is None:
                continue
            return [current], False
        return path, True

    def _directory(self, current):
        siblings = self._siblings(current)
        parent = self._by_id.get(current["parent_id"])
        lo = parent["start"] if parent else min(h["start"] for h in siblings)
        hi = parent["section_end"] if parent else max(h["section_end"] for h in siblings)
        reason = None
        families = {h["family"] for h in siblings}
        if len(siblings) < 3:
            reason = "fewer_than_three_siblings"
        elif current["family"] not in _EXPLICIT:
            reason = self._chinese_parent_reason(parent, siblings)
        elif len(families) != 1:
            reason = "mixed_sibling_marker_families"
        elif any(lo <= item["start"] < hi for item in self.skipped):
            reason = "unresolved_heading_layout_in_parent"
        elif current["family"] in {"chinese", "chinese_parenthetical", "chapter", "decimal", "article"}:
            numbers = [h["number"] for h in siblings]
            if numbers != list(range(1, len(siblings) + 1)):
                reason = "non_contiguous_sibling_numbers"
        return siblings, reason

    def describe(self, start, end):
        """Return a path and a complete parsed sibling group, or a safe skip.

        A range containing both a parent heading and a child heading is
        ambiguous. Unknown plain-text headings are not guessed. Directory
        completeness is limited to the supported explicit heading grammar.
        """
        result = dict(applicable=False, status="skip", skip_reason=None,
            start=start, end=end, path=[], siblings=[], directory_complete=False,
            directory_skip_reason=None, directory_scope="supported_explicit_headings_with_same_parent",
            path_certified=False, path_scope="none")
        if (not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int)
                or isinstance(end, bool) or not 0 <= start < end <= len(self.text)):
            result["skip_reason"] = "invalid_original_interval"
            return result
        if len(self.headings) < 3:
            result["skip_reason"] = "fewer_than_three_heading_nodes"
            return result
        left, right = start, end
        while left < right and self.text[left].isspace():
            left += 1
        while right > left and self.text[right - 1].isspace():
            right -= 1
        if left == right:
            result["skip_reason"] = "whitespace_only_interval"
            return result
        index = bisect.bisect_right(self._starts, left) - 1
        if index < 0:
            result["skip_reason"] = "before_first_explicit_heading"
            return result
        current = self.headings[index]
        if right > current["section_end"] or any(left < h["start"] < right for h in self.headings):
            result.update(status="ambiguous", skip_reason="crosses_heading_boundary")
            return result
        # An unresolved structural line invalidates ownership until the next
        # accepted heading; do not silently carry an older title across it.
        if any(current["start"] < item["start"] < right for item in self.skipped):
            result.update(status="ambiguous", skip_reason="unresolved_heading_before_or_inside_interval")
            return result
        if current["family"] not in _EXPLICIT:
            bounds = self._first_body_line(current)
            if bounds is None or not bounds[0] <= left < right <= bounds[1]:
                result.update(skip_reason="outside_first_body_line_of_numbered_heading",
                              permitted_body_line=list(bounds) if bounds else None)
                return result
        path, certified = self._certified_path(current)
        siblings, directory_reason = self._directory(current)
        if directory_reason is None and not certified:
            directory_reason = "uncertified_ancestor_chain"
        result.update(applicable=True, status="ok", path=copy.deepcopy(path),
            siblings=copy.deepcopy(siblings), directory_complete=directory_reason is None,
            directory_skip_reason=directory_reason, path_certified=certified,
            path_scope="certified_original_hierarchy" if certified else "current_heading_only")
        return result

# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Conservative discourse guard for saved prose outside requested records.

Unknown prose stays visible. Explicit progress/status/plan blocks with no
binding cues may be displayed as separate, expandable original context. All
paragraphs remain cited original dependencies. This is a bounded lexical guard,
not semantic entailment or a guarantee that all dependencies are recognized.
"""
import re

from .question_plan import paragraphs
from .source_scope import norm

VERSION = 'conservative-projection-context-v1'

# A selected record or another tab may refer back to an otherwise innocuous
# introduction. In that case retain all context across the complete scope.
REFERENCE = re.compile(r'前述|上述|以上|以下|下列|前文|上文|下文|同上|同前|'
                       r'详见|参见|根据|依据|参照|按照|依照|见(?:上|下)')


def context_is_referenced(bodies):
    return any(REFERENCE.search(body) for body in bodies)

# Discourse roles, not business labels or document/question-specific values.
PROGRESS = re.compile(
    r'^(?:#{1,6}\s*)?(?:'
    r'(?:近期|目前|当前|本阶段|上一阶段|下一阶段|本期|上期|下期|'
    r'(?:本|这|上|下)(?:周|月|季度|年度))的?'
    r'(?:(?:工作|任务|实施|项目)的?)?(?:已经|已|将)?(?:完成|进展|进度|回顾|总结|重点|计划|安排|继续)'
    r'|(?:工作|任务|实施|项目|阶段)(?:进展|进度|回顾|总结)'
    r'|已完成(?:工作|任务|事项)|后续(?:工作|计划|安排))[：:]?')

# References or operational qualifications take precedence over the discourse
# role, even inside an otherwise clearly marked progress paragraph. Broad cues
# intentionally preserve some unrelated text instead of risking lost terms.
BINDING = re.compile(
    r'本(?:清单|节|条|项|规定|规则|要求|方案|流程|协议)|'
    r'以下|下列|上述|以上|前述|这些|各项|每项|全部(?:免|停|不|须|需)|'
    r'适用|生效|失效|有效期|前提|条件|限制|例外|除非|否则|只有|仅(?:在|限|供)|'
    r'限于|限定于|限.{0,10}使用|专用|(?:从|自).{1,16}起|'
    r'必须|应当|务必|不得|禁止|不能|不允许|不予|无需|无须|不用|不收|不执行|'
    r'不办理|不代表|不再|免除|豁免|取消|退还|退回|暂停|停止|另行|'
    r'尚未|未获|未满足|未完成|未批准|未经|须|需(?:要|经|在|于|先|同时)|'
    r'如果|假如|(?:当|若|如).{0,24}(?:时|则)|(?:时|后|前)[，,]|'
    r'截止|截至|期限|到期|最迟|不得晚于|之前|以内|之内|前一天|'
    r'明[天日]|后天|次日|月底|月末|周末|(?:周|星期)[一二三四五六日天].{0,8}(?:前|后|内)|'
    r'(?:\d|[一二三四五六七八九十])[日天周月年点时分].{0,8}(?:前|后|内)|'
    r'根据|依据|参照|参见|见(?:上|下)|详见')

# An actorless qualification is presumed to govern the requested material.
# An explicit actor inside a progress description may be ancillary (e.g. a
# report's own output behavior), but remains fully visible in expandable cited
# context, never removed from the evidence/dependency contract.
IMPLICIT_QUALIFIER = re.compile(r'(?:^|[。；;，,：:\n])\s*(?:只在|只对|只供|只限|仅用于)')


def context_parts(body, start, end, labels, protect_background=False):
    """Return exact paragraph spans and auditable keep/omit decisions.

    Bounds are computed by each producer/consumer independently. Keeping is
    the default, including opaque prose and context-only tabs. No source text
    is modified; background remains a cited dependency with a display role.
    """
    for a, b, text in paragraphs(body[start:end]):
        while a < b and body[start+a].isspace():
            a += 1
        while b > a and body[start+b-1].isspace():
            b -= 1
        if a == b:
            continue
        text = body[start+a:start+b]
        normalized = norm(text)
        mentions_label = any(norm(label) in normalized for label in labels)
        independent = bool(not protect_background and PROGRESS.match(text)
                           and not mentions_label and not BINDING.search(text)
                           and not IMPLICIT_QUALIFIER.search(text))
        yield start+a, start+b, text, ('independent_progress' if independent else 'preserved_unproved_context')

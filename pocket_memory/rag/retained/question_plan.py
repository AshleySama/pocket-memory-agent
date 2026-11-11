# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Bounded, original-text question plans. No LLM, rank-as-proof, or answer repair.

Only fully parsed field/list requests in an explicit metadata scope are eligible.
An unsupported grammar falls back unchanged. An eligible partial plan never asks
the model to invent missing roles. Every displayed statement is an original span.
"""
import copy
import hashlib
import re
import unicodedata

VERSION = 'original-question-plan-v1'
NUM = r'[一二三四五六七八九十两0-9]+'
LABELS = {'owner': '负责人', 'date': '日期', 'steps': '执行顺序',
          'risk': '风险与对应措施', 'acceptance': '验收／合格条件'}


def norm(s):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', s or ''))


def number(s):
    if s.isdecimal(): return int(s)
    table = dict(zip('一二三四五六七八九', range(1, 10))); table['两'] = 2
    if s in table: return table[s]
    if s == '十': return 10
    if s.startswith('十') and s[1:] in table: return 10 + table[s[1:]]
    if s.endswith('十') and s[:-1] in table: return table[s[:-1]] * 10
    return None


def paragraphs(body):
    return [(m.start(), m.end(), m[0]) for m in
            re.finditer(r'[^\r\n]+(?:\r?\n(?!\s*\r?\n)[^\r\n]+)*', body)]


def raw(body, field, a, b):
    return dict(field=field, char_start=a, char_end_exclusive=b, quote=body[a:b])


def parse_request(question, names):
    """Small public grammar, not an approximate semantic question classifier."""
    q = norm(question)
    q = re.sub(r'[《【][^》】]+[》】]', '', q)
    for name in sorted(set(names), key=len, reverse=True):
        if len(norm(name)) >= 3: q = q.replace(norm(name), '')
    q = re.sub(r'(?:页签|标签页)[0-9一二三四五六七八九十]+', '', q)
    q = re.sub(r'项目标识[A-Za-z0-9_-]+的资料', '资料', q)
    roles = []
    patterns = [
        ('owner', r'(?:唯一)?负责人(?:是谁|叫什么|是什么|为谁)?|谁负责'),
        ('date', r'(?:这次|本次)?(?:正式)?(?:演示|交付|发布|上线)?(?:日期(?:是哪一天|是哪天|是什么|是几月几日)?|安排在几月几日、哪一年)'),
        ('steps', rf'(?:(?:准备到演示|执行|操作|实施|办理|处理)的?)?(?:{NUM}步)?(?:执行)?(?:顺序|步骤|流程)'),
        ('risk', r'(?:有哪些|哪些|什么)?风险(?:有哪些|是什么)?(?:[,，]?各自(?:采取|对应|需要)?(?:什么|哪些)?(?:处理|应对)?措施|与对应措施)?'),
        ('acceptance', rf'(?:(?:正式)?(?:演示|试运行|执行|实施|交付|上线|发布|迁移)?(?:结束|完成)?后(?:的)?)?(?:{NUM}项)?(?:最终)?(?:验收|检收|合格)(?:必须|须|要)?(?:同时)?(?:满足|完成)?(?:的)?(?:哪?{NUM}项)?(?:标准|条件|要求)?(?:是什么|有哪些)?'),
    ]
    for role, pattern in patterns:
        found = list(re.finditer(pattern, q))
        if found:
            roles.append(dict(role=role, request_text=[m[0] for m in found]))
            q = re.sub(pattern, '', q)
    # Only presentation and source-introduction syntax may remain. A further
    # substantive request (budget, cause, comparison, etc.) keeps the old route.
    fillers = (r'只依据|只根据|仅依据|仅根据|依据|根据|结合|两份|这两份|资料|项目|'
               r'简要说明|完整说明|说明|请保留通过条件|适用范围|例外条件|'
               r'请用三条列出|请用一句话回答|请简短回答|请给出引用|给出引用|给出处|'
               r'请列出|列出|回答|简短|简要|分别是什么|是什么|各自采取什么措施|'
               r'请|并|和|与|以及|的|中|里|[，,。.!！?？：:、；;\s]')
    residue = re.sub(fillers, '', q)
    if not roles or residue: return None
    return roles


def list_shape(text):
    """Prove a bounded enumeration; counts belong to the source, not question."""
    marker = re.compile(rf'第({NUM})(步|项)|[（(]({NUM})[）)]|(?:^|[；;\n：:])\s*({NUM})[、.．]', re.M)
    marks = list(marker.finditer(text))
    colon = re.search(r'[：:]', text)
    lead_end = marks[0].start() if marks else colon.start() if colon else len(text)
    lead = text[:lead_end]
    declared = re.search(rf'({NUM})(?:项|步|条)', lead)
    expected = number(declared[1]) if declared else None
    if marks:
        nums = [number(m[1] or m[3] or m[4]) for m in marks]
        if nums != list(range(1, len(nums)+1)) or len(nums) < 2: return None
        actual = len(nums)
    elif colon and '；' in text[colon.end():] or colon and ';' in text[colon.end():]:
        # The first sentence is the semicolon list; later sentences are its
        # conditions/signatures. Never count semicolons in later prose.
        members = re.split(r'[。！？!?\n]', text[colon.end():], 1)[0]
        actual = len([s for s in re.split(r'[；;]', members) if s.strip()])
        if actual < 2: return None
    else: return None
    if actual > 16 or expected is not None and expected != actual: return None
    return dict(lead=lead, member_count=actual, declared_count=expected,
                proof='contiguous numbered members' if marks else 'bounded semicolon list')


def list_roles(lead):
    """Only the list's introducing clause can assign its role, not its tail."""
    roles = []
    if re.search(r'(?:执行|操作|实施|办理|处理)?(?:顺序|步骤|流程)(?:固定)?(?:为|是|如下|共|包括|包含)', lead): roles.append('steps')
    if re.search(r'风险(?:恰好|共|有|包括|如下|为|是|处置|处理)', lead): roles.append('risk')
    acceptance = re.search(r'(?:最终验收|验收|检收)(?:标准|条件|要求)?(?:须|需|必须|要|共|有|包括|包含)|合格条件(?:共|须|必须|如下|为|是)', lead)
    shared = re.search(r'同时作为[^。！？]*?(?:最终)?验收标准', lead)
    if acceptance or shared: roles.append('acceptance')
    return roles


def subject_bound(role, lead, names):
    """An introducing role belongs to this object, not an embedded subobject.

    Only syntactic local/explicit subjects and temporal introductions qualify.
    Unknown subjects stay unproved; ranking or mention anywhere cannot bind them.
    """
    subject = lead
    if role=='acceptance' and '同时作为' in lead:
        subject = lead.split('同时作为',1)[1]
    marker = {'acceptance':r'验收|检收|合格条件','steps':r'执行顺序|操作顺序|实施顺序|顺序|步骤|流程',
              'risk':r'风险'}[role]
    subject = re.split(marker, subject, 1)[0]
    for name in sorted(set(names),key=len,reverse=True):
        if len(norm(name))>=3: subject = subject.replace(name, '')
    subject = re.sub(r'(?:(?:正式)?(?:演示|试运行|执行|实施|交付|上线|发布|迁移)?(?:结束|完成)?后)', '', subject)
    subject = re.sub(r'需要关注|下列|以下|本项目|该项目|本次项目|项目|本次|最终|的|[，,\s]', '', subject)
    return not subject


DEPENDENCY = re.compile(r'^(?:本节|适用范围|前提|条件|补充条件|限制|注意|但|除非|只有|若|仅在|任何|以上|上述|这些|不得|必须|同时)')


class QuestionPlan:
    def __init__(self, notes, ocr, scope):
        self.notes, self.ocr, self.scope = notes, ocr, scope
        self.audit = {}

    def resolve(self, question):
        allowed, scopes = self.scope(question)
        names = []
        if scopes:
            if any(not s.get('matched_sources') for s in scopes): return None
            keys = {k for s in scopes for k in s['matched_sources']}
            if any(not k.startswith('note:') for k in keys): return None
            ids = {int(k.split(':')[1]) for k in keys}
            names.extend(s['quoted_title'] for s in scopes)
            scope_kind = 'explicit_saved_source_scope'
        else:
            # A literal shared title stem is an explicit object, not a semantic
            # similarity guess. Unnamed/general topics remain the retained route.
            groups = {}
            for note in self.notes.values():
                title = note['title']
                if note.get('parent_note_id') is not None: continue
                stem = re.split(r'[：:]', title, 1)[0]
                if len(stem) >= 4 and norm(stem) in norm(question): groups.setdefault(stem, set()).add(note['id'])
            if len(groups) != 1: return None
            name, ids = next(iter(groups.items())); names.append(name)
            # Do not silently narrow a family which has additional tabs.
            if any(n.get('parent_note_id') in ids for n in self.notes.values()): return None
            scope_kind = 'literal_unique_metadata_subject'
        if not ids or len(ids) > 8: return None
        notes = [self.notes[i] for i in sorted(ids)]
        for n in notes:
            names.extend([n['title'], re.split(r'[：:]', n['title'], 1)[0]])
        if sum(len(n['content'])+len(self.ocr.get(n['id'], '')) for n in notes) > 24000: return None
        return notes, names, scope_kind

    def units(self, notes, names):
        found = []; self.extraction_errors = []
        for note in notes:
            for field, body in [('content', note['content']), ('ocr_text', self.ocr.get(note['id'], ''))]:
                if not body: continue
                if re.search(r'<(?:table|tr|p|div)\b|\|[^\n]+\|', body, re.I): continue
                ps = paragraphs(body)
                for i, (a, b, text) in enumerate(ps):
                    shape = list_shape(text)
                    lead = re.split(r'[：:。\n]', text, 1)[0]
                    if not shape:
                        for role in list_roles(lead):
                            if subject_bound(role, lead, names):
                                self.extraction_errors.append(dict(role=role, source_id=note['id'],
                                    field=field, reason='visible_relation_but_unproved_list_structure',
                                    raw_evidence=raw(body,field,a,b)))
                    # Follow the complete adjacent dependency run. A second
                    # condition is no less binding than the first one.
                    left = right = i
                    while left and DEPENDENCY.match(ps[left-1][2]): left -= 1
                    while right+1 < len(ps) and DEPENDENCY.match(ps[right+1][2]): right += 1
                    rs = [raw(body,field,pa,pb) for pa,pb,_ in ps[left:right+1]]
                    if shape:
                        for role in list_roles(shape['lead']):
                            if not subject_bound(role, shape['lead'], names): continue
                            found.append(dict(role=role, note=note, field=field, raws=rs,
                                              display=[r['quote'] for r in rs], value=text, shape=shape,
                                              relation_proof=raw(body, field, a, a+len(shape['lead']))))
                    fields = [
                        ('owner', r'(?:^|[。；;，,\n])\s*((?:(?:本|该)?项目)?(?:唯一)?负责人(?:是|为|[：:])\s*[^，,。；;\n]+)'),
                        ('date', r'(?:^|[。；;，,\n])\s*((?:正式)?(?:演示|交付|发布|上线)?日期(?:是|为|[：:])\s*\d{4}年\d{1,2}月\d{1,2}日)'),
                    ]
                    for role, pattern in fields:
                        for m in re.finditer(pattern, text):
                            # The entire containing paragraph is evidence; the
                            # answer clause is verbatim, never synthesized values.
                            independent_fields = ('\n' in text and all(re.fullmatch(r'[^：:\n]{1,30}[：:][^\n]+',v.strip())
                                for v in text.splitlines() if v.strip()))
                            display = ([m[1]] if independent_fields and len(rs)==1 else [r['quote'] for r in rs])
                            found.append(dict(role=role, note=note, field=field,
                                raws=rs, display=display, value=m[1],
                                relation_proof=raw(body, field, a+m.start(1), a+m.end(1))))
        return found

    def prepare(self, question):
        self.audit = dict(version=VERSION, active=False, reason='outside_supported_scope_or_grammar')
        resolved = self.resolve(question)
        if resolved is None: return None
        notes, names, scope_kind = resolved
        facets = parse_request(question, names)
        if facets is None: return None
        units = self.units(notes, names)
        if not any(u['role'] in {f['role'] for f in facets} for u in units): return None
        requested_roles={f['role'] for f in facets}
        if any(e['role'] in requested_roles for e in self.extraction_errors):
            self.audit.update(reason='unparsed_original_structure_uses_retained_contract',
                              extraction_errors=self.extraction_errors)
            return None
        # If an original field cannot be parsed as plain text, no partial plan
        # may conclude missingness across it. Preserve the old parser/route.
        if any(re.search(r'<(?:table|tr|p|div)\b|\|[^\n]+\|', body, re.I)
               for n in notes for body in (n['content'],self.ocr.get(n['id'],''))):
            self.audit['reason'] = 'non_plain_original_uses_retained_contract'; return None
        sources = []; by_span = {}; bindings = []; lines = []
        def bind(unit):
            note, rs = unit['note'], unit['raws']
            key = (note['id'], unit['field'], tuple((r['char_start'],r['char_end_exclusive']) for r in rs))
            if key in by_span: return by_span[key]
            title = note['title']; parent = self.notes.get(note.get('parent_note_id'))
            if parent: title = parent['title'] + ' / ' + title
            source = dict(index=len(sources)+1, source_type='note', source_id=note['id'],
                source_key=f'note:{note["id"]}', title=title, parent_note_id=note.get('parent_note_id'),
                tab_name=note.get('tab_name'), kind='ocr' if unit['field']=='ocr_text' else 'content',
                evidence='\n'.join(r['quote'] for r in rs), raw_evidence=copy.deepcopy(rs),
                source_field=unit['field'], coverage_complete=False, truncated=False,
                location=('图片识别' if unit['field']=='ocr_text' else '保存正文')+' · '+', '.join(f'{r["char_start"]}–{r["char_end_exclusive"]}' for r in rs))
            source['evidence_id'] = 'plan:' + hashlib.sha256(repr(key).encode()).hexdigest()[:24]
            sources.append(source); by_span[key] = source['index']; return source['index']
        globally_shown=set()
        for facet in facets:
            role = facet['role']; candidates = [u for u in units if u['role']==role]
            if role=='date':
                phases=set(re.findall(r'演示|交付|发布|上线',''.join(facet['request_text'])))
                if phases:
                    candidates=[u for u in candidates if set(re.findall(r'演示|交付|发布|上线',u['value']))==phases]
            # Distinct values are a conflict, not a reason to trust top-1.
            values = {norm(u['value']) for u in candidates}
            facet.update(state='complete' if len(values)==1 else 'ambiguous' if values else 'insufficient', support=[])
            section_start=len(lines)
            lines.append('**'+LABELS[role]+'**')
            if not candidates:
                lines.append('当前所选资料未能确认完整的'+LABELS[role]+'；不会用其他事项代替。')
                continue
            if len(values)>1: lines.append('所选资料存在不同记录，尚不能确定唯一结果；分别列出原文，请核对对象或版本。')
            requested = {number(m[1]) for text in facet['request_text'] for m in re.finditer(rf'({NUM})(?:项|步|条)',text)}
            counts = {u['shape']['member_count'] for u in candidates if u.get('shape')}
            if len(requested)==1 and len(counts)==1 and requested!=counts:
                lines.append(f'问题提到{next(iter(requested))}项，原文实际列出{next(iter(counts))}项；以下保留完整原文。')
            shown = set()
            for unit in candidates:
                index = bind(unit)
                facet['support'].append(dict(index=index, relation_proof=unit['relation_proof'],
                    member_count=unit.get('shape',{}).get('member_count')))
                displayed = [r['quote'] for r in unit['raws']] if len(values)>1 else unit['display']
                for quoted in displayed:
                    key = (quoted, index)
                    if key in shown or key in globally_shown: continue
                    shown.add(key)
                    globally_shown.add(key)
                    lines.append(f'{quoted} [{index}]')
                    bindings.append(dict(role=role, source_index=index, quote=quoted,
                                         relation_proof=unit['relation_proof']))
            if len(lines)==section_start+1:lines.pop()
        self.audit = dict(version=VERSION, active=True, scope_kind=scope_kind,
            scope_note_ids=[n['id'] for n in notes], facets=facets,
            source_revisions={f'{n["id"]}:{f}':hashlib.sha256(t.encode()).hexdigest()
                for n in notes for f,t in [('content',n['content']),('ocr_text',self.ocr.get(n['id'],''))]},
            rendering='verbatim original clauses/complete lists and dependencies',
            fallback_reason=None)
        response = dict(answer='\n\n'.join(lines), sources=sources, provided_sources=copy.deepcopy(sources),
            answer_status='answered', citation_validation=dict(passed=True, errors=[]),
            trace=dict(question_plan=copy.deepcopy(self.audit), generation_calls=0,
                       bindings=bindings, rendering='original-question-plan', messages=[]))
        return dict(mode='single_free_text', sources=sources, direct_record_response=response)

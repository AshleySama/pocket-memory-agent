# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Schema-led, zero-generation record projection. No dataset IDs or domain values.

Unsupported unrestricted queries keep the retained route. Explicit OCR requests
never fall back to a path which can silently substitute the saved body.
"""
import copy
import hashlib
import re
from dataclasses import dataclass
from . import table_contract as tables
from .source_scope import SourceScopes, TITLE, norm
from .field_boundary import IDENT, TYPES, STORAGE
from .request_contract import field_actions, normalized_positions, original_span, bind_operations, endpoint_projection

VERSION = 'source-field-projection-v3-cell-endpoints'

def revision(body):
    return hashlib.sha256(body.encode('utf-8')).hexdigest()

def ocr_qualifier(text):
    # Merely discussing OCR is not a restriction to the saved OCR field.
    return bool(re.search(r'(?:已经|已)?保存的OCR(?:文本)?|'
                          r'(?:的OCR|OCR文本)(?:里|中|[，,。?？]|$)|OCR(?:里|中)|'
                          r'(?:只读|只看|仅用|仅根据)OCR(?:文本)?', text,re.I))

@dataclass
class ScopeSpec:
    pairs: dict
    explicit: bool = False
    strict: bool = False
    unresolved: bool = False
    constrained: bool = False
    actions: tuple = ()
    excluded: tuple = ()

    def permits(self, sid, field):
        return field in self.pairs.get(sid, ())

def resolve_scope(question, notes):
    """Associate each field qualifier with its source, never union fields globally."""
    titles = list(TITLE.finditer(question)); pairs = {}; unresolved = False
    preliminary_fields,_,preliminary_actions,_=field_actions(TITLE.sub(lambda m:' '*len(m[0]),question))
    strict = 'ocr_text' in preliminary_fields; constrained=bool(preliminary_actions); actions=[]; excluded=[]
    if titles:
        for i, m in enumerate(titles):
            end = titles[i+1].start() if i+1 < len(titles) else len(question)
            segment = question[m.start():end]
            found = [n for n in notes.values() if norm(n['title']) == norm(m[1])]
            if len(found) != 1: unresolved = True; continue
            n = found[0]
            resolved = SourceScopes(notes).resolve(segment)
            if resolved:
                if resolved['status'] != 'resolved': unresolved = True; continue
                ids = [int(k.split(':')[1]) for k in resolved['allowed']]
            else:
                ids = [n['id']] + [v['id'] for v in notes.values() if v.get('parent_note_id') == n['id']]
            # An OCR qualifier before the first source applies only to that
            # expression. Subsequent source expressions own their own qualifiers.
            start=0 if i==0 else m.start()
            local=question[start:end]
            # Mask only bound source names, preserving positions for action spans.
            local=TITLE.sub(lambda t:' '*len(t[0]),local)
            fields,deny,events,conflict=field_actions(local,start,bound_source=True)
            strict |= 'ocr_text' in fields; constrained |= bool(events);unresolved |= conflict
            for sid in ids:
                pairs.setdefault(sid,set()).update(fields)
                excluded.extend((sid,f) for f in sorted(deny))
                actions.extend(dict(e,source_id=sid) for e in events)
        return ScopeSpec(pairs, True, strict, unresolved, constrained,tuple(actions),tuple(excluded))
    # Saved metadata identifiers, not values in the body, identify interface docs.
    ident = re.search(r'(接口)\s*(?:ID\s*[:：]?)?\s*(\d+)', question, re.I)
    fields,deny,events,conflict=field_actions(question,bound_source=bool(ident))
    strict='ocr_text' in fields;constrained=bool(events)
    if ident:
        pattern = re.compile(re.escape(ident[1])+r'\s*(?:ID\s*[:：]?)?\s*'+re.escape(ident[2])+r'(?!\d)', re.I)
        found = [n for n in notes.values() if pattern.search(n['title'])]
        if len(found) == 1:
            sid=found[0]['id']
            return ScopeSpec({sid:fields},True,strict,conflict,constrained,
                             tuple(dict(e,source_id=sid) for e in events),tuple((sid,f) for f in sorted(deny)))
        if constrained: return ScopeSpec({}, True, strict, True,True,tuple(events))
    if constrained: return ScopeSpec({}, True, strict, True,True,tuple(events))
    return ScopeSpec({i: {'content'} for i in notes}, False)

def strip_scopes(question, notes):
    """Mask only bound source expressions, retaining original character positions."""
    spans=[]
    for m in TITLE.finditer(question):
        if sum(norm(n['title'])==norm(m[1]) for n in notes.values())==1:
            spans.append(m.span())
            local=re.match(r'\s*(?:里|中|内)',question[m.end():])
            if local:spans.append((m.end(),m.end()+local.end()))
    ident=re.search(r'接口\s*(?:ID\s*[:：]?)?\s*\d+',question,re.I)
    scope=resolve_scope(question,notes)
    if ident and scope.explicit and not scope.unresolved:spans.append(ident.span())
    resolved=SourceScopes(notes).resolve(question)
    if resolved and resolved['status']=='resolved':
        lead=re.split(r'[，,：:]',question,maxsplit=1)[0]
        spans.extend(m.span() for m in re.finditer(r'(?:标签页|页签)\s*[0-9一二三四五六七八九十]+|首页|主页|主笔记',lead))
    if not scope.unresolved:
        spans.extend((e['start'],e['end']) for e in scope.actions)
    chars=list(question)
    for a,b in spans:chars[a:b]=' '*(b-a)
    return ''.join(chars)

def request_fields(question, table, entities, notes):
    """Match original labels or a unique grammatical suffix; consume every ask.

    A short suffix is eligible only when used as a requested property. No fuzzy
    semantic expansion, entity-specific aliases, or model-selected columns.
    """
    q,positions = normalized_positions(strip_scopes(question, notes))
    entity_spans=[]
    for name in sorted(set(entities), key=len, reverse=True):
        for m in re.finditer(re.escape(norm(name)),q):
            if not any(m.start()<b and m.end()>a for a,b,_ in entity_spans):
                entity_spans.append((m.start(),m.end(),name))
    # Keep the pre-mask form for relation binding, then mask in place.
    original_q=q
    for a,b,_ in entity_spans:q=q[:a]+' '*(b-a)+q[b:]
    headers = table['headers']; found = []; intervals = []
    # Unknown labels are accepted only when explicitly quoted as a requested
    # column/field. They become missing-column states, never invented values.
    missing=[]
    if re.search(r'字段|列', question):
        for m in re.finditer(r'[“"]([^”"\n]{1,24})[”"]', question):
            label=m[1]
            if norm(label) not in [norm(h) for h in headers] and norm(label) in q:
                missing.append(label)
    for h in sorted(headers, key=len, reverse=True):
        if h == headers[table['key']]: continue
        forms = [norm(h)]
        # "适合什么场景" / "读哪些资料" preserve an actual unique label suffix.
        if len(norm(h)) >= 4:
            suffix = norm(h)[-2:]
            if sum(norm(x).endswith(suffix) for x in headers) == 1:
                forms.append(suffix)
        for form in forms:
            for m in re.finditer(re.escape(form), q):
                if any(m.start() < b and m.end() > a for a,b,_ in intervals): continue
                if form != norm(h) and not re.search(r'(?:什么|哪些|适用|适合|列出|的)$', q[:m.start()]): continue
                intervals.append((m.start(), m.end(), h))
    if not intervals: return None
    for label in missing:
        for m in re.finditer(re.escape(norm(label)),q): intervals.append((m.start(),m.end(),label))
    # Only an explicit property-list grammar may yield partial projection. The
    # residual is kept as an unconfirmed ask, NEVER inferred as a missing column.
    unconfirmed=[]
    if len(set(entities))==1 and len(entity_spans)==1:
        a,b,_=entity_spans[0]
        property_list=re.fullmatch(r'的?(.+?)(?:分别是什么|各是什么|是什么)[?？。]*',original_q[b:])
        if property_list:
            start=b+property_list.start(1);value=property_list[1];parts=list(re.finditer(r'[^、和与及]+',value))
            if len(parts)>=2:
                for part in parts:
                    pa,pb=start+part.start(),start+part.end()
                    if any(pa<=x and y<=pb for x,y,_ in intervals):continue
                    if re.fullmatch(r'[\u4e00-\u9fffA-Za-z0-9_]{2,24}',part[0]):
                        unconfirmed.append(dict(text=question[slice(*original_span(positions,pa,pb))],
                                                span=original_span(positions,pa,pb)))
                        q=q[:pa]+' '*(pb-pa)+q[pb:]
    # Read in question order, not header order; comparisons apply only to the
    # clause in which the requested label occurs.
    endpoints=endpoint_projection(original_q,intervals,entity_spans,positions)
    operations=(endpoints['operations'] if endpoints else bind_operations(original_q,intervals,entity_spans,entities,positions))
    if operations is None:return None
    compares={o['field'] for o in operations if o['kind']=='same_field_across_entities'}
    for a,b,h in sorted(intervals):
        if h not in found: found.append(h)
    for a,b,h in sorted(intervals, reverse=True): q = q[:a]+' '+q[b:]
    # Closed question scaffolding only: meaningful residual text forces fallback.
    neutral = (r'没有这一项就说明找不到|不存在就说明找不到|如果没有就说明找不到|'
               r'分别是什么|是什么|需要哪些|读哪些|适合什么|适用什么|目前是什么|'
               r'是否相同|是否一致|是不是一样|一样吗|相同吗|有何差异|'
               r'只根据|只按|只看|只读|仅根据|根据|按照|分别|各自|它们|两者|'
               r'现在|目前|什么|哪些|请问|列出|告诉我|需要|比较|对比|'
               r'字段|这些列|它|的|和|与|及|是|吗|并')
    residual = re.sub(neutral, '', q)
    if re.sub(r'[\W_]+', '', residual): return None
    plan=dict(fields=found, compare=sorted(compares),missing_columns=missing,
        operations=operations,mentions=dict(
            entities=[dict(entity=n,span=original_span(positions,a,b)) for a,b,n in sorted(entity_spans)],
            fields=[dict(field=h,span=original_span(positions,a,b)) for a,b,h in sorted(intervals)]))
    if endpoints:plan['projections']=endpoints['projections']
    if unconfirmed:plan['unconfirmed']=unconfirmed
    return plan


def absent_subject(question,table,notes):
    """Syntactic candidate plus vetoes; similarity can reject, never prove absence."""
    # Unknown unquoted phrases are not by themselves proof of an entity name.
    # Restrict negative assertions to an explicit requested absence check.
    if not re.search(r'(?:没有|不存在|找不到).*(?:说明|说|标|写)|(?:说明|说|标|写).*(?:没有|不存在|找不到)',question):return None
    tail=norm(strip_scopes(question,notes))
    tail=re.sub(r'^(?:只按|只看|只根据|按照|根据|按)?[，,]?(?:里|中|内)?','',tail)
    m=re.match(r'^([^，,。？?的]{4,60})的',tail)
    if not m:return None
    name=m[1]
    if any(norm(h) in name for h in table['headers'] if norm(h)):return None
    keys=[norm(r['values'][table['key']]) for r in table['rows']]
    if any(name in k or k in name or tables.one_edit(name,k) for k in keys):return None
    # Predicates and abbreviations present in any cell cannot certify an absent
    # entity. This is independent of the producer's entity finder.
    cells=[norm(v) for r in table['rows'] for v in r['values'] if norm(v)]
    if any(name in v or v in name for v in cells):return None
    return name


def subject_candidates(question,table,notes):
    """A source-bound noun phrase can be a cell value or abbreviated row name.

    Return candidate identities, never an inferred entity or a filtered answer.
    No fuzzy selection: only literal contained names or exact non-key values.
    """
    tail=norm(strip_scopes(question,notes))
    tail=re.sub(r'^(?:只按|只看|只根据|按照|根据|按)?[，,]?','',tail)
    m=re.match(r'^([^，,。？?的]{2,60})的',tail)
    if not m:return None
    subject=m[1];plan=request_fields(question,table,[subject],notes)
    if not plan or plan['operations'] or plan.get('unconfirmed') or plan['missing_columns']:return None
    name_rows=[r for r in table['rows'] if subject in norm(r['values'][table['key']])]
    value_cols=[h for i,h in enumerate(table['headers']) if i!=table['key'] and
                any(norm(r['values'][i])==subject for r in table['rows'])]
    if value_cols:return dict(subject=subject,relation='cell_value',columns=value_cols,requested=plan,rows=[])
    if 0<len(name_rows)<=4:return dict(subject=subject,relation='name_fragment',columns=[],requested=plan,rows=name_rows)
    return None


def projected_fields(plan,entity):
    if 'projections' not in plan:return plan['fields']
    return next((v['fields'] for v in plan['projections'] if v['entity']==entity),[])


def bound_cells(plan,records,refs):
    """Materialize per-operand source/column identities only when unambiguous."""
    comparisons=[]
    for operation in plan['operations']:
        if operation['kind']!='cross_cell_compare':continue
        operands=[]
        for endpoint in operation['operands']:
            matches=[(i,r) for i,r in enumerate(records) if r['entity']==endpoint['entity'] and endpoint['field'] in r['values']]
            if len(matches)!=1:return None
            i,r=matches[0];col=endpoint['field']
            if col not in r['header']:return None
            operands.append(dict(entity=r['entity'],column=col,column_index=r['header'].index(col),
                value=r['values'][col],source_id=r['source_id'],source_field=r['field'],
                row_start=r['row_start'],reference_index=i+1,revision=refs[i]['source_revision'],
                parent_note_id=refs[i]['parent_note_id'],tab_name=refs[i]['tab_name'],
                spans=[[x['char_start'],x['char_end_exclusive']] for x in refs[i]['raw_evidence']],
                context=r['context'],entity_span=endpoint['entity_span'],field_span=endpoint['field_span']))
        comparisons.append(dict(operands=operands,predicate_span=operation['predicate_span']))
    return comparisons

def evidence(note, field, body, spans, kind):
    raws = [dict(field=field, char_start=a, char_end_exclusive=b, quote=body[a:b],
                 revision=revision(body)) for a,b in spans]
    return dict(source_type='note', source_id=note['id'], source_key=f'note:{note["id"]}',
        title=note['title'], parent_note_id=note.get('parent_note_id'), tab_name=note.get('tab_name'),
        source_field=field, kind=kind, raw_evidence=raws, evidence='\n'.join(r['quote'] for r in raws),
        location='已保存OCR' if field=='ocr_text' else '保存正文表格',
        coverage_complete=False, truncated=False, source_revision=revision(body))

def table_context(body, table):
    """Preserve ALL short table-external prose, not a guessed condition lexicon.

    Multiple tables or long external prose may carry relations we cannot prove:
    keep the retained route. Do not select condition sentences by keywords.
    """
    if len(tables.parse_tables(body)) != 1: return None
    spans=[]
    for a,b in ((0,table['header_start']),(table['rows'][-1]['end'],len(body))):
        while a<b and body[a].isspace():a+=1
        while b>a and body[b-1].isspace():b-=1
        if a<b:spans.append((a,b))
    return spans if sum(b-a for a,b in spans)<=400 else None

def typed_ocr_rows(body):
    """Identify typed records from OCR alone; never corroborate against content.

    A supported record needs its next start (or end of saved text), a unique
    identifier/type, and a description. This proves a local record, not a full
    interface schema or the original image's recognition correctness.
    """
    lines = [(m.group().strip(), m.start(), m.end()) for m in re.finditer(r'[^\r\n]+',body)]
    starts = [i for i in range(len(lines)-1) if IDENT.fullmatch(lines[i][0]) and lines[i+1][0] in TYPES]
    if len(starts) < 2: return []
    result = []
    for j,i in enumerate(starts):
        end = starts[j+1] if j+1 < len(starts) else len(lines)
        following = lines[i+2:end]
        if following and STORAGE.fullmatch(following[0][0]): following = following[1:]
        if not following: continue
        name = lines[i][0]
        if sum(lines[k][0] == name for k in starts) != 1: continue
        result.append(dict(name=name, type=lines[i+1][0], description='\n'.join(v[0] for v in following),
                           start=lines[i][1], end=lines[end-1][2]))
    return result

def typed_request(question, label, notes, description=''):
    q=norm(strip_scopes(question,notes)).replace(norm(label),'')
    q=re.sub(r'接口(?:id[:：]?)?\d+','',q)
    requested_numbers=re.findall(r'(?<!\d)\d+(?!\d)',q)
    if any(not re.search(r'(?<!\d)'+re.escape(value)+r'(?!\d)',description) for value in requested_numbers):return False
    q=re.sub(r'(?:值)?\d+(?:和\d+)*','',q)
    q=re.sub(r'已经保存的ocr文本|保存的ocr|ocr文本|ocr|'
             r'返回类型|哪个字段|字段名|字段|分别表示什么|表示什么|含义|'
             r'是什么|什么|请问|只读|只看|根据|只按|分别|看|的|里|中|和|值', '', q)
    return not re.sub(r'[\W_]+','',q)

class FieldProjection:
    def __init__(self, notes, ocr):
        self.notes, self.ocr = notes, ocr
        self.tables = {i: tables.parse_tables(n['content']) for i,n in notes.items()}
        self.audit = {}

    def original(self, sid, field):
        return self.notes[sid]['content'] if field=='content' else self.ocr.get(sid, '')

    def prepare(self, question):
        scope = resolve_scope(question, self.notes)
        self.audit = dict(version=VERSION, active=False, reason='unproved_request_or_structure')
        if scope.unresolved and scope.constrained:
            return self.finish(question,scope,dict(kind='scope_unresolved',refs=[]))
        if scope.strict:
            return self.ocr_request(question, scope)
        result=self.table_request(question,scope)
        if result is None and scope.constrained:
            # Never delegate a forbidden source to an unrestricted legacy path.
            return self.finish(question,scope,dict(kind='scope_unresolved',refs=[]))
        return result

    def table_request(self,question,scope):
        selected = []; all_tables = []
        for sid, fields in scope.pairs.items():
            for field in fields:
                body = self.original(sid, field)
                for table in (self.tables[sid] if field=='content' else tables.parse_tables(body)):
                    all_tables.append((sid, field, table))
                    rows, approximate = tables.find_entities(question, table)
                    # Literal exact identities only; the old spelling-repair
                    # route remains responsible for approximate matches.
                    if approximate: continue
                    selected.extend((sid,field,table,row) for row in rows)
        if not selected:
            # Complete absence is scoped to the FULL SAVED table, never an OCR
            # fragment or an excerpt returned by retrieval.
            if scope.explicit and not scope.unresolved and len(all_tables)==1:
                sid,field,table = all_tables[0]; body=self.original(sid,field)
                unresolved=subject_candidates(question,table,self.notes)
                if unresolved:
                    context=table_context(body,table)
                    if context is None:return None
                    refs=[];records=[]
                    for row in unresolved.pop('rows'):
                        refs.append(evidence(self.notes[sid],field,body,[(table['header_start'],table['header_end']),
                            (row['start'],row['end'])]+context,'table_row'))
                        records.append(dict(entity=row['values'][table['key']],row_start=row['start'],
                            context=[body[a:b] for a,b in context],values={h:row['values'][table['headers'].index(h)]
                            for h in unresolved['requested']['fields'] if h in table['headers']}))
                    if unresolved['relation']=='cell_value':
                        refs=[evidence(self.notes[sid],field,body,
                            [(table['header_start'],table['rows'][-1]['end'])]+context,'saved_table')]
                    return self.finish(question,scope,dict(kind='subject_unconfirmed',source_id=sid,field=field,
                        records=records,refs=refs,**unresolved))
                subject=absent_subject(question,table,self.notes)
                if subject:
                    plan = request_fields(question,table,[subject],self.notes)
                    last=table['rows'][-1]['end']
                    # No malformed table tail or explicit omission marker may
                    # certify absence. The claim is only about saved rows.
                    complete = not body[last:].strip() and not re.search(r'\.\.\.|…|省略|截断',body)
                    if plan and complete:
                        payload=dict(kind='absent_entity', source_id=sid,field=field,
                            entity=subject,requested=plan, row_count=len(table['rows']),
                            refs=[evidence(self.notes[sid],field,body,[(table['header_start'],last)],'complete_saved_table')])
                        return self.finish(question,scope,payload)
            return None
        if len(selected)>4 or scope.unresolved: return None
        if len({tuple(t['headers']) for _,_,t,_ in selected}) != 1: return None
        entities=[r['values'][t['key']] for _,_,t,r in selected]
        if len(set(map(norm,entities))) < len(entities) and not scope.explicit: return None
        plan=request_fields(question,selected[0][2],entities,self.notes)
        if (not plan or (plan['compare'] and len(selected)<2)
                or (plan.get('projections') and len(set(entities))!=len(entities))):
            # The old model-selected-column renderer also loses endpoint/ask
            # identity. Do not re-enter it after rejecting this exact-row request.
            # A bounded raw record is partial evidence, not a complete answer.
            refs=[];identities=[]
            for sid,field,t,row in selected:
                body=self.original(sid,field);context=table_context(body,t)
                if context is None:return None
                refs.append(evidence(self.notes[sid],field,body,
                    [(t['header_start'],t['header_end']),(row['start'],row['end'])]+context,'table_row'))
                identities.append(dict(source_id=sid,field=field,row_start=row['start'],entity=row['values'][t['key']]))
            return self.finish(question,scope,dict(kind='request_unresolved_excerpt',refs=refs,identities=identities))
        records=[];refs=[]
        for sid,field,t,row in selected:
            body=self.original(sid,field)
            context=table_context(body,t)
            if context is None: return None
            refs.append(evidence(self.notes[sid],field,body,[(t['header_start'],t['header_end']),(row['start'],row['end'])]+context,'table_row'))
            records.append(dict(source_id=sid,field=field,entity=row['values'][t['key']],
                header=t['headers'],row_start=row['start'],
                context=[body[a:b] for a,b in context],
                values={h:row['values'][t['headers'].index(h)] if h in t['headers'] else None for h in projected_fields(plan,row['values'][t['key']])}))
        payload=dict(kind='table_projection',requested=plan,records=records,refs=refs)
        if plan.get('projections'):
            comparisons=bound_cells(plan,records,refs)
            if comparisons is None:return None
            payload['cell_comparisons']=comparisons
        return self.finish(question,scope,payload)

    def ocr_request(self, question, scope):
        # Mixed field/source expressions are retained as a constraint, but do
        # not claim a cross-source projection until its relation is proved.
        if scope.unresolved or not scope.pairs or any(f != {'ocr_text'} for f in scope.pairs.values()):
            return self.finish(question,scope,dict(kind='scope_unresolved',refs=[]))
        matches=[]
        for sid in scope.pairs:
            body=self.ocr.get(sid,'')
            for row in typed_ocr_rows(body):
                label = re.split(r'[，,：:；;\n]',row['description'])[0]
                if len(label)>=2 and norm(label) in norm(question):
                    matches.append((sid,row))
        if len(matches)==1:
            sid,row=matches[0];body=self.ocr[sid]
            label=re.split(r'[，,：:；;\n]',row['description'])[0]
            if not typed_request(question,label,self.notes,row['description']):
                return self.finish(question,scope,dict(kind='ocr_unmapped_excerpt',refs=[
                    evidence(self.notes[sid],'ocr_text',body,[(row['start'],row['end'])],'ocr')]))
            # The entire original record is displayed, including enum meanings,
            # qualifiers and exceptions. No schema role is invented from prose.
            return self.finish(question,scope,dict(kind='ocr_typed_record',source_id=sid,
                record=row,refs=[evidence(self.notes[sid],'ocr_text',body,[(row['start'],row['end'])],'ocr')]))
        # Damaged OCR table layout: preserve the contiguous original row, do not
        # infer which wrapped line belongs to which column using the body.
        snippets=[]
        q=norm(question)
        for sid in scope.pairs:
            body=self.ocr.get(sid,''); starts=list(re.finditer(r'(?m)^\d+([^\r\n]+)',body))
            for i,m in enumerate(starts):
                name=norm(m[1])
                if len(name)>=4 and name in q:
                    end=starts[i+1].start() if i+1<len(starts) else len(body)
                    snippets.append(evidence(self.notes[sid],'ocr_text',body,[(m.start(),end)],'ocr'))
        return self.finish(question,scope,dict(kind='ocr_unmapped_excerpt' if snippets else 'scope_unresolved',refs=snippets))

    def finish(self, question, scope, payload):
        # Independent consumer reconstructs table rows and the requested fields;
        # producing a payload cannot certify its own validity.
        from .projection_guard import validate_projection, render_projection
        errors=validate_projection(question,scope,payload,self.notes,self.ocr)
        if errors:
            self.audit.update(reason='independent_validation_failed',errors=errors)
            if not scope.constrained: return None
            payload=dict(kind='scope_unresolved',refs=[])
        response=render_projection(payload)
        self.audit=dict(version=VERSION,active=True,kind=payload['kind'],
                        scope={str(k):sorted(v) for k,v in scope.pairs.items()},
                        actions=scope.actions,excluded=scope.excluded,validation_errors=errors)
        response.update(citation_validation=dict(passed=not errors,errors=errors),
            trace=dict(field_projection=copy.deepcopy(self.audit), projection=copy.deepcopy(payload),
                       generation_calls=0,messages=[]))
        return dict(mode='single_free_text', sources=response['sources'],direct_record_response=response)

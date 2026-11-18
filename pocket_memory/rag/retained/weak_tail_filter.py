# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Conservative review of optional retrieval-channel tails, never a top-k cut.

FlashRank values only trigger inspection; they are not accuracy probabilities.
Original anchors, requested rows and uncertain dependencies remain unchanged.
Only complete verified units introduced as optional padding can be removed.
"""
import math
import re
import unicodedata

from .table_contract import cells, parse_tables, find_entities

VERSION = 'optional-channel-tail-v1'
STRONG_SCORE = .8
TAIL_SCORE = .01
TAIL_RATIO = .01

# Ambiguous selection/parallel operations need coverage beyond the best unit.
# A why/how pair about one subject is intentionally not rejected by punctuation.
MULTI_OR_FILTER = re.compile(
    r'分别|各自|以及|同时|另外|除此|另一|二者|两(?:份|个|种|项|者)|多(?:份|个|种|项)|'
    r'对比|比较|相比|相较|异同|差异|区别|相同|一致|哪些|哪几|所有|全部|每个|各项|'
    r'是否|能否|满足|筛选|过滤|排除|除外|条件|限制|例外|需要|必须|不得|不能|未提供|不存在')
DEPENDENCY = re.compile(
    r'如果|假如|除非|否则|若|仅|只(?:在|有|能|限|可|对|适用)|前提|条件|例外|除外|'
    r'不得|不能|禁止|必须|不(?:可|应|得|能|适用|包括|改写|超过|低于)|未(?:经|获|完成|确认)|'
    r'但|然而|除(?:了|此)|另(?:外|有|见)|上述|下述|如下|以下|以上|前述|后述|该(?:项|条|条件|规定)|'
    r'参见|见表|附注|脚注|注[:：]|同上|取决于|以.+为准|适用于|不适用')
NUMERIC = re.compile(r'\d|[%％￥¥$≤≥<>]|[一二三四五六七八九十百千万]+(?:元|天|日|月|年|倍|成)')
CROSS_ROW = re.compile(r'同上|同前|上述|下述|前述|后述|其他|其余|全部|所有|各项|通用|共同|参见|见表|附注|脚注|注[:：]')


def _compound_or_condition(question):
    if MULTI_OR_FILTER.search(question) or re.search(r'不|未|无|没|避免',question):return True
    asks=r'为什么|为何|如何|怎么|怎样|什么|多少|何时|哪(?:个|种|项|年)|是谁'
    clauses=[p.strip() for p in re.split(r'[，,；;。?？]',question) if re.search(asks,p)]
    # A subjectless how-follow-up can share the preceding why-question's
    # evidence. A second explicit subject/question is kept as a coverage task.
    followup=r'(?:(?:那|那么|又|并|还|应该|应|可以|可|该|要|请)\s*)*(?:如何|怎么|怎样).+'
    return len(clauses)>1 and any(not re.fullmatch(followup,p) for p in clauses[1:])


def _norm(text):
    return re.sub(r'\s+','',unicodedata.normalize('NFKC',str(text))).casefold()


def _score(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):return None
    return float(value)


def _raws(source,notes):
    if source.get('source_type')!='note' or source.get('source_field')!='content':return None
    if source.get('source_verification')!='saved_original_spans' or source.get('truncated'):return None
    note=(notes or {}).get(source.get('source_id'));raw=source.get('raw_evidence')
    if not isinstance(note,dict) or not isinstance(note.get('content'),str) or not isinstance(raw,list) or not raw:return None
    body=note['content']
    for span in raw:
        if (not isinstance(span,dict) or span.get('field')!='content'
            or type(span.get('char_start'))!=int or type(span.get('char_end_exclusive'))!=int
            or not 0<=span['char_start']<span['char_end_exclusive']<=len(body)
            or body[span['char_start']:span['char_end_exclusive']]!=span.get('quote')):return None
    return body,raw


def _literal_matches(question,values):
    q=_norm(question)
    return any(len(value:=_norm(raw))>=2 and value in q for raw in values if raw is not None)


def _organization_only(text,title,headers):
    """Consume every outside clause as metadata or a column-selection directive.

    This grammar supplies no facts and chooses no rows. Unknown trailing text,
    approval rules, exceptions, quantities and cross-row references all fail.
    """
    lines=[]
    for line in text.splitlines():
        heading=re.fullmatch(r'\s*#{1,6}\s+(.+?)\s*',line)
        self_link=re.fullmatch(r'\s*\[\[([^\]\n]+)\]\]\s*',line)
        if (heading and _norm(heading[1])==_norm(title)) or (self_link and _norm(self_link[1])==_norm(title)):continue
        lines.append(line)
    text='\n'.join(lines).strip()
    if not text:return True
    if NUMERIC.search(text) or re.search(r'未|无|没|不同意|不同步|审批|批准|授权|权限|例外|除外|除非|否则|如果|仅|只|同上|上述|参见',text):return False
    clauses=[p.strip() for p in re.split(r'[。！？；;\n]',text) if p.strip()]
    for clause in clauses:
        introduction=re.fullmatch(r'(?:以下|下面|下文|本页|这里)(?:是|为|列出|列示|展示|给出)(.+)',clause)
        if introduction:
            label=re.sub(r'[《》〈〉【】\[\]“”"‘’]','',introduction[1])
            if _norm(label)==_norm(title) and '不' not in clause:continue
        selection=re.fullmatch(
            r'(?:请|建议|需要|需|应当|应|须|可以)?(?:按照|按|依据|根据)'
            r'([\u4e00-\u9fffA-Za-z]{1,20}?)(?:进行)?(?:选择|选取|挑选)'
            r'(?:(?:相应|合适|适用)?(?:条目|项目|记录|场景|对象))?',clause)
        if selection:
            dimension=re.sub(r'^(?:(?:实际|不同|具体|相应|所需|合适))+','',selection[1])
            # “不同” is a modifier only inside a fully parsed selection of an
            # existing column dimension. Never remove arbitrary negation text.
            resolved=clause[:selection.start(1)]+dimension+clause[selection.end(1):]
            if ('不' not in resolved and len(_norm(dimension))>=2
                and any(_norm(dimension) in _norm(header) for header in headers)):continue
        return False
    return bool(clauses)


def _table_guard(question,source,body,raw):
    """Require a complete, unambiguous table with no unbound outside prose."""
    if len(raw)!=2:return 'table_shape_unconfirmed'
    tables=parse_tables(body)
    matches=[(table,row) for table in tables for row in table['rows']
        if table['header_start']==raw[0]['char_start'] and row['start']==raw[1]['char_start']
        and row['raw'].strip()==raw[1]['quote'].strip()]
    if len(matches)!=1:return 'table_mapping_unconfirmed'
    table,row=matches[0]
    # A very small schema is easy for a paragraph reranker to underestimate.
    if len(table['headers'])<3:return 'short_table_schema'
    entities,near=find_entities(question,table)
    if near=='ambiguous' or any(r['start']==row['start'] for r in entities):return 'explicit_table_entity'
    if _literal_matches(question,table['headers']):return 'explicit_table_field'
    if _literal_matches(question,row['values']):return 'explicit_table_value'
    # The complete row owns its local qualifications. Dropping that unasked
    # entity drops the whole row, never a qualification from a retained fact.
    if CROSS_ROW.search(source.get('evidence','')):return 'cross_row_dependency'
    if len(tables)!=1:return 'multiple_table_dependency_unconfirmed'
    outside=body[:table['header_start']]+body[table['rows'][-1]['end']:]
    if not _organization_only(outside,source.get('title',''),table['headers']):return 'table_external_context'
    return None


def _prose_guard(source,body,raw,sources,roles):
    if len(raw)!=1:return 'prose_structure_unconfirmed'
    start,end=raw[0]['char_start'],raw[0]['char_end_exclusive']
    if source.get('paragraph_range') not in ([start,end],(start,end)):return 'prose_paragraph_unconfirmed'
    same=[s for s in sources if s.get('source_key')==source.get('source_key') and s.get('evidence_id')!=source.get('evidence_id')]
    anchors=[s for s in same if 'primary_prose' in roles.get(s.get('evidence_id'),())]
    if not anchors:return 'prose_primary_missing'
    if any(s.get('kind')!='content' for s in same):return 'mixed_source_dependencies'
    for anchor in anchors:
        spans=anchor.get('raw_evidence') or []
        if not spans:return 'prose_primary_spans_missing'
        for span in spans:
            if start<span['char_end_exclusive'] and span['char_start']<end:return 'prose_anchor_overlap'
        if DEPENDENCY.search(anchor.get('evidence','')):return 'primary_condition_or_reference'
    text=source.get('evidence','')
    if DEPENDENCY.search(text):return 'prose_condition_or_reference'
    if NUMERIC.search(text):return 'prose_numeric_or_temporal_dependency'
    if not re.search(r'[。！？.!?][\s”’\"\']*$',text):return 'prose_sentence_incomplete'
    return None


def filter_weak_tails(question,sources,*,scores,roles,explicit_sources=(),notes=None):
    """Return the original kept objects plus a text-free decision audit.

    roles is evidence_id -> iterable of channel roles. Unknown roles, scores or
    provenance always preserve input. No model call, rewrite or source rebinding.
    """
    normalized_roles={key:set(value) for key,value in roles.items()}
    present=[_score(scores.get(s.get('evidence_id'))) for s in sources]
    maximum=max((value for value in present if value is not None),default=0)
    operation_guard=_compound_or_condition(question);explicit=set(explicit_sources)
    kept=[];audit=[];decisions={}
    for source in sources:
        eid=source.get('evidence_id');value=_score(scores.get(eid));channel=normalized_roles.get(eid,set())
        reason=None
        if eid in decisions:
            if decisions[eid]=='keep':kept.append(source)
            continue
        if 'old_row' in channel:reason='preserved_prior_row'
        elif 'primary_prose' in channel:reason='primary_full_query_anchor'
        elif source.get('source_key') in explicit:reason='explicit_source_scope'
        elif _literal_matches(question,[source.get('title',''),source.get('entity','')]):reason='explicit_source_or_entity'
        elif not channel.intersection({'independent_row','alternate_prose'}):reason='not_optional_channel_tail'
        elif operation_guard:reason='multi_subject_or_selection_request'
        elif value is None:reason='reranker_score_missing'
        elif maximum<STRONG_SCORE:reason='no_strong_primary_score'
        elif value>TAIL_SCORE or value>maximum*TAIL_RATIO:reason='not_extreme_tail'
        else:
            verified=_raws(source,notes)
            if verified is None:reason='source_dependency_unverified'
            elif source.get('kind')=='table_row' and 'independent_row' in channel:
                reason=_table_guard(question,source,*verified)
            elif source.get('kind')=='content' and 'alternate_prose' in channel:
                reason=_prose_guard(source,*verified,sources,normalized_roles)
            else:reason='unsupported_optional_kind'
        action='keep' if reason else 'drop';decisions[eid]=action
        if action=='keep':kept.append(source)
        audit.append(dict(evidence_id=eid,source_key=source.get('source_key'),kind=source.get('kind'),
            roles=sorted(channel),score=value,highest_score=maximum,action=action,
            reason=reason or 'extreme_optional_tail_without_structural_dependency'))
    return kept,audit

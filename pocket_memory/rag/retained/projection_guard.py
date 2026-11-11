# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Independent consumer checks source identity, request coverage and cell values.

This validates bounded structural claims, not arbitrary natural-language entailment.
"""
import copy
from . import table_contract as tables

def validate_projection(question, scope, payload, notes, ocr):
    from .field_projection import (revision, request_fields, resolve_scope, typed_ocr_rows,
                                  typed_request, table_context, projected_fields, bound_cells, absent_subject, subject_candidates)
    errors=[]; actual=resolve_scope(question,notes)
    if actual != scope: errors.append('scope_changed')
    refs=payload['refs']
    for ref in refs:
        sid=ref['source_id']; field=ref['source_field']; n=notes.get(sid)
        if not n or not scope.permits(sid,field): errors.append('source_field_outside_scope'); continue
        if ref.get('title')!=n['title'] or ref.get('source_key')!=f'note:{sid}':errors.append('source_identity_mismatch')
        if ref.get('parent_note_id') != n.get('parent_note_id') or ref.get('tab_name') != n.get('tab_name'):
            errors.append('tab_identity_mismatch')
        body=n['content'] if field=='content' else ocr.get(sid,'')
        if ref.get('location')!=('已保存OCR' if field=='ocr_text' else '保存正文表格'):
            errors.append('field_location_mismatch')
        if ref.get('source_revision') != revision(body): errors.append('stale_source')
        if not ref['raw_evidence']:errors.append('empty_original_evidence')
        if ref.get('evidence')!='\n'.join(r['quote'] for r in ref['raw_evidence']):
            errors.append('display_not_original_serialization')
        for r in ref['raw_evidence']:
            a,b=r['char_start'],r['char_end_exclusive']
            if (r['field']!=field or not 0<=a<b<=len(body) or body[a:b]!=r['quote']
                    or r.get('revision')!=revision(body)): errors.append('original_span_mismatch')
    kind=payload['kind']
    if kind=='table_projection':
        records=payload['records']
        if len(records)!=len(refs): return errors+['record_reference_count']
        parsed=[]
        for row,ref in zip(records,refs):
            sid=row['source_id'];field=row['field'];body=notes[sid]['content'] if field=='content' else ocr.get(sid,'')
            matches=[(t,r) for t in tables.parse_tables(body) for r in t['rows']
                     if r['start']==row['row_start'] and r['values'][t['key']]==row['entity']]
            if len(matches)!=1: errors.append('row_identity_not_unique');continue
            t,r=matches[0];parsed.append(t)
            if row['header']!=t['headers']: errors.append('headers_changed')
            if sid!=ref['source_id'] or field!=ref['source_field']: errors.append('record_reference_identity')
            context=table_context(body,t)
            if context is None:errors.append('unbounded_external_context');context=[]
            if row.get('context') != [body[a:b] for a,b in context]:errors.append('external_conditions_missing')
            expected_spans=[(t['header_start'],t['header_end']),(r['start'],r['end'])]+context
            if [(s['char_start'],s['char_end_exclusive']) for s in ref['raw_evidence']]!=expected_spans:
                errors.append('header_and_complete_row_required')
            for col,value in row['values'].items():
                if col not in t['headers']:
                    if value is not None or col not in payload['requested']['missing_columns']:errors.append('unproved_missing_column')
                elif value != r['values'][t['headers'].index(col)]: errors.append('cell_mismatch')
        if parsed:
            expected=request_fields(question,parsed[0],[r['entity'] for r in records],notes)
            if expected != payload['requested']: errors.append('requested_fields_mismatch')
            if expected and any(list(r['values'])!=projected_fields(expected,r['entity']) for r in records):errors.append('field_omitted_or_added')
            for operation in payload['requested'].get('operations',[]):
                if operation['kind']=='cross_cell_compare':
                    if len(operation['operands'])!=2 or len({o['entity'] for o in operation['operands']})!=2:
                        errors.append('unbound_comparison')
                    continue
                if (operation['kind']!='same_field_across_entities' or len(set(operation['entities']))<2
                    or set(operation['entities'])!={r['entity'] for r in records}
                    or operation['field'] not in payload['requested']['fields']):errors.append('unbound_comparison')
            if expected and expected.get('projections'):
                verified=bound_cells(expected,records,refs)
                if verified is None or verified!=payload.get('cell_comparisons'):errors.append('cell_operand_identity_mismatch')
                # Also require actual question mentions and quoted cell content;
                # reference existence alone cannot certify a relationship.
                for comparison in payload.get('cell_comparisons',[]):
                    for operand in comparison['operands']:
                        a,b=operand['entity_span'];x,y=operand['field_span']
                        if tables.norm(question[a:b])!=tables.norm(operand['entity']):errors.append('operand_not_named')
                        if tables.norm(question[x:y]) not in tables.norm(operand['column']):errors.append('operand_field_not_named')
                        i=operand['reference_index']-1
                        if not 0<=i<len(refs):errors.append('operand_reference_missing');continue
                        if operand['value'] and operand['value'] not in refs[i]['raw_evidence'][1]['quote']:
                            errors.append('displayed_value_not_in_row')
    elif kind=='subject_unconfirmed':
        sid=payload['source_id'];field=payload['field'];body=notes[sid]['content'] if field=='content' else ocr.get(sid,'')
        ts=tables.parse_tables(body)
        if not scope.explicit or scope.unresolved or len(ts)!=1:return errors+['unconfirmed_scope_unproved']
        t=ts[0];expected=subject_candidates(question,t,notes);context=table_context(body,t)
        if expected is None or context is None:return errors+['unconfirmed_subject_not_reconstructed']
        for key in ['subject','relation','columns','requested']:
            if payload[key]!=expected[key]:errors.append('unconfirmed_request_changed')
        if payload['relation']=='name_fragment':
            rows=expected['rows']
            if len(rows)!=len(refs) or len(rows)!=len(payload['records']):return errors+['candidate_count_changed']
            for row,item,ref in zip(rows,payload['records'],refs):
                values={h:row['values'][t['headers'].index(h)] for h in expected['requested']['fields']}
                if item!=dict(entity=row['values'][t['key']],row_start=row['start'],
                              context=[body[a:b] for a,b in context],values=values):errors.append('candidate_record_changed')
                if payload['subject'] not in tables.norm(row['values'][t['key']]):errors.append('candidate_not_literal_fragment')
                if ref['source_id']!=sid or ref['source_field']!=field:errors.append('candidate_source_changed')
                if [(r['char_start'],r['char_end_exclusive']) for r in ref['raw_evidence']]!=[
                    (t['header_start'],t['header_end']),(row['start'],row['end'])]+context:errors.append('candidate_row_context_missing')
        elif payload['relation']=='cell_value':
            if len(refs)!=1 or payload['records']:return errors+['value_predicate_not_row_projection']
            if refs[0]['source_id']!=sid or refs[0]['source_field']!=field:errors.append('candidate_source_changed')
            if [(r['char_start'],r['char_end_exclusive']) for r in refs[0]['raw_evidence']]!=[
                (t['header_start'],t['rows'][-1]['end'])]+context:errors.append('predicate_original_table_missing')
        else:errors.append('unconfirmed_relation_unknown')
    elif kind=='absent_entity':
        sid=payload['source_id'];field=payload['field'];body=notes[sid]['content'] if field=='content' else ocr.get(sid,'')
        ts=tables.parse_tables(body)
        if not scope.explicit or scope.unresolved or len(ts)!=1 or len(refs)!=1:errors.append('absence_scope_incomplete')
        else:
            t=ts[0];last=t['rows'][-1]['end']
            if body[last:].strip() or len(t['rows'])!=payload['row_count']: errors.append('absence_extent_incomplete')
            if any(tables.norm(r['values'][t['key']])==tables.norm(payload['entity']) for r in t['rows']):errors.append('entity_present')
            if absent_subject(question,t,notes)!=payload['entity']:errors.append('absence_subject_unproved')
            name=tables.norm(payload['entity'])
            for row in t['rows']:
                for value in row['values']:
                    value=tables.norm(value)
                    if value and (name in value or value in name):errors.append('absence_subject_is_cell_value')
            spans=refs[0]['raw_evidence']
            if len(spans)!=1 or (spans[0]['char_start'],spans[0]['char_end_exclusive'])!=(t['header_start'],last):
                errors.append('absence_requires_all_saved_rows')
            if request_fields(question,t,[payload['entity']],notes)!=payload['requested']:errors.append('absence_request_mismatch')
    elif kind=='ocr_typed_record':
        sid=payload['source_id'];row=payload['record']
        if row not in typed_ocr_rows(ocr.get(sid,'')): errors.append('ocr_record_structure_not_proved')
        import re
        label=re.split(r'[，,：:；;\n]',row['description'])[0]
        if not typed_request(question,label,notes,row['description']):errors.append('unparsed_ocr_request')
        if len(refs)!=1 or refs[0]['source_id']!=sid or refs[0]['source_field']!='ocr_text':errors.append('ocr_reference_identity')
        elif [(r['char_start'],r['char_end_exclusive']) for r in refs[0]['raw_evidence']]!=[(row['start'],row['end'])]:errors.append('ocr_record_span')
    elif kind=='request_unresolved_excerpt':
        identities=payload['identities']
        if len(identities)!=len(refs):errors.append('record_reference_count')
        for item,ref in zip(identities,refs):
            sid=item['source_id'];field=item['field'];n=notes.get(sid,{})
            body=n.get('content','') if field=='content' else ocr.get(sid,'')
            found=[(t,r) for t in tables.parse_tables(body) for r in t['rows']
                   if r['start']==item['row_start'] and r['values'][t['key']]==item['entity']]
            if len(found)!=1:errors.append('row_identity_not_unique');continue
            t,r=found[0];context=table_context(body,t)
            if context is None:errors.append('unbounded_external_context');continue
            if sid!=ref['source_id'] or field!=ref['source_field']:errors.append('record_reference_identity')
            if [(v['char_start'],v['char_end_exclusive']) for v in ref['raw_evidence']]!=[
                (t['header_start'],t['header_end']),(r['start'],r['end'])]+context:errors.append('header_and_complete_row_required')
    elif kind not in ('scope_unresolved','ocr_unmapped_excerpt'): errors.append('unknown_projection_kind')
    return errors

def render_projection(payload):
    refs=copy.deepcopy(payload['refs']); lines=[];kind=payload['kind']
    for i,ref in enumerate(refs,1):
        ref['index']=i
        # The validated span quotes are the single display/source serialization.
        ref['evidence']='\n'.join(r['quote'] for r in ref['raw_evidence'])
        ref['location']='已保存OCR' if ref['source_field']=='ocr_text' else '保存正文表格'
    if kind=='table_projection':
        records=payload['records']
        duplicate=len({r['entity'] for r in records})<len(records)
        if duplicate:lines.append('所选资料有同名对象；分别保留各页签的记录，不擅自选择版本。')
        shown=set()
        for i,row in enumerate(records,1):
            for text in row['context']:
                key=(row['source_id'],row['field'],text)
                if key not in shown:
                    lines.append('表外原文说明（以下字段需连同此说明阅读）：'+text+f' [{i}]');shown.add(key)
        for i,row in enumerate(records,1):
            title=(refs[i-1]['title']+' / ' if duplicate else '')+row['entity']
            lines.append('**'+title+'**')
            for col,value in row['values'].items():
                display=('（这张表未提供该名称的列；不代表其他资料没有此信息）' if value is None
                         else value if value else '（该单元格为空，不能判定为没有此字段）')
                lines.append(f'- {col}：{display} [{i}]')
        for col in payload['requested']['compare']:
            values=[r['values'][col] for r in records]
            result='相同' if all(v and v.strip() for v in values) and len(set(values))==1 else '不完全相同'
            if not all(v and v.strip() for v in values):result='存在空单元格或缺失列，无法完整比较'
            lines.append(f'{col}比较：{result}。'+''.join(f'[{i}]' for i in range(1,len(refs)+1)))
        for comparison in payload.get('cell_comparisons',[]):
            left,right=comparison['operands'];values=[left['value'],right['value']]
            result=('存在空单元格，无法确认' if not all(v and v.strip() for v in values)
                    else '相同' if values[0]==values[1] else '不同')
            lines.append(f'{left["entity"]}的{left["column"]} ↔ {right["entity"]}的{right["column"]}，'
                         f'原文文本比较：{result}。[{left["reference_index"]}][{right["reference_index"]}]')
            lines.append('仅比较上述单元格保存的文本；不判断业务含义等价，也不认定表外条件已经成立。')
        for item in payload['requested'].get('unconfirmed',[]):
            lines.append(f'- {item["text"]}：未确认对应字段；本次表头没有这一同名列，未用相近列替代。'
                         '这不表示其他原文中不存在相关信息。'+''.join(f'[{i}]' for i in range(1,len(refs)+1)))
        status='answered'
        if payload['requested'].get('unconfirmed'):status='partially_answered'
    elif kind=='subject_unconfirmed':
        if payload['relation']=='cell_value':
            labels='、'.join(payload['columns'])
            lines=[f'“{payload["subject"]}”出现在此表的“{labels}”列中，是字段取值，不能据此断言缺少同名对象。'
                   '本入口尚未完成按该条件筛选；请指定完整对象名称，或在所附原表中核对筛选结果。[1]']
        else:
            lines=[f'尚未确认“{payload["subject"]}”对应哪个完整对象。以下是名称片段匹配的候选记录，不代表已确认对应关系：']
            for i,row in enumerate(payload['records'],1):
                lines.append('**'+row['entity']+'**')
                for text in row['context']:lines.append('表外原文说明：'+text+f' [{i}]')
                for col,value in row['values'].items():
                    lines.append(f'- {col}：'+(value or '（该单元格为空）')+f' [{i}]')
            lines.append('请确认是否指上述完整名称；未将缩写直接认定为该对象，也未断言原表没有相关记录。')
        status='subject_unconfirmed'
    elif kind=='absent_entity':
        refs[0]['coverage_complete']=True
        refs[0]['coverage_notice']='完整覆盖当前保存的这张表；不代表原始图片或外部资料。'
        fields='、'.join(payload['requested']['fields'])
        lines.append(f'在《{refs[0]["title"]}》当前保存表格的全部 {payload["row_count"]} 条记录中，未找到“{payload["entity"]}”；不能据此给出其{fields}。[1]')
        status='answered'
    elif kind=='ocr_typed_record':
        r=payload['record']
        lines=[f'按《{refs[0]["title"]}》已保存的 OCR 记录：',f'- 字段：{r["name"]} [1]',
               f'- 返回类型：{r["type"]} [1]',f'- 原文说明（保留枚举与限定）：{r["description"]} [1]']
        status='answered'
    elif kind=='ocr_unmapped_excerpt':
        lines=['已定位以下 OCR 原文，但尚不能据此完整回答所有问项或可靠确认每个字段的对应值；以下仅展示原文，未借用正文补全。']
        for i,r in enumerate(refs,1):lines.append(f'《{r["title"]}》：\n'+r['evidence']+f' [{i}]')
        status='scope_limited_excerpt'
    elif kind=='request_unresolved_excerpt':
        lines=['已定位对象的原文记录，但尚不能完整确认本次问题的字段或比较关系。下面仅展示原文，未用相近字段替代所问字段，也未代作比较；这不是完整答案。']
        for i,r in enumerate(refs,1):lines.append(f'《{r["title"]}》原文：\n'+r['evidence']+f' [{i}]')
        status='request_limited_excerpt'
    else:
        lines=['暂未能在指定资料及正文/OCR范围内确认完整的对象、字段或列对应关系；请明确资料和字段。未使用范围外资料补答，也不据此断言原资料不存在该信息。']
        status='scope_unresolved'
    return dict(answer='\n\n'.join(lines),sources=refs,provided_sources=copy.deepcopy(refs),answer_status=status)

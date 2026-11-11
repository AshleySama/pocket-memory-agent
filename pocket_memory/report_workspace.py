# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Local report workspace. Snapshot identities and chart math never come from the LLM.

Independent of ask/retrieval: one existing 4B report-claims call, then source-backed
rendering. Quote membership is NOT a semantic truth certificate.
"""
from __future__ import annotations
import copy
import hashlib
import html
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from collections import Counter
from .report_sections import readable, focus_view, quote_span

TEMPLATES = {
    'project': ('项目复盘／工作汇报', '围绕已完成工作、产品取舍、风险与下一步行动，区分笔记记录与建议'),
    'research': ('专题研究／知识整理', '归纳核心认识、观点依据、资料分歧和未知问题，不推断因果'),
    'compare': ('方案对比／辅助决策', '按相同维度比较资料中的方案、适用条件与代价，缺失信息不可补齐'),
    'proposal': ('方案介绍／立项说明', '说明资料记录的目标、当前实现与未实现的能力、拟议升级方向；不把历史笔记或计划当作现状，不虚构预算和审批'),
    'meeting': ('会议纪要／行动跟进', '依据记录区分已决定事项、讨论意见、责任人和待办；没有会议实录时明确为记录整理，不虚构召开时间、参会人或决议'),
    'learning': ('学习总结／培训材料', '整理概念、区别和操作要点；保留字段原名、否定、例外和后续修订，不擅自合并不同资料的版本'),
}
HEADINGS = {
    'project': ('复盘结论', '工作记录与产品取舍', '风险与责任行动'),
    'research': ('核心认识', '观点与证据矩阵', '资料中的限制与待办'),
    'compare': ('对比要点', '比较依据与条件', '决策限制与后续核对'),
    'proposal': ('方案摘要', '方案组成与落地条件', '实施风险与责任行动'),
    'meeting': ('会议要点', '记录中的意见与决定', '行动跟进'),
    'learning': ('学习提要', '知识要点', '实践注意事项'),
}

def digest(text): return hashlib.sha256(text.encode('utf-8')).hexdigest()
def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')
def esc(value): return html.escape(str(value), quote=True)

def paragraphs(body):
    for match in re.finditer(r'[^\n]+(?:\n(?!\s*\n)[^\n]+)*', body):
        text = match.group().strip()
        if text:
            start = match.start() + len(match.group()) - len(match.group().lstrip())
            yield start, start + len(text), text

def markdown_tables(body):
    """Only rectangular, explicit Markdown tables. No OCR column guessing."""
    lines = list(re.finditer(r'[^\n]+', body))
    result = []
    def cells(line): return [s.strip() for s in line.strip().strip('|').split('|')]
    i = 0
    while i + 1 < len(lines):
        head, sep = lines[i].group().strip(), lines[i+1].group().strip()
        headers, separators = cells(head), cells(sep)
        if not head.startswith('|') or len(headers) < 2 or len(headers) != len(separators) or not all(re.fullmatch(r':?-{3,}:?', v) for v in separators):
            i += 1; continue
        rows = []; j = i + 2
        while j < len(lines) and lines[j].group().strip().startswith('|'):
            row = cells(lines[j].group())
            if len(row) != len(headers): break
            rows.append(row); j += 1
        malformed = j < len(lines) and lines[j].group().strip().startswith('|')
        if rows and not malformed and len(set(headers)) == len(headers):
            result.append({'headers': headers, 'rows': rows, 'start': lines[i].start(), 'end': lines[j-1].end()})
        i = max(j, i+1)
    return result

def make_ref(unit, start, end):
    quote = unit['body'][start:end]
    if not quote or start < 0 or end > len(unit['body']): raise ValueError('无效原文范围')
    return {k: unit[k] for k in ('unit_id','kind','source_id','note_id','title','tab','field','revision')} | {'start': start, 'end': end, 'quote': quote}

def valid_ref(ref, units):
    u = next((x for x in units if x['unit_id'] == ref.get('unit_id')), None)
    if not u: return False
    try:
        return (all(ref[k] == u[k] for k in ('kind','source_id','note_id','title','tab','field','revision'))
                and isinstance(ref['start'], int) and isinstance(ref['end'], int)
                and 0 <= ref['start'] < ref['end'] <= len(u['body'])
                and ref['quote'] == u['body'][ref['start']:ref['end']])
    except (KeyError, TypeError): return False

class ReportWorkspace:
    def __init__(self, store, engine, operation_lock):
        self.store, self.engine, self.operation_lock = store, engine, operation_lock
        self.lock = threading.RLock()
        self.jobs = {}
        self.worker = None

    @property
    def directory(self):
        p = self.store.data_dir / 'reports-v2'; p.mkdir(exist_ok=True); return p

    def path(self, report_id):
        if not isinstance(report_id, str) or not re.fullmatch('[a-f0-9]{32}', report_id): raise ValueError('无效报告编号')
        return self.directory / (report_id + '.json')

    def _trash_path(self, undo_token):
        if not isinstance(undo_token, str) or not re.fullmatch('[a-f0-9]{32}', undo_token):
            raise ValueError('无效的撤销凭据')
        return self.directory / '.trash' / (undo_token + '.json')

    def list_reports(self):
        with self.lock:
            records = [json.loads(p.read_text('utf-8')) for p in self.directory.glob('*.json')]
        return sorted([{k: r[k] for k in ('id','title','template','updated','version')} for r in records], key=lambda x: x['updated'], reverse=True)

    def read(self, report_id):
        with self.lock:
            p = self.path(report_id)
            if not p.exists(): raise KeyError(report_id)
            return json.loads(p.read_text('utf-8'))

    def write(self, report):
        target = self.path(report['id']); tmp = target.with_suffix('.tmp')
        with tmp.open('w', encoding='utf-8') as out:
            json.dump(report, out, ensure_ascii=False, indent=2); out.flush(); os.fsync(out.fileno())
        os.replace(tmp, target)

    def save_edit(self, report_id, body):
        with self.lock:
            r = self.read(report_id)
            if body.get('version') != r['version']: raise ValueError('报告已更新，请重新打开后再编辑')
            title = str(body.get('title',r['title'])).strip()
            notes = body.get('notes',r.get('notes',{}))
            if not 1 <= len(title) <= 100 or not isinstance(notes,dict): raise ValueError('标题或章节说明无效')
            if any(k not in ('summary','records','actions','chart','gaps','outcomes') or not isinstance(v,str) or len(v)>4000 for k,v in notes.items()): raise ValueError('章节说明超出范围')
            r.update(title=title, notes=notes, reviewed=False, version=r['version']+1, updated=now(), edited=True)
            self.write(r); return r

    def delete(self, report_id, expected_version):
        """Move only the saved report to local trash; its source data is untouched."""
        with self.lock:
            target = self.path(report_id)
            if type(expected_version) is not int or expected_version < 1:
                raise ValueError('请提供要删除报告的有效版本')
            report = self.read(report_id)
            if expected_version != report['version']:
                raise ValueError('报告已更新，请重新打开后再删除')
            undo_token = uuid.uuid4().hex
            archived = self._trash_path(undo_token)
            archived.parent.mkdir(exist_ok=True)
            while archived.exists() or archived.is_symlink():
                undo_token = uuid.uuid4().hex
                archived = self._trash_path(undo_token)
            # One atomic move preserves the complete saved snapshot and edits.
            os.replace(target, archived)
            return {'deleted': report_id, 'undo_token': undo_token}

    def restore(self, undo_token):
        with self.lock:
            archived = self._trash_path(undo_token)
            if not archived.exists(): raise KeyError(undo_token)
            report = json.loads(archived.read_text('utf-8'))
            target = self.path(report['id'])
            if target.exists() or target.is_symlink():
                raise ValueError('同编号报告已存在，无法覆盖；已删除的报告仍保留在本机')
            os.replace(archived, target)
            return report

    def snapshot(self, sources):
        if not isinstance(sources,list) or not 1 <= len(sources) <= 8: raise ValueError('请选择 1–8 项资料')
        units, selected, seen = [], [], set()
        for source in sources:
            kind, sid = source.get('kind'), int(source.get('id',0))
            if kind == 'note':
                note = self.store.get_note(sid)
                root_id = note.parent_note_id or note.id
                key = (kind,root_id)
                if key in seen: continue
                seen.add(key)
                root = self.store.get_note(root_id)
                selected.append({'kind':kind,'id':root_id,'title':root.title})
                tabs = self.store.get_note_tabs(root_id)
                for tab in tabs:
                    n = self.store.get_note(int(tab['id']))
                    for field in ('content','ocr_text'):
                        text = getattr(n,field,'') or ''
                        if not text.strip(): continue
                        units.append(dict(unit_id=f'n{n.id}:{field}', kind=kind, source_id=root_id, note_id=n.id,
                                          title=root.title, tab=n.tab_name or '页签1', field=field, revision=digest(text), body=text))
            elif kind == 'document':
                key = (kind,sid)
                if key in seen: continue
                seen.add(key); doc = self.store.get_document(sid)
                selected.append({'kind':kind,'id':sid,'title':doc.title})
                for chunk in self.store.list_document_chunks(sid):
                    d=chunk.as_dict(); text=d.get('text') or d.get('content') or ''
                    if text:
                        units.append(dict(unit_id=f'd{sid}:{chunk.id}',kind=kind,source_id=sid,note_id=None,title=doc.title,
                                          tab=d.get('location') or f'片段 {chunk.id}',field='document_text',revision=digest(text),body=text))
            else: raise ValueError('不支持的资料类型')
        if not units: raise ValueError('所选资料没有已保存的正文或 OCR')
        if sum(len(u['body']) for u in units)>400000: raise ValueError('本批资料超过 40 万字符，请缩小范围')
        return selected, units

    def create_job(self, body):
        title, template = str(body.get('title','')).strip(), body.get('template','project')
        if not 1 <= len(title) <= 100 or template not in TEMPLATES: raise ValueError('请输入标题并选择报告类型')
        with self.lock:
            if self.worker and self.worker.is_alive(): raise ValueError('已有报告正在生成，请等待完成')
            selected, units = self.snapshot(body.get('sources'))
            job_id = uuid.uuid4().hex
            job = {'id':job_id,'status':'queued','stage':'资料快照已保存，等待本地模型','started':time.time()}
            self.jobs[job_id] = job
            self.worker = threading.Thread(target=self._run,args=(job_id,title,template,selected,units),daemon=True)
            self.worker.start()
            return dict(job)

    def job(self, job_id):
        with self.lock:
            if job_id not in self.jobs: raise KeyError(job_id)
            return dict(self.jobs[job_id])

    def _stage(self, jid, text):
        with self.lock: self.jobs[jid]['stage']=text

    def _run(self, jid, title, template, selected, units):
        try:
            with self.operation_lock:
                self._stage(jid,'正在组织原文、表格和引用')
                report = self.build(title,template,selected,units,lambda s:self._stage(jid,s))
                with self.lock:
                    self.write(report)
                    self.jobs[jid].update(status='done',stage='报告已保存到本机',report_id=report['id'],elapsed=round(time.time()-self.jobs[jid]['started'],2))
        except Exception as exc:
            import logging
            logging.exception('Report workspace generation failed')
            with self.lock: self.jobs[jid].update(status='error',stage='生成失败',error=str(exc))

    def build(self,title,template,selected,units,stage=lambda _:None):
        refs, records, tables, actions = [], [], [], []
        def register(u,a,b):
            ref=make_ref(u,a,b)
            for old in refs:
                if old['unit_id']==ref['unit_id'] and old['start']==a and old['end']==b: return old['id']
            ref['id']=len(refs)+1; refs.append(ref); return ref['id']
        for u in units:
            if u['field']=='content':
                for t in markdown_tables(u['body']):
                    t['ref']=register(u,t['start'],t['end']); t['title']=u['title']; tables.append(t)
            for a,b,text in paragraphs(u['body']):
                if re.search(r'^\s*\|',text) or re.search(r'<(?:table|div|p)\b',text): continue
                rid=register(u,a,b)
                records.append({'text':text,'ref':rid,'title':u['title'],'field':u['field'],'unit_id':u['unit_id']})
                # Explicitly labelled prose only. Preserve the entire paragraph as evidence.
                if re.match(r'^风险[一二三四五六七八九十\d]+[：:]',text):
                    fields={}
                    for label in ('处理动作','负责人','完成时间'):
                        m=re.search(re.escape(label)+r'[：:]\s*(.*?)(?=[；;]\s*(?:处理动作|负责人|完成时间)[：:]|$)',text)
                        fields[label]=m.group(1).strip('；;。 ') if m else '资料未提供'
                    risk=re.split(r'处理动作[：:]',text,maxsplit=1)[0].strip('；;。 ')
                    actions.append({'risk':risk,'action':fields['处理动作'],'owner':fields['负责人'],'due':fields['完成时间'],'ref':rid})
        if not records and not tables: raise ValueError('资料格式尚不能安全组织，请选择可读正文或规范表格')
        # Bounded model input; report tables and complete original records remain independent.
        selected_records=[]; queues=[[r for r in records if refs[r['ref']-1]['source_id']==s['id'] and refs[r['ref']-1]['kind']==s['kind']] for s in selected]
        if template!='project':
            # Exclude bare headings/links; reserve both source beginning and ending
            # so a later revision is not lost to several introductory paragraphs.
            def prioritize(queue):
                eligible=[r for r in queue if len(readable(r['text']))>=30 and not re.fullmatch(r'\[\[.*?\]\]|https?://\S+',r['text'].strip())]
                return [eligible[0],eligible[-1],*eligible[1:-1]] if len(eligible)>1 else eligible
            queues=[prioritize(q) for q in queues]
        while any(queues) and len(selected_records)<8:
            for q in queues:
                if q and len(selected_records)<8: selected_records.append(q.pop(0))
        model_blocks=[]; used_chars=0
        for record in selected_records:
            text=record['text']
            if len(text)>460:
                sentences=re.findall(r'.+?[。！？](?:[”’])?|.+$',text)
                text=''
                for sentence in sentences:
                    if len(text)+len(sentence)>460: break
                    text+=sentence
            if not text or used_chars+len(text)>2200: continue
            used_chars+=len(text)
            model_blocks.append({'key':f'E{record["ref"]}','source_label':record['title'],'location':'所选资料原文','text':text,'ref':record['ref']})
        stage('4B 正在归纳所选资料；完整原文与图表同时保留')
        started=time.perf_counter()
        generated = self.engine.generate_report_claims(title,{'writing_focus':TEMPLATES[template][1]},model_blocks) if model_blocks else []
        model_seconds=round(time.perf_counter()-started,3)
        stage('正在核对原文引用、计算图表并保存')
        claims=[]; rejected=[]; recovered=[]
        for claim in generated:
            keys=claim.get('evidence',[]); key=keys[0] if len(keys)==1 else None
            block=next((x for x in model_blocks if x['key']==key),None)
            quote=str(claim.get('quote','')).strip()
            span=quote_span(block['text'],quote,str(claim.get('claim','')).strip()) if block else None
            if not span:
                rejected.append(claim); continue
            parent=refs[block['ref']-1]; u=next(x for x in units if x['unit_id']==parent['unit_id'])
            a,b,method=span
            original_quote=block['text'][a:b]
            original_start=parent['quote'].find(original_quote)
            if original_start<0:
                rejected.append(claim); continue
            if method!='exact': recovered.append({'raw':claim,'method':method,'span':[a,b],'evidence':key})
            a=parent['start']+original_start; b=a+len(original_quote)
            origin='原文摘录 · 引句格式恢复' if method=='claim_is_verbatim' else '4B归纳 · 待核对'
            claims.append({'topic':str(claim.get('topic','归纳'))[:40],'text':str(claim.get('claim',''))[:600], 'ref':register(u,a,b), 'origin':origin})
        charts=[]
        for table in tables:
            if '状态' not in table['headers']: continue
            index=table['headers'].index('状态'); counts=Counter(row[index] or '未填写' for row in table['rows'])
            if len(counts)>12: continue
            charts.append({'title':table['title']+' · 清单状态分布','series':[{'label':k,'value':v} for k,v in counts.items()],
                           'total':len(table['rows']),'ref':table['ref'],'method':'按当前保存表格逐行计数，每行计 1；空状态单独计为未填写。不代表软件完成率。'})
        report={'id':uuid.uuid4().hex,'version':1,'created':now(),'updated':now(),'title':title,'template':template,'template_label':TEMPLATES[template][0],
                'selected':selected,'units':units,'refs':refs,'claims':claims,'records':records,'tables':tables,'actions':actions,'charts':charts,
                'notes':{},'edited':False,'reviewed':False,'model':'Qwen3-4B-Instruct-2507 / 本地现有配置',
                'generation':{'calls':1 if model_blocks else 0,'seconds':model_seconds,'model_blocks':model_blocks,'raw_claims':generated,'rejected':rejected,'format_recovered':recovered},
                'limitations':['AI 归纳仅代表初稿；原文逐字可定位不等于结论已获人工确认。','原文记录、未来计划及本轮实际实现不是同一事实；报告不会自动验证原资料是否仍然有效。','数值图表仅计算原文明确列值；结构图只展示原文清单／顺序，不推断收益、概率或因果。',f'AI 仅阅读 {len(model_blocks)} 段摘选（{used_chars} 字符）；下方另保留全部可组织正文、原表与 OCR。完整快照不表示全部资料均已由模型总结。']}
        if any(not valid_ref(r,units) for r in refs): raise ValueError('引用身份核对失败，报告未保存')
        return report

    def source(self, report_id, ref_id):
        r=self.read(report_id); ref=next((x for x in r['refs'] if x['id']==ref_id),None)
        if not ref or not valid_ref(ref,r['units']): raise ValueError('引用不存在或快照无效')
        current=None
        if ref['kind']=='note':
            try: current=getattr(self.store.get_note(ref['note_id']),ref['field']) or ''
            except KeyError: pass
        else:
            for c in self.store.list_document_chunks(ref['source_id']):
                if f'd{ref["source_id"]}:{c.id}'==ref['unit_id']:
                    d=c.as_dict();current=d.get('text') or d.get('content') or ''
        return {'ref':ref,'changed':current is None or digest(current)!=ref['revision'],'current':current,
                'snapshot':next(x['body'] for x in r['units'] if x['unit_id']==ref['unit_id'])}


def citation(n): return f'<a class="cite" href="#source-{n}" data-ref="{n}" aria-label="查看引用 {n}">[{n}]</a>'
def table_html(headers, rows):
    return '<div class="table-scroll"><table><thead><tr>'+''.join('<th>'+esc(v)+'</th>' for v in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+v+'</td>' for v in row)+'</tr>' for row in rows)+'</tbody></table></div>'

def chart_svg(chart):
    max_value=max([x['value'] for x in chart['series']]+[1]); h=54*len(chart['series'])+30
    colors=['#218e8f','#729dad','#c8a768','#a08cb1']
    bars=[]
    for i,s in enumerate(chart['series']):
        y=20+i*54; w=s['value']/max_value*360
        bars.append(f'<text x="0" y="{y+20}" fill="#31545a" font-size="15">{esc(s["label"])}</text><rect x="105" y="{y}" width="360" height="29" rx="4" fill="#edf3f1"/><rect x="105" y="{y}" width="{w}" height="29" rx="4" fill="{colors[i%4]}"/><text x="480" y="{y+20}" fill="#20414a" font-size="16">{s["value"]}</text>')
    return f'<svg viewBox="0 0 520 {h}" role="img" aria-label="{esc(chart["title"])}">'+''.join(bars)+'</svg>'

def render_report(r, appendix=False):
    if any(not valid_ref(ref,r['units']) for ref in r['refs']):
        raise ValueError('报告快照引用不一致，请重新生成')
    heading=HEADINGS[r['template']]; notes=r.get('notes',{})
    def note(key): return '<p class="edited-copy">'+esc(notes[key])+'</p><small class="tag amber">人工编辑 · 待复核</small>' if notes.get(key) else ''
    section_number=0
    def section(key,title,body):
        nonlocal section_number
        section_number+=1
        return f'<section class="section" id="section-{key}"><div class="section-head"><span class="section-number" aria-hidden="true">{section_number:02}</span><h2>{esc(title)}</h2><button class="edit" data-edit="{key}">编辑说明</button></div>{note(key)}{body}</section>'
    # Presentation only: keep every saved claim and its reference in original order.
    # Provenance stays in the record; one report-level notice replaces repeated cards.
    has_ai=any(c.get('origin','').startswith('4B归纳') for c in r['claims']) and r['template']=='project'
    provenance='4B归纳 · 待核对' if has_ai else '原文整理 · 待核对'
    result=f'<header class="report-heading"><div class="report-title-row"><div><div class="eyebrow">{esc(r["template_label"])}</div><h1>{esc(r["title"])}</h1></div><span class="report-review">{provenance}</span></div><div class="report-meta"><span>{esc(r["created"][:10])}</span><span>本机保存 · v{r["version"]}</span><span>{len(r["selected"])} 项资料</span><span>{len(r["refs"])} 处引用</span></div></header>'
    topics=[str(c.get('topic','')).strip() for c in r['claims']]
    def topic_label(c):
        topic=str(c.get('topic','')).strip()
        if topic and topic not in ('主题','归纳','要点','事实摘要') and topics.count(topic)==1:
            return f'<strong class="summary-topic">{esc(topic)}：</strong>'
        return ''
    claims=''.join(f'<li><p>{topic_label(c)}{esc(c["text"])} {citation(c["ref"])}</p></li>' for c in r['claims'])
    if r['template']!='project' and r['claims']:
        # Quote identity cannot certify a paraphrase. Present the proven text,
        # keeping model rewrites only in the saved audit record, not as facts.
        claims=''.join(f'<li><p>{esc(readable(r["refs"][c["ref"]-1]["quote"]))} {citation(c["ref"])}</p></li>' for c in r['claims'])
    summary='<ol class="summary-list">'+claims+'</ol>' if claims else '<p class="section-desc">暂未形成可核对的提要，可在文末参考资料中查看来源。</p>'
    if r['template']!='project' and claims:
        summary='<p class="section-desc">以下为所选资料的原文提要，计划、条件和否定均按原文保留。</p>'+summary
    result+=section('summary',heading[0],summary)
    if r['template']=='project':
        enumerated=''
        for record in r['records']:
            if record['field']=='ocr_text': continue
            parts=list(re.finditer(r'(?:第一|第二|第三|第四|第五|第六)[，、]',record['text']))
            if len(parts)<3: continue
            intro=record['text'][:parts[0].start()].strip()
            enumerated+='<div class="panel record-panel"><div class="panel-head"><h3>'+esc(intro)+'</h3><span class="tag">原文列举</span></div><div class="recorded-grid">'
            for i,p in enumerate(parts):
                text=record['text'][p.end():parts[i+1].start() if i+1<len(parts) else len(record['text'])].strip()
                enumerated+=f'<article><span class="index">0{i+1}</span><p>{esc(text)} {citation(record["ref"])}</p></article>'
            enumerated+='</div></div>'
        if enumerated: result+=section('outcomes','资料明确列出的成果与取舍',enumerated)
    if r['template']!='project':
        focus_title,focus_body=focus_view(r,esc,citation,table_html)
        result+=section('chart',focus_title,focus_body)
    elif r['charts']:
        body=''
        for c in r['charts']:
            body+=f'<div class="panel"><div class="panel-head"><h3>{esc(c["title"])}</h3>{citation(c["ref"])}</div><div class="two-col"><div class="chart-box">{chart_svg(c)}</div><div class="panel-body"><h3>{c["total"]} 条清单记录</h3><p>{esc(c["method"])}</p><div class="distribution">'+''.join(f'<div><b>{esc(s["label"])}</b><span>{s["value"]} / {c["total"]} 条</span></div>' for s in c['series'])+'</div></div></div><details><summary>查看图表数据与完整清单</summary>'
            t=next(t for t in r['tables'] if t['ref']==c['ref'])
            body+=table_html(t['headers'],[[esc(v) for v in row] for row in t['rows']])+f'<p class="table-caption">分母：{c["total"]} 行；所有行均计入；来源 {citation(c["ref"])}</p></details></div>'
        result+=section('chart','资料清单 · 可核对的状态分布',body)
    else:
        result+=section('chart','资料结构', '<div class="source-docs">'+''.join(f'<div class="source-doc"><b>{esc(s["title"])}</b><small>已选资料</small></div>' for s in r['selected'])+'</div><p class="muted">没有可按同一口径统计的状态表，未生成数值图表。</p>')
    if r['template']!='project':
        # The summary and focus view already contain the saved points and lists.
        # Do not repeat every source fragment as report prose or invent more points.
        # Keep useful tables not already rendered by the research focus view.
        records=''
        if r['template']!='research':
            for t in r['tables']:
                records+='<div class="panel record-panel"><div class="panel-head"><h3>'+esc(t['title'])+'</h3>'+citation(t['ref'])+'</div>'+table_html(t['headers'],[[esc(v) for v in row] for row in t['rows']])+'</div>'
    else:
        records=''
        for s in r['selected']:
            items=[x for x in r['records'] if r['refs'][x['ref']-1]['source_id']==s['id'] and r['refs'][x['ref']-1]['kind']==s['kind'] and not re.match(r'^风险[一二三四五六七八九十\d]+[：:]',x['text'])]
            if items:
                def passage(x):
                    ref=r['refs'][x['ref']-1]
                    body=f'<p>{esc(readable(x["text"]))} {citation(x["ref"])}</p>'
                    if x['field']=='ocr_text': return '<details class="ocr-record"><summary>OCR 原文 · '+esc(ref['tab'])+' · 未推断单元格</summary>'+body+'</details>'
                    if len(re.findall(r'(?:第一|第二|第三)[，、]',x['text']))>=3:
                        return '<details><summary>查看上方列举的完整原段落 '+citation(x['ref'])+'</summary>'+body+'</details>'
                    return body
                records+=f'<div class="panel record-panel"><div class="panel-head"><h3>{esc(s["title"])}</h3><small>原文整理</small></div><div class="panel-body">'+''.join(passage(x) for x in items)+'</div></div>'
        # Tables without a status column used to disappear from the report body.
        for t in r['tables']:
            if not any(c['ref']==t['ref'] for c in r['charts']):
                records+='<div class="panel record-panel"><div class="panel-head"><h3>'+esc(t['title'])+' · 完整原表</h3>'+citation(t['ref'])+'</div>'+table_html(t['headers'],[[esc(v) for v in row] for row in t['rows']])+'</div>'
    if records or notes.get('records'):
        result+=section('records',heading[1],records)
    if r['actions']:
        actions=table_html(['风险 / 问题','处理动作（原文）','负责人','资料中的期限','依据'],[[esc(a['risk']),esc(a['action']),esc(a['owner']),esc(a['due']),citation(a['ref'])] for a in r['actions']])
        actions+='<p class="table-caption">期限保留资料原话；相对时间没有擅自换算日期。资料未给出执行状态，不标为已完成。</p>'
    else:
        prompts={
            'research':'研究边界：原文摘录与模型归纳应回到来源核对；没有原始政策或实测结果时，不把材料摘要当权威结论。',
            'compare':'决策边界：资料没有提供可统一计算的成本、准确率或耗时指标时，不生成优劣评分。适用性以原文条件为准。',
            'proposal':'立项待确认：预算、排期、实际负责人和批准记录需由用户补充。资料中的拟议技术路线不代表本工具已经采用。',
            'learning':'学习核对：先对照原文确认字段、条件、否定和修订关系；不能把历史需求或笔记自述核验当作已连接业务系统验证。',
        }
        actions='<p class="muted">'+esc(prompts.get(r['template'],'资料中没有可确定解析的“风险—动作—负责人—期限”记录，不自动指派责任人。'))+'</p>'
    result+=section('actions',heading[2],'<div class="panel">'+actions+'</div>')
    limitations=[str(x).replace('下方另保留全部可组织正文、原表与 OCR', '完整正文、原表与 OCR 可在引用资料和导出附录中核对') for x in r['limitations']]
    gaps='<div class="decision-banner"><div><strong>交付前需要确认</strong><ul>'+''.join('<li>'+esc(x)+'</li>' for x in limitations)+'</ul><p>需人工补充：实际验收结果、行动完成情况、确定日期，以及仍存在的冲突资料。</p></div></div>'
    result+=section('gaps','待确认事项与使用边界',gaps)
    # One compact entry per saved page/field keeps all source snapshots reachable,
    # including material not selected as a summary point. No originals are deleted.
    source_units={}
    for ref in r['refs']:
        source_units.setdefault(ref['unit_id'],ref)
    if source_units:
        result+='<details class="report-source-index"><summary>参考资料</summary><ul>'
        for ref in source_units.values():
            result+='<li>'+citation(ref['id'])+' '+esc(ref['title'])+'<span> · '+esc(ref['tab'])+' · '+('OCR' if ref['field']=='ocr_text' else '正文')+'</span></li>'
        result+='</ul></details>'
    result+=f'<footer class="report-footer"><span>纯本地 4B · 生成 {r["generation"]["seconds"]:.1f}s · 原文与图表同一快照</span><span>可编辑初稿，不代替专业审批</span></footer>'
    if appendix:
        result+='<section class="section appendix"><h2>引用附录 · 生成时的原文快照</h2><p>离线文件已包含下列原文，不连接本机服务。区间按保存字段字符位置记录。</p>'
        for ref in r['refs']:
            result+=f'<article id="source-{ref["id"]}"><h3>[{ref["id"]}] {esc(ref["title"])} · {esc(ref["tab"])} · {"OCR" if ref["field"]=="ocr_text" else "正文"}</h3><blockquote>{esc(ref["quote"])}</blockquote><small>字段 {esc(ref["field"])} · 区间 [{ref["start"]}, {ref["end"]}) · 修订 {ref["revision"][:12]}</small></article>'
        result+='</section>'
    return result

def export_html(r):
    css=(Path(__file__).resolve().parent.parent/'frontend'/'reports-v2.css').read_text('utf-8')
    return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+esc(r['title'])+'</title><style>'+css+'\n.edit{display:none!important}body{background:white}.report-wrap{max-width:1200px;margin:auto}.appendix article{scroll-margin-top:20px;margin:25px 0}.appendix blockquote{white-space:pre-wrap;border-left:3px solid #218e8f;padding:14px;background:#f1f8f5}</style></head><body><main class="report-wrap">'+render_report(r,appendix=True)+'</main></body></html>'

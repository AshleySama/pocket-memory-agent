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
from contextlib import nullcontext
from .report_sections import readable, focus_view, quote_span, presentation_records

TEMPLATES = {
    'auto': ('按资料自动组织', '根据所选资料自动识别主题并分章；不强行合并无关主题，完整保留清单、条件、版本与来源'),
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
        self.composing_report = None

    @property
    def directory(self):
        p = self.store.data_dir / 'reports-v2'; p.mkdir(exist_ok=True); return p

    def path(self, report_id):
        if not isinstance(report_id, str) or not re.fullmatch('[a-f0-9]{32}', report_id): raise ValueError('无效文稿编号')
        return self.directory / (report_id + '.json')

    def _trash_path(self, undo_token):
        if not isinstance(undo_token, str) or not re.fullmatch('[a-f0-9]{32}', undo_token):
            raise ValueError('无效的撤销凭据')
        return self.directory / '.trash' / (undo_token + '.json')

    def list_reports(self):
        with self.lock:
            records = [json.loads(p.read_text('utf-8')) for p in self.directory.glob('*.json')]
        return sorted([dict({k: r[k] for k in ('id','title','template','updated','version')},status=r.get('adaptive',{}).get('status','complete')) for r in records], key=lambda x: x['updated'], reverse=True)

    def read(self, report_id):
        with self.lock:
            p = self.path(report_id)
            if not p.exists(): raise KeyError(report_id)
            report = json.loads(p.read_text('utf-8'))
            # Project old, unedited compilations on read without rewriting
            # their evidence, saved file or any user's full-document edits.
            from .report_reading_view import attach_compiled_layout
            return attach_compiled_layout(report, self.store)

    def write(self, report):
        target = self.path(report['id']); tmp = target.with_suffix('.tmp')
        with tmp.open('w', encoding='utf-8') as out:
            json.dump(report, out, ensure_ascii=False, indent=2); out.flush(); os.fsync(out.fileno())
        os.replace(tmp, target)

    def save_edit(self, report_id, body):
        from .report_document import ensure_editable_report, sanitize_document_html
        if not isinstance(body, dict): raise ValueError('无效的文稿编辑请求')
        with self.lock:
            if self.composing_report == report_id: raise ValueError('该文稿正在成稿，请完成后再编辑')
            r = self.read(report_id)
            ensure_editable_report(r)
            if type(body.get('version')) is not int or body['version'] != r['version']:
                raise ValueError('文稿已更新，请重新打开后再编辑')
            if any(not valid_ref(ref,r['units']) for ref in r['refs']):
                raise ValueError('文稿快照引用不一致，请重新生成')
            edit_fields = ('title', 'document_html', 'notes', 'edited', 'reviewed')
            if 'undo_document_edit' in body:
                if body['undo_document_edit'] is not True or set(body) != {'version', 'undo_document_edit'}:
                    raise ValueError('撤回保存不能同时提交其他编辑')
                previous = r.get('last_document_edit')
                if not isinstance(previous, dict) or not isinstance(previous.get('fields'), dict):
                    raise ValueError('没有可撤回的人工保存')
                for key in edit_fields:
                    if key in previous['fields']: r[key] = copy.deepcopy(previous['fields'][key])
                    else: r.pop(key, None)
                if 'document_html' in r:
                    r['document_html'] = sanitize_document_html(r['document_html'], r['refs'])
                r.pop('last_document_edit', None)
                r.update(version=r['version']+1, updated=now())
                self.write(r); return r
            title = str(body.get('title',r['title'])).strip()
            notes = body.get('notes',r.get('notes',{}))
            if not 1 <= len(title) <= 100 or not isinstance(notes,dict): raise ValueError('标题或章节说明无效')
            allowed = {'summary','records','actions','chart','gaps','outcomes'} | {f'chapter-{c["id"]}' for c in r.get('adaptive',{}).get('chapters',[])}
            if 'notes' in body and any(k not in allowed or not isinstance(v,str) or len(v)>4000 for k,v in notes.items()): raise ValueError('章节说明超出范围')
            document = (sanitize_document_html(body['document_html'], r['refs'])
                        if 'document_html' in body else None)
            # One reversible manual save, including legacy title/notes clients.
            # Generation, evidence identities and chapter facts are never edited.
            r['last_document_edit'] = {'from_version': r['version'], 'saved_at': now(),
                'fields': {key: copy.deepcopy(r[key]) for key in edit_fields if key in r}}
            if document is not None: r['document_html'] = document
            r.update(title=title, notes=notes, reviewed=False, version=r['version']+1, updated=now(), edited=True)
            self.write(r); return r

    def delete(self, report_id, expected_version):
        """Move only the saved report to local trash; its source data is untouched."""
        with self.lock:
            if self.composing_report == report_id: raise ValueError('该文稿正在成稿，请完成后再删除')
            target = self.path(report_id)
            if type(expected_version) is not int or expected_version < 1:
                raise ValueError('请提供要删除文稿的有效版本')
            report = self.read(report_id)
            if expected_version != report['version']:
                raise ValueError('文稿已更新，请重新打开后再删除')
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
                raise ValueError('同编号文稿已存在，无法覆盖；已删除的文稿仍保留在本机')
            os.replace(archived, target)
            return report

    def delete_many(self, reports, all_reports=False):
        """Preflight the complete selection before moving any report to trash."""
        if not isinstance(reports, list) or not reports or type(all_reports) is not bool:
            raise ValueError('请选择要删除的文稿')
        with self.lock:
            selected, seen = [], set()
            for item in reports:
                if not isinstance(item, dict) or set(item) != {'id', 'version'}:
                    raise ValueError('批量删除需要每份文稿的编号和版本')
                report_id, version = item['id'], item['version']
                target = self.path(report_id)
                if report_id in seen:
                    raise ValueError('批量删除不能重复选择同一文稿')
                seen.add(report_id)
                if type(version) is not int or version < 1:
                    raise ValueError('请提供要删除文稿的有效版本')
                report = self.read(report_id)
                if report['version'] != version:
                    raise ValueError('文稿已更新，本批未删除；请刷新列表后重新选择')
                if self.composing_report == report_id or self.active_job(report_id):
                    raise ValueError('所选文稿正在生成或成稿，本批未删除；请完成后再操作')
                selected.append((target, report_id))
            if all_reports and seen != {r['id'] for r in self.list_reports()}:
                raise ValueError('文稿列表已变化，本次未清空；请刷新列表后重新确认')
            undo_token = uuid.uuid4().hex
            folder = self._trash_path(undo_token).with_suffix('')
            folder.parent.mkdir(exist_ok=True)
            while folder.exists() or folder.is_symlink():
                undo_token = uuid.uuid4().hex
                folder = self._trash_path(undo_token).with_suffix('')
            folder.mkdir()
            manifest = folder / 'batch.json'
            moved = []
            try:
                with manifest.open('x', encoding='utf-8') as out:
                    json.dump({'id': undo_token, 'reports': [rid for _, rid in selected], 'created': now()}, out)
                    out.flush(); os.fsync(out.fileno())
                for target, report_id in selected:
                    archived = folder / (report_id + '.json')
                    os.replace(target, archived)
                    moved.append((target, archived))
            except OSError:
                # A normal move failure must not leave half the batch deleted.
                # If the filesystem also refuses rollback, all snapshots and the
                # manifest stay in this batch's trash folder for recovery.
                for target, archived in reversed(moved):
                    os.replace(archived, target)
                manifest.unlink(missing_ok=True)
                folder.rmdir()
                raise
            return {'deleted': [rid for _, rid in selected], 'count': len(selected), 'undo_token': undo_token}

    def restore_many(self, undo_token):
        """Restore a whole deletion batch without overwriting existing reports."""
        with self.lock:
            folder = self._trash_path(undo_token).with_suffix('')
            manifest = folder / 'batch.json'
            if folder.is_symlink() or manifest.is_symlink() or not manifest.is_file():
                raise KeyError(undo_token)
            batch = json.loads(manifest.read_text('utf-8'))
            report_ids = batch.get('reports')
            if (batch.get('id') != undo_token or not isinstance(report_ids, list)
                    or not report_ids or any(not isinstance(rid, str) for rid in report_ids)
                    or len(set(report_ids)) != len(report_ids)):
                raise ValueError('文稿撤销记录无效')
            selected = []
            for report_id in report_ids:
                target = self.path(report_id)
                archived = folder / (report_id + '.json')
                if target.exists() or target.is_symlink():
                    raise ValueError('同编号文稿已存在，本批未恢复；已删除文稿仍保留在本机')
                if self.composing_report == report_id or self.active_job(report_id):
                    raise ValueError('同编号文稿正在生成，本批未恢复；请完成后重试')
                if archived.is_symlink() or not archived.is_file():
                    raise ValueError('文稿撤销记录不完整，本批未恢复')
                report = json.loads(archived.read_text('utf-8'))
                if report.get('id') != report_id:
                    raise ValueError('文稿撤销记录无效')
                selected.append((archived, target))
            moved = []
            try:
                for archived, target in selected:
                    os.replace(archived, target)
                    moved.append((archived, target))
            except OSError:
                for archived, target in reversed(moved):
                    os.replace(target, archived)
                raise
            try:
                manifest.unlink()
                folder.rmdir()
            except OSError:
                # The reports are already restored. A leftover receipt must not
                # turn a successful restore into a misleading failure message.
                pass
            return {'restored': report_ids, 'count': len(report_ids)}

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
        requested_at = time.time()
        title, template = str(body.get('title','')).strip(), body.get('template','auto')
        if not 1 <= len(title) <= 100 or template not in TEMPLATES: raise ValueError('请输入标题并选择组织方式')
        focus = str(body.get('focus','')).strip()
        if len(focus)>1200: raise ValueError('整理要求请控制在1200字以内')
        phase=body.get('phase','complete')
        if phase not in ('complete','outline'):raise ValueError('文稿阶段无效')
        if phase=='outline' and template!='auto':raise ValueError('可编辑提纲请使用按要求组织')
        if focus and template!='auto':raise ValueError('自定义整理要求请使用按资料自动组织，旧模板不会应用这段要求')
        composition_mode=body.get('composition_mode','model_written')
        if composition_mode not in ('model_written','source_compiled'):
            raise ValueError('文稿写作方式无效')
        if composition_mode=='source_compiled' and template!='auto':
            raise ValueError('快速编排请使用按资料自动组织')
        with self.lock:
            if self.worker and self.worker.is_alive(): raise ValueError('已有文稿正在生成，请等待完成')
            selected, units = self.snapshot(body.get('sources'))
            job_id = uuid.uuid4().hex
            job = {'id':job_id,'status':'queued','stage':'资料快照已保存，准备整理','started':requested_at}
            self.jobs[job_id] = job
            self.worker = threading.Thread(target=self._run,args=(job_id,title,template,selected,units,focus,phase=='outline',composition_mode),daemon=True)
            self.worker.start()
            return dict(job)

    def job(self, job_id):
        with self.lock:
            if job_id not in self.jobs: raise KeyError(job_id)
            return dict(self.jobs[job_id])

    def active_job(self,report_id):
        with self.lock:
            return next((dict(j) for j in self.jobs.values()
                         if j.get('report_id')==report_id and j.get('status') in ('queued','running')),None)

    def _stage(self, jid, text):
        with self.lock: self.jobs[jid]['stage']=text

    def discover_sources(self, query='', kind='all', page=1, page_size=30):
        from .report_sources import discover
        return discover(self.store,query,kind,page,page_size)

    def _outline_update(self, report_id, body):
        from .report_adaptive import validate_outline
        r=self.read(report_id)
        if r.get('template')!='auto' or r.get('adaptive',{}).get('status')!='outline':
            raise ValueError('只有尚未成稿的提纲可以调整')
        if body.get('version')!=r['version']:raise ValueError('提纲已更新，请重新打开后再修改')
        if self.composing_report==report_id:raise ValueError('正在按提纲成稿，请等待完成')
        if r['adaptive'].get('reading_mode')=='source_brief_v2':
            from .report_brief import validate_outline as validate_brief_outline
            outline=validate_brief_outline(body.get('outline',r['adaptive']['outline']),r['adaptive']['facts'],r['adaptive']['groups'])
        else:
            outline=validate_outline(body.get('outline',r['adaptive']['outline']),r['adaptive']['facts'])
        r['adaptive']['outline']=outline
        r.update(version=r['version']+1,updated=now(),outline_edited=True)
        return r

    def save_outline(self, report_id, body):
        with self.lock:
            r=self._outline_update(report_id,body);self.write(r);return r

    def compose(self, report_id, body):
        with self.lock:
            if self.worker and self.worker.is_alive():raise ValueError('已有文稿正在生成，请等待完成')
            r=self._outline_update(report_id,body)
            self.write(r)  # Durable edited plan; a failed draft never deletes it.
            jid=uuid.uuid4().hex
            self.jobs[jid]=dict(id=jid,status='queued',stage='提纲已保存，准备分章成稿',started=time.time(),report_id=report_id)
            self.composing_report=report_id
            self.worker=threading.Thread(target=self._run_compose,args=(jid,r),daemon=True)
            self.worker.start()
            return dict(self.jobs[jid])

    def _run_compose(self,jid,prepared_report):
        try:
            with self.lock:self.jobs[jid]['status']='running'
            old=prepared_report
            r=self.build(old['title'],'auto',old['selected'],old['units'],lambda s:self._stage(jid,s),
                         focus=old.get('focus',''),prepared=old['adaptive'],outline_override=old['adaptive']['outline'],
                         composition_mode=old.get('composition_mode','model_written'))
            r.update(id=old['id'],created=old['created'],version=old['version']+1,
                     notes=old.get('notes',{}),outline_edited=old.get('outline_edited',False),
                     planning_generation=old['generation'])
            with self.lock:
                if self.read(old['id'])['version']!=old['version']:
                    raise ValueError('文稿版本已变化，未覆盖原提纲')
                self.write(r)
                partial=r['adaptive']['status']=='partial'
                self.jobs[jid].update(status='done',stage='部分成稿已保存，请核对未覆盖项' if partial else '文稿已保存到本机',
                                     partial=partial,report_id=r['id'],elapsed=round(time.time()-self.jobs[jid]['started'],2))
        except Exception as exc:
            import logging
            logging.exception('Report outline composition failed')
            with self.lock:self.jobs[jid].update(status='error',stage='成稿未完成，已保留提纲',error=str(exc))
        finally:
            with self.lock:self.composing_report=None

    def _run(self, jid, title, template, selected, units, focus='', stop_after_outline=False,composition_mode='model_written'):
        try:
            # Auto reports use immutable source snapshots only. Holding the global
            # mutation lock during every chapter would also block note navigation
            # (its draft flush is a write). Model access remains engine-serialized;
            # the server rejects library changes while this worker is active.
            with (nullcontext() if template=='auto' else self.operation_lock):
                with self.lock: self.jobs[jid]['status']='running'
                self._stage(jid,'正在组织原文、表格和引用')
                def checkpoint(report):
                    with self.lock:
                        self.composing_report=report['id']
                        self.write(report)
                        self.jobs[jid]['report_id']=report['id']
                report = self.build(title,template,selected,units,lambda s:self._stage(jid,s),focus=focus,
                                    stop_after_outline=stop_after_outline,on_checkpoint=checkpoint,composition_mode=composition_mode)
                with self.lock:
                    self.write(report)
                    status=report.get('adaptive',{}).get('status')
                    partial=status in ('partial','reading_paused')
                    label='阅读进度已保存，可继续阅读' if status=='reading_paused' else ('提纲已保存，可调整章节后继续成稿' if status=='outline' else ('部分成稿已保存，请查看未覆盖项' if partial else '文稿已保存到本机'))
                    self.jobs[jid].update(status='done',stage=label,partial=partial,report_id=report['id'],elapsed=round(time.time()-self.jobs[jid]['started'],2))
        except Exception as exc:
            import logging
            logging.exception('Report workspace generation failed')
            with self.lock: self.jobs[jid].update(status='error',stage='生成失败',error=str(exc))
        finally:
            with self.lock:self.composing_report=None

    def resume_reading(self,report_id,body):
        """Continue a durable reading snapshot, never accept client evidence."""
        with self.lock:
            if self.worker and self.worker.is_alive():raise ValueError('已有文稿正在生成，请等待完成')
            old=self.read(report_id)
            if old.get('adaptive',{}).get('status')!='reading_paused':raise ValueError('这份文稿没有待续读任务')
            if body.get('version')!=old['version']:raise ValueError('阅读进度已更新，请重新打开')
            jid=uuid.uuid4().hex
            self.jobs[jid]=dict(id=jid,status='queued',stage='正在恢复已保存的阅读进度',started=time.time(),report_id=report_id)
            self.composing_report=report_id
            self.worker=threading.Thread(target=self._run_reading,args=(jid,old),daemon=True)
            self.worker.start()
            return dict(self.jobs[jid])

    def _run_reading(self,jid,old):
        def retain_identity(r):
            r.update(id=old['id'],created=old['created'],version=old['version']+1)
            # Report-level totals include previous sessions; cached traces do not
            # represent another generation and are never added as new calls.
            g=r['generation'];prior=old.get('generation',{})
            g['current_session_calls']=g.get('calls',0)
            g['calls']=g.get('calls',0)+prior.get('calls',0)
            g['seconds']=round(g.get('seconds',0)+prior.get('seconds',0),3)
            return r
        def checkpoint(r):
            with self.lock:self.write(retain_identity(r))
        try:
            with self.lock:self.jobs[jid]['status']='running'
            r=self.build(old['title'],'auto',old['selected'],old['units'],lambda s:self._stage(jid,s),
                         focus=old.get('focus',''),stop_after_outline=True,
                         resume_reading=old['adaptive'],on_checkpoint=checkpoint)
            r=retain_identity(r)
            with self.lock:
                self.write(r);paused=r['adaptive']['status']=='reading_paused'
                self.jobs[jid].update(status='done',stage='阅读进度已保存，可继续阅读' if paused else '提纲已保存，可调整后成稿',
                                     partial=paused,report_id=r['id'],elapsed=round(time.time()-self.jobs[jid]['started'],2))
        except Exception as exc:
            import logging
            logging.exception('Report reading continuation failed')
            with self.lock:self.jobs[jid].update(status='error',stage='续读未完成，已保留进度',error=str(exc))
        finally:
            with self.lock:self.composing_report=None

    def build(self,title,template,selected,units,stage=lambda _:None,focus='',stop_after_outline=False,prepared=None,outline_override=None,
              resume_reading=None,on_checkpoint=None,composition_mode='model_written'):
        refs, records, tables, actions = [], [], [], []
        def register(u,a,b):
            ref=make_ref(u,a,b)
            for old in refs:
                if old['unit_id']==ref['unit_id'] and old['start']==a and old['end']==b: return old['id']
            ref['id']=len(refs)+1; refs.append(ref); return ref['id']
        if template=='auto':
            if composition_mode not in ('model_written','source_compiled'):
                raise ValueError('文稿写作方式无效')
            # Saved reports retain their protocol. A request cannot silently
            # reinterpret a full-reading receipt as the faster compilation mode.
            saved=prepared if prepared is not None else resume_reading
            # No content range means compile all selected sources, not ask the
            # model to infer exclusions from the title. Explicit requirements
            # and saved legacy drafts retain their existing selection contract.
            if composition_mode=='source_compiled' and saved is None and not stop_after_outline:
                from .report_complete import plan as complete_plan
                started=time.monotonic()
                structure=complete_plan(units,focus)
                if structure is not None:
                    stage('按原文结构编排全部所选资料并核对来源')
                    from .report_reading_view import collect as collect_reading_layout
                    reading_layout=collect_reading_layout(self.store,selected,units)
                    by_unit={u['unit_id']:u for u in units};chapters=[]
                    for i,chapter in enumerate(structure['chapters'],1):
                        segments=[]
                        for segment in chapter['segments']:
                            u=by_unit[segment['unit_id']]
                            if u['body'][segment['start']:segment['end']]!=segment['text']:
                                raise ValueError('原文结构与引用范围不一致')
                            segments.append(dict(segment,ref=register(u,segment['start'],segment['end'])))
                        chapters.append(dict(id=f'C{i}',title=chapter['title'],segments=segments,
                                             source_label=chapter.get('source_label','')))
                    if any(not valid_ref(r,units) for r in refs):
                        raise ValueError('引用身份核对失败，文稿未保存')
                    created=now()
                    return dict(id=uuid.uuid4().hex,version=1,created=created,updated=created,
                        title=title,template=template,template_label=TEMPLATES[template][0],
                        selected=selected,units=units,refs=refs,claims=[],records=[],tables=[],actions=[],charts=[],
                        notes={},edited=False,reviewed=False,focus=focus,model='原文结构整理 / 未调用4B',
                        generation=dict(calls=0,seconds=round(time.monotonic()-started,3)),composition_mode=composition_mode,
                        adaptive=dict(reading_mode='source_structured_v1',status='complete',chapters=chapters,
                            reading_layout=reading_layout,
                            outline=[dict(id=c['id'],title=c['title'],purpose='',fact_ids=[]) for c in chapters],
                            coverage=structure['coverage'],omissions=structure['omissions'],issues=[]),
                        limitations=['按已有标题整理原文，不代表新增综合分析或外部专业核验。'])
            if saved is not None:
                composition_mode='source_compiled' if saved.get('reading_mode') in ('source_compiled_v1','source_brief_v2') else 'model_written'
            if composition_mode=='source_compiled':
                if saved is not None and saved.get('reading_mode')=='source_compiled_v1':
                    from .report_compiled import generate
                else:
                    from .report_brief import generate
            else:
                from .report_adaptive import generate
            report_id=uuid.uuid4().hex;created=now()
            def envelope(adaptive,claims,generation):
                report={'id':report_id,'version':1,'created':created,'updated':now(),
                    'title':title,'template':template,'template_label':TEMPLATES[template][0],
                    'selected':selected,'units':units,'refs':copy.deepcopy(refs),'claims':claims,
                    'records':[],'tables':[],'actions':[],'charts':[],
                    'notes':{},'edited':False,'reviewed':False,'focus':focus,
                    'model':'Qwen3-4B-Instruct-2507 / 本地现有配置',
                    'generation':generation,'adaptive':adaptive,'composition_mode':composition_mode,
                    'limitations':['完整阅读记录表示原文已送入本地模型，不代表每项事实已经准确归纳。',
                                    '归纳使用生成时快照；历史资料中的计划、技术版本不自动代表当前现状。',
                                    '精确引用可核对来源，语义、条件和遗漏仍需人工核对。']}
                if composition_mode=='source_compiled':
                    report['tables']=[copy.deepcopy(t) for c in adaptive.get('chapters',[]) for t in c.get('tables',[])]
                    report['limitations']=[
                        '原文快照完整保留；快速模式依据原子内容目录和标明范围的预览选材，不代表模型通读全文。',
                        '来源事实、原文表格与编写建议分开呈现；编辑建议不是原文规定或已经验证的业务结论。',
                        '内容依据生成时快照，数字、条件、语义与培训适用性仍需人工核对。']
                if any(not valid_ref(r,units) for r in refs): raise ValueError('引用身份核对失败，文稿未保存')
                return report
            def checkpoint(adaptive,generation):
                if on_checkpoint:on_checkpoint(envelope(adaptive,[],copy.deepcopy(generation)))
            adaptive, claims, generation = generate(self.engine,title,focus,units,register,stage,self.directory/'.adaptive-cache',
                                                   stop_after_outline=stop_after_outline,prepared=prepared,outline_override=outline_override,
                                                   resume_reading=resume_reading,reading_checkpoint=checkpoint if on_checkpoint else None)
            from .report_reading_view import attach_compiled_layout
            return attach_compiled_layout(envelope(adaptive,claims,generation), self.store)
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
        from . import report_digest
        if template in ('learning','research') and report_digest.applicable(units):
            from . import report_grounded_digest
            from .report_pdf_structure import enrich_pdf_units
            stage('正在读取PDF标题层级并核对原文位置')
            structure_receipts=[]
            for source in selected:
                if source['kind']!='document':continue
                doc=self.store.get_document(source['id'])
                if doc.file_type.lower()!='pdf':continue
                original=self.store.data_dir / doc.stored_path
                same_source=[u for u in units if u['kind']=='document' and u['source_id']==source['id']]
                structure_receipts.append(enrich_pdf_units(original,same_source))
            digest_result, digest_claims, generation = report_grounded_digest.generate(
                self.engine, title, TEMPLATES[template][1], units, register,
                self.directory / '.digest-cache', stage)
            digest_result['source_structure']=structure_receipts
            report={'id':uuid.uuid4().hex,'version':1,'created':now(),'updated':now(),
                    'title':title,'template':template,'template_label':TEMPLATES[template][0],
                    'selected':selected,'units':units,'refs':refs,'claims':digest_claims,
                    'records':records,'tables':tables,'actions':actions,'charts':[],
                    'notes':{},'edited':False,'reviewed':False,
                    'model':'Qwen3-4B-Instruct-2507 / 本地现有配置',
                    'generation':generation,'digest':digest_result,
                      'limitations':['按原文主题、角色、完整条件与清单选编；模型决定要点取舍，事实文字来自绑定原文，不进行无依据自由扩写。',
                                   '归纳基于所选资料快照，不代表资料中的技术表述已按最新版本核实。',
                                   '引用身份与原文区间已核对；归纳仍需人工核对语义、条件与遗漏。']}
            if any(not valid_ref(r,units) for r in refs): raise ValueError('引用身份核对失败，文稿未保存')
            return report
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
                'limitations':['AI 归纳仅代表初稿；原文逐字可定位不等于结论已获人工确认。','原文记录、未来计划及本轮实际实现不是同一事实；文稿不会自动验证原资料是否仍然有效。','数值图表仅计算原文明确列值；结构图只展示原文清单／顺序，不推断收益、概率或因果。',f'AI 仅阅读 {len(model_blocks)} 段摘选（{used_chars} 字符）；下方另保留全部可组织正文、原表与 OCR。完整快照不表示全部资料均已由模型总结。']}
        if any(not valid_ref(r,units) for r in refs): raise ValueError('引用身份核对失败，文稿未保存')
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
        return {'ref':ref,'quote_text':readable(ref['quote']),'changed':current is None or digest(current)!=ref['revision'],'current':current,
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
    from .report_citation_presentation import citation_free_body
    # Build/validate provenance first, including export appendices. Rendering
    # then suppresses numbered labels only; stored refs and source text remain.
    return citation_free_body(_render_report(r, appendix=appendix))

def _render_report(r, appendix=False):
    if any(not valid_ref(ref,r['units']) for ref in r['refs']):
        raise ValueError('文稿快照引用不一致，请重新生成')
    if 'document_html' in r:
        from .report_document import render_document
        return render_document(r, appendix=appendix)
    if r.get('adaptive'):
        from .report_adaptive_view import render_adaptive
        return render_adaptive(r,appendix=appendix)
    if r.get('digest',{}).get('protocol') == 'whole-document-v1':
        from .report_digest_view import render_digest
        return render_digest(r, appendix=appendix)
    heading=HEADINGS[r['template']]; notes=r.get('notes',{})
    # Reading view only. Original units, references, model input and saved edits
    # stay untouched; repeated PDF watermark fragments remain in the appendix.
    view_records=presentation_records(r)
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
        for record in view_records:
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
        focus_title,focus_body=focus_view(dict(r,records=view_records),esc,citation,table_html)
        if focus_body or notes.get('chart'):
            result+=section('chart',focus_title,focus_body)
    elif r['charts']:
        body=''
        for c in r['charts']:
            body+=f'<div class="panel"><div class="panel-head"><h3>{esc(c["title"])}</h3>{citation(c["ref"])}</div><div class="two-col"><div class="chart-box">{chart_svg(c)}</div><div class="panel-body"><h3>{c["total"]} 条清单记录</h3><p>{esc(c["method"])}</p><div class="distribution">'+''.join(f'<div><b>{esc(s["label"])}</b><span>{s["value"]} / {c["total"]} 条</span></div>' for s in c['series'])+'</div></div></div><details><summary>查看图表数据与完整清单</summary>'
            t=next(t for t in r['tables'] if t['ref']==c['ref'])
            body+=table_html(t['headers'],[[esc(v) for v in row] for row in t['rows']])+f'<p class="table-caption">分母：{c["total"]} 行；所有行均计入；来源 {citation(c["ref"])}</p></details></div>'
        result+=section('chart','资料清单 · 可核对的状态分布',body)
    elif notes.get('chart'):
        result+=section('chart','补充说明','')
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
            items=[x for x in view_records if r['refs'][x['ref']-1]['source_id']==s['id'] and r['refs'][x['ref']-1]['kind']==s['kind'] and not re.match(r'^风险[一二三四五六七八九十\d]+[：:]',x['text'])]
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
        result+=section('actions',heading[2],'<div class="panel">'+actions+'</div>')
    elif notes.get('actions'):
        result+=section('actions',heading[2],'')
    # Preserve user-authored notes; technical audit data stays in the saved report.
    if notes.get('gaps'):
        result+=section('gaps','待确认事项','')
    if appendix:
        used_refs={int(n) for n in re.findall(r'data-ref="(\d+)"',result)}
        if used_refs:
            result+='<details class="section appendix" id="report-source-appendix"><summary>引用核对</summary><p>点击正文引用可展开对应原文。</p>'
            for ref in r['refs']:
                if ref['id'] in used_refs:
                    result+=f'<article id="source-{ref["id"]}"><h3>[{ref["id"]}] {esc(ref["title"])} · {esc(ref["tab"])}</h3><blockquote>{esc(ref["quote"])}</blockquote></article>'
            result+='</details>'
    return result

def export_html(r):
    if r.get('adaptive',{}).get('status') in ('outline','reading_paused'):raise ValueError('当前为阅读或提纲阶段，请先完成分章成稿再导出文稿')
    if r.get('adaptive',{}).get('status') == 'partial' and not r['adaptive'].get('chapters'):
        raise ValueError('文稿尚未生成，不能导出空结果；资料与错误记录仍保留')
    css=(Path(__file__).resolve().parent.parent/'frontend'/'reports-v2.css').read_text('utf-8')
    # This fixed local script reveals a cited passage inside collapsed details.
    # No report text is interpolated into JavaScript and no network access is used.
    source_links = '''<script data-report-source-links>(function(){
function reveal(){var id=location.hash.slice(1);if(!/^source-\\d+$/.test(id))return;
var target=document.getElementById(id);if(!target)return;
for(var p=target.parentElement;p;p=p.parentElement){if(p.tagName==='DETAILS')p.open=true;}
target.scrollIntoView({block:'start'});}
window.addEventListener('hashchange',reveal);
document.addEventListener('click',function(e){var a=e.target.closest('a[href^="#source-"]');
if(a&&a.hash===location.hash)setTimeout(reveal,0);});reveal();})();</script>'''
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><title>'
            +esc(r['title'])+'</title><style>'+css+'</style></head>'
            '<body class="export-document"><header class="export-masthead">'
            '<strong>Pocket Memory</strong><span>资料汇编 · 离线阅读</span></header>'
            '<main class="report-wrap" id="report-document">'+render_report(r,appendix=True)
            +'</main><nav class="export-page-end" aria-label="文档导航">'
            '<a href="#report-document">返回文稿开头 ↑</a></nav>'+source_links+'</body></html>')

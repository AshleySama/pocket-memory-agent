# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Local, opt-in feedback snapshots; never a training label or auto-upload."""
import hashlib,json,sqlite3,uuid
from datetime import datetime,timezone
from pathlib import Path

def scope_key(data_dir, epoch):
    return hashlib.sha256((str(Path(data_dir).resolve())+'\n'+epoch).encode()).hexdigest()

def connect(data_dir):
    db=sqlite3.connect(Path(data_dir)/'qa-feedback.sqlite')
    db.execute('CREATE TABLE IF NOT EXISTS feedback (id TEXT PRIMARY KEY, created TEXT NOT NULL, updated TEXT NOT NULL, snapshot_hash TEXT NOT NULL, snapshot TEXT NOT NULL, rating TEXT NOT NULL, wrong_sources TEXT NOT NULL, comment TEXT NOT NULL)')
    return db

def save(data_dir, body):
    if not isinstance(body,dict):raise ValueError('反馈格式无效')
    try:ident=str(uuid.UUID(body.get('id','')))
    except (ValueError,TypeError,AttributeError):raise ValueError('反馈编号无效')
    rating=body.get('rating')
    if rating not in ('useful','not_useful','source_issue'):raise ValueError('请选择反馈类型')
    question=body.get('question');answer=body.get('answer');sources=body.get('sources')
    if not isinstance(question,str) or not question.strip() or len(question)>10000:raise ValueError('问题无效')
    if not isinstance(answer,str) or not answer.strip() or len(answer)>100000:raise ValueError('回答无效')
    if not isinstance(sources,list) or len(sources)>100 or not all(isinstance(s,dict) for s in sources):raise ValueError('来源格式无效')
    allowed={'index','title','source_key','source_type','source_id','parent_note_id','tab_name','kind','location','evidence','excerpt','raw_evidence','source_revision','revision','url'}
    sources=[{k:v for k,v in s.items() if k in allowed} for s in sources]
    indices={s.get('index') for s in sources if type(s.get('index')) is int}
    wrong=body.get('wrong_sources',[])
    if not isinstance(wrong,list) or any(type(i) is not int or i not in indices for i in wrong):raise ValueError('有误来源不在本次回答中')
    comment=body.get('comment','')
    if not isinstance(comment,str) or len(comment)>2000:raise ValueError('备注最多2000字')
    snapshot={'question':question,'answer':answer,'sources':sources,'timing':body.get('timing',{}),
              'build':'claude-local-feedback-20261103','provenance':'user_visible_client_snapshot','human_feedback':True}
    encoded=json.dumps(snapshot,ensure_ascii=False,sort_keys=True,allow_nan=False)
    if len(encoded.encode('utf-8'))>1024*1024:raise ValueError('反馈内容过大')
    digest=hashlib.sha256(encoded.encode()).hexdigest();now=datetime.now(timezone.utc).isoformat()
    db=connect(data_dir)
    try:
        with db:
            prior=db.execute('SELECT snapshot_hash FROM feedback WHERE id=?',(ident,)).fetchone()
            if prior and prior[0]!=digest:raise ValueError('反馈编号对应其他回答，请重新打开当前回答')
            db.execute('INSERT INTO feedback VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET updated=excluded.updated,rating=excluded.rating,wrong_sources=excluded.wrong_sources,comment=excluded.comment',
                (ident,now,now,digest,encoded,rating,json.dumps(sorted(set(wrong))),comment))
        return {'saved':True,'id':ident,'storage':'local_only'}
    finally:db.close()

def export(data_dir):
    path=Path(data_dir)/'qa-feedback.sqlite'
    result={'schema_version':1,'exported_at':datetime.now(timezone.utc).isoformat(),'contains':'仅主动提交的问答、显示的来源与反馈；未导出整库','automatic_upload':False,'records':[]}
    if not path.exists():return result
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        for ident,created,updated,digest,snapshot,rating,wrong,comment in db.execute('SELECT * FROM feedback ORDER BY created,id'):
            result['records'].append(dict(id=ident,created=created,updated=updated,snapshot_sha256=digest,**json.loads(snapshot),rating=rating,wrong_sources=json.loads(wrong),comment=comment))
    finally:db.close()
    return result

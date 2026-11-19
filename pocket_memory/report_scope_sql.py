# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Read-only structured-scope compiler. No record IDs or business word lists.

Borrowed architecture: schema-grounded text-to-SQL (LlamaIndex) and an
allow-listed SQLite authorizer. This module never opens the user's database.
"""
import json,re,sqlite3
from . import report_scope_fields as fields,report_adaptive as a

PROTOCOL='scope-readonly-sql-v1'
MAX_TOKENS=768

class ScopeUnclear(ValueError):
    """An unexpressible user condition is not a normal empty result."""

def source_schema(groups,blocks,focus=''):
    records,catalog=fields.extract(groups,blocks)
    # Do not turn prose, repeated/conflicting fields or mixed sources into a
    # partial table. They continue through the existing original-text path.
    if not records or any(len(v)<3 for v in records.values()):return None
    keys=set(catalog)
    if any(set(row)!=keys for row in records.values()):return None
    if any(not re.fullmatch(r'[\w\u4e00-\u9fff]{1,30}',k) for k in keys):return None
    # Preserve source headings as metadata, with their actual source interval.
    # A generated/display-only title is not admitted as a source fact.
    by_id={b['id']:b for b in blocks};titles={}
    for group in groups:
        title=group.get('title','');hits=[]
        for sid in group['source_ids']:
            block=by_id[sid]
            for m in re.finditer(r'^\s*#{1,6}\s+([^\n]+)',block['text'],re.M):
                if m.group(1).strip()==title:
                    begin=block['start']+m.start(1)
                    hits.append(dict(value=title,quote=title,source_id=sid,unit_id=block['unit_id'],start=begin,end=begin+len(title)))
        if len(hits)==1:titles[group['id']]=hits
    # Schema pruning: title values are needed for explicit naming predicates,
    # not for every owner/status/date filter. Never add the whole title catalog
    # as unrelated context to ordinary field queries.
    names_requested=bool(re.search(r'标题|名称|名字|命名',focus))
    if names_requested and len(titles)==len(records) and '原文标题' not in catalog:
        for gid,row in records.items():row['原文标题']=titles[gid]
        catalog['原文标题']=sorted({v[0]['value'] for v in titles.values()})
    return records,catalog

def request(focus,catalog):
    system='/no_think\n将用户的资料汇编范围转换为 SQLite WHERE 布尔表达式。只筛选已有记录，不生成报告。\n先在 analysis 中逐项列出用户的必要条件、或分支和排除项，再写 where。不得根据常识新增条件，不能遗漏条件。含任一用 OR，多个必须满足条件用 AND，明确排除用 NOT。注意括号和分支中各负责人、项目的归属。状态值严格按原文，不把批准当成完成。未说明的字段不加限制。\n只使用给定列名和确切字段值；日期为YYYY-MM-DD文本，可用比较/BETWEEN或LIKE判断年月。中文列名用双引号，值用单引号。where只包含表达式，不含SELECT或其他语句。不允许访问外部表或调用函数。无法从字段表达的要求、未说明时间窗口等歧义，在clarification说明并令where为空，不能猜测。\n只输出JSON，字段按analysis、clarification、where顺序。'
    messages=[dict(role='system',content=system),dict(role='user',content='records表列名与已有取值：\n'+json.dumps(catalog,ensure_ascii=False)+'\n用户完整要求：\n'+focus)]
    schema=a._obj(dict(analysis=dict(type='string'),clarification=dict(type='string'),where=dict(type='string')))
    return dict(messages=messages,schema=schema,max_output_tokens=MAX_TOKENS)

_TOKEN=re.compile(r'''\s*(?:('(?:''|[^'])*')|("(?:""|[^"])*")|([\w\u4e00-\u9fff]+)|(<>|!=|<=|>=|[=<>(),]))''')
_WORDS={'AND','OR','NOT','IN','BETWEEN','IS','NULL','LIKE','ESCAPE'}

def run_where(where,records,catalog):
    if not isinstance(where,str) or not where.strip():raise ValueError('范围条件仍需确认，未擅自放宽')
    if len(where)>4000:raise ValueError('范围表达式过长')
    pos=0;references=set();depth=0
    while pos<len(where):
        if not where[pos:].strip():break
        m=_TOKEN.match(where,pos)
        if not m:raise ValueError('范围表达式包含不允许的语法')
        literal,quoted,word,symbol=m.groups();pos=m.end()
        if symbol=='(':depth+=1
        if symbol==')':depth-=1
        if quoted:
            key=quoted[1:-1].replace('""','"')
            if key not in catalog:raise ValueError('范围表达式引用未知字段')
            references.add(key)
        elif word:
            if word in catalog:references.add(word)
            elif word.upper() not in _WORDS and word not in ('0','1'):raise ValueError('范围表达式包含不允许的标识符')
    if not references:raise ValueError('范围表达式未绑定原文字段')
    quote=lambda s:'"'+s.replace('"','""')+'"'
    cols=list(catalog);db=sqlite3.connect(':memory:')
    try:
        db.execute('CREATE TABLE records (_record_key TEXT, '+','.join(quote(k)+' TEXT' for k in cols)+')')
        db.executemany('INSERT INTO records VALUES ('+','.join('?' for _ in range(len(cols)+1))+')',
            [[gid]+[rec[k][0]['value'] for k in cols] for gid,rec in records.items()])
        db.execute('PRAGMA query_only=ON')
        def authorize(op,t,c,d,s):
            if op==sqlite3.SQLITE_SELECT:return sqlite3.SQLITE_OK
            if op==sqlite3.SQLITE_READ and t=='records':return sqlite3.SQLITE_OK
            if op==sqlite3.SQLITE_FUNCTION and c in ('like',):return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        db.set_authorizer(authorize);db.set_progress_handler(lambda:1,10000)
        return [row[0] for row in db.execute('SELECT _record_key FROM records WHERE ('+where+')')]
    except sqlite3.Error as exc:raise ValueError('范围表达式无法安全执行：'+str(exc)) from exc
    finally:db.close()

def top_mixed(where):
    """Detect ungrouped OR mixed with global AND, excluding BETWEEN's AND."""
    pos=0;depth=0;between=False;ops=set()
    while pos<len(where):
        m=_TOKEN.match(where,pos)
        if not m:break
        literal,quoted,word,symbol=m.groups();pos=m.end()
        if symbol=='(':depth+=1
        if symbol==')':depth-=1
        if depth==0 and word:
            word=word.upper()
            if word=='BETWEEN':between=True
            elif word=='AND' and between:between=False
            elif word in ('AND','OR'):ops.add(word)
    return ops=={'AND','OR'}

def parse(response,records,catalog):
    obj=a._parse(response)
    if not isinstance(obj,dict) or set(obj)!={'analysis','clarification','where'} or not isinstance(obj['analysis'],str):raise ValueError('范围协议不完整')
    if not obj['where'].strip():raise ScopeUnclear('范围条件仍需确认：'+obj['analysis'][:240])
    return run_where(obj['where'],records,catalog)

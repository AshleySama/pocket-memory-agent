# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Metadata-only source expressions. No questions, gold facts or titles hard coded.

Resolve each named source's immediately following members, then union independent
expressions. A later reference to the *subject* of a question never expands scope.
"""
import re,unicodedata
TITLE=re.compile(r'[《【]([^》】]+)[》】]')
MEMBER=r'(?:标签页|页签)\s*[0-9一二三四五六七八九十]+|首页|主页|主笔记'
TOKEN=re.compile(MEMBER)
HOME={'首页','主页','主笔记'}
def norm(s):return re.sub(r'\s+','',unicodedata.normalize('NFKC',s or '')).casefold()
def member(s):return norm(s).replace('标签页','页签')

def natural_source_question(question, notes, documents=None):
    """Recognize an unquoted, explicit source preamble using saved metadata.

    Only the scope resolver sees this normalization. No fuzzy title lookup,
    question rewriting for generation, or inferred relationship across tabs.
    Bare topic mentions remain global unless a short saved enumeration makes
    the user's request for that list unambiguous.
    """
    if TITLE.search(question):
        return question, None
    q = norm(question)
    q = re.sub(r'^(?:麻烦)?(?:请问一下|请问|请|帮我|我想知道|想问一下|问一下)', '', q)
    preamble = re.match(r'^(?:(?:只|仅)?(?:根据|依据|看|读|参考))', q)
    rest = q[preamble.end():] if preamble else q
    candidates = [n for n in notes.values() if len(norm(n['title'])) >= 4 and rest.startswith(norm(n['title']))]
    if not candidates:
        return question, None
    longest = max(len(norm(n['title'])) for n in candidates)
    candidates = [n for n in candidates if len(norm(n['title'])) == longest]
    if len(candidates) != 1:
        return question, None
    note = candidates[0]; title = norm(note['title']); suffix = rest[len(title):]
    if not suffix or any(norm(d.get('title', '')) == title for d in (documents or {}).values()):
        return question, None
    # Another independent named source/comparison must not be narrowed to one.
    if re.search(r'其他(?:资料|笔记|文章)|跨(?:文档|资料|笔记)|结合|比较|对比|相比|相较', suffix) or re.match(r'^(?:和|与|及|以及|跟)', suffix):
        return question, None
    family = {note['id'], note.get('parent_note_id')} | {n['id'] for n in notes.values() if n.get('parent_note_id') == note['id']}
    if any(n['id'] not in family and len(norm(n['title'])) >= 4 and norm(n['title']) in suffix for n in notes.values()):
        return question, None
    explicit = bool(preamble or re.match(r'^(?:里面|里|中|内|的正文|的笔记|的资料|提到|提及|记录|描述|写到|列出)', suffix) or TOKEN.match(suffix.lstrip('的')))
    if not explicit:
        body = note.get('content', '')
        # Preserve ordinary all-library topic queries. Bare title admission is
        # limited to a short named list visibly present in that exact note.
        labels = re.findall(r'(?m)^[ \t]*(?:[-*]\s*)?([\u4e00-\u9fff]{1,8}?)(?:[一二三四五六七八九十]+|\d+)[：:、.．]', body)
        categories = {label for label in labels if labels.count(label) >= 2}
        if len(body) > 700 or not re.search(r'哪些|什么|列出|列举|几[项个条种]', suffix) or not any(label in suffix for label in categories):
            return question, None
    canonical = f'《{note["title"]}》{suffix}'
    return canonical, dict(source_id=note['id'], title=note['title'],
                          resolution='unique_leading_note_title', explicit_source_phrase=explicit)

class SourceScopes:
    def __init__(self,notes):
        self.notes=notes;self.children={}
        for n in notes.values():
            if n.get('parent_note_id') is not None:self.children.setdefault(n['parent_note_id'],[]).append(n)
    def resolve(self,question):
        q=unicodedata.normalize('NFKC',question)
        titles=list(TITLE.finditer(q));outside=TITLE.sub(lambda m:' '*len(m[0]),q)
        if not TOKEN.search(outside):return None
        if not titles and not re.search(r'(?:只看|只读|根据|结合|页签)',q):return None
        entries=[];consumed=[];parents=set();decisions=[]
        def failure(label,why):return dict(status=why,allowed=set(),scope=entries+[dict(quoted_title=label,matched_sources=[],resolution=why)],decisions=decisions)
        for pos,t in enumerate(titles):
            matches=[n for n in self.notes.values() if norm(n['title'])==norm(t[1])]
            if len(matches)!=1:return None # Leave old identifier disambiguation intact.
            n=matches[0];id=n['id'];family=self.children.get(id,[])
            prefix=q[:t.start()]
            negative=bool(re.search(r'(?:不要|不|排除|禁止)(?:看|读|借|用|使用|采用|参考)?[\s“”"]*$',prefix))
            if family:parents.add(id)
            end=titles[pos+1].start() if pos+1<len(titles) else len(q)
            segment=q[t.end():end]
            # Only an adjacent source-member expression is a qualifier.
            lead=re.match(r'^[\s的“”"里中]*(?:(及|和|与|加|以及)\s*(?:它的|其)?)?[\s“”"]*('+MEMBER+r')',segment)
            selected={id}|{c['id'] for c in family}
            if lead:
                parents.add(id)
                selected={id} if lead[1] else set()
                current=lead
                label=lead[2];at=t.end()+lead.start(2);stop=t.end()+lead.end(2)
                while True:
                    consumed.append((at,stop))
                    if label in HOME:selected.add(id)
                    else:
                        found=[c for c in family if member(label) in {member(c.get('tab_name')),member(c['title'])}]
                        if len(found)!=1:return failure(t[1]+' / '+label,'missing' if not found else 'ambiguous')
                        selected.add(found[0]['id'])
                    continuation=re.match(r'^[\s“”"]*(?:和|及|与|加|、|以及)\s*(?:它的|其)?[\s“”"]*('+MEMBER+r')',q[stop:end])
                    if not continuation:break
                    at=stop+continuation.start(1);stop=stop+continuation.end(1);label=continuation[1]
            entries.append(dict(quoted_title=t[1],matched_sources=[f'note:{i}' for i in sorted(selected)],source_note_id=id,negative=negative,resolution='local_metadata_members' if lead else 'independent_source'))
            decisions.append(dict(source=t[1],selected=sorted(selected),local_qualifier=bool(lead)))
        # A free-standing named child is resolved globally only if truly unique.
        for tok in TOKEN.finditer(outside):
            if tok[0] in HOME or any(a<=tok.start()<b for a,b in consumed):continue
            before=q[max(0,tok.start()-12):tok.start()]
            if re.search(r'(?:排除|不(?:要)?(?:使用|用|看|读|借|含|采用)?)[^，,。；;?？]{0,6}$',before):continue
            # A source preamble is distinct from later question content such as
            # 回答：“页签2”这个按钮是什么. Never scan arbitrary prose for scope.
            preamble=outside[:tok.start()]
            if re.search(r'[：:，,。；;?？]',preamble) or not re.match(r'^\s*(?:结合|根据|只看|只读|仅看|仅根据|按|参考|翻到)',preamble):continue
            explicit_parents=[n for n in self.notes.values() if n.get('parent_note_id') is None and len(norm(n['title']))>=2 and norm(n['title']) in norm(preamble)]
            explicit_parents=[n for n in explicit_parents if not any(n['id']!=o['id'] and norm(n['title']) in norm(o['title']) for o in explicit_parents)]
            if len(explicit_parents)>1:return failure(tok[0],'ambiguous_parent')
            # A parent's explicit adjacent missing child was handled above;
            # never borrow a child from another family in that case.
            found=[n for n in self.notes.values() if n.get('parent_note_id') is not None and member(tok[0]) in {member(n.get('tab_name')),member(n['title'])} and (not explicit_parents or n['parent_note_id']==explicit_parents[0]['id'])]
            if len(found)!=1:return failure(tok[0],'missing' if not found else 'ambiguous')
            id=found[0]['id'];parents.add(found[0]['parent_note_id'])
            entries.append(dict(quoted_title=tok[0],matched_sources=[f'note:{id}'],source_note_id=id,resolution='unique_standalone_child'))
        if not entries:return None
        negative_keys={k for e in entries if e.get('negative') for k in e['matched_sources']}
        entries=[e for e in entries if not e.get('negative')]
        for e in entries:e['matched_sources']=[k for k in e['matched_sources'] if k not in negative_keys]
        if negative_keys:decisions.append(dict(excluded_source_expressions=sorted(negative_keys)))
        # Explicit exclusion constrains the one identified family; it cannot
        # silently remove a member of another independent cited source.
        neg=re.compile(r'(?:排除|不(?:要)?(?:使用|用|看|读|借|含|采用)?)[ \t“”"]*('+MEMBER+r')')
        for m in neg.finditer(outside):
            if len(parents)!=1:return failure(m[1],'ambiguous_exclusion')
            parent=next(iter(parents));family=[self.notes[parent]]+self.children.get(parent,[])
            excluded={parent} if m[1] in HOME else {n['id'] for n in family if member(m[1]) in {member(n.get('tab_name')),member(n['title'])}}
            for e in entries:e['matched_sources']=[k for k in e['matched_sources'] if int(k.split(':')[1]) not in excluded]
            decisions.append(dict(excluded=sorted(excluded),expression=m[0]))
        allowed={k for e in entries for k in e['matched_sources']}
        if not allowed:return failure('source_scope','conflicting_members')
        # Empty entries caused by exclusions are provenance, not a missing title.
        entries=[e for e in entries if e['matched_sources']]
        return dict(status='resolved',allowed=allowed,scope=entries,decisions=decisions)

class ScopedRetrieval:
    def __init__(self,owner,prior):self.owner=owner;self.prior=prior;self.resolver=SourceScopes(owner.notes);self.last=None
    def __call__(self,question):
        scope_question, natural = natural_source_question(question,self.owner.notes,getattr(self.owner,'documents',None))
        result=self.resolver.resolve(scope_question);self.last=result or natural
        if result is None:
            allowed, scope = self.prior(scope_question)
            if natural:
                scope = [dict(s, source_expression=natural) for s in scope]
            return allowed, scope
        return [n for n in self.owner.nodes if n.metadata['source_key'] in result['allowed']],result['scope']

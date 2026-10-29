# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Literal provenance and original list rendering, never semantic entailment.

No corpus/case access, no generation, no added factual values. Unsupported,
ambiguous, negative and comparative clauses keep their original citations.
"""
import difflib,re

def norm(text):return re.sub(r'[^\w\u4e00-\u9fff]','',text).casefold()
def identifiers(text):return set(re.findall(r'`([A-Za-z_][\w.]*)',text))
def render(answer,question,sources):
    audit=[]
    # Only complete an original enumerated process, never assemble novel steps.
    process=bool(re.search(r'步骤|流程|回调|链路',question)) and not re.search(
        r'(?:不要|不需)[^。！？]{0,10}(?:步骤|流程|回调|链路)|(?:只|仅)[^。！？]{0,8}第[一二三四五六七八九\d]+步',question)
    blocks=[]
    if process:
        for source in sources:
            for span in source.get('raw_evidence',[]):
                text=span['quote'].strip();lines=text.splitlines()
                if len(text)>900 or len(lines)<3 or not all(re.match(r'^\s*(?:[-*]|\d+[.、])\s+',line) for line in lines):continue
                if not any(word in question and word in text for word in ('步骤','流程','回调','链路')):continue
                if sum(bool(identifiers(line)) for line in lines)<3:continue
                if not any(text==b[1] for b in blocks):blocks.append((source['index'],text,lines))
    pieces=re.findall(r'[^。\n]+。?|\n',answer)
    rendered=[];active_row=None
    for piece in pieces:
        if piece=='\n':rendered.append(piece);continue
        refs={int(n) for n in re.findall(r'\[(\d+)\]',piece)}
        matches=[]
        for index,text,lines in blocks:
            if index not in refs:continue
            matched=sum(bool(identifiers(line)&identifiers(piece)) for line in lines)
            if matched>=2:matches.append((index,text,lines))
        if len(matches)==1:
            index,text,lines=matches[0]
            replacement='原文记录的完整步骤：\n'+'\n'.join(line+f' [{index}]' for line in lines)+'\n'
            rendered.append(replacement);audit.append(dict(kind='complete_original_list',source=index,original_quote=text,prior_clause=piece));continue
        bare=re.sub(r'\[\d+\]','',piece).strip()
        if not bare:continue
        # A literal match cannot establish a negation or relational inference.
        if re.search(r'没有|不是|不能|无法|尚未|未验证|不相|相同|不同|比较|大于|小于|高于|低于|均|两者|判断：',bare):
            rendered.append(piece);active_row=None;continue
        q=norm(bare);bindings=[];next_row=None
        if len(q)<4:rendered.append(piece);continue
        texts={s['index']:norm(s['evidence']) for s in sources}
        for source in sources:
            index=source['index'];body=texts[index]
            m=difflib.SequenceMatcher(None,q,body,autojunk=False).find_longest_match()
            quote=q[m.a:m.a+m.size]
            if m.size>=8 and sum(quote in t for t in texts.values())==1:
                bindings.append((index,quote))
                matched_rows=[n for n,row in enumerate(source.get('bound_cells',[])) if any(norm(v) in q and len(norm(v))>=8 for v in row.values())]
                if len(matched_rows)==1:next_row=(index,matched_rows[0])
                continue
            for n,row in enumerate(source.get('bound_cells',[])):
                for field,value in row.items():
                    literal=norm(value)
                    if len(literal)>=4 and literal in q and sum(literal in t for t in texts.values())==1 and (norm(field) in q or active_row==(index,n)):
                        bindings.append((index,literal));break
                if bindings and bindings[-1][0]==index:break
        if bindings:
            indices=list(dict.fromkeys(i for i,quote in bindings))
            if next_row is not None:active_row=next_row
            if set(indices).issubset(refs):
                rendered.append(piece)
                audit.append(dict(kind='existing_reference_preserved',indices=indices,prior_clause=piece));continue
            # Bind only the matched literal span, not all other claims in the
            # sentence. Keep pre-existing in-sentence references for review.
            positions=[];normalized=''
            for pos,char in enumerate(bare):
                for v in norm(char):normalized+=v;positions.append(pos)
            inserts={}
            for index,quote in bindings:
                if index in refs:continue
                at=normalized.find(quote)
                if at>=0:
                    pos=positions[at+len(quote)-1]+1
                    while pos<len(bare) and bare[pos] in '`）)"”』】':pos+=1
                    inserts.setdefault(pos,[]).append(index)
            annotated=bare
            for pos,ids in sorted(inserts.items(),reverse=True):annotated=annotated[:pos]+''.join(f'[{i}]' for i in dict.fromkeys(ids))+annotated[pos:]
            preserved=''.join(f'[{i}]' for i in sorted(refs))
            rendered.append(annotated+preserved)
            audit.append(dict(kind='literal_source_association',indices=indices,matches=bindings,prior_clause=piece,
                              semantic_support='not_proven_by_literal_match'))
        else:rendered.append(piece);active_row=None
    return ''.join(rendered),audit

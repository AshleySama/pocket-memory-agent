# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Identity-based request boundary, resolved from saved library metadata.

The boundary is applied before constructing any retriever, original-text unit,
or evidence registry. Titles are display metadata, never authorization keys.
"""
from dataclasses import dataclass
import hashlib
import json
import re


@dataclass(frozen=True)
class AskScope:
    mode: str = 'all'
    note_ids: tuple = ()
    document_ids: tuple = ()
    expanded_note_ids: tuple = ()
    titles: tuple = ()

    @property
    def restricted(self):
        return self.mode != 'all'

    @property
    def single_note_family(self):
        return self.restricted and len(self.note_ids) == 1 and not self.document_ids

    @property
    def key(self):
        if not self.restricted:
            return 'all'
        value = (self.mode, self.note_ids, self.document_ids, self.expanded_note_ids)
        return hashlib.sha256(json.dumps(value).encode()).hexdigest()[:24]

    def permits(self, kind, ident):
        if not self.restricted:
            return True
        return ident in (self.expanded_note_ids if kind == 'note' else
                         self.document_ids if kind == 'document' else ())

    def sql(self, kind, column='id'):
        """Return a parameterized WHERE condition; empty never means all."""
        if not self.restricted:
            return '1', ()
        ids = self.expanded_note_ids if kind == 'note' else self.document_ids
        return (f'{column} IN ({",".join("?" for _ in ids)})', ids) if ids else ('0', ())

    def payload(self):
        if self.mode == 'all':
            label = '全部资料'
        elif self.mode == 'current':
            label = '当前笔记：' + self.titles[0] + '（含全部页签）'
        else:
            label = '选中资料：' + '、'.join(self.titles)
        return dict(mode=self.mode, note_ids=list(self.note_ids),
                    document_ids=list(self.document_ids),
                    expanded_note_ids=list(self.expanded_note_ids), label=label)


class ScopedNotes(dict):
    """Original saved notes with explicit request identity, without prompt edits."""
    def __init__(self, entries=(), ask_scope=None):
        super().__init__(entries)
        self.ask_scope = ask_scope or AskScope()


def source_catalog(store):
    with store._lock:
        notes = [dict(row) for row in store.connection.execute(
            'SELECT id,title,parent_note_id FROM notes ORDER BY id')]
        documents = [dict(row) for row in store.connection.execute(
            'SELECT id,title,original_name,status FROM documents ORDER BY id')]
    return dict(notes=[dict(id=n['id'], title=n['title'],
                            tab_count=1 + sum(v['parent_note_id'] == n['id'] for v in notes))
                       for n in notes if n['parent_note_id'] is None],
                documents=documents)


def resolve_ask_scope(store, raw=None):
    if raw is None:
        return AskScope()
    if (not isinstance(raw, dict) or not isinstance(raw.get('mode'), str)
            or raw['mode'] not in {'all', 'current', 'selected'}):
        raise ValueError('问答范围无效，请重新选择全部资料、当前笔记或指定资料。')
    if set(raw) - {'mode', 'note_ids', 'document_ids'}:
        raise ValueError('问答范围包含未知字段，请重新选择资料。')
    ids = {}
    for key in ('note_ids', 'document_ids'):
        value = raw.get(key, [])
        if (not isinstance(value, list) or len(value) > 1000
                or any(type(i) is not int or i <= 0 for i in value)):
            raise ValueError('问答范围必须使用有效的资料ID列表。')
        ids[key] = tuple(sorted(set(value)))
    mode = raw['mode']
    if mode == 'all':
        if any(ids.values()):
            raise ValueError('全部资料范围不能同时指定资料ID。')
        return AskScope()
    if not any(ids.values()):
        raise ValueError('尚未选择问答资料，请至少选择一条笔记或一份文档。')
    with store._lock:
        notes = {row['id']: dict(row) for row in store.connection.execute(
            'SELECT id,title,parent_note_id FROM notes')}
        documents = {row['id']: dict(row) for row in store.connection.execute(
            'SELECT id,title FROM documents')}
    if any(i not in notes for i in ids['note_ids']) or any(i not in documents for i in ids['document_ids']):
        raise ValueError('选中的资料已不存在或来自其他资料库，请重新选择问答范围。')
    roots = set()
    for ident in ids['note_ids']:
        seen = set()
        while notes[ident]['parent_note_id'] is not None:
            if ident in seen or notes[ident]['parent_note_id'] not in notes:
                raise ValueError('选中笔记的页签关系无效，请重新选择资料。')
            seen.add(ident)
            ident = notes[ident]['parent_note_id']
        roots.add(ident)
    if mode == 'current' and (len(roots) != 1 or ids['document_ids']):
        raise ValueError('当前笔记范围只能包含一个笔记家族。')
    expanded = set(roots)
    while True:
        children = {i for i, n in notes.items() if n['parent_note_id'] in expanded}
        if children <= expanded:
            break
        expanded |= children
    roots = tuple(sorted(roots))
    titles = tuple(notes[i]['title'] for i in roots) + tuple(documents[i]['title'] for i in ids['document_ids'])
    return AskScope(mode, roots, ids['document_ids'], tuple(sorted(expanded)), titles)


def conflicting_source_title(store, question, scope):
    """A named existing source outside the selection cannot be substituted."""
    if not scope.restricted:
        return False
    quoted = re.findall(r'[《【]([^》】]+)[》】]', question)
    if not quoted:
        return False
    normalize = lambda text: re.sub(r'[^\w\u4e00-\u9fff]', '', str(text).lower())
    with store._lock:
        sources = [('note', r['id'], r['title']) for r in store.connection.execute('SELECT id,title FROM notes')]
        sources += [('document', r['id'], r['title']) for r in store.connection.execute('SELECT id,title FROM documents')]
    for name in quoted:
        found = [(kind, ident) for kind, ident, title in sources if normalize(title) == normalize(name)]
        if found and not any(scope.permits(kind, ident) for kind, ident in found):
            return True
    return False



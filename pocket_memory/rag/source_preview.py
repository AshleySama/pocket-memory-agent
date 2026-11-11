# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Read-only saved-original preview. Never changes model inputs or citations."""
import hashlib
import json


def revision_token(server, revision=None):
    revision = server.store.source_revision() if revision is None else revision
    return hashlib.sha256(json.dumps([server.preview_epoch, revision]).encode()).hexdigest()


def source_preview(server, sources):
    local = getattr(server.rag_pipeline, 'local', None)
    revision = getattr(local, 'revision', None)
    if revision is None or revision != server.store.source_revision():
        return None
    items, seen = [], set()
    # No re-ranking, truncation, guessing offsets, or generated excerpt fallback.
    for source in sources:
        for member in source.get('citation_members', []):
            if member.get('verification') != 'exact_saved_original':
                continue
            spans = member.get('spans', [])
            if not spans or not all(isinstance(s.get('quote'), str) and s['quote']
                                    and s.get('source_revision') for s in spans):
                continue
            ident = member.get('id')
            if not ident or ident in seen:
                continue
            seen.add(ident)
            sid = member.get('source_id')
            if member.get('source_type') != 'note' or not isinstance(sid, int):
                continue
            parent = member.get('parent_note_id')
            url = f'/api/notes/{parent}?tab_id={sid}' if parent is not None else f'/api/notes/{sid}'
            items.append(dict(id=ident, title=source.get('title') or '未命名资料',
                              url=url, tab_name=member.get('tab_name') or '',
                              location='图片识别文字（请核对原图）' if any(
                                  s.get('field') == 'ocr_text' for s in spans) else '正文',
                              quotes=[s['quote'] for s in spans]))
    return dict(revision=revision_token(server, revision), items=items,
                label='相关原文，尚未整理为回答')

# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Verified incremental indexes; receipts are committed with the actual vectors.

Old databases without receipts rebuild once. A receipt is only reusable when
source, model/protocol and complete persisted index bytes all still match.
"""
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import numpy as np
from .rag.performance import metric, span, measured_lock

PROTOCOL = 'note-document-inputs-and-split-note-text-v1'

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,separators=(',',':'),sort_keys=True).encode()).hexdigest()

def document_source(document,chunks):
    # Index completion changes status/time, not the model input or provenance.
    data=asdict(document)
    for key in ('status','error','updated_at'):data.pop(key,None)
    return digest([PROTOCOL,data,[asdict(c) for c in chunks]])

def model_key(engine):
    identity=getattr(engine,'index_identity',None)
    if not callable(identity):return None  # Unversioned third-party engines rebuild safely.
    try:
        value=identity()
        return digest([PROTOCOL,value]) if value else None
    except Exception:
        return None

@contextmanager
def transaction(store):
    with measured_lock(store._lock,'index.store_lock_wait'), span('index.publish_transaction'):
        conn=store.connection
        if conn.in_transaction:
            raise RuntimeError('Index publication requires its own transaction')
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            yield conn

def index_digest(store,kind,ident):
    """Read metadata plus actual vector bytes; missing/corrupt entries never hit."""
    conn=store.connection;h=hashlib.sha256()
    if kind=='note':
        vector=conn.execute('SELECT vector,dimension FROM embeddings WHERE note_id=?',(ident,)).fetchone()
        if vector is None:return None
        rows=[vector]
        rows+=conn.execute('''SELECT c.id,c.title,c.text,c.location,c.kind,c.chunk_order,e.vector,e.dimension
            FROM note_chunks c LEFT JOIN note_chunk_embeddings e ON c.id=e.chunk_id
            WHERE c.note_id=? ORDER BY c.chunk_order,c.id''',(ident,)).fetchall()
    else:
        rows=conn.execute('''SELECT c.id,c.title,c.text,c.location,c.page,c.section,c.sheet,c.cell_range,
            c.chunk_order,c.source_start_order,c.source_end_order,e.vector,e.dimension
            FROM document_chunks c LEFT JOIN document_chunk_embeddings e ON c.id=e.chunk_id
            WHERE c.document_id=? ORDER BY c.chunk_order,c.id''',(ident,)).fetchall()
    for row in rows:
        vector,dimension=row[-2],row[-1]
        if vector is None or not dimension or len(vector)!=dimension*4:return None
        h.update(json.dumps(list(row[:-2]),ensure_ascii=False,separators=(',',':')).encode())
        h.update(str(dimension).encode());h.update(vector)
    return h.hexdigest()

def reuse(store,kind,ident,model,source,validate):
    if model is None:return False
    with transaction(store) as conn:
        if not validate():return False
        row=conn.execute(f'SELECT model_key,source_key,index_key FROM {kind}_embedding_state WHERE {kind}_id=?',(ident,)).fetchone()
        hit=bool(row and row[0]==model and row[1]==source and row[2]==index_digest(store,kind,ident))
    metric('index.reused' if hit else 'index.rebuilt')
    return hit

def _vectors(values):
    vectors=[np.asarray(v,dtype=np.float32) for v in values]
    if any(v.ndim!=1 or not v.size or not np.isfinite(v).all() for v in vectors):
        raise ValueError('Invalid index vector')
    return vectors

def _write_vectors(conn,table,column,ids,vectors,now):
    if len(ids)!=len(vectors):raise ValueError('Incomplete index vectors')
    conn.executemany(f'''INSERT INTO {table}({column},vector,dimension,updated_at) VALUES (?,?,?,?)
        ON CONFLICT({column}) DO UPDATE SET vector=excluded.vector,dimension=excluded.dimension,updated_at=excluded.updated_at''',
        [(ident,v.tobytes(),v.size,now) for ident,v in zip(ids,vectors)])

def _receipt(store,conn,kind,ident,model,source):
    if model is None:
        conn.execute(f'DELETE FROM {kind}_embedding_state WHERE {kind}_id=?',(ident,))
        return
    key=index_digest(store,kind,ident)
    if key is None:raise ValueError('Incomplete persisted index')
    conn.execute(f'''INSERT INTO {kind}_embedding_state VALUES (?,?,?,?)
        ON CONFLICT({kind}_id) DO UPDATE SET model_key=excluded.model_key,source_key=excluded.source_key,index_key=excluded.index_key''',
        (ident,model,source,key))

def publish_note(store,ident,chunks,vector,chunk_vectors,model,source,validate):
    vectors=_vectors([vector]+chunk_vectors)
    if len(chunk_vectors)!=len(chunks):raise ValueError('Incomplete chunk vectors')
    with transaction(store) as conn:
        if not validate():raise ValueError('Source changed during indexing; reindex required')
        stored=store.list_note_chunks(ident)
        existing=[(c.title,c.text,c.location,c.kind,c.chunk_order) for c in stored]
        computed=[(c['title'],c['text'],c['location'],c['kind'],i) for i,c in enumerate(chunks)]
        if existing!=computed:
            conn.execute('DELETE FROM note_chunks WHERE note_id=?',(ident,))
            conn.executemany('INSERT INTO note_chunks(note_id,title,text,location,kind,chunk_order) VALUES (?,?,?,?,?,?)',
                             [(ident,*row) for row in computed])
            stored=store.list_note_chunks(ident)
        now=store._now()
        _write_vectors(conn,'embeddings','note_id',[ident],vectors[:1],now)
        _write_vectors(conn,'note_chunk_embeddings','chunk_id',[c.id for c in stored],vectors[1:],now)
        _receipt(store,conn,'note',ident,model,source)

def publish_document(store,ident,chunks,chunk_vectors,model,source,validate):
    if not chunks:
        raise ValueError('Document text has not been extracted')
    vectors=_vectors(chunk_vectors)
    if len(chunks)!=len(vectors):raise ValueError('Incomplete document vectors')
    with transaction(store) as conn:
        if not validate():raise ValueError('Source changed during indexing; reindex required')
        now=store._now()
        _write_vectors(conn,'document_chunk_embeddings','chunk_id',[c.id for c in chunks],vectors,now)
        conn.execute("UPDATE documents SET status='ready',error='',updated_at=? WHERE id=?",(now,ident))
        _receipt(store,conn,'document',ident,model,source)

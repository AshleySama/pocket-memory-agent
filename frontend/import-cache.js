/* Copyright 2026 AshleySama
 * SPDX-License-Identifier: Apache-2.0
 */

/* Local binary staging. Files never go into localStorage or leave this computer. */
class PocketImportCache {
  constructor({indexedDB = globalThis.indexedDB, maxBytes = 200 * 1024 * 1024} = {}) {
    this.idb = indexedDB; this.maxBytes = maxBytes; this.opening = null;
  }
  open() {
    if (this.opening) return this.opening;
    this.opening = new Promise((resolve, reject) => {
      let abandoned = false;
      if (!this.idb) { reject(new Error('当前浏览器不支持本机文件暂存')); return; }
      const req = this.idb.open('pocket-memory-import-files', 1);
      req.onupgradeneeded = () => req.result.createObjectStore('files', {keyPath:'id'});
      req.onerror = () => reject(req.error || new Error('无法打开本机暂存'));
      req.onblocked = () => { abandoned=true; reject(new Error('暂存被另一页面占用，请关闭旧页面后重试')); };
      req.onsuccess = () => {
        const db = req.result;
        if (abandoned) { db.close(); return; }
        db.onversionchange = () => { db.close(); this.opening = null; };
        resolve(db);
      };
    }).catch(error => { this.opening = null; throw error; });
    return this.opening;
  }
  id(scope, key) { return scope + ':' + key; }
  async transaction(mode, work) {
    const db = await this.open();
    return new Promise((resolve, reject) => {
      let tx;
      try { tx = db.transaction('files', mode, {durability:'strict'}); }
      catch (_) { tx = db.transaction('files', mode); }
      let result, failure;
      // A successful put request is NOT a committed file: wait for oncomplete.
      tx.oncomplete = () => resolve(result);
      tx.onabort = () => reject(failure || tx.error || new Error('本机暂存事务未完成'));
      tx.onerror = () => { /* onabort owns the failure, including quota errors */ };
      try { work(tx.objectStore('files'), value => { result = value; }, error => { failure=error; tx.abort(); }); }
      catch (error) { failure=error; tx.abort(); }
    });
  }
  list(scope) {
    return this.transaction('readonly',(store,done) => {
      const req=store.getAll(); req.onsuccess=()=>done(req.result.filter(x=>x.scope===scope));
    });
  }
  get(scope,key) {
    return this.transaction('readonly',(store,done)=>{const req=store.get(this.id(scope,key));req.onsuccess=()=>done(req.result);});
  }
  stage(scope,row) {
    if (!row.file || !Number.isFinite(row.file.size) || row.file.size !== row.size) return Promise.reject(new Error('无法读取完整原文件，请重新选择'));
    return this.transaction('readwrite',(store,done,fail)=>{
      const id=this.id(scope,row.key),req=store.getAll();
      req.onsuccess=()=>{
        const previous=req.result.find(x=>x.id===id);
        // A stale window must never overwrite a confirmed upload receipt.
        if (previous?.documentId) { done(previous); return; }
        if (previous?.blob) { done(previous); return; }
        const used=req.result.reduce((n,x)=>n+(x.blob?.size || 0),0);
        if (used+row.size>this.maxBytes) { fail(new Error('本机待导入暂存已达到 200MB。请先继续导入或移除不需要的暂存文件，再重试。')); return; }
        const saved={id,scope,key:row.key,name:row.name,size:row.size,ext:row.ext,fingerprint:row.fingerprint,
          blob:row.file,created:Date.now(),status:'staged'};
        store.put(saved);done(saved);
      };
    });
  }
  acknowledge(scope,key,document) {
    return this.transaction('readwrite',(store,done)=>{
      const req=store.get(this.id(scope,key));
      req.onsuccess=()=>{
        const saved=req.result;
        if (saved) store.put({...saved,blob:null,documentId:document.id,status:document.status,
          error:document.error || '',sha256:document.sha256,reused:Boolean(document.reused)});
        done();
      };
    });
  }
  remove(scope,key) { return this.transaction('readwrite',(store,done)=>{store.delete(this.id(scope,key));done();}); }
  removeDocument(scope,documentId) {
    return this.transaction('readwrite',(store,done)=>{
      const req=store.getAll();req.onsuccess=()=>{
        for (const saved of req.result) if (saved.scope===scope&&saved.documentId===documentId&&!saved.blob) store.delete(saved.id);
        done();
      };
    });
  }
}
if (typeof module !== 'undefined') module.exports = PocketImportCache;

/* Copyright 2026 AshleySama
 * SPDX-License-Identifier: Apache-2.0
 */

/* File bytes are staged before upload; receipts remain library-scoped. */
class PocketImportQueue {
  constructor({api, read, library, changed, storage, hash, cache}) {
    Object.assign(this, {api, read, library, changed, storage, hash, cache});
    this.rows=[]; this.scope=null; this.running=false; this.staging=0; this.capturing=0; this.loading=null;
    this.storageError=false; this.cacheError='';
  }
  get busy() { return this.running || this.staging>0 || this.capturing>0 || Boolean(this.loading); }
  async activate(reload=false) {
    const scope=this.library();
    if (!scope) throw new Error('资料库尚未就绪，请稍后重试。');
    if (this.loading) { await this.loading; return this.activate(reload); }
    if (scope===this.scope && !reload) return;
    if (scope!==this.scope && (this.running||this.staging>0)) throw new Error('原资料库的文件仍在暂存或提交，请稍后重试。');
    if (scope!==this.scope) {
      this.scope=scope; this.rows=[];
      try {
        const saved=JSON.parse(this.storage?.getItem('pm-import-receipts:'+scope)||'[]');
        this.rows=(Array.isArray(saved)?saved:[]).filter(x=>x&&typeof x.name==='string').map(x=>({
          ...x,file:null,staged:false,retrying:false,
          status:x.documentId||x.status==='rejected'?x.status:'needs_file',
          error:x.documentId||x.status==='rejected'?x.error:'未找到可恢复的暂存原件，请重新选择原文件。',
        }));
      } catch (_) { this.storageError=true; }
    }
    const load=(async()=>{
      if (!this.cache) return;
      try {
        const saved=await this.cache.list(scope);
        if (this.scope!==scope || this.library()!==scope) return;
        for (const item of saved) {
          let row=this.rows.find(x=>x.key===item.key);
          if (!row) { row={key:item.key,name:item.name,size:item.size,ext:item.ext,fingerprint:item.fingerprint};this.rows.push(row); }
          if (item.documentId) Object.assign(row,{documentId:item.documentId,status:item.status,error:item.error||'',sha256:item.sha256,file:null,staged:false,reused:item.reused});
          else if (item.blob && !row.documentId && !row.file) Object.assign(row,{file:item.blob,staged:true,status:'staged',error:''});
        }
        this.cacheError='';
      } catch (error) { this.cacheError=`无法读取本机暂存：${error.message}。请刷新状态重试；不会清理未读出的暂存文件。`; }
    })();
    this.loading=load;
    try { await load; } finally { if (this.loading===load) this.loading=null; this.notify(); }
  }
  notify() {
    try {
      const pending=this.rows.filter(x=>!['ready','rejected'].includes(x.status));
      const recent=this.rows.filter(x=>['ready','rejected'].includes(x.status)).slice(-20);
      this.storage?.setItem('pm-import-receipts:'+this.scope,JSON.stringify([...pending,...recent].map(({file,...x})=>x)));
    } catch (_) { this.storageError=true; }
    this.changed();
  }
  async add(files) {
    this.capturing++;
    try {
    await this.activate();
    const added=[];
    for (const file of files) {
      const name=file.webkitRelativePath||file.name;
      if (this.rows.some(x=>x.file===file&&['staging','queued','uploading'].includes(x.status))) continue;
      const fingerprint=`${name}:${file.size}:${file.lastModified}`,ext=file.name.split('.').pop().toLowerCase();
      // Metadata alone never authorizes overwriting a previously staged original.
      let row=this.rows.find(x=>x.fingerprint===fingerprint&&x.status==='needs_file'&&!x.file);
      const reason=!['txt','docx','xlsx','pdf'].includes(ext)?'格式不支持。请选择 TXT、DOCX、XLSX 或 PDF。'
        :file.size>10*1024*1024?'文件超过 10MB，请拆分后再导入。':file.size===0?'文件内容为空，请检查后重新选择。':'';
      if (!row) { row={key:globalThis.crypto?.randomUUID?.()||`${Date.now()}:${Math.random()}`,fingerprint,ext,name,size:file.size};this.rows.push(row); }
      Object.assign(row,{file:reason?null:file,status:reason?'rejected':'staging',error:reason,staged:false});
      if (!reason) added.push(row);
    }
    this.staging++; this.notify();
    try { for (const row of added) if (await this.stage(row)) row.status='queued'; }
    finally { this.staging--; this.notify(); }
    return this.drain();
    } finally { this.capturing--; this.notify(); }
  }
  async stage(row) {
    if (!this.cache) return true;
    try {
      const saved=await this.cache.stage(this.scope,row);
      if (saved.documentId) { this.adopt(row,{...saved,id:saved.documentId});return false; }
      row.file=saved.blob;row.staged=true;row.error='';this.notify();return true;
    } catch (error) {
      row.staged=false;row.status='stage_failed';row.error=`尚未暂存：${error.message}。请保持此页面打开后重试。`;this.notify();return false;
    }
  }
  adopt(row,result) {
    Object.assign(row,{documentId:result.id,status:result.status,error:result.error||'',sha256:result.sha256||row.sha256,reused:result.reused,file:null,staged:false});
  }
  async acknowledge(row,result,scope) {
    if (!this.cache) return;
    try { await this.cache.acknowledge(scope,row.key,result); row.cleanupError=''; }
    catch (_) { row.cleanupError='原件已入库，暂存副本尚未清理。刷新状态后可重试清理，不会重复入库。'; }
  }
  async drain() {
    if (this.running || this.staging>0) return;
    this.running=true;
    try {
      let row;
      while ((row=this.rows.find(x=>x.status==='queued'))) {
        const scope=this.scope;
        if (this.library()!==scope) break;
        row.status='uploading';row.error='';this.notify();
        try {
          if (this.cache && row.staged) {
            const saved=await this.cache.get(scope,row.key);
            if (saved?.documentId) { this.adopt(row,{...saved,id:saved.documentId});this.notify();continue; }
            if (!saved?.blob) { row.file=null;row.staged=false;row.status='needs_file';row.error='暂存文件已被移除，请重新选择原文件。';this.notify();continue; }
            row.file=saved.blob;
          }
          const data=await this.read(row.file);
          if (this.hash) { try { row.sha256=await this.hash(data); } catch (_) { row.sha256=null; }this.notify(); }
          if (this.library()!==scope) throw new Error('资料库已改变，请切回原资料库后重试。');
          const result=await this.api('/api/documents',{method:'POST',headers:{'X-PocketMemory-Library':scope},body:JSON.stringify({name:row.name,data_url:data,chunk_profile:'standard'})});
          this.adopt(row,result);
          await this.acknowledge(row,result,scope);
          const duplicates=this.rows.filter(x=>x!==row&&x.documentId===result.id);
          for (const duplicate of duplicates) await this.acknowledge(duplicate,result,scope);
          this.rows=this.rows.filter(x=>x===row||x.documentId!==result.id);
        } catch (error) {
          if (!row.documentId) { row.status='upload_failed';row.error=`未确认提交结果：${error.message}。${row.staged?'原件已暂存本机，重开后可继续导入。':'原文件仍在此页面，可安全重试。'}`; }
        }
        this.notify();
      }
    } finally { this.running=false;this.notify(); }
  }
  async retry(key) {
    await this.activate();
    const row=this.rows.find(x=>x.key===key);
    if (!row || this.busy || row.retrying) return;
    if (!row.documentId) {
      if (!row.file) { row.status='needs_file';this.notify();return; }
      if (!row.staged && this.cache) {
        this.staging++;row.status='staging';this.notify();
        let saved;try { saved=await this.stage(row); } finally { this.staging--;this.notify(); }
        if (!saved) return;
      }
      row.status='queued';this.notify();return this.drain();
    }
    if (row.status!=='failed') return;
    row.retrying=true;row.error='';this.notify();
    try {
      const result=await this.api(`/api/documents/${row.documentId}/reindex`,{method:'POST',body:'{}',headers:{'X-PocketMemory-Library':this.scope}});
      row.status=result.status;row.error=result.error||'';
    } catch(error) { row.error=`重试未确认：${error.message}。已保存的原件不会删除，请刷新状态后再试。`; }
    finally { row.retrying=false;this.notify(); }
  }
  async resume() {
    await this.activate(true);
    if (this.busy) return;
    for (const row of this.rows) if (!row.documentId&&row.staged&&['staged','upload_failed'].includes(row.status)) row.status='queued';
    this.notify();return this.drain();
  }
  async reconcile(documents, expectedScope=this.library()) {
    if (expectedScope!==this.library()) return;
    await this.activate();
    const scope=this.scope;
    if (scope!==expectedScope||scope!==this.library()) return;
    for (const doc of documents) {
      const matches=this.rows.filter(x=>x.documentId===doc.id||(!x.documentId&&x.sha256&&x.sha256===doc.sha256&&x.ext===doc.file_type));
      if (!matches.length&&['failed','pending','extracting','indexing'].includes(doc.status)) {
        const row={key:'saved:'+doc.id,documentId:doc.id,name:doc.original_name,size:doc.size_bytes,file:null};this.rows.push(row);matches.push(row);
      }
      for (const row of matches) {
        if (/FileNotFoundError|原文件暂不可用/.test(doc.error||'')&&row.staged) continue;
        this.adopt(row,{...doc,reused:row.reused});await this.acknowledge(row,doc,scope);
        if (this.scope!==scope||this.library()!==scope) return;
      }
      if (matches.length>1) this.rows=this.rows.filter(x=>x===matches[0]||!matches.includes(x));
    }
    this.notify();
  }
  async clearCompleted() {
    const scope=this.scope;
    const completed=this.rows.filter(x=>['ready','rejected'].includes(x.status));
    for (const row of completed) {
      if (this.cache) {
        if (row.documentId) await this.cache.removeDocument(scope,row.documentId);
        else await this.cache.remove(scope,row.key);
      }
      this.rows=this.rows.filter(x=>x!==row);
    }
    this.notify();
  }
  async dismiss(key) {
    const row=this.rows.find(x=>x.key===key);
    if (!row||row.documentId||['staging','queued','uploading'].includes(row.status)) return;
    if (this.cache) await this.cache.remove(this.scope,key);
    this.rows=this.rows.filter(x=>x!==row);this.notify();
  }
  async forget(documentId, scope=this.scope) {
    try {
      if (this.cache) await this.cache.removeDocument(scope,documentId);
    } catch (_) { this.cacheError='文件已删除，部分暂存记录未能清理，请刷新状态后重试。'; }
    if (this.scope===scope) this.rows=this.rows.filter(x=>x.documentId!==documentId);
    this.notify();
  }
  hasUnsaved() { return this.capturing>0||this.staging>0||this.rows.some(x=>!x.documentId&&x.file&&!x.staged); }
}
if (typeof module!=='undefined') module.exports=PocketImportQueue;

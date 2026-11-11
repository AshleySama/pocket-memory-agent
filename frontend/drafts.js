/* Copyright 2026 AshleySama
 * SPDX-License-Identifier: Apache-2.0
 */

/* Local recovery journal. It never writes to notes or runs indexing/AI. */
class PocketDraftVault {
  constructor({api, storage, changed = () => {}, delay = 400}) {
    this.api = api; this.storage = storage; this.changed = changed; this.delay = delay;
    this.library = null; this.epoch = 0; this.records = new Map(); this.active = new Map();
    this.jobs = new Map(); this.pending = new Map(); this.error = ''; this.loading = false;
    this.prefix = 'pocket-memory-draft-v1:';
  }
  key(id, library = this.library) { return `${this.prefix}${library}:${id}`; }
  localWrite(record, pending = false) {
    try { this.storage.setItem(this.key(record.draft_id, record.library_id), JSON.stringify({...record,_pending:pending})); return true; }
    catch (_) { return false; }
  }
  localRemove(id, library = this.library) { try { this.storage.removeItem(this.key(id, library)); } catch (_) {} }
  notify() { this.changed(this); }
  async init() {
    const epoch = ++this.epoch;
    for (const e of this.jobs.values()) clearTimeout(e.timer);
    this.active.clear(); this.jobs.clear(); this.records.clear(); this.library = null; this.loading = true; this.error = ''; this.notify();
    try {
      const data = await this.api('/api/drafts');
      if (epoch !== this.epoch) return false;
      this.library = data.library_id;
      for (const r of data.drafts) this.records.set(r.draft_id, {...r, library_id:this.library});
      const local = [];
      let localLength=0;try{localLength=this.storage?.length||0;}catch(_){}
      for (let i=0;i<localLength;i++) {
        const key=this.storage.key(i);
        if (!key?.startsWith(`${this.prefix}${this.library}:`)) continue;
        try { const r=JSON.parse(this.storage.getItem(key)); if(r.library_id===this.library && r.draft_id) local.push(r); } catch (_) {}
      }
      for(const cached of local) {
        const {_pending,...r}=cached;
        if((data.closed_ids||[]).includes(r.draft_id)) {
          this.localRemove(r.draft_id);
          if(!r.closed && _pending) { r.draft_id=crypto.randomUUID();r.revision=1; }
          else continue;
        }
        const old=this.records.get(r.draft_id);
        if (r.closed) { this.records.delete(r.draft_id); this.sendClose(r); }
        else if(!old || r.revision >= old.revision) {
          this.records.set(r.draft_id,r);
          const e={record:r, dirty:true, local:true, status:'pending'};
          this.jobs.set(r.draft_id,e); this.flush(e);
        }
      }
      this.loading=false;
      for(const [key,payload] of this.pending) this.capture(key,payload);
      this.pending.clear();this.notify();return true;
    } catch(error) {
      if(epoch!==this.epoch)return false;
      this.loading=false;this.error=error.message;this.notify();return false;
    }
  }
  capture(key,payload) {
    if(!this.library) { this.pending.set(key,payload); this.notify();return; }
    let e=this.active.get(key);
    const signature=JSON.stringify(payload);
    if(e && e.signature===signature)return e;
    if(!e) {
      e={record:{draft_id:crypto.randomUUID(),library_id:this.library,revision:0},dirty:false,status:'pending'};
      this.active.set(key,e);this.jobs.set(e.record.draft_id,e);
    }
    e.record={...e.record,...payload,revision:e.record.revision+1};e.signature=signature;
    e.dirty=true;e.status='pending';e.local=this.localWrite(e.record,true);
    this.records.set(e.record.draft_id,e.record);
    clearTimeout(e.timer);e.timer=setTimeout(()=>this.flush(e),this.delay);this.notify();return e;
  }
  adopt(key,record) {
    // Fork the recovered snapshot so another open window cannot overwrite it.
    this.active.delete(key);
    const {draft_id,revision,library_id,created_at,updated_at,...payload}=record;
    const e=this.capture(key,payload);if(e)e.replaces=draft_id;
  }
  async flush(e) {
    if(!e || e.closed || !e.dirty)return true;
    if(e.sending) { await e.sending; return e.closed || !e.dirty ? true : this.flush(e); }
    const epoch=this.epoch, sent={...e.record}; clearTimeout(e.timer);
    e.status='writing';this.notify();
    e.sending=(async()=>{
      try {
        const result=await this.api('/api/drafts',{method:'POST',body:JSON.stringify(sent)});
        if(epoch!==this.epoch || e.closed)return true;
        if(e.record.revision===sent.revision) {
          e.dirty=false;e.status='saved';e.record={...result.draft,library_id:sent.library_id};
          this.records.set(sent.draft_id,e.record);this.localWrite(e.record);
          if(e.replaces) { const old=e.replaces;delete e.replaces;await this.discard(old); }
        }
        return true;
      } catch(error) {
        if(epoch!==this.epoch || e.closed)return true;
        e.status='error';e.error=error.message;return false;
      } finally { if(epoch===this.epoch)this.notify(); }
    })();
    const ok=await e.sending;e.sending=null;
    if(ok && e.dirty && !e.closed && epoch===this.epoch)return this.flush(e);
    return ok;
  }
  async flushAll() {
    if(!this.library)return false;
    const results=await Promise.all([...this.jobs.values()].filter(e=>!e.closed).map(e=>this.flush(e)));
    return results.every(Boolean);
  }
  async park(key, canRelease = () => true) {
    // Navigation releases the editor slot, never the persisted recovery copy.
    // A later blank/new editor must not close or overwrite this draft.
    if (!this.library || this.pending.has(key)) return false;
    const e=this.active.get(key), epoch=this.epoch;
    if (!e) return canRelease();
    if (!await this.flush(e) || epoch!==this.epoch || this.active.get(key)!==e || e.closed || e.dirty || !canRelease()) return false;
    this.active.delete(key);this.notify();return true;
  }
  close(key) {
    this.pending.delete(key);const e=this.active.get(key);this.active.delete(key);
    if(e)return this.discard(e.record.draft_id);
    return Promise.resolve(true);
  }
  async discard(id) {
    const record=this.records.get(id)||this.jobs.get(id)?.record;
    if(!record)return true;
    const e=this.jobs.get(id);if(e){e.closed=true;clearTimeout(e.timer);}
    for(const[k,v]of this.active)if(v.record.draft_id===id)this.active.delete(k);
    const tomb={library_id:record.library_id||this.library,draft_id:id,closed:true};
    this.localWrite(tomb);this.records.delete(id);this.notify();return this.sendClose(tomb);
  }
  async sendClose(tomb) {
    try {
      await this.api(`/api/drafts/${encodeURIComponent(tomb.draft_id)}`,{method:'DELETE',body:JSON.stringify({library_id:tomb.library_id})});
      this.localRemove(tomb.draft_id,tomb.library_id);return true;
    }catch(error){this.error=`草稿清理待重试：${error.message}`;this.notify();return false;}
  }
  async retry() {
    if(!this.library)return this.init();
    this.error='';
    for(let i=(this.storage?.length||0)-1;i>=0;i--){
      const key=this.storage.key(i);if(!key?.startsWith(`${this.prefix}${this.library}:`))continue;
      try{const r=JSON.parse(this.storage.getItem(key));if(r.closed)await this.sendClose(r);}catch(_){}
    }
    const ok=await this.flushAll();this.notify();return ok;
  }
  detach() {
    ++this.epoch;for(const e of this.jobs.values())clearTimeout(e.timer);
    this.library=null;this.active.clear();this.jobs.clear();this.records.clear();this.pending.clear();this.notify();
  }
  status(key) {
    const e=this.active.get(key);
    if(!this.library)return this.error?'草稿保护未连接，请保持页面打开':'正在连接草稿保护…';
    if(!e)return this.records.size ? `有 ${this.records.size} 份草稿可恢复` : '草稿自动保存在本机';
    if(e.status==='saved')return '草稿已保存到本机';
    if(e.status==='error')return e.local?'浏览器已暂存，本机写入失败，请重试':'草稿未保存，请勿关闭页面';
    return e.local?'浏览器已暂存，正在写入本机…':'正在保存草稿，请勿关闭页面…';
  }
  unsafe() { return this.pending.size>0 || [...this.active.values()].some(e=>e.dirty && !e.local); }
  pagehide() {
    for(const e of this.jobs.values()) {
      if(!e.dirty || e.closed)continue;
      this.localWrite(e.record,true);
      // Best effort only; the ongoing journal and local snapshot are primary.
      try { navigator.sendBeacon('/api/drafts',new Blob([JSON.stringify(e.record)],{type:'application/json'})); } catch(_) {}
    }
  }
}
window.PocketDraftVault=PocketDraftVault;

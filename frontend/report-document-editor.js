/* Copyright 2026 AshleySama | SPDX-License-Identifier: Apache-2.0 */
'use strict';
// Shares the note editor's native rich-text commands; all persisted HTML is
// independently sanitized and citation IDs are validated by the server.
window.PocketReportEditor = class PocketReportEditor {
  constructor(options) {
    this.options=options;this.active=false;this.saving=false;this.dirty=false;this.range=null;
    this.host=document.querySelector('#report');this.bar=document.querySelector('#document-editor-bar');
    this.status=document.querySelector('#document-edit-status');this.dialog=document.querySelector('#document-choice');
    document.querySelector('#edit-document').onclick=()=>this.active?this.save(true):this.start();
    document.querySelector('#save-document').onclick=()=>this.save(false);
    document.querySelector('#cancel-document-edit').onclick=()=>this.cancel();
    document.querySelector('#undo-document-save').onclick=()=>this.undoSave();
    this.bar.addEventListener('mousedown',e=>{if(e.target.closest('button'))e.preventDefault();});
    this.bar.addEventListener('click',e=>{const b=e.target.closest('[data-doc-command]');if(b)this.command(b.dataset.docCommand,b.dataset.docValue);});
    this.bar.querySelector('[data-doc-format]').onchange=e=>{this.command('formatBlock',e.target.value);e.target.value='p';};
    this.bar.querySelector('[data-doc-color]').onclick=e=>{
      if(!this.active||this.saving)return;
      const trigger=e.currentTarget;
      window.PMColorPalette.open({trigger,title:'文字颜色',kind:'text',defaultValue:'#253439',
        value:document.queryCommandValue('foreColor')||trigger.dataset.colorValue,
        onPick:color=>this.command('foreColor',color)});
    };
    document.addEventListener('selectionchange',()=>{
      if(!this.active)return;const s=window.getSelection();
      if(s?.rangeCount&&this.body?.contains(s.anchorNode)&&this.body.contains(s.focusNode))this.range=s.getRangeAt(0).cloneRange();
    });
    window.addEventListener('beforeunload',e=>{if(this.active&&this.dirty){this.persist();e.preventDefault();e.returnValue='';}});
    document.addEventListener('keydown',e=>{if(this.active&&(e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='s'){e.preventDefault();this.save(false);}});
    document.querySelectorAll('.appbar a[href="/"]').forEach(link=>link.addEventListener('click',async e=>{
      if(!this.active)return;e.preventDefault();if(await this.leave())location.assign(link.href);
    }));
  }
  report(){return this.options.report();}
  key(){const r=this.report();return `pm-report-document:${r.id}:${r.created||''}`;}
  value(){return {title:this.title.value,document_html:this.body.innerHTML};}
  choice(title,message,choices) {
    return new Promise(resolve=>{
      this.dialog.querySelector('h2').textContent=title;this.dialog.querySelector('p').textContent=message;
      const actions=this.dialog.querySelector('.dialog-footer');actions.replaceChildren();
      for(const [value,label,primary] of choices){const b=document.createElement('button');b.type='button';b.textContent=label;if(primary)b.className='primary';b.onclick=()=>this.dialog.close(value);actions.append(b);}
      this.dialog.returnValue='';this.dialog.addEventListener('close',()=>resolve(this.dialog.returnValue),{once:true});this.dialog.showModal();
    });
  }
  persistedDraft(){try{return JSON.parse(localStorage.getItem(this.key())||'null');}catch{return null;}}
  persist(){
    if(!this.active||!this.dirty)return true;
    try{localStorage.setItem(this.key(),JSON.stringify({...this.value(),version:this.report().version}));return true;}
    catch{this.status.textContent='草稿暂存失败，请保存到本机后再离开';this.status.dataset.error='true';return false;}
  }
  clearDraft(){try{localStorage.removeItem(this.key());}catch{}}
  cleanBody(html) {
    const box=document.createElement('div');box.innerHTML=html;
    box.querySelectorAll('.report-edited-body').forEach(x=>x.replaceWith(...x.childNodes));
    box.querySelectorAll('.report-heading,.adaptive-intro,.report-toc,.appendix,#report-source-appendix,button,[data-edit],.document-edit-notice').forEach(x=>x.remove());
    // Old local drafts can still carry visible citation labels. Keep their
    // identities for server validation, without inserting them into the prose.
    box.querySelectorAll('a.cite[data-ref]').forEach(a=>{
      if(/^[1-9][0-9]*$/.test(a.dataset.ref)&&a.getAttribute('href')===`#source-${a.dataset.ref}`){a.textContent='';a.hidden=true;}
    });
    [...box.querySelectorAll('.source-citation,.chapter-citations')].reverse().forEach(x=>{
      if(!x.textContent.trim()&&!x.querySelector('img,svg,table,hr,br'))x.hidden=true;
    });
    // Strip only presentation controls; retained paragraphs, tables and quotes
    // remain editable in their original order.
    return box.innerHTML;
  }
  async start(useDraft=true) {
    if(this.active||!this.report()||this.options.blocked())return;
    let draft=useDraft?this.persistedDraft():null;
    if(draft&&draft.version!==this.report().version){
      const action=await this.choice('发现旧版本草稿',`草稿基于 v${draft.version}，当前文稿是 v${this.report().version}。恢复后请核对全文，再决定是否保存。`,[['','取消'],['latest','编辑已保存版本'],['draft','恢复草稿']]);
      if(!action)return;
      if(action==='latest'){
        // Archive the stale draft before starting another one; do not silently
        // overwrite the only recoverable copy after a version conflict.
        try{localStorage.setItem(this.key()+`:archived-v${draft.version}`,JSON.stringify(draft));}catch{this.options.toast('旧草稿无法暂存，请先复制草稿内容');return;}
        draft=null;
      }
    }
    this.originalHTML=this.host.innerHTML;this.originalTitle=this.report().title;
    this.active=true;this.range=null;document.body.classList.remove('present');document.body.classList.add('document-editing');
    const head=document.createElement('header');head.className='report-heading document-edit-heading';
    const label=document.createElement('label');label.className='document-title-label';label.textContent='文稿标题';
    this.title=document.createElement('input');this.title.className='document-title-input';this.title.maxLength=100;this.title.setAttribute('aria-label','文稿标题');this.title.value=draft?.title??this.originalTitle;label.append(this.title);head.append(label);
    this.body=document.createElement('div');this.body.id='document-editor';this.body.className='document-editor';this.body.contentEditable='true';this.body.setAttribute('role','textbox');this.body.setAttribute('aria-label','全文正文');this.body.setAttribute('aria-multiline','true');this.body.spellcheck=false;
    this.body.innerHTML=this.cleanBody(draft?.document_html??this.originalHTML);this.body.querySelectorAll('[data-ref]').forEach(a=>a.contentEditable='false');
    this.host.replaceChildren(head,this.body);this.bar.hidden=false;this.dirty=!!draft;this.baseline=JSON.stringify({title:this.originalTitle,document_html:this.cleanBody(this.originalHTML)});
    this.body.addEventListener('input',()=>this.changed());this.title.addEventListener('input',()=>this.changed());
    this.body.addEventListener('paste',e=>{e.preventDefault();document.execCommand('insertText',false,e.clipboardData?.getData('text/plain')||'');this.changed();});
    this.body.addEventListener('drop',e=>{e.preventDefault();this.options.toast('请用粘贴或编辑栏添加内容');});
    this.body.addEventListener('click',e=>{const link=e.target.closest('a');if(link&&!link.dataset.ref)e.preventDefault();});
    this.setStatus(draft?'已恢复未保存草稿':'全文编辑 · Ctrl+S 保存');this.sync();this.options.outline();this.body.focus();
  }
  setStatus(text,error=false){this.status.textContent=text;this.status.dataset.error=String(error);}
  changed(){
    this.dirty=JSON.stringify(this.value())!==this.baseline;
    this.setStatus(this.dirty?'未保存 · 草稿已暂存':'全文编辑 · 无新修改');this.persist();
    clearTimeout(this.outlineTimer);this.outlineTimer=setTimeout(()=>this.options.outline(),400);
  }
  sync(){
    const blocked=!this.report()||this.options.blocked();
    const edit=document.querySelector('#edit-document');edit.disabled=blocked||this.saving;edit.textContent=this.active?'完成编辑':'编辑全文';
    document.querySelector('#undo-document-save').hidden=!this.report()?.last_document_edit;
    document.querySelector('#undo-document-save').disabled=this.active||this.saving;
    document.querySelector('#save-document').disabled=this.saving;document.querySelector('#cancel-document-edit').disabled=this.saving;
    for(const id of ['reading','rename','export']){const b=document.querySelector('#'+id);if(this.active)b.disabled=true;}
    if(this.body)this.body.contentEditable=String(!this.saving);
    if(this.title)this.title.disabled=this.saving;
    this.bar.querySelectorAll('[data-doc-command],[data-doc-color],select,input').forEach(x=>x.disabled=this.saving);
  }
  restoreSelection(){
    this.body.focus();const sel=window.getSelection();
    if(this.range&&this.body.contains(this.range.commonAncestorContainer)){sel.removeAllRanges();sel.addRange(this.range);}
    else{const r=document.createRange();r.selectNodeContents(this.body);r.collapse(false);sel.removeAllRanges();sel.addRange(r);}
  }
  async command(command,value){
    if(!this.active||this.saving)return;
    if(command==='citation'){
      const select=document.querySelector('#document-citation-select');select.replaceChildren();
      for(const ref of this.report().refs||[]){const o=document.createElement('option');o.value=ref.id;o.textContent=`[${ref.id}] ${ref.title} · ${ref.tab||''}`;select.append(o);}
      if(!select.options.length){this.options.toast('这份文稿没有可插入的原文引用');return;}
      const dialog=document.querySelector('#document-citation-dialog'),preview=dialog.querySelector('blockquote');
      select.onchange=()=>{preview.textContent=(this.report().refs||[]).find(r=>r.id===Number(select.value))?.quote||'';};select.onchange();
      dialog.returnValue='';dialog.showModal();await new Promise(resolve=>dialog.addEventListener('close',resolve,{once:true}));
      if(dialog.returnValue!=='insert')return;this.restoreSelection();
      const id=Number(select.value);document.execCommand('insertHTML',false,`<a class="cite" data-ref="${id}" href="#source-${id}" contenteditable="false" aria-label="查看引用 ${id}">[${id}]</a> `);
    }else{
      this.restoreSelection();
      if(command==='table')document.execCommand('insertHTML',false,'<table><thead><tr><th>项目</th><th>说明</th><th>备注</th></tr></thead><tbody><tr><td>内容</td><td><br></td><td><br></td></tr><tr><td><br></td><td><br></td><td><br></td></tr></tbody></table><p><br></p>');
      else if(command==='removeTable'){
        const node=window.getSelection()?.anchorNode;const table=(node?.nodeType===1?node:node?.parentElement)?.closest('table');
        if(table&&this.body.contains(table)){const range=document.createRange();range.selectNode(table);const sel=window.getSelection();sel.removeAllRanges();sel.addRange(range);document.execCommand('delete');}
      }else document.execCommand(command,false,value||null);
    }
    this.changed();
  }
  stop(){window.PMColorPalette?.close();this.active=false;this.dirty=false;this.range=null;this.bar.hidden=true;document.body.classList.remove('document-editing');this.body=null;this.title=null;}
  beforeDisplay(){if(this.active)this.persist();this.stop();}
  async save(close=false){
    if(!this.active||this.saving)return false;
    if(!this.dirty){if(close){const html=this.originalHTML;this.stop();this.host.innerHTML=html;this.options.restore();}return true;}
    const payload={version:this.report().version,...this.value()};
    if(!payload.title.trim()){this.setStatus('请输入文稿标题',true);this.title.focus();return false;}
    this.saving=true;this.setStatus('正在保存全文…');this.sync();
    try{
      const data=await this.options.save(payload);this.clearDraft();this.dirty=false;this.stop();
      await this.options.committed(data);this.saving=false;
      if(!close)await this.start(false);this.options.toast('全文已保存');return true;
    }catch(e){this.setStatus('保存失败：'+e.message+'；草稿仍保留',true);this.persist();return false;}
    finally{this.saving=false;this.sync();}
  }
  async leave(){
    if(!this.active)return true;if(this.saving){this.options.toast('正在保存，请稍候');return false;}
    if(!this.dirty){this.stop();this.options.restore();return true;}
    const choice=await this.choice('还有未保存的全文修改','可以保存后继续，或将修改保留为本机草稿。', [['','继续编辑'],['draft','保留草稿并离开'],['save','保存并继续',true]]);
    if(choice==='save')return this.save(true);
    if(choice==='draft'&&this.persist()){this.stop();this.options.restore();return true;}
    return false;
  }
  async cancel(){
    if(this.saving)return;
    if(this.dirty&&await this.choice('放弃本次修改？','回到上一次保存的文稿；已经保存的正文不会删除。',[['','继续编辑'],['discard','放弃修改']])!=='discard')return;
    const html=this.originalHTML;this.clearDraft();this.stop();this.host.innerHTML=html;this.options.restore();
  }
  async undoSave(){
    if(this.active||this.saving||!this.report()?.last_document_edit)return;
    if(await this.choice('撤回上次保存？','文稿将恢复到上次人工保存之前，原始资料和引用快照保持不变。',[['','取消'],['undo','撤回上次保存',true]])!=='undo')return;
    this.saving=true;this.sync();
    try{const data=await this.options.save({version:this.report().version,undo_document_edit:true});await this.options.committed(data);this.options.toast('已撤回上次保存');}
    catch(e){this.options.toast('撤回未完成：'+e.message);}finally{this.saving=false;this.sync();}
  }
};

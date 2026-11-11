/* Copyright 2026 AshleySama
 * SPDX-License-Identifier: Apache-2.0
 */

function draftKey(id,field='content') { return `note:${id}:${field}`; }
function draftPayload(note,content,field='content') {
  return {kind:'note',edit_field:field,note_id:Number(note.id),parent_note_id:note.parent_note_id||null,
    title:note.title||'未命名笔记',content,base_content:String(note[field]||''),
    base_updated_at:note.updated_at||'',base_created_at:note.created_at||'',folder_id:note.folder_id||null};
}
function capturePersistentEditor(content,session) {
  if(!content || !session)return;
  const id=Number(content.dataset.noteId),key=draftKey(id);
  // The server may already have committed a pending PUT, even before its
  // response arrives (or if that response fails). Keep the user's last edit.
  if(session.draft===session.saved && !session.writeUncertain && !state.pendingContentSaves.has(id) && !['saving','error'].includes(session.status)) { void draftVault.close(key);return; }
  const note=state.draftNoteSources.get(id)||state.allNotes.find(n=>Number(n.id)===id);
  if(!note)return;
  draftVault.capture(key,{...draftPayload(note,session.draft),base_content:session.baseContent ?? note.content});
}
function capturePersistentNew() {
  const d=state.newNoteDraft;if(!d)return;
  if(!String(d.title||'').trim() && !String(d.content||'').trim() && !state.imageDataUrl) { void draftVault.close('new');return; }
  draftVault.capture('new',{kind:'new',edit_field:'content',note_id:null,parent_note_id:null,title:d.title||'',content:d.content||'',
    base_content:'',base_created_at:'',base_updated_at:'',folder_id:d.folder_id||null,
    image_data_url:state.imageDataUrl||null,image_name:state.imageName||null});
}
function currentDraftKey() {
  const rename=document.querySelector('[data-draft-rename]');
  if(rename)return rename.dataset.draftRename;
  return state.newNoteDraft?'new':draftKey(Number(currentNoteEditor()?.dataset.noteId)||0);
}
function paintDraftProtection() {
  const label=document.querySelector('#draft-protection-status'),button=document.querySelector('#open-drafts');
  if(!label || !button)return;
  label.textContent=draftVault.status(currentDraftKey());
  label.dataset.error=String(Boolean(draftVault.error || [...draftVault.jobs.values()].some(e=>e.status==='error'&&!e.closed)));
  button.textContent=`草稿箱${draftVault.records.size?`（${draftVault.records.size}）`:''}`;
  document.querySelector('#retry-drafts').hidden=label.dataset.error!=='true';
  const status=document.querySelector('.new-note-editor .note-save-status');
  if(status && !state.newNoteSaving)status.textContent=(state.newNoteDraft?.saveError?'笔记提交失败；':'')+draftVault.status('new');
  if(document.querySelector('#draft-dialog')?.open)renderDraftList();
}
function renderDraftList() {
  const list=document.querySelector('#draft-list');if(!list)return;
  list.replaceChildren();
  const items=[...draftVault.records.values()].sort((a,b)=>String(b.updated_at||'').localeCompare(String(a.updated_at||'')));
  const count=document.querySelector('#draft-dialog-count');
  if(count){count.hidden=!items.length;count.textContent=String(items.length);}
  const dialog=document.querySelector('#draft-dialog');if(dialog)dialog.dataset.empty=String(!items.length);
  if(!items.length){
    const empty=document.createElement('div');empty.className='draft-empty';
    empty.innerHTML='<span class="draft-empty-icon" aria-hidden="true"><svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M14 7h14l9 9v23a2 2 0 0 1-2 2H14a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2Z"/><path d="M28 7v9h9M19 24h11M19 30h8"/></svg></span><h3>暂时没有草稿</h3><p>未完成的笔记会自动保留在这里，<br>下次可以接着写。</p>';
    list.append(empty);return;
  }
  for(const record of items) {
    const card=document.createElement('article');card.className='draft-card';
    const title=document.createElement('h3');title.textContent=record.title||'未命名草稿';card.append(title);
    const meta=document.createElement('p');meta.className='draft-card-meta';
    const kind=record.kind==='new'?'新建笔记':record.edit_field==='title'?'笔记标题':record.edit_field==='tab_name'?'页签名称':'笔记正文';
    meta.textContent=`${kind}${record.parent_note_id?' · 独立页签':''}${record.updated_at?` · ${new Date(record.updated_at).toLocaleString('zh-CN')}`:''}`;card.append(meta);
    const text=record.content|| (record.image_data_url?'已保留图片附件':'仅标题，尚未填写正文');
    const preview=document.createElement('p');preview.className='draft-card-preview';preview.textContent=text;card.append(preview);
    if(text.length>140 || text.split('\n').length>3){
      const full=document.createElement('details');full.className='draft-full-content';
      const toggle=document.createElement('summary');toggle.textContent='查看完整内容';
      const body=document.createElement('pre');body.textContent=text;full.append(toggle,body);card.append(full);
    }
    if(record.image_data_url){const image=document.createElement('img');image.alt='草稿中的图片';image.src=record.image_data_url;card.append(image);}
    const actions=document.createElement('div');actions.className='draft-card-actions';
    for(const[action,label]of [['copy','另存一份'],['discard','丢弃'],['restore','继续编辑']]) {
      const btn=document.createElement('button');btn.type='button';btn.textContent=label;btn.className=`ghost draft-action-${action}${action==='discard'?' danger':''}`;
      if(action==='copy')btn.title='恢复为新笔记副本，原笔记不变';
      btn.addEventListener('click',async()=>{
        btn.disabled=true;
        try {
          if(action==='discard') {
            if(await showConfirm('丢弃这份恢复草稿？已保存的笔记不会被删除。','丢弃草稿',{confirmText:'丢弃',isDanger:true})) {
              if([...draftVault.active.values()].some(e=>e.record.draft_id===record.draft_id)) {
                showToast('草稿正在编辑','请先保存笔记，或在编辑器中取消新建。');return;
              }
              await draftVault.discard(record.draft_id);
            }
          }else await restoreLocalDraft(record,action==='copy');
        }catch(error){showToast('暂时无法恢复',error.message);}finally{btn.disabled=false;}
      });actions.append(btn);
    }card.append(actions);list.append(card);
  }
}
async function restoreLocalDraft(record,asCopy=false) {
  if(record.library_id!==draftVault.library)throw Error('资料库已经改变，请重新打开草稿箱。');
  if(state.newNoteSaving || state.renameSaving || state.pendingContentSaves.size || state.pendingAttachmentReads){showToast('正在保存或读取图片','请等待当前操作完成。');return;}
  let note=null;
  if(record.kind==='note'&&!asCopy) {
    try{note=await request(`/api/notes/${record.note_id}`);}catch(e){if(e.status!==404)throw e;}
    const field=record.edit_field||'content';
    if(!note || note.created_at!==record.base_created_at || String(note[field]||'')!==record.base_content) {
      if(note && note.created_at===record.base_created_at && String(note[field]||'')===record.content) {
        await draftVault.discard(record.draft_id);showToast('这份内容已经保存','已清理对应的恢复草稿。');return;
      }
      if(!await showConfirm('原笔记已更新或已删除。为保护现有内容，这份草稿将另存为新笔记。','保留两份内容',{confirmText:'恢复为新笔记'}))return;
      asCopy=true;
    }
  }
  syncNewNoteDraftFromDom();captureEditorDraft();
  if(!await draftVault.flushAll()) {showToast('先保留当前草稿','本机写入尚未成功，请重试后再切换草稿。');return;}
  if(state.newNoteDraft && !await draftVault.park('new')) {showToast('先保留当前草稿','草稿尚未保存完成，请重试后再切换。');return;}
  // Stash the current editing session without silently committing it to notes.
  const old=currentNoteEditor();if(old){old.setAttribute('contenteditable','false');state.noteEditorSessions.delete(Number(old.dataset.noteId));}
  $('#editor-dock-content').replaceChildren();
  state.newNoteDraft=null;state.imageDataUrl=null;state.imageName=null;
  state.filter={};$('#search').value='';$('#search-clear').classList.add('hidden');++state.refreshRevision;
  if(record.kind==='new'||asCopy) {
    state.newNoteDraft={title:asCopy?`${record.title||'笔记'}（恢复副本）`:record.title,content:record.content,folder_id:record.folder_id||null};
    state.imageDataUrl=record.image_data_url||null;state.imageName=record.image_name||null;
    // Do not copy an invalid/deleted folder identity into a new note.
    const allFolders=(rows)=>rows.flatMap(r=>[r,...allFolders(r.children||[])]);
    if(state.newNoteDraft.folder_id&&!allFolders(state.folders).some(f=>Number(f.id)===Number(state.newNoteDraft.folder_id)))state.newNoteDraft.folder_id=null;
    draftVault.active.delete('new');
    const payload={...record,kind:'new',note_id:null,parent_note_id:null,edit_field:'content',title:state.newNoteDraft.title,folder_id:state.newNoteDraft.folder_id,base_content:'',base_updated_at:'',base_created_at:''};
    draftVault.adopt('new',payload);renderEditorDock();
  }else {
    const parent=note.parent_note_id?await request(`/api/notes/${note.parent_note_id}`):note;
    state.draftNoteSources.set(note.id,note);state.draftNoteSources.set(parent.id,parent);
    state.notes=[parent,...state.allNotes.filter(n=>Number(n.id)!==Number(parent.id))];state.selectedNoteId=parent.id;
    state.editorActiveTabs.delete(parent.id);renderEditorDock();
    if(note.id!==parent.id)await switchTab(parent.id,note.id,{tabsBar:$('#editor-dock .note-tabs-bar')});
    const field=record.edit_field||'content',key=draftKey(note.id,field);draftVault.adopt(key,record);
    if(field==='content') {
      const content=currentNoteEditor(),session=noteEditorSession(content);
      content.innerHTML=renderNoteContent(record.content,[]);
      Object.assign(session,{draft:record.content,baseContent:note.content,saved:editableContentToMarkdownForOriginal(note.content),editing:true,status:'dirty',error:''});
      paintEditorState(content);
    }else if(field==='title')makeTitleEditable($('#editor-dock .editable-note-title'),record.content);
    else {
      const label=$(`#editor-dock .tab-chip[data-tab-id="${note.id}"] .tab-label`);
      if(!label)throw Error('页签未加载，请重试。');makeTabLabelEditable(label,note.id,note.tab_name||note.title,record.content);
    }
  }
  $('#draft-dialog').close();switchPane('editor-pane');paintDraftProtection();
  showToast('草稿已恢复到编辑器','确认后保存笔记；原有笔记不会被自动覆盖。');
}
function editableContentToMarkdownForOriginal(text) {
  const div=document.createElement('div');div.innerHTML=renderNoteContent(text,[]);return editableContentToMarkdown(div);
}
function installDraftEvents() {
  $('#open-drafts').addEventListener('click',()=>{renderDraftList();$('#draft-dialog').showModal();});
  $('#retry-drafts').addEventListener('click',()=>draftVault.retry());
  document.addEventListener('input',event=>{if(event.target.matches('#new-note-title'))syncNewNoteDraftFromDom();});
  document.addEventListener('change',event=>{if(event.target.matches('#new-note-folder'))syncNewNoteDraftFromDom();});
  const checkpoint=()=>{syncNewNoteDraftFromDom();captureEditorDraft();};
  window.addEventListener('beforeunload',event=>{checkpoint();if(draftVault.unsafe()){event.preventDefault();event.returnValue='';}});
  window.addEventListener('pagehide',()=>{checkpoint();draftVault.pagehide();});
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='hidden'){checkpoint();void draftVault.flushAll();}});
}

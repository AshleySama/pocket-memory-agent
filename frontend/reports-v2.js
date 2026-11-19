/* Copyright 2026 AshleySama
 * SPDX-License-Identifier: Apache-2.0
 */

'use strict';
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const API='/api/report-workspace';
let state={templates:[],reports:[],sources:[],selected:new Set(),selectedDetails:new Map(),sourcePage:1,sourceTotal:0,sourcePageSize:30,sourceLoading:false,sourceError:'',template:'auto',report:null,edit:null,slide:0,busy:false,outlineDraft:null,outlineSaving:false,deleteTarget:null,deleting:false,historySelecting:false,historySelected:new Set(),historyUndo:null,historyRestoring:false,exporting:false,exports:new Map(),exportDismissed:new Set()};
const welcomeMarkup=$('#report').innerHTML;
let toastTimer,sourceSearchTimer,sourceRequest=0,fullEditor=null;
async function api(url,body,method){const r=await fetch(url,body?{method:method||'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});let data=await r.json();if(!r.ok)throw Error(data.error||data.message||'请求失败');return data;}
function toast(text,undo){clearTimeout(toastTimer);const el=$('#toast');el.textContent=text;el.hidden=false;if(undo){const b=document.createElement('button');b.type='button';b.textContent='撤销删除';b.onclick=async()=>{b.disabled=true;try{await undo();}catch(e){b.disabled=false;toast('恢复失败：'+e.message,undo);}};el.append(b);}toastTimer=setTimeout(()=>el.hidden=true,undo?15000:4500);}
function setType(type){state.template=type;$$('#types button').forEach(b=>b.classList.toggle('active',b.dataset.type===type));$('#template-input').value=type;$('#generate').textContent='生成文稿';if($('#composition-mode-field'))$('#composition-mode-field').hidden=type!=='auto';}
async function refresh(){const d=await api(API);state.templates=d.templates;state.reports=d.reports;$('#types').innerHTML=d.templates.map((t,i)=>`<button class="report-kind" data-type="${t.id}"><span class="kind-index">0${i+1}</span>${esc(t.label.split('／')[0])}</button>`).join('');$('#template-input').innerHTML=d.templates.map(t=>`<option value="${t.id}">${esc(t.label)}</option>`).join('');renderHistory();setType(state.report?.template||state.template);}
function renderHistory(){
  const ids=new Set(state.reports.map(r=>r.id));for(const id of state.historySelected)if(!ids.has(id))state.historySelected.delete(id);
  const selected=state.historySelected.size,total=state.reports.length,blocked=state.deleting||state.historyRestoring;
  $('#history').innerHTML=total?state.reports.map(r=>`<div class="history-row${state.historySelecting?' history-row-selecting':''}">${state.historySelecting?`<input class="history-select" type="checkbox" data-select-report="${r.id}" aria-label="选择文稿：${esc(r.title)}" ${state.historySelected.has(r.id)?'checked':''} ${blocked?'disabled':''}>`:''}<button class="history-item" data-report="${r.id}"><span>${esc(r.title)}</span><small>${r.status==='reading_paused'?'待续读 · ':r.status==='outline'?'提纲 · ':''}v${r.version} · ${r.updated.slice(0,10)}</small></button><button class="history-delete" type="button" data-delete-report="${r.id}" aria-label="删除文稿：${esc(r.title)}" title="删除文稿" ${blocked?'disabled':''}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v5M14 11v5"/></svg></button></div>`).join(''):'<p class="muted">还没有保存的文稿</p>';
  $('#history-manage').disabled=!total||blocked;$('#history-manage').textContent=state.historySelecting?'取消选择':'批量选择';$('#history-manage').setAttribute('aria-expanded',String(state.historySelecting));
  $('#history-clear-all').disabled=!total||blocked;$('#history-batch-actions').hidden=!state.historySelecting;
  $('#history-selection-count').textContent=`已选 ${selected} / ${total}`;$('#history-select-all').checked=!!total&&selected===total;$('#history-select-all').indeterminate=selected>0&&selected<total;$('#history-select-all').disabled=!total||blocked;
  $('#history-delete-selected').textContent=`删除选中${selected?'（'+selected+'）':''}`;$('#history-delete-selected').disabled=!selected||blocked;
  $('#history-undo').hidden=!state.historyUndo;$('#history-undo-status').textContent=state.historyUndo?`已删除 ${state.historyUndo.count} 份文稿`:'';$('#history-undo-button').disabled=blocked;
}
function clearReport(){fullEditor?.beforeDisplay();outlineObserver?.disconnect();state.report=null;state.outlineDraft=null;state.edit=null;state.slide=0;document.body.classList.remove('present');$('#reading').textContent='汇报视图';$('#report').innerHTML=welcomeMarkup;$('#begin').onclick=()=>create();$('#outline').innerHTML='';$('#save-status').textContent='选择资料后开始';['reading','rename','export'].forEach(id=>$('#'+id).disabled=true);renderExportResult();history.replaceState(null,'','/reports.html');updateSlide();fullEditor?.sync();}
function requestDelete(id){requestDeleteReports([id]);}
async function requestDeleteReports(ids,allReports=false){
  if(fullEditor?.active&&!(await fullEditor.leave()))return;
  if(state.deleting||state.historyRestoring)return;
  const reports=[...new Set(ids)].map(id=>state.report?.id===id?state.report:state.reports.find(r=>r.id===id)).filter(Boolean).map(r=>({id:r.id,version:r.version,title:r.title}));if(!reports.length)return;
  state.deleteTarget={reports,allReports};const count=reports.length;
  $('#delete-title').textContent=allReports?`清空全部 ${count} 份文稿？`:count===1?'删除这份文稿？':`删除选中的 ${count} 份文稿？`;
  $('#delete-report-name').textContent=reports.slice(0,3).map(r=>r.title).join('、')+(count>3?` 等 ${count} 份文稿`:'');
  $('#confirm-delete').textContent=allReports?`清空 ${count} 份文稿`:`删除 ${count} 份文稿`;$('#delete-error').textContent='';$('#delete-dialog').showModal();
}
async function undoHistoryDelete(){
  if(!state.historyUndo||state.historyRestoring||state.deleting)return;const undo=state.historyUndo;state.historyRestoring=true;renderHistory();
  try{const restored=await api(API+'/restore-batch',{undo_token:undo.undo_token});state.historyUndo=null;let refreshed=true;try{await refresh();}catch{refreshed=false;}if(undo.openReportId&&!state.report)await openReport(undo.openReportId);toast(`已恢复 ${restored.count} 份文稿${refreshed?'':'，列表暂未刷新'}`);}
  catch(err){toast('恢复未完成：'+err.message);}finally{state.historyRestoring=false;renderHistory();}
}
$('#history-manage').onclick=()=>{if(state.deleting||state.historyRestoring)return;state.historySelecting=!state.historySelecting;if(!state.historySelecting)state.historySelected.clear();renderHistory();};
$('#history-select-all').onchange=e=>{if(state.deleting||state.historyRestoring)return;state.historySelected=e.target.checked?new Set(state.reports.map(r=>r.id)):new Set();renderHistory();};
$('#history').onchange=e=>{const id=e.target.dataset?.selectReport;if(!id||state.deleting||state.historyRestoring)return;if(e.target.checked)state.historySelected.add(id);else state.historySelected.delete(id);renderHistory();};
$('#history-delete-selected').onclick=()=>requestDeleteReports([...state.historySelected]);
$('#history-clear-all').onclick=()=>requestDeleteReports(state.reports.map(r=>r.id),true);
$('#history-undo-button').onclick=undoHistoryDelete;
$('#delete-form').onsubmit=async e=>{
  e.preventDefault();if(state.deleting||!state.deleteTarget)return;const target=state.deleteTarget;state.deleting=true;$('#confirm-delete').disabled=true;renderHistory();
  try{const result=await api(API+'/delete-batch',{reports:target.reports.map(({id,version})=>({id,version})),all_reports:target.allReports});
    if($('#delete-dialog').open)$('#delete-dialog').close();const deleted=new Set(result.deleted),openReportId=deleted.has(state.report?.id)?state.report.id:null;
    if(openReportId)clearReport();state.reports=state.reports.filter(r=>!deleted.has(r.id));state.historySelected.clear();state.historySelecting=false;state.historyUndo={...result,openReportId};state.deleteTarget=null;renderHistory();
    let refreshed=true;try{await refresh();}catch{refreshed=false;}toast(`已删除 ${result.count} 份文稿${refreshed?'':'，列表暂未刷新'}`,undoHistoryDelete);
  }catch(err){$('#delete-error').textContent=err.message;try{await refresh();}catch{}}finally{state.deleting=false;$('#confirm-delete').disabled=false;renderHistory();}
};
document.addEventListener('click',e=>{const remove=e.target.closest('[data-delete-report]');if(remove){e.preventDefault();requestDelete(remove.dataset.deleteReport);}});
async function create(type){if(fullEditor?.active&&!(await fullEditor.leave()))return;if(state.busy||state.outlineSaving)return toast('当前文稿正在处理，请等待完成');setType(type||state.template);syncFocusMode();$('#create-error').textContent='';$('#source-search').value='';$('#source-kind').value='all';state.sources=[];state.sourceTotal=0;renderSelection();if(!$('#create-dialog').open)$('#create-dialog').showModal();await searchSources(1);}
function syncFocusMode(){const focus=$('#focus-input').value;$('#focus-count').textContent=focus.length+' / 1200';if(focus.trim()&&$('#template-input').value!=='auto'){setType('auto');toast('已切换为按资料自动组织，以使用你的要求');}}
function renderSelection(){
  $('#selected-count').textContent='已选 '+state.selected.size+' 项';
  $('#selected-sources').innerHTML=[...state.selected].map(key=>{const source=state.selectedDetails.get(key),title=source?.title||key;return `<span class="selected-source"><span title="${esc(title)}">${esc(title)}</span><button type="button" data-remove-source="${esc(key)}" aria-label="移除资料：${esc(title)}">×</button></span>`;}).join('');
  $('#selected-sources').hidden=!state.selected.size;
}
function sourceMeta(source){const tabs=Array.isArray(source.tabs)?source.tabs.length:Number(source.tabs)||1;const matched=typeof source.matched_tab==='object'?source.matched_tab?.title:source.matched_tab;return [source.kind==='note'?`笔记 · ${tabs} 个页签`:'已导入文档',source.category,matched?'命中页签：'+matched:''].filter(Boolean).join(' · ');}
function renderPicker(){
  const picker=$('#source-picker');picker.setAttribute('aria-busy',String(state.sourceLoading));
  picker.innerHTML=state.sourceLoading?'<p class="source-empty">正在查找资料…</p>':state.sourceError?`<div class="source-empty"><p>${esc(state.sourceError)}</p><button type="button" data-retry-sources>重试</button></div>`:state.sources.map(source=>{const key=source.kind+':'+source.id;const tags=Array.isArray(source.tags)?source.tags.join('、'):source.tags;return `<label class="source-option"><input type="checkbox" value="${esc(key)}" ${state.selected.has(key)?'checked':''}><span><b>${esc(source.title)}</b><small>${esc(sourceMeta(source))}</small>${source.snippet?`<span class="source-match">${esc(source.snippet)}</span>`:''}${tags?`<small class="source-tags">标签：${esc(tags)}</small>`:''}</span></label>`;}).join('')||`<p class="source-empty">${$('#source-search').value.trim()?'没有找到匹配资料，试试其他关键词。':'当前没有可选资料，请先添加笔记或导入文档。'}</p>`;
  $('#source-result-status').textContent=state.sourceLoading?'查找中':state.sourceError?'查找未完成':`共 ${state.sourceTotal} 项 · 第 ${state.sourcePage} / ${Math.max(1,Math.ceil(state.sourceTotal/state.sourcePageSize))} 页`;
  $('#source-prev').disabled=state.sourceLoading||!!state.sourceError||state.sourcePage<=1;
  $('#source-next').disabled=state.sourceLoading||!!state.sourceError||state.sourcePage*state.sourcePageSize>=state.sourceTotal;
}
async function searchSources(page=1){
  clearTimeout(sourceSearchTimer);const request=++sourceRequest;state.sourcePage=page;state.sourceLoading=true;state.sourceError='';renderPicker();
  const query=new URLSearchParams({q:$('#source-search').value.trim(),kind:$('#source-kind').value,page:String(page),page_size:String(state.sourcePageSize)});
  try{const data=await api(API+'/sources?'+query);if(request!==sourceRequest)return;state.sources=data.items||[];state.sourceTotal=data.total||0;state.sourcePage=data.page||page;state.sourcePageSize=data.page_size||30;for(const source of state.sources)if(state.selected.has(source.kind+':'+source.id))state.selectedDetails.set(source.kind+':'+source.id,source);renderSelection();}
  catch(error){if(request!==sourceRequest)return;state.sources=[];state.sourceError='资料查找失败：'+error.message;}
  finally{if(request===sourceRequest){state.sourceLoading=false;renderPicker();}}
}
let outlineObserver;
function setCurrentSection(id){$$('#outline a').forEach(a=>{if(a.getAttribute('href')==='#'+id)a.setAttribute('aria-current','location');else a.removeAttribute('aria-current');});}
function renderOutline(){
  outlineObserver?.disconnect();
  const sections=$$('#report .section').filter(s=>s.querySelector('h2'));
  $('#outline').innerHTML=sections.map((s,i)=>`<a href="#${esc(s.id)}"><span class="ordinal" aria-hidden="true">${String(i+1).padStart(2,'0')}</span><span>${esc(s.querySelector('h2').textContent)}</span></a>`).join('');
  if(sections.length)setCurrentSection(sections[0].id);
  if(typeof IntersectionObserver!=='undefined'){
    outlineObserver=new IntersectionObserver(entries=>{const visible=entries.filter(entry=>entry.isIntersecting).sort((a,b)=>a.boundingClientRect.top-b.boundingClientRect.top);if(visible.length)setCurrentSection(visible[0].target.id);},{rootMargin:'-140px 0px -55% 0px',threshold:0});
    sections.forEach(section=>outlineObserver.observe(section));
  }
}
function isOutline(report=state.report){return report?.adaptive?.status==='outline';}
function isReadingPaused(report=state.report){return report?.adaptive?.status==='reading_paused';}
function isEmptyFailure(report=state.report){return report?.adaptive?.status==='partial'&&!report.adaptive.chapters?.length;}
function isPreparation(report=state.report){return isOutline(report)||isReadingPaused(report);}
function outlineStorageKey(report=state.report){return 'pm-report-v2-outline-'+report.id;}
function copyOutline(outline){return (outline||[]).map(chapter=>({id:chapter.id,title:chapter.title||'',purpose:chapter.purpose||'',fact_ids:[...(chapter.fact_ids||[])]}));}
function persistOutlineDraft(){
  if(!isOutline()||!state.outlineDraft)return;
  let stored=true;try{localStorage.setItem(outlineStorageKey(),JSON.stringify({version:state.report.version,outline:state.outlineDraft}));}catch{stored=false;}
  $('#save-status').textContent=`提纲有修改 · v${state.report.version} · ${stored?'已在此浏览器暂存':'请保存到本机，刷新前不要关闭页面'}`;
  if($('#outline-draft-status'))$('#outline-draft-status').textContent=stored?'修改已在此浏览器暂存；点击「保存提纲」同步到本机文稿。':'浏览器暂存不可用，请点击「保存提纲」。';
}
function restoreOutlineDraft(){
  state.outlineDraft=copyOutline(state.report.adaptive.outline);let restored=false;
  try{const saved=JSON.parse(localStorage.getItem(outlineStorageKey())||'null');if(saved?.version===state.report.version&&Array.isArray(saved.outline)){state.outlineDraft=copyOutline(saved.outline);restored=true;}else if(saved)localStorage.removeItem(outlineStorageKey());}catch{}
  return restored;
}
function outlineReviewMarkup(report){
  const adaptive=report.adaptive,coverage=adaptive.coverage||{},issues=adaptive.issues||[];
  const summary=`${adaptive.facts?.length||0} 个已提取要点 · ${report.selected?.length||0} 项资料`;
  const fallback=adaptive.outline_origin==='source_fallback'?'<p class="coverage-warning">按整理要求规划未完成，目前保留按资料来源形成的提纲，请核对并调整章节重点。</p>':'';
  const warning=issues.length?'<p class="coverage-warning">资料提取或提纲组织有待核对项，确认提纲不代表内容已验收。</p>':'';
  const sourceList=(report.units||[]).map(unit=>{const ref=(report.refs||[]).find(item=>item.unit_id===unit.unit_id);return `<li>${ref?`<button type="button" class="cite" data-ref="${esc(ref.id)}">[${esc(ref.id)}]</button> `:''}${esc(unit.title)} · ${esc(unit.tab)} · ${unit.field==='ocr_text'?'OCR':'正文'}</li>`;}).join('');
  const originals=adaptive.reading_contract?.ledger_version?'<p class="outline-snapshot-note">以下要点保留原文，便于核对；章节主题是建议，可在成稿前调整。</p>':'';
  const coverageText=adaptive.reading_mode==='source_brief_v2'?`已保存全部 ${esc(coverage.total_blocks??0)} 个原文块；本次选入 ${esc(adaptive.facts?.length??0)} 个要点，其余保留在来源快照。4B按目录及预览选材，不代表通读全文。条件、清单和表格需整组移动，不能拆散。`:adaptive.reading_mode==='source_compiled_v1'?`程序保全 ${esc(coverage.source_scan_blocks??0)} 个原文块；4B只依据来源目录和原文预览组织。确认后直接编排完整原文与表格，不代表模型通读全文。`:`已送入模型：${esc(coverage.read_units??0)} / ${esc(coverage.total_units??0)} 份正文、页签或文档片段。阅读完成不等于事实完整或语义正确。`;
  return `${originals}${fallback}${warning}<details class="outline-review"><summary>${summary} · 资料与待核对项</summary><p>${coverageText}</p>${issues.length?`<ul class="outline-issues">${issues.map(issue=>`<li>${esc(typeof issue==='string'?issue:issue.message||'请核对资料覆盖情况')}</li>`).join('')}</ul>`:''}<ul>${sourceList}</ul></details>`;
}
function renderOutlineEditor(){
  const report=state.report,facts=report.adaptive.facts||[],chapters=state.outlineDraft;
  const chapterLimit=8;
  const expanded=new Set($$('#report [data-outline-facts][open]').map(el=>el.dataset.outlineFacts));
  const owner=new Map(chapters.flatMap(chapter=>chapter.fact_ids.map(id=>[id,chapter.title])));
  $('#report').innerHTML=`<header class="report-heading"><span class="eyebrow">提纲已生成 · 确认后成稿</span><h1>${esc(report.title)}</h1><p class="outline-intro">调整章节标题、写作重点和顺序，也可把已提取要点移动到其他章节。每个要点只归属一个章节，事实正文及引用保持只读。</p>${report.focus?`<details class="outline-requirements"><summary>查看整理要求</summary><p>${esc(report.focus)}</p></details>`:''}</header><div class="outline-snapshot-note">正式成稿继续使用生成这份提纲时保存的资料快照和要点，不会重新读取当前资料或重新规划提纲。</div><form id="outline-editor-form"><div class="outline-chapters">${chapters.map((chapter,index)=>`<section class="section outline-chapter" id="outline-chapter-${index}" data-outline-chapter="${index}"><div class="section-head"><span class="section-number">${String(index+1).padStart(2,'0')}</span><h2>${esc(chapter.title||'新章节')}</h2><div class="outline-order"><button type="button" data-move-chapter="${index}" data-direction="-1" ${index===0?'disabled':''} aria-label="上移第 ${index+1} 章">↑</button><button type="button" data-move-chapter="${index}" data-direction="1" ${index===chapters.length-1?'disabled':''} aria-label="下移第 ${index+1} 章">↓</button><button type="button" data-remove-chapter="${index}" ${chapter.fact_ids.length||chapters.length===1?'disabled':''} title="先将要点移至其他章节，再删除空章">删除空章</button></div></div><label class="outline-field">章节标题<input data-outline-field="title" data-chapter="${index}" minlength="2" maxlength="60" value="${esc(chapter.title)}" required></label><label class="outline-field">写作重点<textarea data-outline-field="purpose" data-chapter="${index}" rows="2" maxlength="500" placeholder="说明本章要回答的问题、比较角度或呈现顺序">${esc(chapter.purpose)}</textarea></label><details class="outline-facts" data-outline-facts="${esc(chapter.id)}" ${expanded.has(String(chapter.id))?'open':''}><summary>本章 ${chapter.fact_ids.length} 个要点 · 查看与调整归属</summary><p class="muted">勾选其他章节的要点，会将它移入本章。要移出本章，请在目标章节勾选。</p><div class="outline-fact-list">${[...facts].sort((a,b)=>Number(chapter.fact_ids.includes(b.id))-Number(chapter.fact_ids.includes(a.id))).map(fact=>`<div class="outline-fact"><label><input type="checkbox" data-outline-fact="${esc(fact.id)}" data-chapter="${index}" ${chapter.fact_ids.includes(fact.id)?'checked':''}><span><b>${esc(fact.topic||fact.id)}</b><span class="outline-fact-text">${esc(fact.text)}</span><small>${chapter.fact_ids.includes(fact.id)?'归属本章':'当前：'+(owner.get(fact.id)||'尚未归属章节')}</small></span></label><span class="outline-fact-refs">${(fact.refs||[]).map(ref=>`<button type="button" class="cite" data-ref="${esc(ref)}" aria-label="查看要点引用 ${esc(ref)}">[${esc(ref)}]</button>`).join('')}</span></div>`).join('')}</div></details></section>`).join('')}</div><div class="outline-add"><button type="button" id="outline-add-chapter" ${chapters.length>=chapterLimit?'disabled':''}>＋ 添加章节</button><span class="muted">${chapters.length} / ${chapterLimit} 章 · 新章节需分配要点后保存</span></div><p id="outline-error" class="error" role="alert"></p><div class="outline-editor-footer"><p id="outline-draft-status" role="status">提纲已保存到本机，可稍后从左侧文稿列表继续。</p><div><button type="button" id="outline-later">稍后继续</button><button type="button" id="outline-save">保存提纲</button><button type="submit" id="outline-compose" class="primary">按此提纲正式成稿</button></div></div></form>`;
  $('#report .outline-snapshot-note').insertAdjacentHTML('afterend',outlineReviewMarkup(report));
  renderOutline();updateOutlineControls();
  $('#outline-editor-form').onsubmit=event=>{event.preventDefault();submitOutline(true);};
  $('#outline-save').onclick=()=>submitOutline(false);
  $('#outline-later').onclick=()=>clearReport();
  $('#outline-add-chapter').onclick=()=>{if(state.busy||state.outlineSaving||chapters.length>=chapterLimit)return;chapters.push({id:'C'+Date.now(),title:'新章节',purpose:'',fact_ids:[]});renderOutlineEditor();persistOutlineDraft();$$('#report [data-outline-field="title"]').at(-1)?.focus();};
}
function updateOutlineControls(){if(!isOutline())return;$$('#outline-editor-form input,#outline-editor-form textarea,#outline-editor-form button').forEach(el=>{if(state.busy||state.outlineSaving){if(!el.disabled)el.dataset.processingDisabled='true';el.disabled=true;}else if(el.dataset.processingDisabled){el.disabled=false;delete el.dataset.processingDisabled;}});}
function validateOutline(){
  const chapters=state.outlineDraft,facts=state.report.adaptive.facts||[],ids=new Set(facts.map(fact=>fact.id)),seen=new Set();
  const chapterLimit=8;
  if(!chapters?.length||chapters.length>chapterLimit)throw Error(`提纲需要 1–${chapterLimit} 个章节`);
  for(const [index,chapter] of chapters.entries()){
    if(chapter.title.trim().length<2||chapter.title.trim().length>60)throw Error(`第 ${index+1} 章标题需为 2–60 字`);
    if(chapter.purpose.length>500)throw Error(`第 ${index+1} 章写作重点最多 500 字`);
    if(!chapter.fact_ids.length)throw Error(`第 ${index+1} 章还没有要点，请移入要点或删除空章`);
    for(const id of chapter.fact_ids){if(!ids.has(id)||seen.has(id))throw Error('要点归属无效，请重新打开已保存的提纲');seen.add(id);}
  }
  if(seen.size!==ids.size)throw Error('每个要点都需要归属一个章节，请核对要点分配');
  return copyOutline(chapters).map(chapter=>({...chapter,title:chapter.title.trim(),purpose:chapter.purpose.trim()}));
}
async function submitOutline(compose){
  if(state.busy||state.outlineSaving||!isOutline())return;
  const report=state.report;
  try{
    const outline=validateOutline();state.outlineSaving=true;updateOutlineControls();fullEditor?.sync();$('#outline-error').textContent='';
    const result=await api(`${API}/${report.id}/${compose?'compose':'outline'}`,{version:report.version,outline});
    if(compose){
      try{localStorage.removeItem(outlineStorageKey(report));localStorage.setItem('pm-report-v2-job',result.id);}catch{}
      // Starting composition also saves the edited outline and advances its version.
      // Keep that durable version available if drafting fails and the user retries.
      if(state.report?.id===report.id)await openReport(report.id);
      poll(result.id);
    }
    else{try{localStorage.removeItem(outlineStorageKey(report));}catch{}if(state.report?.id===report.id)display(result.report&&result.html!==undefined?result:await api(API+'/'+report.id));await refresh();toast('提纲已保存，可稍后继续成稿');}
  }catch(error){if($('#outline-error'))$('#outline-error').textContent=error.message;else toast(error.message);}
  finally{state.outlineSaving=false;updateOutlineControls();fullEditor?.sync();}
}
function display(data){
  fullEditor?.beforeDisplay();
  state.report=data.report;state.outlineDraft=null;const outline=isOutline(),preparation=isPreparation(),emptyFailure=isEmptyFailure();
  if(outline){document.body.classList.remove('present');$('#reading').textContent='汇报视图';const restored=restoreOutlineDraft();renderOutlineEditor();$('#save-status').textContent=`已保存提纲 · v${data.report.version} · 待确认成稿`;if(restored)persistOutlineDraft();}
  else if(isReadingPaused()){$('#report').innerHTML=data.html;document.body.classList.remove('present');$('#reading').textContent='汇报视图';$('#save-status').textContent=`阅读进度已保存 · v${data.report.version} · 可继续`;renderOutline();$('#resume-reading').disabled=state.busy;$('#resume-reading').onclick=resumeReading;}
  else{$('#report').innerHTML=data.html;$('#save-status').textContent=`已保存 · v${data.report.version} · ${data.report.edited?'含人工编辑，待复核':emptyFailure?'生成未完成，任务记录已保留':data.report.adaptive?.status==='partial'?'部分成稿，请核对未覆盖项':data.report.adaptive?.reading_mode==='source_structured_v1'&&data.report.generation?.calls===0?'原文整理，待核对':'AI 初稿，待核对'}`;renderOutline();}
  ['reading','rename','export'].forEach(id=>$('#'+id).disabled=preparation);$('#export').disabled=preparation||emptyFailure||state.exporting;renderExportResult();setType(data.report.template);history.replaceState(null,'','/reports.html?id='+data.report.id);state.slide=0;updateSlide();fullEditor?.sync();
  if(data.active_job&&!state.busy){try{localStorage.setItem('pm-report-v2-job',data.active_job.id);}catch{}poll(data.active_job.id);}
}
async function resumeReading(){
  if(state.busy||!isReadingPaused())return;
  const report=state.report;$('#resume-reading').disabled=true;$('#resume-reading-error').textContent='';
  try{const job=await api(`${API}/${report.id}/resume-reading`,{version:report.version});try{localStorage.setItem('pm-report-v2-job',job.id);}catch{}await poll(job.id);}
  catch(error){if(state.report?.id===report.id){$('#resume-reading-error').textContent=error.message;$('#resume-reading').disabled=false;}else toast(error.message);}
}
async function openReport(id){if(fullEditor?.active&&!(await fullEditor.leave()))return;try{display(await api(API+'/'+id));window.scrollTo(0,0);}catch(e){toast(e.message);}}
async function poll(id){let checkpointId=null;state.busy=true;updateOutlineControls();fullEditor?.sync();if(isReadingPaused()&&$('#resume-reading'))$('#resume-reading').disabled=true;$('#progress').hidden=false;$('#new-report').disabled=true;try{for(;;){const j=await api(API+'/jobs/'+id);checkpointId=j.report_id||checkpointId;const seconds=Math.max(0,Math.round(Date.now()/1000-j.started));$('#progress').innerHTML=`<span class="pulse"></span><div><b>${esc(j.stage)}</b><p>已等待 ${seconds} 秒 · 本地运行，离开页面后任务继续。保存完成后可在左侧重新打开。</p></div>`;if(j.status==='done'){await refresh();await openReport(j.report_id);toast(isReadingPaused()?'阅读进度已保存，可稍后继续':isOutline()?'提纲已保存，请核对并调整后正式成稿':isEmptyFailure()?'文稿未生成，已保留资料与错误记录':j.partial?'部分成稿已保存，请查看未覆盖项':'文稿已生成并保存');break;}if(j.status==='error')throw Error(j.error);await new Promise(r=>setTimeout(r,1300));}}catch(e){if(checkpointId){try{await refresh();await openReport(checkpointId);}catch{}}toast(e.message);$('#save-status').textContent='生成未完成：'+e.message;}finally{state.busy=false;updateOutlineControls();fullEditor?.sync();if(isReadingPaused()&&$('#resume-reading'))$('#resume-reading').disabled=false;$('#progress').hidden=true;$('#new-report').disabled=false;try{localStorage.removeItem('pm-report-v2-job');}catch{}}}
function updateSlide(){const sections=$$('.section');sections.forEach((s,i)=>s.classList.toggle('slide-visible',i===state.slide));$('#slide-label').textContent=`${state.slide+1} / ${sections.length}`;$('#previous').disabled=state.slide===0;$('#next').disabled=state.slide>=sections.length-1;}
document.addEventListener('click',async e=>{const close=e.target.closest('[data-close]');if(close){$('#'+close.dataset.close).close();return;}const type=e.target.closest('[data-type]');if(type){await create(type.dataset.type);return;}const old=e.target.closest('[data-report]');if(old){await openReport(old.dataset.report);return;}const ref=e.target.closest('[data-ref]');if(ref){e.preventDefault();try{const d=await api(`${API}/${state.report.id}/sources/${ref.dataset.ref}`);const r=d.ref;$('#source-title').textContent=`[${r.id}] ${r.title}`;$('#source-body').innerHTML=`<div class="ref-meta">${esc(r.tab)} · ${r.field==='ocr_text'?'OCR':'正文'} · 生成时快照</div>${d.changed?'<p class="error">原资料已修改或移除。以下是生成时引用，当前原文不再使用旧区间定位。</p>':'<span class="tag">与当前保存原文一致</span>'}<blockquote>${esc(d.quote_text??r.quote)}</blockquote><details><summary>查看该页签完整原文</summary><pre>${esc(d.snapshot.slice(0,r.start))}<mark>${esc(r.quote)}</mark>${esc(d.snapshot.slice(r.end))}</pre></details>${d.changed&&d.current!==null?'<details><summary>查看当前原文（已变更）</summary><pre>'+esc(d.current)+'</pre></details>':''}<details><summary>引用定位信息</summary><p>字段 ${esc(r.field)} · 字符 [${r.start}, ${r.end}) · 修订 ${esc(r.revision.slice(0,12))}</p></details>`;$('#source-dialog').showModal();}catch(err){toast(err.message);}return;}const edit=e.target.closest('[data-edit]');if(edit)fullEditor?.start();});
document.addEventListener('click',event=>{
  const removeSource=event.target.closest('[data-remove-source]');if(removeSource){state.selected.delete(removeSource.dataset.removeSource);state.selectedDetails.delete(removeSource.dataset.removeSource);renderSelection();renderPicker();return;}
  if(event.target.closest('[data-retry-sources]')){searchSources(state.sourcePage);return;}
  if(!isOutline()||state.busy||state.outlineSaving)return;
  const move=event.target.closest('[data-move-chapter]');if(move){const index=Number(move.dataset.moveChapter),target=index+Number(move.dataset.direction),chapters=state.outlineDraft;if(target<0||target>=chapters.length)return;[chapters[index],chapters[target]]=[chapters[target],chapters[index]];renderOutlineEditor();persistOutlineDraft();$(`#outline-chapter-${target} [data-outline-field="title"]`)?.focus();return;}
  const remove=event.target.closest('[data-remove-chapter]');if(remove){const index=Number(remove.dataset.removeChapter);if(state.outlineDraft[index]?.fact_ids.length||state.outlineDraft.length<=1)return;state.outlineDraft.splice(index,1);renderOutlineEditor();persistOutlineDraft();}
});
$('#report').addEventListener('input',event=>{
  const field=event.target.dataset.outlineField,index=Number(event.target.dataset.chapter);if(!field||!isOutline()||state.busy||state.outlineSaving)return;
  state.outlineDraft[index][field]=event.target.value;
  if(field==='title'){const title=event.target.value||'新章节';$(`#outline-chapter-${index} h2`).textContent=title;const link=$$('#outline a')[index]?.querySelector('span:last-child');if(link)link.textContent=title;}
  persistOutlineDraft();
});
$('#report').addEventListener('change',event=>{
  const id=event.target.dataset.outlineFact,index=Number(event.target.dataset.chapter);if(!id||!isOutline()||state.busy||state.outlineSaving)return;
  if(!event.target.checked){event.target.checked=true;toast('要移出此要点，请在目标章节勾选它');return;}
  const adaptive=state.report.adaptive;
  const moved=adaptive.reading_mode==='source_brief_v2'?(adaptive.groups||[]).find(group=>group.fact_ids.includes(id))?.fact_ids||[id]:[id];
  for(const chapter of state.outlineDraft)chapter.fact_ids=chapter.fact_ids.filter(factId=>!moved.includes(factId));
  state.outlineDraft[index].fact_ids.push(...moved);renderOutlineEditor();persistOutlineDraft();
});
$('#begin').onclick=()=>create();$('#new-report').onclick=()=>create();
function syncCompositionHelp(){
  const selected=$('#composition-mode').value==='source_compiled';
  $('#focus-label').textContent=selected?'内容范围（可选）':'写作要求（可选）';
  $('#focus-input').placeholder=selected?'例如：只整理设备保养的步骤和注意事项，不包含采购流程。':'例如：面向新同事，归纳资料中的概念、操作步骤和适用条件。';
  $('#focus-help').textContent=selected?'当前方式按要求选择原文内容，保留步骤、条件和引用；需要归纳、比较或改写时，请选择“逐章撰写”。':'要求用于资料阅读、提纲和分章写作；成稿仍需核对事实与引用。';
  $('#composition-help').textContent=selected?'用于筛选内容范围、组织章节；不自动改写成培训讲稿或生成新的建议。':'逐章撰写会分批读取完整原文并生成正文；资料越多耗时越长。';
}
if($('#composition-mode')){$('#composition-mode').onchange=syncCompositionHelp;syncCompositionHelp();}
$('#focus-input').oninput=syncFocusMode;
$('#template-input').onchange=()=>{setType($('#template-input').value);syncFocusMode();};
$('#source-search').oninput=()=>{clearTimeout(sourceSearchTimer);++sourceRequest;state.sourceLoading=true;state.sourceError='';state.sourcePage=1;renderPicker();sourceSearchTimer=setTimeout(()=>searchSources(1),280);};
$('#source-kind').onchange=()=>searchSources(1);
$('#source-prev').onclick=()=>searchSources(Math.max(1,state.sourcePage-1));
$('#source-next').onclick=()=>searchSources(state.sourcePage+1);
$('#create-dialog').addEventListener('close',()=>{clearTimeout(sourceSearchTimer);++sourceRequest;state.sourceLoading=false;});
$('#source-picker').onchange=event=>{if(event.target.type!=='checkbox')return;const key=event.target.value;if(event.target.checked&&state.selected.size>=8){event.target.checked=false;toast('最多选择 8 项资料');return;}if(event.target.checked){state.selected.add(key);const source=state.sources.find(item=>item.kind+':'+item.id===key);if(source)state.selectedDetails.set(key,source);}else{state.selected.delete(key);state.selectedDetails.delete(key);}renderSelection();};
$('#create-form').onsubmit=async event=>{event.preventDefault();if(state.busy||state.outlineSaving)return;$('#generate').disabled=true;try{if(!state.selected.size)throw Error('请至少选择一项资料');syncFocusMode();const sources=[...state.selected].map(key=>{const[kind,id]=key.split(':');return{kind,id:Number(id)};}),template=$('#template-input').value;const job=await api(API+'/jobs',{title:$('#title-input').value,template,sources,focus:$('#focus-input').value,...(template==='auto'?{phase:'complete',composition_mode:$('#composition-mode')?.value||'model_written'}:{})});try{localStorage.setItem('pm-report-v2-job',job.id);}catch{}$('#create-dialog').close();poll(job.id);}catch(error){$('#create-error').textContent=error.message;}finally{$('#generate').disabled=false;}};
$('#edit-form').onsubmit=async e=>{e.preventDefault();try{const payload={version:state.report.version};if(state.edit==='title')payload.title=$('#edit-text').value;else payload.notes={...state.report.notes,[state.edit]:$('#edit-text').value};display(await api(`${API}/${state.report.id}/save`,payload));$('#edit-dialog').close();await refresh();toast('已保存到本机，编辑内容待复核');}catch(err){$('#edit-error').textContent=err.message;}};
$('#rename').onclick=()=>{if(!state.report||isPreparation())return;state.edit='title';$('#edit-title').textContent='修改汇编标题';$('#edit-text').value=state.report.title;$('#edit-error').textContent='';$('#edit-dialog').showModal();};
$('#reading').onclick=()=>{if(!state.report||isPreparation())return;document.body.classList.toggle('present');$('#reading').textContent=document.body.classList.contains('present')?'完整文稿':'汇报视图';updateSlide();window.scrollTo(0,0);};
$('#previous').onclick=()=>{state.slide--;updateSlide();};$('#next').onclick=()=>{state.slide++;updateSlide();};
function exportKey(report){return report?`${report.id}:v${report.version}`:'';}
function currentExport(){return state.exports.get(exportKey(state.report));}
function setExportDetails(expanded){$('#export-details').hidden=!expanded;$('#export-details-toggle').setAttribute('aria-expanded',String(expanded));}
function renderExportResult(){const saved=currentExport(),panel=$('#export-result');panel.hidden=isPreparation()||!saved||state.exportDismissed.has(saved.id);if(!saved||panel.hidden)return;if(panel.dataset.exportId!==saved.id){setExportDetails(false);$('#export-action-status').textContent='';}panel.dataset.exportId=saved.id;$('#export-result-title').textContent='已导出';$('#export-result-title').title=`已保存到本机 · v${saved.version}`;$('#export-filename').textContent=saved.filename;$('#export-filename').title=saved.filename;$('#export-path').value=saved.path;$('#export-download').href=saved.download_url;$('#export-download').download=saved.filename;}
async function exportRequest(url,body){const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-PocketMemory-Report-Export':'1'},body:JSON.stringify(body)});const result=await response.json();if(!response.ok)throw Error(result.error||'导出未完成，请重试');return result;}
$('#export').onclick=async()=>{if(!state.report||state.exporting||isPreparation()||isEmptyFailure())return;const report={id:state.report.id,version:state.report.version},button=$('#export');state.exporting=true;button.disabled=true;button.textContent='正在保存…';try{const saved=await exportRequest('/api/report-exports',{report_id:report.id,version:report.version});state.exports.set(exportKey(report),saved);state.exportDismissed.delete(saved.id);renderExportResult();if(currentExport()?.id===saved.id)$('#export-result').scrollIntoView({block:'nearest'});toast('完整 HTML 已保存到本机');}catch(e){toast('导出未完成：'+e.message);}finally{state.exporting=false;button.textContent='导出完整 HTML';button.disabled=!state.report||isPreparation()||isEmptyFailure();}};
async function openExport(action){const saved=currentExport();if(!saved)return;const status=$('#export-action-status');if(action==='open'&&saved.view_url){window.open(saved.view_url,'_blank','noopener');status.textContent='已在新标签页打开导出的 HTML。';return;}try{await exportRequest(`/api/report-exports/${saved.id}/${action}`,{});if(currentExport()?.id===saved.id)status.textContent=action==='open'?'已请求用默认应用打开文稿。':'已请求打开文件所在文件夹。';}catch(e){if(currentExport()?.id===saved.id)status.textContent=e.message;toast(e.message);}}
$('#export-open').onclick=()=>openExport('open');$('#export-folder').onclick=()=>openExport('open-folder');
$('#export-details-toggle').onclick=()=>setExportDetails($('#export-details').hidden);
$('#export-dismiss').onclick=()=>{const saved=currentExport();if(saved)state.exportDismissed.add(saved.id);$('#export-result').hidden=true;setExportDetails(false);$('#export').focus();};
$('#export-copy').onclick=async()=>{const saved=currentExport();if(!saved)return;try{await navigator.clipboard.writeText(saved.path);toast('保存路径已复制');}catch{const input=$('#export-path');input.focus();input.select();toast('路径已选中，可按 Ctrl+C 复制');}};
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!$$('dialog[open]').length&&document.body.classList.contains('present'))$('#reading').click();});
if(window.PocketReportEditor)fullEditor=new window.PocketReportEditor({
  report:()=>state.report,blocked:()=>state.busy||state.outlineSaving||isPreparation()||isEmptyFailure(),
  toast,outline:renderOutline,
  save:payload=>api(`${API}/${state.report.id}/save`,payload),
  committed:async data=>{display(data);try{await refresh();}catch{toast('正文已保存，目录暂未刷新');}},
  restore:()=>display({report:state.report,html:fullEditor.originalHTML})
});
(async()=>{try{await refresh();const id=new URLSearchParams(location.search).get('id');if(id)await openReport(id);let job;try{job=localStorage.getItem('pm-report-v2-job');}catch{}if(job&&!state.busy)poll(job);}catch(e){toast(e.message);}})();

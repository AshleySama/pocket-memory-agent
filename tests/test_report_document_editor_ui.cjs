/* Copyright 2026 AshleySama
 * SPDX-License-Identifier: Apache-2.0 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '..', 'frontend', 'report-document-editor.js'), 'utf8');

function element() {
  return {innerHTML:'',value:'',textContent:'',dataset:{},hidden:false,disabled:false,
    contentEditable:'true',focusCalls:0,focus(){this.focusCalls++;},
    querySelectorAll:()=>[],classList:{remove(){},add(){}}};
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes,no)=>{resolve=yes;reject=no;});
  return {promise,resolve,reject};
}

function harness({active=true, dirty=true}={}) {
  const nodes = new Map(), storage = new Map(), requests = [], choices = [], answers = [], toasts = [], commits = [];
  const get = selector => {if(!nodes.has(selector))nodes.set(selector,element());return nodes.get(selector);};
  let report = {id:'a'.repeat(32),created:'2026-11-18T00:00:00Z',version:7,title:'已保存的标题',
    document_html:'<p>已保存的正文</p>',last_document_edit:{from_version:6}};
  let failStorage = false, restoreCalls = 0, saveHandler = async()=>{throw Error('请为测试配置保存结果');};
  const context = vm.createContext({window:{},document:{querySelector:get,body:element()},
    localStorage:{getItem:key=>storage.get(key)??null,
      setItem(key,value){if(failStorage)throw Error('storage full');storage.set(key,value);},
      removeItem:key=>storage.delete(key)}});
  vm.runInContext(source,context,{filename:'report-document-editor.js',timeout:1000});
  // Keep the actual editor state methods; omit constructor event wiring and
  // rich-text DOM creation, which are exercised by the browser flow separately.
  const editor = Object.create(context.window.PocketReportEditor.prototype);
  Object.assign(editor,{active,dirty,saving:false,range:null,host:element(),bar:element(),status:element(),
    body:element(),title:element(),originalHTML:'<header><h1>已保存的标题</h1></header><p>已保存的正文</p>',
    originalTitle:report.title,options:{report:()=>report,blocked:()=>false,
      async save(payload){requests.push(JSON.parse(JSON.stringify(payload)));return saveHandler(payload);},
      async committed(data){commits.push(data);report=data.report;editor.host.innerHTML=data.html;},
      restore(){restoreCalls++;},toast:message=>toasts.push(message),outline(){}}});
  editor.title.value='待保存的新标题';editor.body.innerHTML='<h2>新章节</h2><p>修改正文 <a data-ref="1">[1]</a></p>';
  editor.baseline=JSON.stringify({title:report.title,document_html:report.document_html});
  editor.choice=async(title,message,options)=>{choices.push({title,message,options});return answers.shift()??'';};
  return {editor,requests,choices,answers,toasts,commits,storage,get,
    report:()=>report,restores:()=>restoreCalls,failStorage:()=>{failStorage=true;},
    saveWith:handler=>{saveHandler=handler;},
    saved:()=>({report:{...report,version:report.version+1,title:editor.title.value,
      document_html:editor.body.innerHTML},html:'<h1>保存后的文稿</h1><p>保存后的正文</p>'})};
}

test('save failure keeps the full editable draft, title and revision for retry',async()=>{
  const h=harness(), e=h.editor, draft={title:e.title.value,document_html:e.body.innerHTML}, body=e.body, title=e.title;
  h.saveWith(async()=>{throw Error('文稿已更新，请重新打开');});
  assert.equal(await e.save(false),false);
  assert.equal(e.active,true);assert.equal(e.dirty,true);assert.equal(e.saving,false);
  assert.equal(e.body,body);assert.equal(e.title,title);
  assert.equal(e.body.innerHTML,draft.document_html);assert.equal(e.title.value,draft.title);
  assert.equal(e.body.contentEditable,'true');assert.equal(e.title.disabled,false);
  assert.deepEqual(JSON.parse(h.storage.get(e.key())),{...draft,version:7});
  assert.match(e.status.textContent,/保存失败.*文稿已更新.*草稿仍保留/);
  assert.equal(e.status.dataset.error,'true');assert.equal(h.report().version,7);
  assert.equal(h.commits.length,0);assert.equal(h.restores(),0);
});

test('a pending save locks duplicate requests and submits the exact current version and full document',async()=>{
  const h=harness(), e=h.editor, pending=deferred(), saved=h.saved();
  const expected={version:7,title:e.title.value,document_html:e.body.innerHTML},key=e.key(),body=e.body,title=e.title;
  e.persist();h.saveWith(()=>pending.promise);
  const first=e.save(true);
  assert.equal(e.saving,true);assert.equal(body.contentEditable,'false');assert.equal(title.disabled,true);
  assert.equal(await e.save(true),false);assert.equal(h.requests.length,1);
  assert.deepEqual(h.requests[0],expected);assert.equal(h.report().version,7);
  pending.resolve(saved);
  assert.equal(await first,true);assert.equal(e.saving,false);assert.equal(e.active,false);
  assert.equal(e.dirty,false);assert.equal(h.storage.has(key),false);
  assert.equal(h.commits.length,1);assert.equal(h.report().version,8);
  assert.match(h.toasts.at(-1),/全文已保存/);
});

test('leaving can keep editing or preserve the full local draft without saving the report',async()=>{
  const h=harness(),e=h.editor,key=e.key(),draft={title:e.title.value,document_html:e.body.innerHTML,version:7};
  h.answers.push('');
  assert.equal(await e.leave(),false);assert.equal(e.active,true);assert.equal(e.dirty,true);
  assert.equal(h.restores(),0);assert.equal(h.requests.length,0);
  h.answers.push('draft');
  assert.equal(await e.leave(),true);assert.equal(e.active,false);assert.equal(e.dirty,false);
  assert.deepEqual(JSON.parse(h.storage.get(key)),draft);
  assert.equal(h.restores(),1);assert.equal(h.requests.length,0);assert.equal(h.report().version,7);
});

test('a failed local draft write prevents leaving and keeps the in-memory document',async()=>{
  const h=harness(),e=h.editor,body=e.body,title=e.title;
  h.answers.push('draft');h.failStorage();
  assert.equal(await e.leave(),false);
  assert.equal(e.active,true);assert.equal(e.dirty,true);assert.equal(e.body,body);assert.equal(e.title,title);
  assert.equal(h.restores(),0);assert.equal(h.requests.length,0);
  assert.equal(e.status.dataset.error,'true');assert.match(e.status.textContent,/草稿暂存失败/);
});

test('cancel requires discard confirmation and only then clears the draft and restores saved display',async()=>{
  const h=harness(),e=h.editor,key=e.key(),savedHTML=e.originalHTML,body=e.body;
  e.persist();h.answers.push('');await e.cancel();
  assert.equal(e.active,true);assert.equal(e.body,body);assert.equal(h.storage.has(key),true);
  assert.equal(h.restores(),0);
  h.answers.push('discard');await e.cancel();
  assert.equal(e.active,false);assert.equal(e.dirty,false);assert.equal(h.storage.has(key),false);
  assert.equal(e.host.innerHTML,savedHTML);assert.equal(h.restores(),1);
  assert.equal(h.requests.length,0);assert.equal(h.report().version,7);
  assert.equal(h.report().document_html,'<p>已保存的正文</p>');
});

test('undo cancellation sends nothing and confirmed undo uses the saved version without editor HTML',{timeout:1000},async()=>{
  const h=harness({active:false,dirty:false}),e=h.editor,pending=deferred(),requestStarted=deferred();
  h.answers.push('');await e.undoSave();assert.equal(h.requests.length,0);
  h.answers.push('undo');h.saveWith(()=>{requestStarted.resolve();return pending.promise;});
  const undo=e.undoSave();
  // The VM awaits a host-realm choice Promise, which needs Promise assimilation
  // before its continuation. Wait for the observable request boundary instead
  // of assuming one host microtask also flushes that cross-realm continuation.
  await requestStarted.promise;
  assert.equal(e.saving,true);assert.deepEqual(h.requests,[{version:7,undo_document_edit:true}]);
  const choicesBefore=h.choices.length;
  await e.undoSave();assert.equal(h.requests.length,1);assert.equal(h.choices.length,choicesBefore);
  pending.resolve({report:{...h.report(),version:8,last_document_edit:undefined},html:'<p>上一次保存前的正文</p>'});
  await undo;
  assert.equal(e.saving,false);assert.equal(h.report().version,8);assert.equal(h.commits.length,1);
  assert.equal(e.host.innerHTML,'<p>上一次保存前的正文</p>');assert.match(h.toasts.at(-1),/已撤回上次保存/);
});

test('undo failure leaves the current saved report and recovery snapshot intact',async()=>{
  const h=harness({active:false,dirty:false}),e=h.editor,before=JSON.stringify(h.report());
  h.answers.push('undo');h.saveWith(async()=>{throw Error('文稿已更新');});
  await e.undoSave();
  assert.deepEqual(h.requests,[{version:7,undo_document_edit:true}]);
  assert.equal(JSON.stringify(h.report()),before);assert.equal(h.commits.length,0);
  assert.equal(e.saving,false);assert.equal(e.active,false);assert.match(h.toasts.at(-1),/撤回未完成：文稿已更新/);
  assert.equal(h.get('#undo-document-save').hidden,false);assert.equal(h.get('#undo-document-save').disabled,false);
});

/* Copyright 2026 AshleySama
 * SPDX-License-Identifier: Apache-2.0 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '..', 'frontend', 'reports-v2.js'), 'utf8');

function element() {
  const classes = new Set(), listeners = new Map(), attributes = new Map();
  return {
    textContent:'',innerHTML:'',value:'',dataset:{},hidden:false,disabled:false,checked:false,indeterminate:false,
    classList:{add:x=>classes.add(x),remove:x=>classes.delete(x),contains:x=>classes.has(x),toggle(x,force){const yes=force??!classes.has(x);if(yes)classes.add(x);else classes.delete(x);return yes;}},
    append(){},focus(){},select(){},scrollIntoView(){},querySelector(){return null;},querySelectorAll(){return [];},
    showModal(){this.open=true;},close(){this.open=false;},
    setAttribute(k,v){attributes.set(k,String(v));},getAttribute(k){return attributes.get(k);},
    addEventListener(type,fn){listeners.set(type,[...(listeners.get(type)||[]),fn]);},
  };
}

function fixture(letter,version=1){return {id:letter.repeat(32),title:'报告 '+letter,version,template:'project',updated:'2026-11-18T00:00:00Z',edited:false};}

async function harness(){
  const nodes=new Map(),get=key=>{if(!nodes.has(key))nodes.set(key,element());return nodes.get(key);};
  let catalogue=['a','b','c'].map((letter,i)=>fixture(letter,i+1)),deleted=[],rejectDelete='',rejectRestore='';
  const requests=[],opened=[];
  const context=vm.createContext({
    document:{...element(),querySelector:get,querySelectorAll:()=>[],createElement:element,body:element()},
    location:{search:''},history:{replaceState(){}},URLSearchParams,console,
    window:{scrollTo(){},open(){}},navigator:{clipboard:{writeText:async()=>{}}},
    localStorage:{getItem(){return null;},setItem(){},removeItem(){}},setTimeout:()=>1,clearTimeout(){},
    async fetch(url,options){
      const payload=options?.body?JSON.parse(options.body):null;
      if(url==='/api/report-workspace')return {ok:true,json:async()=>({templates:[],reports:JSON.parse(JSON.stringify(catalogue))})};
      if(url.endsWith('/delete-batch')){
        requests.push({url,payload});
        if(rejectDelete)return {ok:false,json:async()=>({error:rejectDelete})};
        const ids=new Set(payload.reports.map(r=>r.id));deleted=catalogue.filter(r=>ids.has(r.id));catalogue=catalogue.filter(r=>!ids.has(r.id));
        return {ok:true,json:async()=>({deleted:deleted.map(r=>r.id),count:deleted.length,undo_token:'d'.repeat(32)})};
      }
      if(url.endsWith('/restore-batch')){
        requests.push({url,payload});
        if(rejectRestore)return {ok:false,json:async()=>({error:rejectRestore})};
        const restored=deleted.map(r=>r.id);catalogue.push(...deleted);deleted=[];
        return {ok:true,json:async()=>({restored,count:restored.length})};
      }
      if(url.startsWith('/api/report-workspace/')){const report=catalogue.find(r=>r.id===url.split('/').pop());opened.push(report?.id);return {ok:true,json:async()=>({report,html:'<h1>'+report.title+'</h1>'})};}
      throw Error('Unexpected request: '+url);
    },
  });
  vm.runInContext(source,context,{filename:'reports-v2.js',timeout:2000});
  await new Promise(resolve=>setImmediate(resolve));
  return {get,requests,opened,run:code=>vm.runInContext(code,context,{timeout:2000}),
    rejectDelete:message=>{rejectDelete=message;},rejectRestore:message=>{rejectRestore=message;}};
}

test('batch selection shows per-report checkboxes, select-all and exact counts',async()=>{
  const h=await harness();h.get('#history-manage').onclick();
  assert.equal(h.get('#history-batch-actions').hidden,false);
  assert.match(h.get('#history').innerHTML,/data-select-report/);
  h.get('#history').onchange({target:{dataset:{selectReport:'a'.repeat(32)},checked:true}});
  assert.equal(h.get('#history-selection-count').textContent,'已选 1 / 3');
  assert.equal(h.get('#history-select-all').indeterminate,true);
  h.get('#history-select-all').onchange({target:{checked:true}});
  assert.equal(h.get('#history-selection-count').textContent,'已选 3 / 3');
  assert.equal(h.get('#history-select-all').checked,true);
  h.get('#history-manage').onclick();
  assert.equal(h.run('state.historySelected.size'),0);
  assert.equal(h.requests.length,0);
});

test('delete-selected confirms count and posts only selected versions; one undo restores them',async()=>{
  const h=await harness();h.get('#history-manage').onclick();
  for(const letter of ['a','c'])h.get('#history').onchange({target:{dataset:{selectReport:letter.repeat(32)},checked:true}});
  h.get('#history-delete-selected').onclick();
  assert.equal(h.get('#delete-title').textContent,'删除选中的 2 份文稿？');
  assert.equal(h.requests.length,0);
  await h.get('#delete-form').onsubmit({preventDefault(){}});
  assert.deepEqual(h.requests[0].payload,{reports:[{id:'a'.repeat(32),version:1},{id:'c'.repeat(32),version:3}],all_reports:false});
  assert.equal(h.run('state.reports.length'),1);
  assert.equal(h.get('#history-undo-status').textContent,'已删除 2 份文稿');
  await h.get('#history-undo-button').onclick();
  assert.deepEqual(h.requests[1].payload,{undo_token:'d'.repeat(32)});
  assert.equal(h.run('state.reports.length'),3);
  assert.equal(h.get('#history-undo').hidden,true);
});

test('clear-all requires an explicit count confirmation with all known versions',async()=>{
  const h=await harness();h.get('#history-clear-all').onclick();
  assert.equal(h.get('#delete-title').textContent,'清空全部 3 份文稿？');
  assert.equal(h.get('#confirm-delete').textContent,'清空 3 份文稿');
  assert.equal(h.requests.length,0);
  await h.get('#delete-form').onsubmit({preventDefault(){}});
  assert.equal(h.requests[0].payload.all_reports,true);
  assert.deepEqual(h.requests[0].payload.reports.map(r=>r.version),[1,2,3]);
  assert.equal(h.run('state.reports.length'),0);
  assert.equal(h.get('#history-clear-all').disabled,true);
  assert.equal(h.get('#history-undo').hidden,false);
});

test('a stale or active batch rejection keeps the reports and the confirmation error',async()=>{
  const h=await harness();h.rejectDelete('所选报告正在成稿，本批未删除');
  h.get('#history-clear-all').onclick();await h.get('#delete-form').onsubmit({preventDefault(){}});
  assert.equal(h.run('state.reports.length'),3);
  assert.equal(h.get('#delete-dialog').open,true);
  assert.match(h.get('#delete-error').textContent,/本批未删除/);
  assert.equal(h.run('state.historyUndo'),null);
  assert.equal(h.get('#confirm-delete').disabled,false);
});

test('deleting an open report clears the view and undo reopens that report',async()=>{
  const h=await harness();h.run('state.report=state.reports[0]');
  h.run(`requestDelete('${'a'.repeat(32)}')`);await h.get('#delete-form').onsubmit({preventDefault(){}});
  assert.equal(h.run('state.report'),null);
  await h.get('#history-undo-button').onclick();
  assert.equal(h.run('state.report.id'),'a'.repeat(32));
  assert.deepEqual(h.opened,['a'.repeat(32)]);
});

test('undo failure retains the same batch token for a retry',async()=>{
  const h=await harness();h.get('#history-clear-all').onclick();await h.get('#delete-form').onsubmit({preventDefault(){}});
  h.rejectRestore('同编号报告已存在，本批未恢复');await h.get('#history-undo-button').onclick();
  assert.equal(h.run('state.historyUndo.undo_token'),'d'.repeat(32));
  assert.match(h.get('#toast').textContent,/本批未恢复/);
  assert.equal(h.get('#history-undo-button').disabled,false);
  h.rejectRestore('');await h.get('#history-undo-button').onclick();
  assert.equal(h.run('state.reports.length'),3);
});

test('refresh prunes selections removed elsewhere while retaining valid selections',async()=>{
  const h=await harness();h.run(`state.historySelecting=true;state.historySelected=new Set(['${'a'.repeat(32)}','${'z'.repeat(32)}']);renderHistory()`);
  assert.equal(h.run('state.historySelected.size'),1);
  assert.equal(h.get('#history-selection-count').textContent,'已选 1 / 3');
});

/* Copyright 2026 AshleySama
 * SPDX-License-Identifier: Apache-2.0
 */

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../frontend/app.js'), 'utf8');
const start = source.indexOf('let directorySelectionRevision =');
const end = source.indexOf('$("#data-dir").addEventListener("input"', start);
function setup(choose) {
  const ids = ['settings-dialog', 'choose-directory', 'data-dir', 'switch-directory', 'migrate-directory', 'settings-load-status'];
  const els = Object.fromEntries(ids.map(id => [id, {handlers:{}, disabled:false, dataset:{}, addEventListener(k, f){this.handlers[k]=f;}, focus(){this.focused=true;}}]));
  els['settings-dialog'].open = true;
  els['data-dir'].value = 'C:\\original';
  els['choose-directory'].textContent = '选择新目录';
  els['switch-directory'].disabled = els['migrate-directory'].disabled = true;
  const ctx = {state:{selectedDirectory:null}, settingsLoadSession:{}, $:id=>els[id.slice(1)], window:{pywebview:{api:{choose_data_directory:choose}}}, setTimeout, clearTimeout, showToast:(...v)=>ctx.toasts.push(v), toasts:[]};
  vm.createContext(ctx); vm.runInContext(source.slice(start,end),ctx);
  return {els,ctx,click:()=>els['choose-directory'].handlers.click()};
}
(async () => {
  const ok=setup(async initial=>{assert.equal(initial,'C:\\original');return {data_dir:'C:\\中文资料'};});
  await ok.click();
  assert.equal(ok.els['data-dir'].value,'C:\\中文资料');
  assert.equal(ok.els['switch-directory'].disabled,false);
  assert.equal(ok.els['choose-directory'].disabled,false);
  const cancel=setup(async()=>({data_dir:null})); await cancel.click();
  assert.equal(cancel.els['data-dir'].value,'C:\\original');
  assert.equal(cancel.els['switch-directory'].disabled,true);
  assert.equal(cancel.ctx.toasts.length,0);
  const fail=setup(async()=>({error:'无法打开'})); await fail.click();
  assert.equal(fail.ctx.toasts[0][0],'选择目录失败');
  assert.equal(fail.els['choose-directory'].disabled,false);
  let finish, calls=0;
  const late=setup(()=>{calls++;return new Promise(r=>finish=r);});
  const pending=late.click(); await Promise.resolve(); await late.click();
  assert.equal(calls,1);
  late.els['settings-dialog'].open=false; late.els['settings-dialog'].handlers.close();
  late.els['settings-dialog'].open=true;
  finish({data_dir:'C:\\late'}); await pending;
  assert.equal(late.els['data-dir'].value,'C:\\original');
  assert.equal(late.els['choose-directory'].disabled,false);
  const edited=setup(async()=>{edited.els['data-dir'].value='C:\\typed';return {data_dir:'C:\\late'};});
  await edited.click(); assert.equal(edited.els['data-dir'].value,'C:\\typed');
  console.log('Native directory UI: select/cancel/error/reentry/stale/edit checks passed.');
})().catch(e=>{console.error(e); process.exitCode=1;});

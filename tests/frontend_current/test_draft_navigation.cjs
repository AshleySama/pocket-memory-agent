const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const assert = require('node:assert/strict'), crypto = require('node:crypto');
const root = path.join(__dirname, '../../frontend');
const app = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
const ui = fs.readFileSync(path.join(root, 'draft-ui.js'), 'utf8');
const take = (s, a, b) => s.slice(s.indexOf(a), s.indexOf(b, s.indexOf(a)));
const code = fs.readFileSync(path.join(root, 'drafts.js'), 'utf8') + '\n' +
  take(ui, 'function capturePersistentNew()', 'function currentDraftKey()') +
  take(app, 'async function prepareEditorNavigation()', 'async function prepareLibraryChange()');
async function setup() {
  const saved = new Map(), closed = [], local = new Map(), notices = [];
  let fail = false, block = null, confirmations = 0;
  const storage = {get length(){return local.size;},key:i=>[...local.keys()][i],getItem:k=>local.get(k),setItem:(k,v)=>local.set(k,v),removeItem:k=>local.delete(k)};
  const api = async (url, opts = {}) => {
    if (!opts.method) return {library_id:'test',drafts:[...saved.values()],closed_ids:closed};
    const data = JSON.parse(opts.body);
    if (opts.method === 'POST') { if (block) await block; if (fail) throw Error('disk unavailable'); saved.set(data.draft_id,{...data});return {draft:{...data}}; }
    closed.push(url.split('/').pop()); saved.delete(url.split('/').pop()); return {};
  };
  const c = {window:{},crypto:crypto.webcrypto,setTimeout,clearTimeout,Blob,navigator:{},
    state:{newNoteDraft:{title:'草稿甲',content:'正文第一行\n最后一句',folder_id:7},imageDataUrl:'data:image/png;base64,AA==',imageName:'图.png'},
    $:()=>null,flushPendingContentSaves:async()=>true,showConfirm:async()=>{confirmations++;return true;},showToast:(...v)=>notices.push(v),paintDraftProtection(){}};
  vm.createContext(c);vm.runInContext(code,c);
  c.draftVault = new c.window.PocketDraftVault({api,storage,delay:100000});
  c.syncNewNoteDraftFromDom = () => c.capturePersistentNew();
  await c.draftVault.init();
  return {c,saved,closed,notices,storage,api,get confirmations(){return confirmations;},fail:(value=true)=>fail=value,block:p=>block=p};
}
const results=[];
async function test(name,run){const s=await setup();try{await run(s);results.push({name,pass:true});}catch(e){results.push({name,pass:false,error:e.message});}finally{s.c.draftVault.detach();}}
(async()=>{
  await test('切换持久化全部内容且不弹放弃提示、不删除草稿',async s=>{
    assert.equal(await s.c.prepareEditorNavigation(),true);
    assert.equal(s.confirmations,0); assert.equal(s.closed.length,0); assert.equal(s.saved.size,1);
    const r=[...s.saved.values()][0];assert.equal(r.content,'正文第一行\n最后一句');assert.equal(r.folder_id,7);assert.equal(r.image_name,'图.png');
    assert.equal(s.c.state.newNoteDraft,null);assert.equal(s.c.draftVault.active.has('new'),false);
    s.c.draftVault.detach();await s.c.draftVault.init();assert.equal(s.c.draftVault.records.size,1);
  });
  await test('下一份空白或非空新建都不覆盖已离开的草稿',async s=>{
    await s.c.prepareEditorNavigation(); const old=[...s.saved.keys()][0];
    s.c.state.newNoteDraft={title:'',content:''};await s.c.prepareEditorNavigation();assert(s.saved.has(old));
    s.c.state.newNoteDraft={title:'草稿乙',content:'不同正文'};await s.c.prepareEditorNavigation();
    assert.equal(s.saved.size,2);assert.equal(s.saved.get(old).title,'草稿甲');
  });
  await test('落盘失败留在编辑器，重试后能切换且不丢内容',async s=>{
    s.fail(); assert.equal(await s.c.prepareEditorNavigation(),false);assert.equal(s.c.state.newNoteDraft.title,'草稿甲');
    assert.equal(s.closed.length,0);assert.equal(s.c.draftVault.records.size,1);assert(s.notices.length);
    s.fail(false);assert.equal(await s.c.prepareEditorNavigation(),true);assert.equal(s.saved.size,1);
  });
  await test('等待写入时的新输入也被保留',async s=>{
    let release;s.block(new Promise(r=>release=r));const p=s.c.prepareEditorNavigation();
    await new Promise(r=>setImmediate(r)); s.c.state.newNoteDraft.content='写入期间最后输入';s.c.capturePersistentNew();release();
    assert.equal(await p,true);assert.equal([...s.saved.values()][0].content,'写入期间最后输入');
  });
  await test('快速重复切换仅一个导航成功且保留一份草稿',async s=>{
    const out=await Promise.all([s.c.prepareEditorNavigation(),s.c.prepareEditorNavigation()]);
    assert.equal(out.filter(Boolean).length,1);assert.equal(s.saved.size,1);assert.equal(s.closed.length,0);
  });
  await test('图片仍读取时不离开',async s=>{
    s.c.state.pendingAttachmentReads=1;assert.equal(await s.c.prepareEditorNavigation(),false);assert(s.c.state.newNoteDraft);assert.equal(s.closed.length,0);
  });
  await test('已有笔记保存失败仍阻止切换',async s=>{
    s.c.flushPendingContentSaves=async()=>false;assert.equal(await s.c.prepareEditorNavigation(),false);assert(s.c.state.newNoteDraft);assert.equal(s.closed.length,0);
  });
  await test('等待草稿落盘时开始提交笔记，不解除或清空正在提交的会话',async s=>{
    let release;s.block(new Promise(r=>release=r));const p=s.c.prepareEditorNavigation();
    await new Promise(r=>setImmediate(r));s.c.state.newNoteSaving=true;release();
    assert.equal(await p,false);assert(s.c.state.newNoteDraft);assert(s.c.draftVault.active.has('new'));
  });
  console.log(JSON.stringify(results,null,2));if(results.some(r=>!r.pass))process.exitCode=1;
})();

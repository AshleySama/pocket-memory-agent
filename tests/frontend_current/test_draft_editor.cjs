const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(require('node:path').join(__dirname,'../../frontend/app.js'),'utf8');
const ui=fs.readFileSync(require('node:path').join(__dirname,'../../frontend/draft-ui.js'),'utf8');
const extract=(s,a,b)=>s.slice(s.indexOf(a),s.indexOf(b,s.indexOf(a)));
const funcs=extract(source,'function saveEditorContent(', 'async function toggleNoteEditing(')+extract(ui,'function draftKey(', 'function capturePersistentNew(');
const events=[],session={saved:'O',draft:'A',status:'dirty',baseContent:'O'};
const node={dataset:{noteId:'1'}};
const c={state:{pendingContentSaves:new Map(),draftNoteSources:new Map([[1,{id:1,content:'O'}]]),allNotes:[]},
 captureEditorDraft:()=>session,noteEditorSession:()=>session,refreshEditorSaveState(){},syncCachedNote(){},showToast(){},draftVault:{close:k=>events.push(['close',k]),capture:(k,p)=>events.push(['capture',p.content])}};
vm.createContext(c);vm.runInContext(funcs,c);
(async()=>{
 let resolve;const wait=new Promise(r=>resolve=r);let remote='O', calls=0;
 c.request=async(u,o)=>{remote=JSON.parse(o.body).content;calls++;if(calls===1){await wait;throw Error('response lost')}return{content:remote}};
 const pending=c.saveEditorContent(node); session.draft='O';c.capturePersistentEditor(node,session);
 assert.equal(events.at(-1)[0],'capture');assert.equal(events.at(-1)[1],'O');resolve();assert.equal(await pending,false);
 assert.equal(remote,'A');assert.equal(session.writeUncertain,true);
 await Promise.resolve();assert.equal(await c.saveEditorContent(node),true);assert.equal(remote,'O');assert.equal(calls,2);assert.equal(session.writeUncertain,false);
 const rename=extract(ui,'async function restoreLocalDraft(', 'function editableContentToMarkdownForOriginal(');
 const g={state:{renameSaving:1,pendingContentSaves:new Map()},draftVault:{library:'a'},showToast:()=>events.push(['blocked']),request:()=>{throw Error('must not fetch during rename')}};
 vm.createContext(g);vm.runInContext(rename,g);await g.restoreLocalDraft({library_id:'a'});assert.equal(events.at(-1)[0],'blocked');
 const serializer=extract(source,'function serializeEditableChildren(', '$("#inline-save")');
 const s={Node:{TEXT_NODE:3,ELEMENT_NODE:1},normalizeEditorColor:()=>'',editableTableToMarkdown:()=>''};vm.createContext(s);vm.runInContext(serializer,s);
 const t=x=>({nodeType:3,nodeValue:x}),div=x=>({nodeType:1,tagName:'DIV',childNodes:[t(x)]});
 assert.equal(s.editableContentToMarkdown({childNodes:[t('第一行'),div('第二行'),div('第三行')]}),'第一行\n第二行\n第三行');
 const result={checks:3,pass:true,cases:['PUT结果不确定+回旧正文仍保留并重试','重命名保存中禁止恢复替换','浏览器混合文本块换行恢复']};console.log(result);
})().catch(e=>{console.error(e);process.exitCode=1});

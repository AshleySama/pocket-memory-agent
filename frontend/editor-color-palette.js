/* Copyright 2026 AshleySama | SPDX-License-Identifier: Apache-2.0 */
'use strict';
window.PMColorPalette = (() => {
  const themes=[['白色','#ffffff'],['黑色','#000000'],['暖灰','#e7e6e6'],['蓝灰','#44546a'],['蓝色','#4472c4'],['橙色','#ed7d31'],['灰色','#a5a5a5'],['金色','#ffc000'],['天蓝','#5b9bd5'],['绿色','#70ad47']];
  const standards=[['深红','#c00000'],['红色','#ff0000'],['橙色','#ffc000'],['黄色','#ffff00'],['浅绿','#92d050'],['绿色','#00b050'],['浅蓝','#00b0f0'],['蓝色','#0070c0'],['深蓝','#002060'],['紫色','#7030a0']];
  const RECENT_KEY='pm-editor-recent-colors-v1';let current=null;
  function normalize(color){
    color=String(color||'').toLowerCase().trim();
    const rgb=color.match(/^rgba?\(\s*(\d+)[, ]+\s*(\d+)[, ]+\s*(\d+)/);
    return rgb?'#'+rgb.slice(1,4).map(x=>Math.max(0,Math.min(255,Number(x))).toString(16).padStart(2,'0')).join(''):color;
  }
  function mix(hex,amount){return '#'+hex.slice(1).match(/../g).map(x=>{const n=parseInt(x,16);return Math.round(amount>0?n+(255-n)*amount:n*(1+amount)).toString(16).padStart(2,'0');}).join('');}
  function recent(kind){try{return (JSON.parse(localStorage.getItem(RECENT_KEY)||'{}')[kind]||[]).filter(x=>/^#[0-9a-f]{6}$/.test(x)).slice(0,10);}catch{return [];}}
  function remember(kind,color){try{let all=JSON.parse(localStorage.getItem(RECENT_KEY)||'{}');if(!all||typeof all!=='object'||Array.isArray(all))all={};all[kind]=[color,...recent(kind).filter(x=>x!==color)].slice(0,10);localStorage.setItem(RECENT_KEY,JSON.stringify(all));}catch{}}
  function close(restoreFocus=false){if(!current)return;const {trigger,panel}=current;current=null;panel.remove();trigger.setAttribute('aria-expanded','false');trigger.removeAttribute('aria-controls');if(restoreFocus&&trigger.isConnected)trigger.focus({preventScroll:true});}
  function open({trigger,title='文字颜色',value,defaultValue='#1f2f2d',kind='text',onPick}){
    if(current?.trigger===trigger){close(true);return;}close();
    const selected=normalize(value||defaultValue),panel=document.createElement('div');panel.className='pm-color-palette';panel.id='pm-editor-color-palette';panel.setAttribute('role','dialog');panel.setAttribute('aria-label',title+'色板');
    const heading=document.createElement('div');heading.className='pm-color-palette-title';heading.textContent=title;panel.append(heading);
    const pick=color=>{remember(kind,color);close();trigger.dataset.colorValue=color;trigger.style.setProperty('--editor-picked-color',color);onPick(color);};
    function chip(name,color,extra=''){
      const b=document.createElement('button');b.type='button';b.className='pm-color-chip '+extra;b.style.setProperty('--swatch-color',color);b.setAttribute('aria-label',name);b.title=name+' '+color;b.dataset.color=color;b.setAttribute('aria-pressed',String(color===selected));b.onclick=()=>pick(color);return b;
    }
    const automatic=document.createElement('button');automatic.type='button';automatic.className='pm-color-default';automatic.setAttribute('aria-pressed',String(selected===defaultValue));
    const dot=document.createElement('span');dot.style.background=defaultValue;dot.setAttribute('aria-hidden','true');automatic.append(dot,document.createTextNode(kind==='highlight'?'默认高亮':'自动（默认文字颜色）'));automatic.onclick=()=>pick(defaultValue);panel.append(automatic);
    function group(label){const h=document.createElement('div');h.className='pm-color-section-title';h.textContent=label;panel.append(h);const grid=document.createElement('div');grid.className='pm-color-grid';grid.setAttribute('role','group');grid.setAttribute('aria-label',label);panel.append(grid);return grid;}
    const themeGrid=group('主题颜色');
    themes.forEach(([name,color])=>themeGrid.append(chip('主题'+name,color,'pm-color-base')));
    for(let row=0;row<5;row++)themes.forEach(([name,color],col)=>{
      const ratios=col===0?[-.05,-.15,-.25,-.35,-.5]:col===1?[.85,.65,.45,.25,.1]:[.8,.6,.4,-.25,-.5];
      themeGrid.append(chip(name+'色阶'+(row+1),mix(color,ratios[row])));
    });
    const standardGrid=group('标准颜色');standards.forEach(([name,color])=>standardGrid.append(chip('标准'+name,color)));
    const recents=recent(kind);if(recents.length){const grid=group('最近使用颜色');recents.forEach(color=>grid.append(chip('最近使用 '+color,color)));}
    const more=document.createElement('details');more.className='pm-color-more';const summary=document.createElement('summary');summary.textContent='更多颜色…';more.append(summary);
    const row=document.createElement('div');row.className='pm-color-custom';const label=document.createElement('label');label.textContent='色值';
    const input=document.createElement('input');input.type='text';input.value=/^#[0-9a-f]{6}$/.test(selected)?selected:defaultValue;input.maxLength=7;input.setAttribute('aria-label','自定义颜色色值');input.spellcheck=false;label.append(input);
    const apply=document.createElement('button');apply.type='button';apply.textContent='应用';const error=document.createElement('div');error.className='pm-color-error';error.setAttribute('role','status');
    function applyCustom(){const color=normalize(input.value);if(!/^#[0-9a-f]{6}$/.test(color)){error.textContent='请输入 #RRGGBB，例如 #2563EB';input.focus();return;}pick(color);}
    apply.onclick=applyCustom;input.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();applyCustom();}};row.append(label,apply);more.append(row,error);panel.append(more);
    document.body.append(panel);current={trigger,panel};trigger.setAttribute('aria-expanded','true');trigger.setAttribute('aria-controls',panel.id);
    panel.addEventListener('mousedown',e=>{if(e.target.closest('button'))e.preventDefault();});
    panel.addEventListener('keydown',e=>{
      if(e.key==='Escape'){e.preventDefault();e.stopPropagation();close(true);return;}
      const grid=document.activeElement.closest('.pm-color-grid');
      if(!grid){if(document.activeElement===automatic&&e.key==='ArrowDown'){e.preventDefault();themeGrid.firstElementChild.focus();}return;}
      const chips=Array.from(grid.children),i=chips.indexOf(document.activeElement),delta={ArrowLeft:-1,ArrowRight:1,ArrowUp:-10,ArrowDown:10}[e.key];
      if(delta!==undefined||e.key==='Home'||e.key==='End'){e.preventDefault();const n=e.key==='Home'?0:e.key==='End'?chips.length-1:(i+delta+chips.length)%chips.length;chips[n].focus();}
    });
    const place=()=>{if(current?.panel!==panel)return;const r=trigger.getBoundingClientRect(),w=panel.offsetWidth,h=panel.offsetHeight;panel.style.left=Math.max(8,Math.min(r.left,innerWidth-w-8))+'px';panel.style.top=Math.max(8,r.bottom+7+h<innerHeight?r.bottom+7:r.top-h-7)+'px';};
    more.addEventListener('toggle',place);place();(themeGrid.querySelector('[aria-pressed="true"]')||standardGrid.querySelector('[aria-pressed="true"]')||automatic).focus({preventScroll:true});
  }
  document.addEventListener('pointerdown',e=>{if(current&&!current.panel.contains(e.target)&&!current.trigger.contains(e.target))close();},true);
  document.addEventListener('focusin',e=>{if(current&&!current.panel.contains(e.target)&&!current.trigger.contains(e.target))close();});
  window.addEventListener('resize',()=>close());window.addEventListener('scroll',e=>{if(current&&!current.panel.contains(e.target))close();},true);
  return {open,close};
})();

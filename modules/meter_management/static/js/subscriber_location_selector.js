(function(){
  const root=document.getElementById('mm-location-selector');
  if(!root)return;
  const trigger=document.getElementById('mm-location-trigger');
  const panel=document.getElementById('mm-location-panel');
  const search=document.getElementById('mm-location-search');
  const tree=document.getElementById('mm-location-tree');
  const hidden=document.getElementById('subscriber-location-id');
  const legacy=document.getElementById('legacy-village');
  const value=document.getElementById('mm-location-value');
  const sid=root.dataset.subscriberId;
  const treeUrl=root.dataset.treeUrl;
  const currentUrl=sid?root.dataset.currentUrl:'';
  let data=[];let indexed=new Map();let loaded=false;
  const esc=v=>String(v??'').replace(/[&<>"']/g,s=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]));
  function index(nodes){(nodes||[]).forEach(n=>{indexed.set(String(n.id),n);index(n.children||[]);});}
  function pathOf(n){const a=[];let x=n;while(x){a.unshift(x.name);x=x.parent_id?indexed.get(String(x.parent_id)):null;}return a.join(' / ');}
  function setSelected(n){if(!n)return;hidden.value=n.id;legacy.value=n.name;value.textContent=pathOf(n);value.classList.remove('placeholder');close();}
  function render(nodes,target,depth){
    target.innerHTML='';
    (nodes||[]).forEach(n=>{
      const wrap=document.createElement('div');wrap.className='mm-location-node';
      const row=document.createElement('div');row.className='mm-location-row';row.style.paddingRight=(depth*14+3)+'px';
      const has=(n.children||[]).length>0;
      if(has){const b=document.createElement('button');b.type='button';b.className='mm-location-arrow';b.innerHTML='<i class="bi bi-chevron-left"></i>';row.appendChild(b);
        const ch=document.createElement('div');ch.className='mm-location-children';ch.hidden=true;render(n.children,ch,depth+1);wrap.appendChild(row);wrap.appendChild(ch);b.addEventListener('click',e=>{e.stopPropagation();ch.hidden=!ch.hidden;b.innerHTML=ch.hidden?'<i class="bi bi-chevron-left"></i>':'<i class="bi bi-chevron-down"></i>';});
      }else{const sp=document.createElement('span');sp.className='mm-location-indent';row.appendChild(sp);}
      const radio=document.createElement('input');radio.type='radio';radio.name='mm-location-choice';radio.className='mm-location-radio';radio.checked=String(hidden.value)===String(n.id);row.appendChild(radio);
      const label=document.createElement('div');label.className='mm-location-label';label.innerHTML='<div class="mm-location-name">'+esc(n.name)+'</div><div class="mm-location-meta">'+esc(n.location_type||'موقع')+' · '+(n.subscriber_count||0)+' مشترك</div>';row.appendChild(label);
      row.addEventListener('click',e=>{if(e.target!==radio && e.target.closest('.mm-location-arrow'))return;setSelected(n);});radio.addEventListener('change',()=>setSelected(n));wrap.insertBefore(row,wrap.firstChild);target.appendChild(wrap);
    });
  }
  function showResults(q){
    const box=document.getElementById('mm-location-results');if(!q){box.hidden=true;tree.hidden=false;return;}tree.hidden=true;box.hidden=false;const qq=q.toLocaleLowerCase();const matches=[];indexed.forEach(n=>{const p=pathOf(n);if((n.name||'').toLocaleLowerCase().includes(qq)||p.toLocaleLowerCase().includes(qq)||(n.code||'').toLocaleLowerCase().includes(qq))matches.push(n);});box.innerHTML=matches.length?matches.map(n=>'<div class="mm-location-result" data-id="'+n.id+'"><strong>'+esc(n.name)+'</strong><div class="mm-location-path">'+esc(pathOf(n))+'</div></div>').join(''):'<div class="mm-location-empty">لا توجد نتيجة مطابقة.</div>';box.querySelectorAll('[data-id]').forEach(el=>el.addEventListener('click',()=>setSelected(indexed.get(String(el.dataset.id)))));}
  async function load(){if(loaded)return;const res=await fetch(treeUrl,{headers:{Accept:'application/json'}});if(!res.ok)throw new Error('tree');const json=await res.json();data=json.tree||[];indexed.clear();index(data);render(data,tree,0);loaded=true;
    if(currentUrl){try{const cr=await fetch(currentUrl,{headers:{Accept:'application/json'}});if(cr.ok){const c=await cr.json();if(c.location_id){hidden.value=c.location_id;value.textContent=c.path||c.name||'';value.classList.remove('placeholder');}}}catch(e){}}
  }
  async function open(){trigger.classList.add('open');panel.classList.add('open');search.focus();try{await load();}catch(e){tree.innerHTML='<div class="mm-location-empty">تعذر تحميل شجرة المواقع.</div>';}}
  function close(){trigger.classList.remove('open');panel.classList.remove('open');}
  trigger.addEventListener('click',async e=>{e.preventDefault();panel.classList.contains('open')?close():await open();});
  search.addEventListener('input',()=>showResults(search.value.trim()));
  document.addEventListener('click',e=>{if(!root.contains(e.target))close();});
  root.closest('form')?.addEventListener('submit',()=>{if(panel.classList.contains('open'))close();});
  if(legacy.value){value.textContent=legacy.value;value.classList.remove('placeholder');}
  window.MMOpenLocationSelector=open;
})();
(function(){
  const root=document.getElementById('location-parent-selector');
  if(!root)return;

  const trigger=document.getElementById('location-parent-trigger');
  const panel=document.getElementById('location-parent-panel');
  const search=document.getElementById('location-parent-search');
  const tree=document.getElementById('location-parent-tree');
  const results=document.getElementById('location-parent-results');
  const clear=document.getElementById('location-parent-clear');
  const hidden=document.getElementById('location-parent-id');
  const value=document.getElementById('location-parent-value');
  const blockedId=String(root.dataset.blockedId||'');
  const treeUrl=root.dataset.treeUrl||'';

  let loaded=false;
  let data=[];
  let indexed=new Map();

  const esc=v=>String(v??'').replace(/[&<>"']/g,s=>({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[s]));

  function isBlocked(n){
    return blockedId && String(n.id)===blockedId;
  }

  function index(nodes){
    (nodes||[]).forEach(n=>{
      if(isBlocked(n))return;
      indexed.set(String(n.id),n);
      index(n.children||[]);
    });
  }

  function pathOf(n){
    const parts=[];let cur=n;
    while(cur){
      parts.unshift(cur.name);
      cur=cur.parent_id?indexed.get(String(cur.parent_id)):null;
    }
    return parts.join(' / ');
  }

  function setEmpty(){
    value.textContent='اختر الموقع الأب من الشجرة';
    value.classList.add('placeholder');
  }

  function setSelected(n){
    if(!n)return;
    hidden.value=n.id;
    value.textContent=pathOf(n);
    value.classList.remove('placeholder');
    close();
  }

  function clearSelected(){
    hidden.value='';
    setEmpty();
    search.value='';
    results.hidden=true;
    tree.hidden=false;
    close();
  }

  function render(nodes,target,depth){
    target.innerHTML='';
    (nodes||[]).forEach(n=>{
      if(isBlocked(n))return;

      const wrap=document.createElement('div');
      wrap.className='mm-location-node';

      const row=document.createElement('div');
      row.className='mm-location-row';
      row.style.paddingRight=(depth*14+3)+'px';

      const has=(n.children||[]).some(x=>!isBlocked(x));
      if(has){
        const b=document.createElement('button');
        b.type='button';
        b.className='mm-location-arrow';
        b.innerHTML='<i class="bi bi-chevron-left"></i>';

        const ch=document.createElement('div');
        ch.className='mm-location-children';
        ch.hidden=true;
        render(n.children,ch,depth+1);

        row.appendChild(b);
        wrap.appendChild(row);
        wrap.appendChild(ch);

        b.addEventListener('click',e=>{
          e.stopPropagation();
          ch.hidden=!ch.hidden;
          b.innerHTML=ch.hidden
            ?'<i class="bi bi-chevron-left"></i>'
            :'<i class="bi bi-chevron-down"></i>';
        });
      }else{
        const sp=document.createElement('span');
        sp.className='mm-location-indent';
        row.appendChild(sp);
        wrap.appendChild(row);
      }

      const radio=document.createElement('input');
      radio.type='radio';
      radio.name='mm-parent-choice';
      radio.className='mm-location-radio';
      radio.checked=String(hidden.value)===String(n.id);
      row.appendChild(radio);

      const label=document.createElement('div');
      label.className='mm-location-label';
      label.innerHTML='<div class="mm-location-name">'+esc(n.name)+'</div>' +
        '<div class="mm-location-meta">'+esc(n.location_type||'موقع')+' · '+(n.subscriber_count||0)+' مشترك · '+(n.meter_count||0)+' عداد</div>';
      row.appendChild(label);

      row.addEventListener('click',e=>{
        if(e.target!==radio && e.target.closest('.mm-location-arrow'))return;
        setSelected(n);
      });
      radio.addEventListener('change',()=>setSelected(n));

      target.appendChild(wrap);
    });
  }

  function showResults(q){
    if(!q){
      results.hidden=true;
      tree.hidden=false;
      return;
    }
    tree.hidden=true;
    results.hidden=false;

    const qq=q.toLocaleLowerCase();
    const matches=[];
    indexed.forEach(n=>{
      const p=pathOf(n);
      if((n.name||'').toLocaleLowerCase().includes(qq) ||
         p.toLocaleLowerCase().includes(qq) ||
         (n.code||'').toLocaleLowerCase().includes(qq)){
        matches.push(n);
      }
    });

    results.innerHTML=matches.length
      ?matches.map(n=>'<div class="mm-location-result" data-id="'+n.id+'">' +
        '<strong>'+esc(n.name)+'</strong><div class="mm-location-path">'+esc(pathOf(n))+'</div></div>').join('')
      :'<div class="mm-location-empty">لا توجد نتيجة مطابقة.</div>';

    results.querySelectorAll('[data-id]').forEach(el=>{
      el.addEventListener('click',()=>setSelected(indexed.get(String(el.dataset.id))));
    });
  }

  async function load(){
    if(loaded)return;
    if(!treeUrl){
      tree.innerHTML='<div class="mm-location-empty">تعذر تحديد شجرة المواقع.</div>';
      return;
    }
    try{
      const res=await fetch(treeUrl,{headers:{Accept:'application/json'}});
      if(!res.ok)throw new Error('tree');
      const json=await res.json();
      data=json.tree||[];
      indexed.clear();
      index(data);
      render(data,tree,0);
      loaded=true;
      if(!indexed.size)tree.innerHTML='<div class="mm-location-empty">لا توجد مواقع مضافة بعد.</div>';
    }catch(e){
      tree.innerHTML='<div class="mm-location-empty">تعذر تحميل شجرة المواقع.</div>';
    }
  }

  async function open(){
    trigger.classList.add('open');
    panel.classList.add('open');
    await load();
    search.focus();
  }

  function close(){
    trigger.classList.remove('open');
    panel.classList.remove('open');
  }

  trigger.addEventListener('click',async e=>{
    e.preventDefault();
    panel.classList.contains('open')?close():await open();
  });

  search.addEventListener('input',()=>showResults(search.value.trim()));
  clear?.addEventListener('click',e=>{e.preventDefault();clearSelected();});
  document.addEventListener('click',e=>{if(!root.contains(e.target))close();});
  root.closest('form')?.addEventListener('submit',()=>close());

  window.MMOpenParentLocationSelector=open;
})();
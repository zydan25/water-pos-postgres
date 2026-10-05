(function(){
  const rootNodes=window.MM_LOCATIONS||[];
  const byId=new Map();
  function index(nodes){(nodes||[]).forEach(n=>{byId.set(String(n.id),n);index(n.children);});}
  index(rootNodes);
  function pathOf(id){const parts=[];let n=byId.get(String(id));while(n){parts.unshift(n.name);n=n.parent_id?byId.get(String(n.parent_id)):null;}return parts.join(' / ');}
  function row(n){
    const wrap=document.createElement('div');wrap.className='mm-tree-node';
    const r=document.createElement('div');r.className='mm-tree-row';
    const has=(n.children||[]).length>0;
    if(has){const b=document.createElement('button');b.type='button';b.className='mm-tree-toggle';b.innerHTML='<i class="bi bi-chevron-left"></i>';r.appendChild(b);}
    else{const s=document.createElement('span');s.className='mm-tree-spacer';r.appendChild(s);}
    const c=document.createElement('div');c.className='mm-tree-content';c.innerHTML='<div class="mm-tree-name">'+esc(n.name)+'</div><div class="mm-tree-meta">'+esc(n.location_type||'موقع')+' · '+n.subscriber_count+' مشترك · '+n.meter_count+' عداد</div>';r.appendChild(c);
    const actions=document.createElement('div');actions.className='mm-tree-actions';actions.innerHTML='<a class="btn btn-sm btn-outline-secondary" href="/meter-management/locations/'+n.id+'/edit">تعديل</a>';
    r.appendChild(actions);wrap.appendChild(r);
    if(has){const ch=document.createElement('div');ch.className='mm-tree-children';ch.hidden=true;(n.children||[]).forEach(x=>ch.appendChild(row(x)));wrap.appendChild(ch);r.querySelector('button').addEventListener('click',()=>{ch.hidden=!ch.hidden;r.querySelector('i').className=ch.hidden?'bi bi-chevron-left':'bi bi-chevron-down';});}
    return wrap;
  }
  function esc(v){return String(v??'').replace(/[&<>"']/g,s=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]));}
  document.querySelectorAll('.mm-tree[data-unit-id]').forEach(el=>{
    const unitId=String(el.dataset.unitId);const nodes=rootNodes.filter(n=>String(n.unit_id)===unitId);
    nodes.forEach(n=>el.appendChild(row(n)));
    if(!nodes.length)el.innerHTML='<div class="mm-empty">لا توجد مواقع مرتبطة بهذه الوحدة حتى الآن.</div>';
  });
})();

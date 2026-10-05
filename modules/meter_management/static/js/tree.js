document.addEventListener('DOMContentLoaded', function(){

  const rootNodes=window.MM_LOCATIONS||[];
  const byId=new Map();
  function index(nodes){(nodes||[]).forEach(n=>{byId.set(String(n.id),n);index(n.children);});}
  index(rootNodes);
  function row(n){
    const wrap=document.createElement('div');wrap.className='mm-tree-node';
    const r=document.createElement('div');r.className='mm-tree-row';
    const has=(n.children||[]).length>0;
    if(has){const b=document.createElement('button');b.type='button';b.className='mm-tree-toggle';b.innerHTML='<i class="bi bi-chevron-left"></i>';r.appendChild(b);
      const c=document.createElement('div');c.className='mm-tree-content';c.innerHTML='<div class="mm-tree-name">'+esc(n.name)+'</div><div class="mm-tree-meta">'+esc(n.location_type||'موقع')+' · '+(n.subscriber_count||0)+' مشترك · '+(n.meter_count||0)+' عداد</div>';r.appendChild(c);
      const actions=document.createElement('div');actions.className='mm-tree-actions';actions.innerHTML='<a class="btn btn-sm btn-outline-secondary" href="/meter-management/locations/'+n.id+'/edit">تعديل</a>';r.appendChild(actions);
      wrap.appendChild(r);const ch=document.createElement('div');ch.className='mm-tree-children';ch.hidden=true;(n.children||[]).forEach(x=>ch.appendChild(row(x)));wrap.appendChild(ch);
      b.addEventListener('click',()=>{ch.hidden=!ch.hidden;b.innerHTML=ch.hidden?'<i class="bi bi-chevron-left"></i>':'<i class="bi bi-chevron-down"></i>';});
    }else{const s=document.createElement('span');s.className='mm-tree-spacer';r.appendChild(s);const c=document.createElement('div');c.className='mm-tree-content';c.innerHTML='<div class="mm-tree-name">'+esc(n.name)+'</div><div class="mm-tree-meta">'+esc(n.location_type||'موقع')+' · '+(n.subscriber_count||0)+' مشترك · '+(n.meter_count||0)+' عداد</div>';r.appendChild(c);const actions=document.createElement('div');actions.className='mm-tree-actions';actions.innerHTML='<a class="btn btn-sm btn-outline-secondary" href="/meter-management/locations/'+n.id+'/edit">تعديل</a>';r.appendChild(actions);wrap.appendChild(r);}
    return wrap;
  }
  function esc(v){return String(v??'').replace(/[&<>"']/g,s=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]));}
  document.querySelectorAll('.mm-tree[data-unit-id]').forEach(el=>{
    const id=String(el.dataset.unitId);
    const nodes=rootNodes.filter(n=>{
      if(id==='__all__') return true;
      if(id==='__none__') return !n.unit_id;
      return String(n.unit_id)===id;
    });
    nodes.forEach(n=>el.appendChild(row(n)));
    if(!nodes.length)el.innerHTML='<div class="mm-empty">لا توجد مواقع في هذا القسم.</div>';
  });
}
});
(function(){
  const root = document.getElementById('network-location-selector');
  if (!root) return;

  const trigger = document.getElementById('network-location-trigger');
  const panel = document.getElementById('network-location-panel');
  const search = document.getElementById('network-location-search');
  const tree = document.getElementById('network-location-tree');
  const results = document.getElementById('network-location-results');
  const clear = document.getElementById('network-location-clear');
  const hidden = document.getElementById('network-location-id');
  const value = document.getElementById('network-location-value');

  const treeUrl = "{{ url_for('meter_management.api_locations_tree') }}";
  let indexed = new Map();
  let loaded = false;

  const esc = v => String(v ?? '').replace(/[&<>"']/g, s => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[s]));

  function index(nodes){
    (nodes || []).forEach(n => {
      indexed.set(String(n.id), n);
      index(n.children || []);
    });
  }

  function pathOf(n){
    const parts = [];
    let current = n;
    while(current){
      parts.unshift(current.name);
      current = current.parent_id ? indexed.get(String(current.parent_id)) : null;
    }
    return parts.join(' / ');
  }

  function setEmpty(){
    value.textContent = 'اختر الموقع من الشجرة';
    value.classList.add('placeholder');
  }

  function setSelected(n){
    if(!n) return;
    hidden.value = n.id;
    value.textContent = pathOf(n);
    value.classList.remove('placeholder');
    close();
  }

  function clearSelected(){
    hidden.value = '';
    setEmpty();
    search.value = '';
    results.hidden = true;
    tree.hidden = false;
    close();
  }

  function render(nodes, target, depth){
    target.innerHTML = '';

    (nodes || []).forEach(n => {
      const wrap = document.createElement('div');
      wrap.className = 'mm-location-node';

      const row = document.createElement('div');
      row.className = 'mm-location-row';
      row.style.paddingRight = (depth * 14 + 3) + 'px';

      const hasChildren = (n.children || []).length > 0;

      if(hasChildren){
        const arrow = document.createElement('button');
        arrow.type = 'button';
        arrow.className = 'mm-location-arrow';
        arrow.innerHTML = '<i class="bi bi-chevron-left"></i>';

        const children = document.createElement('div');
        children.className = 'mm-location-children';
        children.hidden = true;
        render(n.children, children, depth + 1);

        arrow.addEventListener('click', e => {
          e.stopPropagation();
          children.hidden = !children.hidden;
          arrow.innerHTML = children.hidden
            ? '<i class="bi bi-chevron-left"></i>'
            : '<i class="bi bi-chevron-down"></i>';
        });

        row.appendChild(arrow);
        wrap.appendChild(row);
        wrap.appendChild(children);
      } else {
        const spacer = document.createElement('span');
        spacer.className = 'mm-location-indent';
        row.appendChild(spacer);
        wrap.appendChild(row);
      }

      const radio = document.createElement('input');
      radio.type = 'radio';
      radio.name = 'network-location-choice';
      radio.className = 'mm-location-radio';
      radio.checked = String(hidden.value) === String(n.id);

      const label = document.createElement('div');
      label.className = 'mm-location-label';
      label.innerHTML =
        '<div class="mm-location-name">' + esc(n.name) + '</div>' +
        '<div class="mm-location-meta">' +
        esc(n.location_type || 'موقع') +
        ' · ' + (n.subscriber_count || 0) + ' مشترك · ' +
        (n.meter_count || 0) + ' عداد</div>';

      row.appendChild(radio);
      row.appendChild(label);

      row.addEventListener('click', e => {
        if (e.target.closest('.mm-location-arrow')) return;
        setSelected(n);
      });
      radio.addEventListener('change', () => setSelected(n));

      target.appendChild(wrap);
    });

    if (!(nodes || []).length) {
      target.innerHTML = '<div class="mm-location-empty">لا توجد مواقع.</div>';
    }
  }

  function showResults(q){
    if(!q){
      results.hidden = true;
      tree.hidden = false;
      return;
    }

    results.hidden = false;
    tree.hidden = true;

    const needle = q.toLocaleLowerCase();
    const matches = [];

    indexed.forEach(n => {
      const path = pathOf(n);
      if(
        (n.name || '').toLocaleLowerCase().includes(needle) ||
        path.toLocaleLowerCase().includes(needle) ||
        (n.code || '').toLocaleLowerCase().includes(needle)
      ){
        matches.push(n);
      }
    });

    results.innerHTML = matches.length
      ? matches.map(n =>
          '<div class="mm-location-result" data-id="' + n.id + '">' +
          '<strong>' + esc(n.name) + '</strong>' +
          '<div class="mm-location-path">' + esc(pathOf(n)) + '</div>' +
          '</div>'
        ).join('')
      : '<div class="mm-location-empty">لا توجد نتيجة مطابقة.</div>';

    results.querySelectorAll('[data-id]').forEach(el => {
      el.addEventListener('click', () => setSelected(indexed.get(String(el.dataset.id))));
    });
  }

  async function load(){
    if(loaded) return;

    const response = await fetch(treeUrl, {headers:{Accept:'application/json'}});
    if(!response.ok) throw new Error('tree');

    const json = await response.json();
    const data = json.tree || [];

    indexed.clear();
    index(data);
    render(data, tree, 0);

    loaded = true;
  }

  async function open(){
    trigger.classList.add('open');
    panel.classList.add('open');

    try{
      await load();
      search.focus();
    }catch(e){
      tree.innerHTML = '<div class="mm-location-empty">تعذر تحميل شجرة المواقع.</div>';
    }
  }

  function close(){
    trigger.classList.remove('open');
    panel.classList.remove('open');
  }

  trigger.addEventListener('click', async e => {
    e.preventDefault();
    if(panel.classList.contains('open')) close();
    else await open();
  });

  search.addEventListener('input', () => showResults(search.value.trim()));

  clear?.addEventListener('click', e => {
    e.preventDefault();
    clearSelected();
  });

  document.addEventListener('click', e => {
    if(!root.contains(e.target)) close();
  });

  root.closest('form')?.addEventListener('submit', () => close());
})();
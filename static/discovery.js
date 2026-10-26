(() => {
  let filled = false, working = false, state = null, loading = false, commandError = '';
  const card = $('#discovery-card');
  function locate() {
    const slot = $(online ? '#discovery-settings-slot' : '#discovery-offline-slot');
    if (card.parentElement !== slot) slot.append(card);
    card.hidden = !state || connInfo?.state === 'locked';
  }
  function render(s) {
    state = s; locate();
    $('#discovery-state').textContent = s.scanning ? `Ищем колонки… ${s.checked} / ${s.total}` :
      s.devices.length ? `Найдено колонок: ${s.devices.filter(d => d.available).length}` : 'Колонки пока не найдены';
    $('#discovery-networks').textContent = `Сети: ${s.networks.join(', ') || 'определяем'} · порты: ${s.ports.join(', ')}`;
    $('#discovery-scan').disabled = working || s.scanning;
    $('#discovery-save').disabled = working || s.scanning;
    $('#discovery-error').hidden = !(commandError || s.error);
    $('#discovery-error').textContent = commandError || s.error || '';
    if (!filled) {
      $('#discovery-auto').checked = s.auto;
      $('#discovery-ports').value = s.ports.join(', ');
      $('#discovery-ranges').value = s.configured_networks.join(', ');
      filled = true;
    }
    const list = $('#discovery-list');
    list.replaceChildren();
    for (const d of s.devices) {
      const row = document.createElement('div'); row.className = 'set-item';
      const label = document.createElement('div'); label.className = 'set-label';
      const name = document.createElement('b'); name.textContent = d.name;
      const detail = document.createElement('small'); detail.textContent = `${d.address} · ${d.available ? 'Доступна' : 'Сейчас недоступна'}`;
      label.append(name, detail);
      const button = document.createElement('button'); button.className = 'btn sm';
      const current = d.url === `/speaker/${PanelContext.deviceId || 'current'}/`;
      button.textContent = current ? 'Открыта' : 'Открыть панель';
      button.disabled = working || !d.available || current;
      button.onclick = () => send('/api/discovery/open', {id:d.id}, true);
      row.append(label, button); list.append(row);
    }
  }
  async function load() {
    locate();
    if (loading || working || connInfo?.state === 'locked') return;
    loading = true;
    try {
      const r = await PanelContext.fetch('/api/discovery');
      if (r.ok) render(await r.json());
    } catch {} finally { loading = false; }
  }
  async function send(path, body, navigate = false) {
    if (working) return;
    working = true; commandError = ''; if (state) render(state);
    try {
      const r = await PanelContext.fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
      const result = await r.json();
      if (!r.ok) throw new Error(result.error || 'Не удалось выполнить поиск');
      if (navigate) {
        if (!/^\/speaker\/[a-z0-9_-]+\/$/.test(result.url)) throw new Error('Некорректный адрес панели');
        location.assign(result.url + '#overview');
      } else render(result);
    } catch (e) {
      commandError = e.message;
    } finally { working = false; if (state) render(state); }
  }
  $('#discovery-scan').onclick = () => send('/api/discovery/scan', {});
  $('#discovery-form').onsubmit = e => {
    e.preventDefault();
    const ports = $('#discovery-ports').value.split(',').map(p => Number(p.trim()));
    const networks = $('#discovery-ranges').value.split(',').map(n => n.trim()).filter(Boolean);
    send('/api/discovery/config', {ports, networks, auto:$('#discovery-auto').checked});
  };
  load();
  setInterval(() => { if (!document.hidden) load(); }, 3000);
})();

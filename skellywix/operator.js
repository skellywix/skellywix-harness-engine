const $ = id => document.getElementById(id);
const owns = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
const tabs = [...document.querySelectorAll('.desk-tabs [role="tab"]')];
const fragments = new URLSearchParams(location.hash.slice(1));
let access = fragments.get('operator') || '';
if (access) history.replaceState(history.state, '', location.pathname + location.search);

const settings = { sources: [], draft: null, key: null, custom: false, loaded: false, busy: false };
const memory = { hints: [], bankHint: '', client: null, graph: null, offset: 0, query: '', loaded: false, busy: false };

function status(id, message, isError = false) {
  const node = $(id);
  node.textContent = message;
  node.classList.toggle('desk-error', isError);
}

async function api(path, body) {
  const response = await fetch(`/operator/${path}`, {
    method: 'POST', cache: 'no-store', credentials: 'omit', redirect: 'error',
    headers: { 'Content-Type': 'application/json', 'X-Skellywix-Operator': access },
    body: JSON.stringify(body),
  });
  const data = response.headers.get('content-type')?.startsWith('application/json') ? await response.json() : null;
  if (!response.ok || data === null) {
    throw new Error(data?.error || (response.status === 403 || response.status === 404
      ? 'Operator access was rejected. Open the private link for this running desk.'
      : `The local desk returned HTTP ${response.status}.`));
  }
  return data;
}

function selectOptions(node, rows, selected = '') {
  node.replaceChildren(...rows.map(([label, value]) => new Option(label, value)));
  if ([...node.options].some(option => option.value === selected)) node.value = selected;
}

const confirmDialog = $('desk-confirm');
confirmDialog.querySelector('form').addEventListener('submit', event => {
  event.preventDefault();
  confirmDialog.close('yes');
});
$('desk-cancel').addEventListener('click', () => confirmDialog.close('no'));

function review(title, detail, action) {
  if (confirmDialog.open) return Promise.resolve(false);
  $('desk-confirm-title').textContent = title;
  $('desk-confirm-detail').textContent = detail;
  $('desk-accept').textContent = action;
  confirmDialog.returnValue = 'no';
  confirmDialog.showModal();
  return new Promise(resolve => confirmDialog.addEventListener('close', () => {
    $('desk-confirm-detail').textContent = '';
    resolve(confirmDialog.returnValue === 'yes');
  }, { once: true }));
}

async function enter(name) {
  const state = name === 'settings' ? settings : memory;
  const statusID = name === 'settings' ? 'settings-status' : 'hindsight-unlock-status';
  if (!access || state.loaded || state.busy) return;
  state.busy = true;
  try {
    status(statusID, 'Discovering local configuration…');
    if (name === 'settings') {
      settings.sources = (await api(name, { action: 'sources' })).sources;
      const preferred = ['omp:active:settings', 'pi:user:settings', 'omp:project:settings', 'pi:project:settings'];
      const available = preferred.map(id => settings.sources.find(source => source.id === id && source.exists)).find(Boolean) ||
        settings.sources.find(source => source.document === 'settings' && source.exists);
      $('settings-harness').value = available?.harness || 'Pi';
      chooseSources(available?.id);
    } else {
      const discovery = await api(name, { action: 'discover' });
      memory.hints = discovery.candidates;
      selectOptions($('hindsight-hint'), memory.hints.map((hint, i) => [hint.source, String(i)]), '0');
      $('hindsight-hint-wrap').hidden = !memory.hints.length;
      chooseHint(discovery.candidate);
    }
    $(`${name}-panel`).querySelector('.desk-lock').hidden = true;
    $(`${name}-body`).hidden = false;
    state.loaded = true;
    status(statusID, '');
  } catch (error) {
    status(statusID, error.message, true);
  } finally {
    state.busy = false;
  }
  if (name === 'settings' && state.loaded && !settings.draft) await openSettings();
}

function showTab(index) {
  tabs.forEach((tab, i) => {
    const active = index === i;
    tab.setAttribute('aria-selected', String(active));
    tab.tabIndex = active ? 0 : -1;
    $(tab.getAttribute('aria-controls')).hidden = !active;
  });
  if (tabs[index].id !== 'tab-map') void enter(tabs[index].id.slice(4));
}

tabs.forEach((tab, i) => {
  tab.addEventListener('click', () => showTab(i));
  tab.addEventListener('keydown', event => {
    const target = ({ ArrowLeft: (i - 1 + tabs.length) % tabs.length, ArrowRight: (i + 1) % tabs.length,
      Home: 0, End: tabs.length - 1 })[event.key];
    if (target === undefined) return;
    event.preventDefault();
    showTab(target);
    tabs[target].focus();
  });
});

document.querySelectorAll('.desk-unlock').forEach(form => form.addEventListener('submit', event => {
  event.preventDefault();
  const key = form.querySelector('input');
  access = key.value.trim();
  key.value = '';
  void enter(form.closest('[role="tabpanel"]').id.split('-')[0]);
}));

function chooseSources(selected) {
  const harness = $('settings-harness').value;
  const rows = settings.sources.filter(source => source.harness === harness);
  selectOptions($('settings-source'), rows.map(s => [`${s.label}${s.exists ? '' : ' (not set)'}`, s.id]), selected);
}

function renderRows() {
  const raw = $('settings-filter').value.toLowerCase().trim();
  const all = settings.draft?.fields || [];
  const rows = all.filter(row => !raw || `${row.key} ${row.description}`.toLowerCase().includes(raw));
  const children = rows.map(field => {
    const line = document.createElement('li');
    const button = document.createElement('button');
    const key = document.createElement('span');
    const detail = document.createElement('small');
    button.type = 'button';
    button.dataset.key = field.key;
    button.setAttribute('aria-pressed', String(!settings.custom && settings.key === field.key));
    key.textContent = field.key;
    detail.textContent = field.stored ? 'Stored' : 'Unset';
    button.append(key, detail);
    line.append(button);
    return line;
  });
  $('settings-rows').replaceChildren(...children);
  status('settings-count', all.length ? `${rows.length} of ${all.length} fields` : 'No registered fields in this source');
}

function inputFor(field) {
  const present = field.stored && !field.secret;
  let node;
  if (field.secret || field.type === 'string' || ['number', 'integer'].includes(field.type)) {
    node = document.createElement('input');
    node.type = field.secret ? 'password' : field.type === 'string' ? 'text' : 'number';
    if (node.type === 'number') node.step = field.type === 'integer' ? '1' : 'any';
    if (present) node.value = field.value;
    if (field.secret) node.placeholder = field.stored ? 'Stored value hidden' : 'Enter a new value';
    node.autocomplete = 'off';
  } else if (field.type === 'boolean' || field.options?.every(option => ['boolean', 'number', 'string'].includes(typeof option))) {
    node = document.createElement('select');
    const options = field.type === 'boolean' ? [true, false] : field.options;
    selectOptions(node, [['Choose a value', ''], ...options.map(option => [String(option), JSON.stringify(option)])],
      present ? JSON.stringify(field.value) : '');
  } else {
    node = document.createElement('textarea');
    node.rows = 8;
    node.spellcheck = false;
    node.placeholder = 'Enter JSON';
    if (present) node.value = JSON.stringify(field.value, null, 2);
  }
  node.id = 'settings-input';
  node.setAttribute('aria-describedby', 'settings-default');
  return node;
}

function activeField() {
  if (settings.custom) return { key: '', type: 'json', stored: false, description: 'Use a native keybinding or language-server name.' };
  return settings.draft?.fields.find(field => field.key === settings.key);
}

function renderEditor() {
  const current = settings.draft;
  const field = activeField();
  $('settings-key').textContent = settings.custom ? 'New custom entry' : field?.key || 'Select a field';
  $('settings-path').textContent = current?.source.path || '';
  $('settings-state').textContent = field?.stored ? 'STORED IN THIS FILE' : 'NOT SET IN THIS FILE';
  $('settings-description').textContent = field?.description || '';
  $('settings-form').hidden = !field;
  $('settings-add').hidden = !current?.canAdd;
  $('settings-undo').hidden = !current?.canUndo;
  const unlisted = current?.unlisted || {};
  $('settings-unlisted').hidden = !Object.keys(unlisted).length;
  $('settings-unlisted').querySelector('pre').textContent = JSON.stringify(unlisted, null, 2);
  if (!field) return;
  $('settings-custom-wrap').hidden = !settings.custom;
  $('settings-custom').value = '';
  const input = inputFor(field);
  $('settings-input')?.remove();
  $('settings-input-wrap').after(input);
  const disabled = Boolean(current.source.readOnly || field.readOnly);
  input.disabled = disabled;
  $('settings-save').disabled = disabled;
  $('settings-reset').hidden = !field.stored || disabled;
  $('settings-default').textContent = field.readOnly ? 'This field is user-scoped and cannot be changed here.' :
    field.secret ? 'The stored credential is never displayed. Enter a new value to replace it.' :
    owns(field, 'default') ? `Built-in default: ${JSON.stringify(field.default)}` :
      'Showing the file value, not a live session override.';
}

function showSettings(result) {
  settings.draft = result;
  if (result) {
    settings.sources = result.sources;
    chooseSources(result.source.id);
  }
  $('settings-boundary').textContent = result?.source.readOnly || (result?.source.harness === 'Pi'
    ? 'These are stored values. Pi applies trusted project settings over user settings.'
    : 'These are stored values. Project files, launch overlays, environment variables, and runtime overrides can take precedence.');
  if (!activeField() && result) settings.key = result.fields.find(field => field.stored)?.key || result.fields[0]?.key || null;
  renderRows();
  renderEditor();
}

async function openSettings({ refresh = false } = {}) {
  if (settings.busy) return;
  settings.busy = true;
  $('settings-controls').disabled = true;
  $('settings-grid')?.setAttribute('aria-busy', 'true');
  try {
    status('settings-status', 'Reading configuration…');
    if (refresh) {
      settings.sources = (await api('settings', { action: 'sources' })).sources;
      chooseSources($('settings-source').value);
    }
    const source = $('settings-source').value;
    settings.custom = false;
    if (source !== settings.draft?.source.id) settings.key = null;
    settings.draft = null;
    renderRows();
    renderEditor();
    showSettings(await api('settings', { action: 'open', source }));
    status('settings-status', '');
  } catch (error) {
    status('settings-status', error.message, true);
  } finally {
    settings.busy = false;
    $('settings-controls').disabled = false;
    $('settings-grid')?.removeAttribute('aria-busy');
  }
}

$('settings-harness').addEventListener('change', () => { chooseSources(); void openSettings(); });
$('settings-source').addEventListener('change', () => void openSettings());
$('settings-refresh').addEventListener('click', () => void openSettings({ refresh: true }));
$('settings-filter').addEventListener('input', renderRows);
$('settings-rows').addEventListener('click', event => {
  const button = event.target.closest('button[data-key]');
  if (!button || settings.busy) return;
  settings.key = button.dataset.key;
  settings.custom = false;
  for (const item of $('settings-rows').querySelectorAll('button')) item.setAttribute('aria-pressed', String(item === button));
  renderEditor();
});
$('settings-add').addEventListener('click', () => {
  if (settings.busy) return;
  settings.custom = true;
  renderRows();
  renderEditor();
  $('settings-custom').focus();
});

async function changeSetting(action) {
  const current = settings.draft;
  const field = activeField();
  if (!current || settings.busy || (action !== 'undo' && !field)) return;
  const key = settings.custom ? $('settings-custom').value.trim() : field?.key;
  const request = { action, source: current.source.id, key, revision: current.revision, confirm: true };
  try {
    if (action === 'save') {
      const input = $('settings-input');
      if (!key) throw new Error('Enter an entry name.');
      if (field.type === 'string' || field.secret) {
        if (field.secret && !input.value) throw new Error('Enter a replacement credential, or remove the override.');
        request.value = input.value;
      } else {
        if (!input.value.trim()) throw new Error('Enter a value before reviewing the change.');
        try { request.value = JSON.parse(input.value); } catch { throw new Error('Enter a valid value or valid JSON.'); }
        if (typeof request.value === 'number' && !Number.isFinite(request.value)) throw new Error('Use a finite number.');
      }
    }
    const verbs = { save: ['Save setting?', 'Save setting'], reset: ['Remove this override?', 'Remove override'],
      undo: ['Undo the last save?', 'Undo save'] };
    const detail = action === 'undo' ? `Restore the previous contents of ${current.source.path}.` :
      `${action === 'save' ? 'Write the value shown in the editor for' : 'Remove only'} ${key} in ${current.source.path}.`;
    if (!await review(verbs[action][0], detail, verbs[action][1])) return;
    settings.busy = true;
    $('settings-controls').disabled = true;
    status('settings-status', 'Checking the change…');
    const result = await api('settings', request);
    settings.custom = false;
    settings.key = key;
    showSettings(result);
    status('settings-status', result.revision === current.revision ? 'No change was needed.' :
      action === 'undo' ? 'Previous file restored.' : 'Saved to the selected source.');
  } catch (error) {
    status('settings-status', error.message, true);
  } finally {
    settings.busy = false;
    $('settings-controls').disabled = false;
  }
}

$('settings-form').addEventListener('submit', event => { event.preventDefault(); void changeSetting('save'); });
$('settings-reset').addEventListener('click', () => void changeSetting('reset'));
$('settings-undo').addEventListener('click', () => void changeSetting('undo'));

function chooseHint(hint) {
  $('hindsight-url').value = hint?.url || '';
  memory.bankHint = hint?.bank || '';
  status('hindsight-discovery', hint
    ? `Found in ${hint.source}${hint.bank ? ` · bank ${hint.bank}` : ''}. Not connected.`
    : 'No Hindsight endpoint was found. Enter an API base URL to connect.');
}

$('hindsight-hint').addEventListener('change', () => chooseHint(memory.hints[Number($('hindsight-hint').value)]));

async function memoryRequest(message, operation) {
  if (memory.busy) return;
  memory.busy = true;
  $('hindsight-network').disabled = true;
  $('hindsight-controls').disabled = true;
  status('hindsight-status', message);
  try {
    await operation();
  } catch (error) {
    status('hindsight-status', error.message, true);
  } finally {
    memory.busy = false;
    $('hindsight-network').disabled = false;
    $('hindsight-controls').disabled = false;
  }
}

function showBanks(data) {
  memory.offset = data.offset;
  $('hindsight-banks').hidden = false;
  $('hindsight-result').hidden = true;
  selectOptions($('hindsight-bank'), data.banks.map(bank => [bank.label, bank.id]), memory.bankHint);
  $('hindsight-load').disabled = !data.banks.length;
  $('hindsight-previous').disabled = data.offset <= 0;
  $('hindsight-next').disabled = data.offset + 100 >= data.total;
  status('hindsight-status', data.total ? `${data.total.toLocaleString()} matching banks. Select a bank to inspect.` : 'No banks matched.');
}

$('hindsight-connect').addEventListener('submit', async event => {
  event.preventDefault();
  const url = $('hindsight-url').value;
  const token = $('hindsight-token').value;
  if (memory.busy || !await review('Connect to Hindsight?',
    `Read banks, graphs, counts, and configurations from ${url}. ${token ? 'Send the entered API key to this address.' : 'No API key will be sent.'}`, 'Connect')) return;
  await memoryRequest('Connecting to the confirmed API…', async () => {
    const data = await api('hindsight', { action: 'connect', url, token, confirm: true });
    memory.client = data.connection;
    memory.query = '';
    $('hindsight-query').value = '';
    $('hindsight-url').value = data.url;
    $('hindsight-token').value = '';
    $('hindsight-disconnect').hidden = false;
    status('hindsight-discovery', `Connected to ${data.url}. Bank data is read-only.`);
    showBanks(data);
  });
});

function readHindsight(body) {
  return api('hindsight', { ...body, connection: memory.client });
}

$('hindsight-disconnect').addEventListener('click', () => void memoryRequest('Disconnecting…', async () => {
  await readHindsight({ action: 'disconnect' });
  memory.client = null;
  memory.graph = null;
  $('hindsight-banks').hidden = true;
  $('hindsight-result').hidden = true;
  $('hindsight-config-text').textContent = '';
  $('hindsight-stats').replaceChildren();
  $('hindsight-node').replaceChildren();
  $('hindsight-bank').replaceChildren();
  $('hindsight-edges').replaceChildren();
  $('hindsight-points').replaceChildren();
  $('hindsight-node-links').replaceChildren();
  $('hindsight-node-heading').textContent = 'Inspect a node';
  $('hindsight-node-text').textContent = '';
  $('hindsight-disconnect').hidden = true;
  status('hindsight-discovery', 'Disconnected. Confirm the API address to connect again.');
  status('hindsight-status', '');
}));

function searchBanks(offset, query = memory.query) {
  return memoryRequest('Loading banks…', async () => {
    const data = await readHindsight({ action: 'list', offset, query });
    memory.query = query;
    showBanks(data);
  });
}

$('hindsight-search').addEventListener('submit', event => { event.preventDefault(); void searchBanks(0, $('hindsight-query').value); });
$('hindsight-previous').addEventListener('click', () => void searchBanks(Math.max(0, memory.offset - 100)));
$('hindsight-next').addEventListener('click', () => void searchBanks(memory.offset + 100));
$('hindsight-load').addEventListener('click', () => void memoryRequest('Reading the selected memory bank…', async () => {
  $('hindsight-result').hidden = true;
  const bank = $('hindsight-bank').value;
  const mode = $('hindsight-mode').value;
  const result = await readHindsight({ action: 'bank', bank, mode });
  showBank(result, mode);
  status('hindsight-status', `Bank ${bank}`);
}));

function showBank(data, mode) {
  const types = { total_nodes: 'Memories', total_links: 'Links', total_documents: 'Documents',
    pending_operations: 'Pending operations', failed_operations: 'Failed operations' };
  $('hindsight-stats').replaceChildren(...Object.entries(types).map(([key, title]) => {
    const card = document.createElement('div');
    const term = document.createElement('dt');
    const value = document.createElement('dd');
    term.textContent = title;
    value.textContent = data.stats?.[key]?.toLocaleString() ?? '—';
    card.append(term, value);
    return card;
  }));
  $('hindsight-issues').textContent = Object.entries(data.issues).map(([key, message]) => `${key}: ${message}`).join(' ');
  $('hindsight-config').hidden = !data.config;
  $('hindsight-config-text').textContent = data.config ? JSON.stringify({ effective: data.config, overrides: data.overrides }, null, 2) : '';
  memory.graph = data.graph || { nodes: [], edges: [] };
  const label = mode === 'entities' ? 'Entity co-occurrence' : 'Memory constellation';
  $('hindsight-graph-title').textContent = label;
  $('hindsight-graph-name').textContent = label;
  const kinds = [...new Set(memory.graph.edges.map(edge => edge.kind))].sort();
  selectOptions($('hindsight-link'), [['All connections', ''], ...kinds.map(kind => [kind, kind])]);
  renderGraph();
  $('hindsight-result').hidden = false;
}

function svg(tag, attributes) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
  return node;
}

function renderGraph() {
  const graph = memory.graph;
  const kind = $('hindsight-link').value;
  const connected = new Map();
  for (const edge of graph.edges) for (const key of [edge.source, edge.target]) connected.set(key, (connected.get(key) || 0) + 1);
  const ranked = [...graph.nodes].sort((a, b) => (connected.get(b.id) || 0) - (connected.get(a.id) || 0));
  const positions = new Map();
  const rings = [1, 8, 22, 44, 85];
  let index = 0;
  for (let r = 0; r < rings.length && index < ranked.length; r++) {
    const count = Math.min(rings[r], ranked.length - index);
    const angleOffset = r * 0.37;
    for (let i = 0; i < count; i++) {
      const phase = 2 * Math.PI * i / count + angleOffset;
      const point = { x: 340 + Math.cos(phase) * 74 * r, y: 197 + Math.sin(phase) * 42 * r };
      positions.set(ranked[index++].id, point);
    }
  }
  const types = [...$('hindsight-link').options].slice(1).map(option => option.value);
  const edges = graph.edges.filter(edge => !kind || edge.kind === kind);
  const visible = kind ? new Set(edges.flatMap(edge => [edge.source, edge.target])) : new Set(positions.keys());
  $('hindsight-edges').replaceChildren(...edges.map(edge => {
    const a = positions.get(edge.source), b = positions.get(edge.target);
    return svg('line', { x1: a.x, y1: a.y, x2: b.x, y2: b.y, class: `link-color-${types.indexOf(edge.kind) % 4}` });
  }));
  $('hindsight-points').replaceChildren(...ranked.filter(node => visible.has(node.id)).map((node, i) => {
    const { x, y } = positions.get(node.id);
    const point = svg('circle', { cx: x, cy: y, r: Math.min(10, 4 + Math.sqrt(connected.get(node.id) || 0)), 'data-id': node.id });
    const item = svg('g', {});
    const title = svg('title', {});
    title.textContent = node.label;
    point.append(title);
    item.append(point);
    if (i < (ranked.length < 10 ? ranked.length : 6) && x < 605) {
      const text = svg('text', { x: x + 12, y: y - 10 });
      text.textContent = node.label.length > 24 ? node.label.slice(0, 23) + '…' : node.label;
      item.append(text);
    }
    return item;
  }));
  const nodePicker = $('hindsight-node');
  const selected = nodePicker.value;
  selectOptions(nodePicker, [['Select a node', ''], ...ranked.filter(node => visible.has(node.id)).map(node => [node.label, node.id])], selected);
  nodePicker.disabled = !visible.size;
  $('hindsight-graph-count').textContent = `${visible.size} shown${graph.total != null ? ` of ${graph.total.toLocaleString()}` : ''} · ${edges.length} connections shown`;
  showNode();
}

function showNode() {
  const chosen = $('hindsight-node').value;
  const node = memory.graph?.nodes.find(item => item.id === chosen);
  const kind = $('hindsight-link').value;
  $('hindsight-node-heading').textContent = node?.label || 'Inspect a node';
  $('hindsight-node-text').textContent = !node ? 'Choose a point in the graph or use the node menu.' :
    node.text || (node.mentions == null ? 'No memory text was returned for this node.' : `${node.mentions} mentions`);
  const lines = node ? memory.graph.edges.filter(edge => (!kind || edge.kind === kind) &&
      (edge.source === chosen || edge.target === chosen)).slice(0, 30).map(edge => {
    const item = document.createElement('li');
    const other = edge.source === chosen ? edge.target : edge.source;
    item.textContent = `${edge.kind} · ${memory.graph.nodes.find(n => n.id === other)?.label || other}`;
    return item;
  }) : [];
  $('hindsight-node-links').replaceChildren(...lines);
  for (const dot of $('hindsight-points').querySelectorAll('circle')) dot.classList.toggle('chosen', dot.dataset.id === chosen);
}

$('hindsight-link').addEventListener('change', renderGraph);
$('hindsight-node').addEventListener('change', showNode);
$('hindsight-points').addEventListener('click', event => {
  const id = event.target.closest('circle')?.dataset.id;
  if (!id) return;
  $('hindsight-node').value = id;
  showNode();
});

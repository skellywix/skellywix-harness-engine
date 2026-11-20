import {NeuralScene} from './neural_scene.js';

const byId = (id) => document.getElementById(id);
const edgeKey = (source, target) => `${source}\0${target}`;
const hindsight = 'memory:native:hindsight';
const hindsightRecall = (event) => event.kind === 'memory' && event.name === 'recall' && event.state === 'injected';
const groups = [
  ['model', 'Models', 'model'],
  ['memory', 'Memory & vectors', 'memory', 'vector'],
  ['mcp', 'MCP servers', 'mcp'],
  ['tool', 'Tools', 'tool'],
  ['hook', 'Hooks & extensions', 'hook', 'extension'],
  ['ttsr', 'TTSR rules', 'ttsr'],
  ['checks', 'Language & lint', 'lsp', 'lint'],
  ['automation', 'Automations', 'automation'],
];
const kinds = new Map([
  ['runtime', 'Runtime'], ['prompt', 'Input'], ['model', 'Model'], ['memory', 'Memory'],
  ['vector', 'Vector search'], ['mcp', 'MCP server'], ['tool', 'Tool'], ['ttsr', 'TTSR rule'],
  ['hook', 'Hook or guard'], ['lsp', 'Language server'], ['lint', 'Lint checker'], ['automation', 'Automation'],
  ['extension', 'Extension'],
]);
const statuses = new Map([
  ['declared', 'Declared'], ['disabled', 'Disabled'], ['cached', 'Cached'], ['documented', 'Documented'],
  ['runtime', 'Local viewer'], ['recorded', 'Recorded'], ['started', 'Started'], ['ok', 'Completed'],
  ['error', 'Error'], ['injected', 'Injected'], ['aborted', 'Aborted'], ['blocked', 'Blocked'],
]);
const failures = new Set(['error', 'aborted', 'blocked', 'denied', 'fail', 'block']);
const groupForKind = new Map(groups.flatMap(([id, , ...members]) => members.map((kind) => [kind, id])));
const parts = new Map();
const core = byId('core');
const canvas = byId('neural-scene');
const timeOfDay = new Intl.DateTimeFormat(undefined, {hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'});
const dateAndTime = new Intl.DateTimeFormat(undefined, {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit'});
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
const pageSize = 32;
let follows = true;
let selectedTrace = null;
let selectedGroup = 'all';
let selectedNode = null;
let limit = pageSize;
let etag = null;
let snapshot = null;
let active = new Map();
let nodes = new Map();
let inspectorEdges = [];
let optionsKey = '';
let timelineKey = '';
let rowsKey = '';
let detailKey = '';

const scene = new NeuralScene(canvas, byId('camera'), reducedMotion, selectNode);
canvas.hidden = !scene.ctx;
byId('scene-fallback').hidden = Boolean(scene.ctx);

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function changeText(node, text) {
  if (node.textContent !== text) node.textContent = text;
}


function chooseGroup(id) {
  selectedGroup = id;
  selectedNode = null;
  limit = pageSize;
  scene.focus(null);
  core.setAttribute('aria-pressed', String(id === 'all'));
  for (const [group, part] of parts) part.button.setAttribute('aria-pressed', String(group === id));
  renderInspector();
}

for (const [id, label] of groups) {
  const button = element('button', undefined, 'branch');
  button.type = 'button';
  button.dataset.group = id;
  button.setAttribute('aria-pressed', 'false');
  const text = element('span');
  const touch = element('small', 'Not recorded');
  const count = element('span', '—', 'badge');
  text.append(element('strong', label), touch);
  button.append(text, count);
  button.addEventListener('click', () => chooseGroup(id));
  byId('branches').append(button);
  parts.set(id, {label, button, touch, count});
}
core.addEventListener('click', () => chooseGroup('all'));

function currentTrace() {
  return snapshot?.traces.find((trace) => trace.id === selectedTrace);
}

function selectOptions() {
  const select = byId('prompt-select');
  const key = JSON.stringify(snapshot.traces.map(({id, at, events}) => [id, at, events.length]));
  if (key !== optionsKey) {
    optionsKey = key;
    select.replaceChildren(...(snapshot.traces.length ? snapshot.traces.map(({id, at, events}) => {
      const option = element('option', `${dateAndTime.format(new Date(at))} · ${events.length} events`);
      option.value = id;
      return option;
    }) : [element('option', 'No recent prompts found')]));
  }
  if (follows || !currentTrace()) {
    selectedTrace = snapshot.traces[0]?.id ?? null;
    follows = true;
  }
  if (selectedTrace) select.value = selectedTrace;
  select.disabled = !selectedTrace;
  byId('follow').setAttribute('aria-pressed', String(follows));
}

function eventNode(event) {
  if (event.kind === 'prompt') return 'runtime:OMP';
  return `${event.kind}:${event.name}`;
}

function renderMap(previous) {
  const trace = currentTrace();
  const events = trace?.events ?? [];
  const {inventory} = snapshot;
  nodes = new Map(inventory.nodes.map((node) => [node.id, node]));
  for (const recorded of snapshot.traces) {
    for (const event of recorded.events) {
      if (hindsightRecall(event) && !nodes.has(hindsight)) {
        nodes.set(hindsight, {id: hindsight, kind: 'memory', name: 'Hindsight - Memory',
          state: 'recorded', source: 'OMP recall injection'});
      }
      const id = eventNode(event);
      if (nodes.has(id)) continue;
      const source = event.via ? 'OMP boundary (addressed device)' : 'OMP session journal';
      nodes.set(id, {id, name: event.name, kind: event.kind, state: 'recorded', source});
    }
  }
  if (selectedNode && !nodes.has(selectedNode)) {
    selectedNode = null;
    scene.focus(null);
  }
  const counts = new Map();
  const touched = new Map();
  const observed = new Map();
  const fresh = new Set();
  const old = new Map(previous?.id === trace?.id ? (previous?.events ?? []).map((event) => [event.id, event.state]) : []);
  active = new Map();
  const mark = (id, status, problem) => {
    if (!active.get(id)?.problem || problem) active.set(id, {status, problem});
  };
  const record = (source, target, label, problem, changed) => {
    const key = edgeKey(source, target);
    if (observed.get(key)?.state !== 'problem' || problem) {
      observed.set(key, {source, target, label, state: problem ? 'problem' : 'recorded'});
    }
    if (changed) fresh.add(key);
  };
  const touch = (group) => touched.set(group, (touched.get(group) ?? 0) + 1);
  for (const event of events) {
    const id = eventNode(event);
    const problem = failures.has(event.state);
    mark(id, statuses.get(event.state) ?? event.state, problem);
    if (event.kind === 'prompt') continue;
    const changed = old.get(event.id) !== event.state;
    const group = groupForKind.get(event.kind);
    if (group) touch(group);
    const owner = event.kind === 'tool' && nodes.get(id)?.owner;
    if (owner && id.startsWith('tool:mcp__') && nodes.has(owner)) {
      mark(owner, problem ? 'Error at OMP boundary' : 'Addressed at OMP', problem);
      touch('mcp');
      record('runtime:OMP', owner, 'Addressed by OMP', problem, changed);
      record(owner, id, 'Recorded tool address', problem, changed);
    } else if (hindsightRecall(event)) {
      mark(hindsight, 'Context injected', false);
      record('runtime:OMP', hindsight, 'Recorded context injection', false, changed);
      record(hindsight, id, 'First-turn recall', false, changed);
    } else {
      if (event.kind === 'tool' && id.startsWith('tool:mcp__')) touch('mcp');
      record('runtime:OMP', id, 'Recorded in OMP journal', problem, changed);
    }
  }
  const edges = new Map(inventory.edges.map((edge) => [edgeKey(edge.source, edge.target), edge]));
  for (const [key, edge] of observed) edges.set(key, edge);
  inspectorEdges = [...observed.values(), ...inventory.edges];
  scene.show(nodes, edges, active, fresh, groupForKind, trace?.id);
  for (const node of nodes.values()) {
    const group = groupForKind.get(node.kind);
    counts.set(group, (counts.get(group) ?? 0) + 1);
  }
  for (const [id, part] of parts) {
    const amount = touched.get(id) ?? 0;
    const count = counts.get(id) ?? 0;
    part.button.classList.toggle('recorded', amount > 0);
    changeText(part.touch, amount ? `${amount} ${id === 'mcp' ? 'addressed' : 'recorded'}` : 'Not recorded');
    changeText(part.count, String(count));
    part.count.setAttribute('aria-label', `${count} inventory components`);
  }
  core.classList.toggle('recorded', Boolean(trace));
  changeText(byId('core-state'), trace ? 'Prompt recorded' : 'Awaiting journal');
  const size = observed.size;
  changeText(byId('scene-summary'), `${nodes.size} components · ${size} recorded ${size === 1 ? 'path' : 'paths'}`);
  renderTimeline(trace, events);
  renderInspector();
}

function renderTimeline(trace, events) {
  const key = JSON.stringify([trace?.id, trace?.omitted, events]);
  if (key === timelineKey) return;
  timelineKey = key;
  const timeline = byId('timeline');
  const followTail = follows && (timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 36);
  const ordered = events.slice().sort((a, b) => a.at.localeCompare(b.at));
  timeline.replaceChildren(...ordered.map((event) => {
    const row = element('li');
    const time = element('time', timeOfDay.format(new Date(event.at)));
    time.dateTime = event.at;
    const body = element('span');
    let note = kinds.get(event.kind);
    if (hindsightRecall(event)) note = 'First-turn recall · context injected';
    if (event.via) note = `${note} via ${event.via} · addressed device`;
    if (event.completedAt) note += ` · result ${timeOfDay.format(new Date(event.completedAt))}`;
    body.append(element('strong', hindsightRecall(event) ? 'Hindsight - Memory' : event.name), element('small', note));
    const outcome = element('span', statuses.get(event.state) ?? event.state,
      failures.has(event.state) ? 'outcome problem' : 'outcome');
    row.append(time, body, outcome);
    return row;
  }));
  if (followTail) timeline.scrollTop = timeline.scrollHeight;
  byId('timeline-empty').hidden = events.length > 0;
  changeText(byId('timeline-count'), `${events.length} ${events.length === 1 ? 'event' : 'events'}`);
  changeText(byId('timeline-note'), trace?.omitted
    ? `${trace.omitted} older events omitted from the live window. No message or tool payloads.`
    : 'Ordered by recorded time on this prompt’s ancestry. No message or tool payloads.');
}

function selectNode(id, moveFocus = false) {
  selectedNode = id;
  scene.focus(id);
  renderInspector();
  if (moveFocus) {
    const title = byId('detail-title');
    title.tabIndex = -1;
    title.focus();
  }
}

function renderInspector() {
  const query = byId('filter').value.trim().toLowerCase();
  const group = parts.get(selectedGroup);
  const selected = nodes.get(selectedNode);
  const filtered = [...nodes.values()].filter((node) => (!group || groupForKind.get(node.kind) === selectedGroup)
    && (!query || `${node.name} ${kinds.get(node.kind)} ${node.source}`.toLowerCase().includes(query)));
  filtered.sort((a, b) => Number(active.has(b.id)) - Number(active.has(a.id)) || a.name.localeCompare(b.name));
  changeText(byId('inspection-scope'), `${group?.label ?? 'All components'} · ${filtered.length} ${filtered.length === 1 ? 'match' : 'matches'}`);
  byId('more').hidden = filtered.length <= limit;
  const shown = filtered.slice(0, limit);
  const key = JSON.stringify([selectedNode, shown.map((node) => [node, active.get(node.id)])]);
  if (key !== rowsKey) {
    rowsKey = key;
    const list = byId('components');
    const buttons = new Map();
    const restore = list.contains(document.activeElement) ? document.activeElement.dataset.node : null;
    list.replaceChildren(...(shown.length ? shown.map((node) => {
      const row = element('li');
      const button = element('button');
      button.type = 'button';
      button.dataset.node = node.id;
      button.setAttribute('aria-pressed', String(selectedNode === node.id));
      const info = element('span');
      info.append(element('strong', node.name), element('small', `${kinds.get(node.kind)} · ${node.source}`));
      const activity = active.get(node.id);
      button.append(info, element('span', activity?.status ?? statuses.get(node.state) ?? node.state,
        activity?.problem ? 'outcome problem' : 'outcome'));
      button.addEventListener('click', () => selectNode(node.id, true));
      buttons.set(node.id, button);
      row.append(button);
      return row;
    }) : [element('li', 'No components match. Try another branch or filter.', 'empty')]));
    if (restore) (buttons.get(restore) ?? byId('filter')).focus({preventScroll: true});
  }
  const edges = inspectorEdges.filter((edge) => edge.source === selectedNode || edge.target === selectedNode);
  const next = JSON.stringify([selected, active.get(selectedNode), edges]);
  if (next === detailKey) return;
  detailKey = next;
  const detail = byId('detail-body');
  if (!selected) {
    detail.replaceChildren(element('p', 'Select a component to see its source and directed connections.'));
    return;
  }
  detail.replaceChildren(element('p', selected.name, 'node-title'),
    element('p', `${kinds.get(selected.kind)} · ${selected.source} · ${statuses.get(selected.state) ?? selected.state}`));
  if (active.has(selectedNode)) detail.append(element('p', `Selected prompt: ${active.get(selectedNode).status}`));
  if (selected.detail) detail.append(element('p', selected.detail));
  if (!edges.length) {
    detail.append(element('p', 'No declared relationship for this component.'));
    return;
  }
  const list = element('ul', undefined, 'relations');
  for (const edge of edges) {
    const outgoing = edge.source === selectedNode;
    const other = nodes.get(outgoing ? edge.target : edge.source);
    if (!other) continue;
    const row = element('li');
    const link = element('button', `${outgoing ? 'To' : 'From'} ${other.name}`, 'relation');
    link.type = 'button';
    link.append(element('small', edge.label));
    if (edge.state) link.append(element('small', edge.state === 'problem' ? 'Recorded error or block' : 'Recorded for this prompt',
      edge.state === 'problem' ? 'recorded-connection problem' : 'recorded-connection'));
    link.addEventListener('click', () => selectNode(other.id, true));
    row.append(link);
    list.append(row);
  }
  detail.append(list);
}

byId('filter').addEventListener('input', () => { limit = pageSize; renderInspector(); });
byId('more').addEventListener('click', () => { limit += pageSize; renderInspector(); });
byId('prompt-select').addEventListener('change', (e) => {
  const prior = currentTrace();
  selectedTrace = e.currentTarget.value;
  follows = false;
  selectOptions();
  renderMap(prior);
  byId('timeline').scrollTop = 0;
});
byId('follow').addEventListener('click', () => {
  follows = !follows;
  if (!snapshot) return;
  const prior = currentTrace();
  selectOptions();
  renderMap(prior);
});

function status(state, text) {
  byId('watch').className = `watch ${state}`;
  changeText(byId('connection'), text);
}

async function poll() {
  try {
    const headers = {'X-Skellywix-Map': '1'};
    if (etag) headers['If-None-Match'] = etag;
    const response = await fetch('/state', {headers, cache: 'no-store', mode: 'same-origin', signal: AbortSignal.timeout(4000)});
    if (response.status !== 304) {
      if (!response.ok) throw new Error('Observer request rejected');
      const next = await response.json();
      const prior = currentTrace();
      snapshot = next;
      etag = response.headers.get('ETag');
      selectOptions();
      renderMap(prior);
      const coverage = {...snapshot.inventory.coverage, 'Session journal': snapshot.traceCoverage};
      byId('coverage').replaceChildren(...Object.entries(coverage).flatMap(([key, value]) => [element('dt', key), element('dd', value)]));
    }
    status('connected', snapshot.inventoryStatus);
  } catch (error) {
    // Keep the last recorded view, explicitly marked stale. Never show request internals.
    status('unavailable', snapshot ? 'Observer unavailable · last recorded view' : 'Local observer unavailable');
  } finally {
    setTimeout(poll, document.hidden ? 3600 : 1100);
  }
}
poll();

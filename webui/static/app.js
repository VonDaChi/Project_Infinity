'use strict';

const $ = (id) => document.getElementById(id);
const stream = $('stream');

const state = {
  authed: false,
  worlds: [],
  backends: [],
  backend: 'ollama',
  session: null,
  ws: null,
  stats: null,
  combat: null,
};

let sysEl = null;
let waitEl = null;
let toolCount = 0;

/* ── helpers ─────────────────────────────────────────────────────────────── */

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function md(s) {
  return esc(s)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+?)\*/g, '$1<em>$2</em>')
    .replace(/\n/g, '<br>');
}

function num(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function fmt(n) {
  const v = num(n);
  if (v == null) return '—';
  return v >= 1000 ? (v / 1000).toFixed(1) + 'k' : String(v);
}

function scroll() {
  stream.scrollTop = stream.scrollHeight;
}

function show(id) { $(id).classList.remove('hidden'); }
function hide(id) { $(id).classList.add('hidden'); }

/* ── connection ──────────────────────────────────────────────────────────── */

function setConn(ok) {
  const el = $('connStatus');
  el.className = 'status ' + (ok ? 'on' : 'off');
  $('connText').textContent = ok ? '已连接' : '未连接';
}

function connect() {
  if (state.ws && state.ws.readyState <= 1) return;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws = ws;
  ws.onopen = () => setConn(true);
  ws.onclose = () => {
    setConn(false);
    if (state.session) setTimeout(connect, 2000);
  };
  ws.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch (_) { return; }
    onEvent(ev);
  };
}

function send(text) {
  if (!text.trim()) return;
  if (!state.ws || state.ws.readyState !== 1) return;
  addMine(text);
  toolCount = 0;
  showWaiting();
  disableInput('等待 GM…');
  state.ws.send(JSON.stringify({ type: 'input', text }));
}

/* ── stream rendering ────────────────────────────────────────────────────── */

function flushSys() { sysEl = null; }

function appendSys(text) {
  if (!sysEl) {
    sysEl = document.createElement('pre');
    sysEl.className = 'sys';
    stream.appendChild(sysEl);
  }
  sysEl.textContent += text;
  scroll();
}

function addNarrative(ev) {
  const el = document.createElement('div');
  el.className = 'msg gm';
  el.innerHTML = `<div class="who">${esc(ev.title || 'gamemaster')}</div>` +
                 `<div class="body">${md(ev.text || '')}</div>`;
  stream.appendChild(el);
  scroll();
}

function addMine(text) {
  const el = document.createElement('div');
  el.className = 'msg me';
  el.innerHTML = `<div class="bubble"><div class="who">你</div>` +
                 `<div class="body">${esc(text).replace(/\n/g, '<br>')}</div></div>`;
  stream.appendChild(el);
  scroll();
}

const TOOL_LABELS = {
  perform_check: '检定',
  resolve_attack: '攻击',
  resolve_magic: '法术',
  roll_dice: '掷骰',
  modify_player_numeric: '数值变更',
  update_player_list: '清单更新',
  dump_player_db: '读取角色',
  rest: '休息',
  register_combatants: '登记战斗',
  export_session_state: '导出战斗',
  import_session_state: '载入战斗',
};

const OUTCOMES = {
  'Critical Success': '大成功',
  'Critical Failure': '大失败',
  Success: '成功',
  Failure: '失败',
};

function addTool(ev) {
  toolCount += 1;
  if (waitEl) {
    waitEl.lastElementChild.textContent =
      `GM 正在推演 · 已调用 ${toolCount} 次工具`;
  }
  const data = parseJson(ev.result);
  const el = document.createElement('div');
  el.className = 'event';

  if (data && data.outcome) {
    const win = /success/i.test(String(data.outcome));
    el.innerHTML =
      `<span class="tag"><span class="die"></span>${esc(data.check_name || TOOL_LABELS[ev.name] || ev.name)}</span>` +
      `<span class="total">${esc(String(data.total ?? '—'))}</span>` +
      `<span class="vs">vs DC ${esc(String(data.dc_to_beat ?? '—'))}</span>` +
      `<span class="verdict ${win ? 'win' : 'lose'}">${esc(OUTCOMES[data.outcome] || data.outcome)}</span>` +
      `<span class="detail">${esc(String(data.base_roll))} + ${esc(String(data.modifier))} · ${esc(ev.name)}</span>`;
  } else {
    el.classList.add('plain');
    el.innerHTML = `<span class="tag">${esc(TOOL_LABELS[ev.name] || ev.name)}</span>` +
                   `<span class="detail">${esc(summarize(ev.name, data, ev.result))}</span>`;
  }
  stream.appendChild(el);
  scroll();
}

function parseJson(text) {
  if (!text) return null;
  try { return JSON.parse(text); } catch (_) { return null; }
}

function short(v) {
  const s = typeof v === 'string' ? v : JSON.stringify(v);
  if (s == null) return '';
  return s.length > 140 ? s.slice(0, 140) + '…' : s;
}

function summarize(name, data, raw) {
  if (data && typeof data === 'object') {
    if (name === 'roll_dice') {
      return `结果 ${data.total ?? '—'}` +
             (data.rolls ? ` (${data.rolls.join(', ')})` : '');
    }
    if (name === 'modify_player_numeric') {
      return `${data.key ?? ''} ${Number(data.delta) >= 0 ? '+' : ''}${data.delta ?? ''} → ${data.new_value ?? data.value ?? ''}`;
    }
    if (name === 'rest') {
      return short(data.narrative_format || data.message || data);
    }
    if (data.narrative_format) return short(data.narrative_format);
    const keys = Object.keys(data).slice(0, 3)
      .map((k) => `${k}: ${short(data[k])}`).join(' · ');
    return keys || short(data);
  }
  return short(raw);
}

function addNotice(text, isError) {
  const el = document.createElement('div');
  el.className = 'notice' + (isError ? ' error' : '');
  el.textContent = text || '';
  stream.appendChild(el);
  scroll();
}

function showWaiting() {
  hideWaiting();
  waitEl = document.createElement('div');
  waitEl.className = 'waiting';
  waitEl.innerHTML =
    '<span class="dots"><i></i><i></i><i></i></span><span>GM 正在推演…</span>';
  stream.appendChild(waitEl);
  scroll();
}

function hideWaiting() {
  if (waitEl) { waitEl.remove(); waitEl = null; }
}

function clearStream() {
  stream.innerHTML = '';
  sysEl = null;
  waitEl = null;
}

/* ── events ──────────────────────────────────────────────────────────────── */

function onEvent(ev) {
  switch (ev.type) {
    case 'narrative': flushSys(); addNarrative(ev); break;
    case 'tool': flushSys(); addTool(ev); break;
    case 'out': appendSys(ev.text); break;
    case 'prompt': flushSys(); onPrompt(ev.text); break;
    case 'stats': onStats(ev); break;
    case 'error': flushSys(); addNotice(ev.text, true); break;
    case 'closed': flushSys(); onClosed(ev.text); break;
    default: break;
  }
}

function onPrompt(text) {
  hideWaiting();
  enableInput(text);
}

function onClosed(text) {
  hideWaiting();
  disableInput('会话已结束');
  addNotice(text || '会话已结束');
  state.session = null;
  $('btnQuit').disabled = true;
}

function onStats(ev) {
  if (ev.data && !ev.data.error) {
    state.stats = ev.data;
    renderPanel();
  }
  if (ev.combat) {
    state.combat = ev.combat;
    renderCombat();
    updateContext(ev.combat);
  }
}

/* ── input box ───────────────────────────────────────────────────────────── */

function enableInput(prompt) {
  const box = $('input');
  box.disabled = false;
  box.placeholder = prompt ? prompt.replace(/[:：]\s*$/, '') + '…' : '输入行动…';
  $('send').disabled = false;
  box.focus();
}

function disableInput(placeholder) {
  $('input').disabled = true;
  $('input').placeholder = placeholder || '等待 GM…';
  $('send').disabled = true;
}

function autoSize() {
  const box = $('input');
  box.style.height = 'auto';
  box.style.height = Math.min(160, box.scrollHeight) + 'px';
}

/* ── sidebar & panel ─────────────────────────────────────────────────────── */

function renderWorlds() {
  const box = $('worldList');
  if (!state.worlds.length) {
    box.innerHTML = '<div class="world-card"><div class="w-name">没有世界存档</div>' +
                    '<div class="w-meta">先用 CLI 的 forge 生成一个</div></div>';
    return;
  }
  box.innerHTML = state.worlds.map((w) => `
    <div class="world-card${state.session && state.session.world === w.name ? ' active' : ''}"
         data-world="${esc(w.file)}">
      <div class="w-name">${esc(w.name)}</div>
      <div class="w-meta">${w.has_save ? `第 ${w.rounds} 回合` : '未开始'}</div>
    </div>`).join('');
  box.querySelectorAll('.world-card[data-world]').forEach((el) => {
    el.addEventListener('click', () => {
      openStart(el.dataset.world);
    });
  });
}

function renderHeader() {
  const sess = state.session;
  $('worldName').textContent = sess ? '· ' + sess.world : '';
  $('worldName').style.display = sess ? '' : 'none';
  const spec = state.backends.find((b) => b.id === (sess ? sess.backend : state.backend));
  $('backendPill').textContent = spec ? spec.label : (sess ? sess.backend : '—');
  $('addrPill').textContent = location.host;
  $('btnQuit').disabled = !sess;
}

function renderPanel() {
  const d = state.stats;
  if (!d) return;
  const name = d.name || '未命名';
  $('charName').textContent = name;
  $('avatar').textContent = name.slice(0, 1);
  const parts = [d.race, d.character_class].filter(Boolean);
  if (d.level) parts.push(d.level + ' 级');
  $('charSub').textContent = parts.join(' · ') || '—';

  const hp = num(d.current_hit_points);
  const max = num(d.total_hit_points);
  if (hp != null && max) {
    const pct = Math.max(0, Math.min(100, (hp / max) * 100));
    $('hpText').textContent = `${hp} / ${max}`;
    const bar = $('hpBar');
    bar.querySelector('i').style.width = pct + '%';
    bar.className = 'bar' + (pct <= 25 ? ' low' : pct <= 50 ? ' mid' : '');
  }

  const quick = [
    ['护甲', d.armor_class],
    ['速度', d.speed],
    ['熟练', d.proficiency_bonus != null ? '+' + d.proficiency_bonus : null],
    ['金币', d.gold],
  ];
  $('quickStats').innerHTML = quick.map(([k, v]) =>
    `<div class="stat"><div class="k">${k}</div><div class="v">${esc(String(v ?? '—'))}</div></div>`
  ).join('');

  const s = d.stats || {};
  const abils = [['力量', 'str'], ['敏捷', 'dex'], ['体质', 'con'],
                 ['智力', 'int'], ['感知', 'wis'], ['魅力', 'cha']];
  $('abilities').innerHTML = abils.map(([label, key]) => {
    const v = num(s[key]);
    const m = v == null ? null : Math.floor((v - 10) / 2);
    const cls = m == null ? ' zero' : m > 0 ? '' : m < 0 ? ' neg' : ' zero';
    return `<div class="stat"><div class="k">${label}</div>` +
           `<div class="v">${v == null ? '—' : v}</div>` +
           `<div class="m${cls}">${m == null ? '—' : (m > 0 ? '+' : '') + m}</div></div>`;
  }).join('');
}

function renderCombat() {
  const box = $('combat');
  const c = state.combat && state.combat.combat;
  if (!c || !c.active || !c.registry) {
    box.innerHTML = '<div class="track"><span class="n">当前无战斗</span></div>';
    return;
  }
  const playerName = state.stats && state.stats.name;
  const list = Object.values(c.registry)
    .sort((a, b) => (num(b.initiative) || 0) - (num(a.initiative) || 0));
  box.innerHTML = list.map((e) => {
    const self = playerName && e.name === playerName;
    const hp = num(e.hp);
    const dead = e.killed ? ' · 倒下' : (hp != null ? ` · ${hp} HP` : '');
    return `<div class="track${self ? ' self' : ''}">` +
           `<span class="n">${esc(e.name || '?')}<span class="i">${esc(dead)}</span></span>` +
           `<span class="i">先攻 ${esc(String(e.initiative ?? '—'))}</span></div>`;
  }).join('');
}

function updateContext(p) {
  const win = num(p.context_window);
  const used = num(p.context_tokens) || 0;
  if (win) {
    $('ctxBar').style.width = Math.min(100, (used / win) * 100) + '%';
    $('ctxText').textContent =
      `第 ${p.round_counter || 0} 回合 · ${fmt(used)} / ${fmt(win)} tokens`;
  } else {
    $('ctxText').textContent = `第 ${p.round_counter || 0} 回合`;
  }
}

async function showLocalPin() {
  // The server only returns the PIN to localhost; a LAN peer gets 403 and the
  // element stays empty. So the PIN surfaces in the one place the user can see
  // (this browser) without needing the console window.
  const el = $('localPin');
  try {
    const res = await fetch('/api/pin');
    if (res.ok) {
      const data = await res.json();
      if (data && data.pin) {
        el.textContent = `本机 PIN：${data.pin}`;
        el.classList.remove('hidden');
        return;
      }
    }
  } catch (e) { /* ignore — not localhost */ }
  el.textContent = '';
  el.classList.add('hidden');
}

/* ── state loading ───────────────────────────────────────────────────────── */

async function loadState() {
  const res = await fetch('/api/state');
  if (res.status === 401) {
    state.authed = false;
    show('login');
    showLocalPin();
    $('pinInput').focus();
    return false;
  }
  const data = await res.json();
  state.authed = true;
  state.worlds = data.worlds || [];
  state.backends = data.backends || [];
  state.backend = data.backend || 'ollama';
  state.session = data.session || null;
  renderWorlds();
  renderHeader();
  return true;
}

/* ── backend forms ───────────────────────────────────────────────────────── */

function fillBackendForm(ids, backendId) {
  const spec = state.backends.find((b) => b.id === backendId) || state.backends[0];
  if (!spec) return;
  $(ids.backend).innerHTML = state.backends
    .map((b) => `<option value="${esc(b.id)}">${esc(b.label)}</option>`).join('');
  $(ids.backend).value = spec.id;
  $(ids.model).innerHTML = (spec.models || [])
    .map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join('');
  if (spec.options && spec.options.model) $(ids.model).value = spec.options.model;
  $(ids.key).value = (spec.options && spec.options.api_key) || '';
  $(ids.url).value = (spec.options && spec.options.base_url) || '';
  const t = spec.options && spec.options.temperature != null
    ? Number(spec.options.temperature) : 0;
  $(ids.temp).value = t;
  $(ids.tempVal).textContent = t.toFixed(1);
  $(ids.keyField).style.display = spec.needs_key ? '' : 'none';
  $(ids.urlField).style.display = spec.id === 'kobold' ? '' : 'none';
}

const START_IDS = {
  backend: 'startBackend', model: 'startModel', key: 'startKey', url: 'startUrl',
  temp: 'startTemp', tempVal: 'startTempVal',
  keyField: 'startKeyField', urlField: 'startUrlField',
};
const SET_IDS = {
  backend: 'setBackend', model: 'setModel', key: 'setKey', url: 'setUrl',
  temp: 'setTemp', tempVal: 'setTempVal',
  keyField: 'setKeyField', urlField: 'setUrlField',
};

function openStart(worldFile) {
  if (!state.worlds.length) {
    addNotice('output/ 目录里还没有 .wwf 世界存档。请先用 CLI 启动器（选项 1）生成一个。');
    return;
  }
  $('startWorld').innerHTML = state.worlds
    .map((w) => `<option value="${esc(w.file)}">${esc(w.name)}` +
                `${w.has_save ? `（第 ${w.rounds} 回合）` : '（未开始）'}</option>`).join('');
  if (worldFile) $('startWorld').value = worldFile;
  fillBackendForm(START_IDS, state.backend);
  $('startError').textContent = '';
  show('start');
}

async function startSession() {
  const backend = $('startBackend').value;
  const options = {
    model: $('startModel').value,
    temperature: parseFloat($('startTemp').value),
  };
  if ($('startKey').value) options.api_key = $('startKey').value;
  if (backend === 'kobold' && $('startUrl').value) options.base_url = $('startUrl').value;

  const res = await fetch('/api/session', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ world: $('startWorld').value, backend, options }),
  });
  const data = await res.json().catch(() => ({ ok: false, error: '服务器无响应' }));
  if (!data.ok) {
    $('startError').textContent = data.error || '启动失败';
    return;
  }
  state.session = data.session;
  hide('start');
  clearStream();
  renderWorlds();
  renderHeader();
  connect();
}

async function stopSession() {
  await fetch('/api/session', { method: 'DELETE' });
  state.session = null;
  disableInput('会话已结束');
  $('btnQuit').disabled = true;
  renderWorlds();
  renderHeader();
}

async function saveSettings() {
  const id = $('setBackend').value;
  const body = {
    model: $('setModel').value,
    temperature: parseFloat($('setTemp').value),
    select: true,
  };
  if ($('setKey').value) body.api_key = $('setKey').value;
  if (id === 'kobold') body.base_url = $('setUrl').value;

  const res = await fetch(`/api/backends/${id}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({ ok: false }));

  const pin = $('setPin').value.trim();
  if (pin) {
    await fetch('/api/pin', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pin }),
    });
    $('setPin').value = '';
  }

  if (!data.ok) {
    $('setError').textContent = data.error || '保存失败';
    return;
  }
  $('setError').textContent = '';
  await loadState();
  renderHeader();
  hide('settings');
}

/* ── login ───────────────────────────────────────────────────────────────── */

async function login() {
  const pin = $('pinInput').value;
  const res = await fetch('/api/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ pin }),
  });
  if (!res.ok) {
    $('pinError').textContent = 'PIN 不正确';
    $('pinInput').select();
    return;
  }
  $('pinError').textContent = '';
  $('pinInput').value = '';
  hide('login');
  const ok = await loadState();
  if (ok && !state.session) openStart();
  if (ok && state.session) connect();
}

/* ── mobile drawers ──────────────────────────────────────────────────────── */

function closeDrawers() {
  $('sidebar').classList.remove('open');
  $('panel').classList.remove('open');
  $('scrim').classList.add('hidden');
}

function toggleDrawer(id) {
  const el = $(id);
  const opening = !el.classList.contains('open');
  closeDrawers();
  if (opening) {
    el.classList.add('open');
    $('scrim').classList.remove('hidden');
  }
}

/* ── wiring ──────────────────────────────────────────────────────────────── */

function bindUI() {
  $('send').addEventListener('click', () => {
    const box = $('input');
    const v = box.value;
    box.value = '';
    autoSize();
    send(v);
  });

  $('input').addEventListener('input', autoSize);
  $('input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      const box = $('input');
      const v = box.value;
      box.value = '';
      autoSize();
      send(v);
    }
  });

  document.querySelectorAll('.chip[data-cmd]').forEach((chip) => {
    chip.addEventListener('click', () => send(chip.dataset.cmd));
  });

  $('pinSubmit').addEventListener('click', login);
  $('pinInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); login(); }
  });

  $('btnStart').addEventListener('click', () => openStart());
  $('btnQuit').addEventListener('click', stopSession);
  $('btnSettings').addEventListener('click', () => {
    fillBackendForm(SET_IDS, state.backend);
    $('setError').textContent = '';
    show('settings');
  });

  $('startCancel').addEventListener('click', () => hide('start'));
  $('startGo').addEventListener('click', startSession);
  $('startBackend').addEventListener('change', (e) =>
    fillBackendForm(START_IDS, e.target.value));
  $('startTemp').addEventListener('input', (e) =>
    { $('startTempVal').textContent = Number(e.target.value).toFixed(1); });

  $('setBackend').addEventListener('change', (e) =>
    fillBackendForm(SET_IDS, e.target.value));
  $('setTemp').addEventListener('input', (e) =>
    { $('setTempVal').textContent = Number(e.target.value).toFixed(1); });
  $('setCancel').addEventListener('click', () => hide('settings'));
  $('setSave').addEventListener('click', saveSettings);

  document.querySelectorAll('.mobile-tabs button').forEach((btn) => {
    btn.addEventListener('click', () => {
      const target = btn.dataset.drawer;
      if (target === 'chat') closeDrawers();
      else toggleDrawer(target);
    });
  });
  $('scrim').addEventListener('click', closeDrawers);
}

async function boot() {
  bindUI();
  const ok = await loadState();
  if (ok) {
    if (state.session) connect();
    else openStart();
  }
}

boot();

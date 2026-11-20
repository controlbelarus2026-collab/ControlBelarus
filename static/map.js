/* СтопКонтроль: карта MapLibre (серая подложка, жёлтые дороги), остановки, голосование */
const APP = window.APP;
const $ = id => document.getElementById(id);
const NS = 'http://www.w3.org/2000/svg';
const stops = new Map();       // id -> {marker, el, data}
let map, openId = null, sheetTimer = null, busy = false;

function icon(name) {
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('class', 'ic');
  const use = document.createElementNS(NS, 'use');
  use.setAttribute('href', '#i-' + name);
  svg.append(use);
  return svg;
}
function toast(text, ms) {
  const t = $('toast'); t.textContent = text; t.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(() => { t.hidden = true; }, ms || 3200);
}

/* ---------- стиль карты: серый фон, жёлтые дороги ---------- */
function rasterStyle() {
  return {version: 8, sources: {r: {type: 'raster', tileSize: 256, maxzoom: 16,
    tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}'],
    attribution: 'Карта © Esri, OpenStreetMap contributors'}}, layers: [{id: 'r', type: 'raster', source: 'r'}]};
}
function patchStyle(s) {
  const GRAY = '#eceff3', WATER = '#cfd8e3', GREEN = '#e3e9e5', BUILD = '#dfe3e8';
  const YELLOW = '#ffd84d', YELLOW_EDGE = '#e2b923', MINOR = '#ffffff', MINOR_EDGE = '#d3d7dd';
  for (const l of s.layers) {
    const id = l.id, sl = l['source-layer'], t = l.type;
    l.paint = l.paint || {};
    if (t === 'background') l.paint['background-color'] = GRAY;
    else if (sl === 'water' && t === 'fill') l.paint['fill-color'] = WATER;
    else if (sl === 'waterway' && t === 'line') l.paint['line-color'] = WATER;
    else if (sl === 'building' && t === 'fill') { l.paint['fill-color'] = BUILD; delete l.paint['fill-outline-color']; }
    else if ((sl === 'park' || sl === 'landcover' || sl === 'landuse') && t === 'fill') l.paint['fill-color'] = GREEN;
    else if (sl === 'transportation' && t === 'line' && !/rail|ferry|aerialway|ford|bridge_rail|transit/.test(id)) {
      const minor = /minor|service|track|path|pedestrian|footway|cycleway|living|residential|street/.test(id);
      const casing = /casing/.test(id);
      l.paint['line-color'] = minor ? (casing ? MINOR_EDGE : MINOR) : (casing ? YELLOW_EDGE : YELLOW);
      delete l.paint['line-opacity'];
    } else if (t === 'symbol') {
      l.paint['text-color'] = '#4a5160';
      l.paint['text-halo-color'] = '#ffffff';
      l.paint['text-halo-width'] = 1.4;
      if (l.layout && l.layout['text-field']) {
        l.layout['text-field'] = ['coalesce', ['get', 'name:ru'], ['get', 'name']];
      }
    }
  }
  return s;
}
async function loadStyle() {
  try {
    const r = await fetch('https://tiles.openfreemap.org/styles/positron');
    if (!r.ok) throw new Error('style');
    return patchStyle(await r.json());
  } catch (e) {
    return rasterStyle();
  }
}

/* ---------- маркеры остановок ---------- */
const STATUS_ICON = {normal: 'bus', control: 'alert', left: 'check'};
function makeEl(s) {
  const el = document.createElement('div');
  el.className = 'stop normal';
  const dot = document.createElement('div'); dot.className = 'dot';
  const lbl = document.createElement('span'); lbl.className = 'lbl'; lbl.textContent = s.name;
  const cnt = document.createElement('span'); cnt.className = 'cnt';
  el.append(dot, lbl, cnt);
  el.addEventListener('click', e => { e.stopPropagation(); openSheet(s.id); });
  return el;
}
function applyStatus(rec, s) {
  const el = rec.el;
  el.classList.remove('normal', 'control', 'left', 'pending');
  el.classList.add(s.status);
  const dot = el.querySelector('.dot');
  if (rec.icon !== STATUS_ICON[s.status]) {
    rec.icon = STATUS_ICON[s.status];
    dot.replaceChildren(icon(rec.icon));
  }
  if (s.pending) {
    el.classList.add('pending');
    el.querySelector('.cnt').textContent = Math.max(s.pending.present, s.pending.absent, s.pending.left) + '/' + APP.needed;
  }
  el.style.zIndex = s.status === 'control' ? 3 : (s.pending ? 2 : 1);
}
function sync(list) {
  const seen = new Set();
  for (const s of list) {
    seen.add(s.id);
    let rec = stops.get(s.id);
    if (!rec) {
      const el = makeEl(s);
      const marker = new maplibregl.Marker({element: el, anchor: 'center'}).setLngLat([s.lng, s.lat]).addTo(map);
      rec = {marker, el, icon: null}; stops.set(s.id, rec);
    }
    applyStatus(rec, s);
  }
  for (const [id, rec] of stops) if (!seen.has(id)) { rec.marker.remove(); stops.delete(id); }
  $('empty').hidden = list.length > 0;
}
async function poll() {
  try {
    const r = await fetch('/api/stops');
    if (r.status === 401) { location.href = '/register'; return; }
    if (r.ok) sync((await r.json()).stops);
  } catch (e) { /* нет сети: повторим */ }
  setTimeout(poll, document.hidden ? 15000 : 4000);
}
function updateLabels() {
  document.getElementById('map').classList.toggle('labels-on', map.getZoom() >= 15.2);
}

/* ---------- нижняя панель ---------- */
function statusText(st) {
  if (st.status === 'control') return 'Контроль стоит' + (st.since != null ? ' · ' + Math.max(1, Math.round(st.since / 60)) + ' мин назад' : '');
  if (st.status === 'left') return 'Контроль уехал · отметка ещё ' + Math.max(1, Math.round((st.remaining || 0) / 60)) + ' мин';
  return 'Контроля нет';
}
function renderSheet(st) {
  $('shName').textContent = st.name;
  const s = $('shStatus'); s.textContent = statusText(st); s.className = 'sh-status ' + st.status;
  const round = $('shRound');
  const mine = st.round ? st.round.mine : null;
  if (st.round) {
    const c = st.round.counts, n = st.round.needed;
    round.hidden = false;
    round.replaceChildren();
    round.append('Идёт подтверждение · осталось ' + st.round.ends_in + ' с');
    const prog = document.createElement('div'); prog.className = 'prog';
    [['Контроль стоит', c.present], ['Контроля нет', c.absent], ['Уехал', c.left]].forEach(([k, v]) => {
      const sp = document.createElement('span'); sp.textContent = k + ': ' + v + '/' + n; prog.append(sp);
    });
    round.append(prog);
  } else round.hidden = true;
  document.querySelectorAll('.vbtn').forEach(b => {
    b.classList.toggle('chosen', mine === b.dataset.kind);
    b.disabled = busy || (mine !== null && mine !== undefined);
  });
}
async function refreshSheet() {
  if (openId === null) return;
  try {
    const r = await fetch('/api/stops/' + openId);
    if (r.ok) renderSheet(await r.json());
  } catch (e) { /* ignore */ }
}
function openSheet(id) {
  openId = id;
  const sh = $('sheet');
  sh.classList.add('open'); document.body.classList.add('sheet-open');
  document.documentElement.style.setProperty('--sheet-h', (sh.offsetHeight || 230) + 'px');
  const rec = stops.get(id);
  if (rec) { $('shName').textContent = rec.el.querySelector('.lbl').textContent; map.easeTo({center: rec.marker.getLngLat(), duration: 300}); }
  refreshSheet();
  clearInterval(sheetTimer); sheetTimer = setInterval(refreshSheet, 1000);
}
function closeSheet() {
  openId = null; clearInterval(sheetTimer);
  $('sheet').classList.remove('open'); document.body.classList.remove('sheet-open');
  document.documentElement.style.setProperty('--sheet-h', '0px');
}
const MESSAGES = {present: 'Контроль подтверждён: остановка стала красной.', absent: 'Подтверждено: контроля нет.', left: 'Подтверждено: контроль уехал.'};
async function vote(kind) {
  if (openId === null || busy) return;
  busy = true; document.querySelectorAll('.vbtn').forEach(b => { b.disabled = true; });
  try {
    const r = await fetch('/api/stops/' + openId + '/vote', {method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': APP.csrf}, body: JSON.stringify({kind})});
    const j = await r.json();
    if (j.state) renderSheet(j.state);
    if (!r.ok) toast(j.error || 'Не удалось отправить отметку.');
    else if (j.resolved) { toast(MESSAGES[j.resolved], 4200); refreshList(); }
    else toast('Отметка принята. Нужно ' + APP.needed + ' подтверждения за ' + APP.window + ' с.');
  } catch (e) { toast('Нет соединения. Попробуйте ещё раз.'); }
  busy = false; refreshSheet();
}
async function refreshList() {
  try { const r = await fetch('/api/stops'); if (r.ok) sync((await r.json()).stops); } catch (e) { /* ignore */ }
}

/* ---------- геолокация ---------- */
let me = null;
function locate() {
  if (!navigator.geolocation) { toast('Геолокация недоступна на этом устройстве.'); return; }
  navigator.geolocation.getCurrentPosition(p => {
    const ll = [p.coords.longitude, p.coords.latitude];
    if (!me) { const d = document.createElement('div'); d.className = 'user-dot'; me = new maplibregl.Marker({element: d}).setLngLat(ll).addTo(map); }
    else me.setLngLat(ll);
    map.flyTo({center: ll, zoom: Math.max(map.getZoom(), 16)});
  }, () => toast('Не удалось определить местоположение. Разрешите доступ в браузере.'), {enableHighAccuracy: true, timeout: 10000});
}

async function init() {
  const style = await loadStyle();
  const c = APP.city;
  map = new maplibregl.Map({container: 'map', style, center: [c.lng, c.lat], zoom: c.zoom, minZoom: 10.5, maxPitch: 0,
    attributionControl: false, dragRotate: false, pitchWithRotate: false});
  map.addControl(new maplibregl.AttributionControl({compact: true}), 'bottom-left');
  map.touchZoomRotate.disableRotation();
  map.on('zoom', updateLabels);
  map.on('click', closeSheet);
  map.on('load', () => { updateLabels(); poll(); });
  map.on('error', () => { /* тайлы могут не загрузиться при плохой сети */ });
  $('locateBtn').addEventListener('click', locate);
  $('shClose').addEventListener('click', closeSheet);
  document.querySelectorAll('.vbtn').forEach(b => b.addEventListener('click', () => vote(b.dataset.kind)));
}
init();

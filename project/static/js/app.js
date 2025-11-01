/* global L, io, initialSettings, gridError, tractorColors, hasGoogleKey, hasYandexKey */
const state = {
  assignments: {},
  tractors: [],
  currentTractor: null,
  mode: initialSettings.mode || 'grid',
  advanced: Boolean(initialSettings.advanced),
  streetSource: initialSettings.street_source || 'google',
  provider: initialSettings.routing_provider || 'google',
  building: false,
  theme: 'light',
};

const map = L.map('map', { zoomControl: false }).setView(
  [initialSettings.center_lat, initialSettings.center_lon],
  13,
);
L.control.zoom({ position: 'bottomright' }).addTo(map);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '&copy; OpenStreetMap contributors',
}).addTo(map);

const socket = io();

// Elements
const sidebar = document.getElementById('sidebar');
const sidebarToggle = document.getElementById('sidebar-toggle');
const themeToggle = document.getElementById('theme-toggle');
const logEl = document.getElementById('log');
const legendEl = document.getElementById('legend');
const buildSpinner = document.getElementById('build-spinner');
const mapSpinner = document.getElementById('map-spinner');
const toastContainer = document.getElementById('toast-container');
const progressBox = document.getElementById('global-progress');
const progressFill = progressBox ? progressBox.querySelector('.progress-fill') : null;
const progressText = document.getElementById('progress-text');
const gridMeta = document.getElementById('grid-meta');
const tractorSelect = document.getElementById('tractor-select');
const gridAlert = document.getElementById('grid-alert');

const sectorLayers = new Map();
const routeLayers = new Map();
const roadLayers = new Map();
let gridLayer = null;
let roadsLayer = null;
let kmlLayer = null;

const themePreference = localStorage.getItem('dispatcher-theme');
if (themePreference === 'dark') {
  document.body.classList.add('theme-dark');
  state.theme = 'dark';
  themeToggle.textContent = '☀️';
}

function toggleSidebar(force) {
  if (window.innerWidth >= 992) return;
  const open = typeof force === 'boolean' ? force : !sidebar.classList.contains('open');
  sidebar.classList.toggle('open', open);
}

sidebarToggle.addEventListener('click', () => toggleSidebar());

function applyTheme(next) {
  state.theme = next;
  document.body.classList.toggle('theme-dark', next === 'dark');
  themeToggle.textContent = next === 'dark' ? '☀️' : '🌙';
  localStorage.setItem('dispatcher-theme', next);
}

themeToggle.addEventListener('click', () => {
  applyTheme(state.theme === 'dark' ? 'light' : 'dark');
});

function showToast(message, kind = 'info', timeout = 4000) {
  const toast = document.createElement('div');
  toast.className = `toast-message ${kind}`;
  toast.textContent = message;
  toastContainer.appendChild(toast);
  setTimeout(() => toast.remove(), timeout);
}

function setBusy(selector, busy) {
  const el = typeof selector === 'string' ? document.querySelector(selector) : selector;
  if (!el) return;
  el.disabled = busy;
  el.classList.toggle('loading', busy);
}

function setSpinner(active) {
  state.building = active;
  buildSpinner.hidden = !active;
}

function setMapSpinner(active) {
  if (active) {
    mapSpinner.hidden = false;
    mapSpinner.classList.add('active');
  } else {
    mapSpinner.hidden = true;
    mapSpinner.classList.remove('active');
  }
}

function updateGlobalProgress(stage, text, percent) {
  if (!progressBox || !progressFill || !progressText) return;
  const clamped = Math.min(100, Math.max(0, Number.isFinite(percent) ? Number(percent) : 0));
  progressBox.classList.remove('hidden');
  progressFill.style.width = `${clamped}%`;
  progressText.textContent = text || stage || 'Прогресс';
  if (clamped >= 100) {
    setTimeout(() => progressBox.classList.add('hidden'), 1500);
  }
}

function classifyMessage(message) {
  if (!message) return 'info';
  const text = message.toLowerCase();
  if (text.includes('error') || text.includes('ошибка') || text.includes('❌')) return 'error';
  if (text.includes('⚠️') || text.includes('warn')) return 'warn';
  if (text.includes('✅') || text.includes('готов') || text.includes('[done]')) return 'success';
  return 'info';
}

function appendLog(message) {
  const time = new Date().toLocaleTimeString();
  const entry = document.createElement('div');
  entry.className = `log-entry ${classifyMessage(message)}`;
  entry.innerHTML = `<strong>[${time}]</strong> ${message}`;
  logEl.appendChild(entry);
  logEl.scrollTop = logEl.scrollHeight;
}

document.getElementById('clear-log').addEventListener('click', () => {
  logEl.innerHTML = '';
  appendLog('🧾 Лог очищен');
});

function buildTractors() {
  state.tractors = [];
  const units = Number(initialSettings.n_units) || 0;
  for (let i = 0; i < units; i += 1) {
    state.tractors.push({
      id: `tractor_${String(i + 1).padStart(2, '0')}`,
      name: `Трактор ${String(i + 1).padStart(2, '0')}`,
      color: tractorColors[i % tractorColors.length],
      length: 0,
    });
  }
  state.currentTractor = state.tractors[0]?.id || null;
  tractorSelect.innerHTML = '';
  state.tractors.forEach((tractor) => {
    const option = document.createElement('option');
    option.value = tractor.id;
    option.textContent = tractor.name;
    tractorSelect.appendChild(option);
  });
  renderLegend();
}

function renderLegend() {
  legendEl.innerHTML = '';
  state.tractors.forEach((tractor) => {
    const item = document.createElement('div');
    item.className = 'item';
    const color = document.createElement('span');
    color.className = 'color';
    color.style.background = tractor.color;
    item.appendChild(color);
    const label = document.createElement('span');
    label.textContent = `${tractor.name}${tractor.length ? ` · ${tractor.length.toFixed(1)} км` : ''}`;
    item.appendChild(label);
    legendEl.appendChild(item);
  });
}

function getTractorById(id) {
  return state.tractors.find((t) => t.id === id);
}

function styleForTractor(id) {
  const tractor = getTractorById(id);
  if (!tractor) return { color: '#5f6b7a', fillColor: '#1f2937' };
  return { color: '#cbd5f5', fillColor: tractor.color };
}

function updateLayerTooltip(layer, sectorId, tractorId) {
  const tractor = getTractorById(tractorId);
  if (!tractor) {
    layer.unbindTooltip();
    return;
  }
  layer.bindTooltip(`${tractor.name}<br>Сектор: ${sectorId}`, { sticky: true, opacity: 0.85 });
}

function applyAssignment(sectorId, tractorId) {
  state.assignments[sectorId] = tractorId;
  const layer = sectorLayers.get(sectorId);
  if (layer) {
    const style = styleForTractor(tractorId);
    layer.setStyle({
      color: style.color,
      fillColor: style.fillColor,
      fillOpacity: 0.45,
      weight: 1.5,
    });
    updateLayerTooltip(layer, sectorId, tractorId);
  }
}

function resetAssignments() {
  Object.keys(state.assignments).forEach((sectorId) => {
    const layer = sectorLayers.get(sectorId);
    if (layer) {
      layer.setStyle({ color: '#1e3352', fillColor: '#10213b', fillOpacity: 0.18, weight: 1 });
      layer.unbindTooltip();
    }
  });
  state.assignments = {};
}

function highlightLayer(layer, tractorId) {
  const style = styleForTractor(tractorId);
  layer.setStyle({ color: style.color, weight: 2.5 });
}

function resetHighlight(layer, tractorId) {
  const style = styleForTractor(tractorId);
  layer.setStyle({ color: tractorId ? style.color : '#1e3352', weight: 1 });
}

function renderGrid(data) {
  if (gridLayer) {
    gridLayer.remove();
  }
  resetAssignments();
  sectorLayers.clear();
  gridLayer = L.geoJSON(data, {
    style: { color: '#1e3352', weight: 1, fillOpacity: 0.18, fillColor: '#10213b' },
    onEachFeature(feature, layer) {
      const { id } = feature.properties;
      layer.on('click', () => {
        if (!state.currentTractor) return;
        applyAssignment(id, state.currentTractor);
        socket.emit('assign_sector', { sector_id: id, tractor_id: state.currentTractor });
      });
      layer.on('mouseover', () => highlightLayer(layer, state.assignments[id]));
      layer.on('mouseout', () => resetHighlight(layer, state.assignments[id]));
      sectorLayers.set(id, layer);
    },
  }).addTo(map);
  if (Array.isArray(data.features)) {
    gridMeta.textContent = `Сетка: ${data.features.length}`;
  }
  if (gridLayer.getBounds().isValid()) {
    map.fitBounds(gridLayer.getBounds(), { padding: [20, 20] });
  }
  setMapSpinner(false);
}

function fetchGrid() {
  setMapSpinner(true);
  fetch('/grid')
    .then((res) => {
      if (!res.ok) {
        return res.json().then((data) => { throw new Error(data.error || 'Ошибка загрузки сетки'); });
      }
      return res.json();
    })
    .then((data) => {
      renderGrid(data);
    })
    .catch((err) => {
      appendLog(`[GRID ERROR] ${err.message}`);
      setMapSpinner(false);
    });
}

let roadsFetched = false;

function loadRoadsLayer({ silent = false } = {}) {
  if (!silent) setMapSpinner(true);
  fetch('/roads')
    .then((res) => {
      if (!res.ok) throw new Error('Дороги недоступны');
      return res.json();
    })
    .then((data) => {
      if (roadsLayer) roadsLayer.remove();
      roadLayers.clear();
      roadsLayer = L.geoJSON(data, {
        style: { color: '#94a3b8', weight: 2 },
        onEachFeature(feature, layer) {
          roadLayers.set(feature.properties.id, layer);
          layer.bindTooltip(feature.properties.name || 'Без названия', { opacity: 0.8 });
        },
      }).addTo(map);
      if (roadsLayer.getBounds().isValid() && state.mode === 'road') {
        map.fitBounds(roadsLayer.getBounds(), { padding: [20, 20] });
      }
      if (!silent) {
        const total = data.features.reduce((acc, feature) => acc + (feature.properties.length_m || 0), 0);
        appendLog(`[ROADS] Загрузил ${data.features.length} линий, всего ${(total / 1000).toFixed(1)} км`);
      }
      roadsFetched = true;
      if (!silent) setMapSpinner(false);
    })
    .catch(() => {
      appendLog('[ROADS] ⚠️ Дороги не загружены');
      if (!silent) setMapSpinner(false);
    });
}

function setMode(newMode) {
  if (state.mode === newMode) return;
  state.mode = newMode;
  appendLog(`[MAP] Переключено: ${newMode === 'grid' ? 'Сетка' : 'Дороги'}`);
  setMapSpinner(true);
  if (newMode === 'grid') {
    if (roadsLayer) roadsLayer.remove();
    fetchGrid();
  } else {
    if (gridLayer) gridLayer.remove();
    if (roadsFetched) {
      loadRoadsLayer({ silent: true });
      setMapSpinner(false);
    } else {
      loadRoadsLayer();
    }
  }
}

function submitSettings() {
  const payload = {
    grid_cells: Number(document.getElementById('grid-cells').value || initialSettings.grid_cells),
    n_units: Number(document.getElementById('n-units').value || initialSettings.n_units),
    target_km: Number(document.getElementById('target-km').value || initialSettings.target_km),
    travel_mode: document.getElementById('travel-mode').value,
    max_waypoints: Number(document.getElementById('max-waypoints').value || initialSettings.max_waypoints),
    use_base_as_start: document.getElementById('use-base-start').checked,
    base_lat: Number(document.getElementById('base-lat').value || initialSettings.base_lat),
    base_lon: Number(document.getElementById('base-lon').value || initialSettings.base_lon),
    center_lat: Number(document.getElementById('center-lat').value || initialSettings.center_lat),
    center_lon: Number(document.getElementById('center-lon').value || initialSettings.center_lon),
    mode: state.mode,
    advanced: state.advanced,
    street_source: state.streetSource,
    routing_provider: state.provider,
  };
  setBusy('#apply-settings', true);
  fetch('/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
    .then((res) => res.json())
    .then((data) => {
      if (data.error) throw new Error(data.error);
      appendLog('[SETTINGS] Настройки обновлены, обновляю сетку…');
      buildTractors();
      showToast('Настройки сохранены, сетка обновится автоматически', 'info');
    })
    .catch((err) => {
      appendLog(`[SETTINGS ERROR] ${err.message}`);
      showToast(err.message, 'error');
    })
    .finally(() => setBusy('#apply-settings', false));
}

function autoAssign() {
  const btn = document.getElementById('auto-assign');
  if (!btn) return;
  if (!btn.dataset.label) btn.dataset.label = btn.textContent;
  setBusy(btn, true);
  btn.textContent = '⏳ Распределяю…';
  appendLog('[ASSIGN] Запускаю автораспределение…');
  resetAssignments();
  fetch('/auto_assign', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      mode: state.mode,
      advanced: state.advanced,
      streetSource: state.streetSource,
    }),
  })
    .then((res) => res.json())
    .then((data) => {
      if (data.error || data.ok === false || data.success === false) {
        throw new Error(data.error || 'Автораспределение не запущено');
      }
      appendLog('🔁 Автораспределение запущено, ожидайте прогресс…');
      showToast('Автораспределение запущено', 'info');
    })
    .catch((err) => {
      appendLog(`[ASSIGN ERROR] ${err.message}`);
      showToast(err.message, 'error');
      setBusy(btn, false);
      btn.textContent = btn.dataset.label || '🔀 Авто-раздать районы';
    });
}

function buildRoutes() {
  appendLog('🚀 Запуск построения маршрутов');
  setSpinner(true);
  disableDuringBuild(true);
  fetch('/build_routes', { method: 'POST' })
    .then((res) => res.json())
    .then((data) => {
      if (data.error) throw new Error(data.error);
    })
    .catch((err) => {
      appendLog(`[ROUTE ERROR] ${err.message}`);
      setSpinner(false);
      disableDuringBuild(false);
    });
}

function disableDuringBuild(flag) {
  const buttons = document.querySelectorAll('.btn');
  buttons.forEach((btn) => {
    if (btn.id !== 'download-kml') btn.disabled = flag;
  });
}

function downloadKml() {
  window.open('/routes_grid.kml', '_blank');
}

function clearRoutes() {
  fetch('/clear_routes', { method: 'POST' })
    .then((res) => res.json())
    .then((data) => {
      if (data.error) throw new Error(data.error);
      appendLog('[ROUTE] Маршруты очищены');
    })
    .catch((err) => appendLog(`[ROUTE ERROR] ${err.message}`));
}

function clearAssignmentsForCurrent() {
  const target = state.currentTractor;
  if (!target) return;
  Object.entries(state.assignments).forEach(([sectorId, tractorId]) => {
    if (tractorId === target) {
      delete state.assignments[sectorId];
      const layer = sectorLayers.get(sectorId);
      if (layer) {
        layer.setStyle({ color: '#1e3352', fillColor: '#10213b', fillOpacity: 0.18, weight: 1 });
        layer.unbindTooltip();
      }
    }
  });
  socket.emit('assignments_reset', { tractor_id: target });
}

function resetAllAssignments() {
  resetAssignments();
  socket.emit('assignments_reset', {});
}

tractorSelect.addEventListener('change', (event) => {
  state.currentTractor = event.target.value;
});

document.getElementById('apply-settings').addEventListener('click', submitSettings);
document.getElementById('auto-assign').addEventListener('click', autoAssign);
document.getElementById('save-build').addEventListener('click', buildRoutes);
document.getElementById('download-kml').addEventListener('click', downloadKml);
document.getElementById('clear-routes').addEventListener('click', clearRoutes);
document.getElementById('clear-selected').addEventListener('click', clearAssignmentsForCurrent);
document.getElementById('reset-all').addEventListener('click', resetAllAssignments);

Array.from(document.querySelectorAll('input[name="mode"]')).forEach((el) => {
  el.addEventListener('change', () => {
    setMode(el.value);
  });
});

Array.from(document.querySelectorAll('input[name="street-source"]')).forEach((el) => {
  el.addEventListener('change', () => {
    state.streetSource = el.value;
    appendLog(`[STREET] Источник улиц: ${state.streetSource}`);
    showToast(`Источник улиц: ${state.streetSource === 'google' ? 'Google' : 'Yandex'}`, 'info');
  });
});

Array.from(document.querySelectorAll('input[name="provider"]')).forEach((el) => {
  el.addEventListener('change', () => {
    state.provider = el.value;
    appendLog(`[ROUTING] Провайдер маршрутов: ${state.provider}`);
  });
});

const advancedToggle = document.querySelector('input[name="advanced"]');
if (advancedToggle) {
  advancedToggle.addEventListener('change', () => {
    state.advanced = advancedToggle.checked;
    appendLog(`[ADVANCED] ${state.advanced ? 'Включено' : 'Отключено'}`);
    showToast(state.advanced ? 'Advanced optimization включена' : 'Advanced optimization отключена', 'info');
  });
}

function handleLogEvent(data) {
  if (data && data.message) appendLog(data.message);
}

socket.on('log', handleLogEvent);

socket.on('progress', (payload) => {
  if (!payload) return;
  updateGlobalProgress(payload.stage, payload.text, payload.progress);
  if (payload.stage === 'ASSIGN') {
    const btn = document.getElementById('auto-assign');
    const text = String(payload.text || '').toLowerCase();
    if (btn && btn.classList.contains('loading')) {
      if (text.includes('ошибка') || text.includes('⚠️')) {
        setBusy(btn, false);
        btn.textContent = btn.dataset.label || '🔀 Авто-раздать районы';
      }
    }
  }
});

socket.on('grid_ready', (data) => {
  if (data && typeof data.cells === 'number') {
    gridMeta.textContent = `Сетка: ${data.cells}`;
  }
});

socket.on('grid_error', (payload) => {
  if (payload && payload.message) {
    gridAlert.hidden = false;
    gridAlert.textContent = `⚠️ ${payload.message}`;
    appendLog(`[GRID ERROR] ${payload.message}`);
  } else {
    gridAlert.hidden = true;
    appendLog('[GRID] Сетка готова к работе');
  }
});

socket.on('grid_updated', (data) => {
  if (state.mode === 'grid') {
    renderGrid(data);
  }
  if (Array.isArray(data.features)) {
    gridMeta.textContent = `Сетка: ${data.features.length}`;
  }
});

socket.on('settings', (payload) => {
  Object.assign(initialSettings, payload.settings);
  document.getElementById('grid-cells').value = initialSettings.grid_cells;
  document.getElementById('n-units').value = initialSettings.n_units;
  document.getElementById('target-km').value = initialSettings.target_km;
  document.getElementById('travel-mode').value = initialSettings.travel_mode;
  document.getElementById('max-waypoints').value = initialSettings.max_waypoints;
  document.getElementById('use-base-start').checked = Boolean(initialSettings.use_base_as_start);
  document.getElementById('base-lat').value = initialSettings.base_lat;
  document.getElementById('base-lon').value = initialSettings.base_lon;
  document.getElementById('center-lat').value = initialSettings.center_lat;
  document.getElementById('center-lon').value = initialSettings.center_lon;
  state.mode = initialSettings.mode || state.mode;
  state.advanced = Boolean(initialSettings.advanced);
  state.streetSource = initialSettings.street_source || state.streetSource;
  state.provider = initialSettings.routing_provider || state.provider;
  const modeInput = document.querySelector(`input[name="mode"][value="${state.mode}"]`);
  if (modeInput) modeInput.checked = true;
  if (advancedToggle) advancedToggle.checked = state.advanced;
  const streetInput = document.querySelector(`input[name="street-source"][value="${state.streetSource}"]`);
  if (streetInput) streetInput.checked = true;
  const providerInput = document.querySelector(`input[name="provider"][value="${state.provider}"]`);
  if (providerInput) providerInput.checked = true;
  if (state.tractors.length !== initialSettings.n_units) {
    buildTractors();
  }
});

socket.on('update_cell_assignment', (payload) => {
  if (!payload || !payload.cell_id || !payload.tractor) return;
  applyAssignment(payload.cell_id, payload.tractor);
});

socket.on('assignments_updated', (payload) => {
  if (!payload || !payload.assignments) return;
  resetAssignments();
  Object.entries(payload.assignments).forEach(([sectorId, tractorId]) => applyAssignment(sectorId, tractorId));
  appendLog('[ASSIGN] Назначения обновлены');
  if (Array.isArray(payload.summary)) {
    payload.summary.forEach((item) => {
      const roads = Math.round(item.roads_m || 0);
      appendLog(`[ASSIGN] ${item.tractor}: ${item.cells} клеток, ${item.area_km2.toFixed(2)} км², дорог ${roads} м`);
    });
  }
  const btn = document.getElementById('auto-assign');
  if (btn && btn.classList.contains('loading')) {
    setBusy(btn, false);
    btn.textContent = btn.dataset.label || '🔀 Авто-раздать районы';
    showToast('Назначения обновлены', 'success');
  }
});

socket.on('roads_assignment', (data) => {
  appendLog('[ASSIGN] Назначения дорог обновлены');
  roadLayers.forEach((layer) => layer.setStyle({ color: '#64748b', weight: 2 }));
  Object.entries(data).forEach(([roadId, tractorId]) => {
    const layer = roadLayers.get(roadId);
    if (!layer) return;
    const tractor = getTractorById(tractorId);
    const color = tractor ? tractor.color : '#38bdf8';
    layer.setStyle({ color, weight: 3 });
  });
  const btn = document.getElementById('auto-assign');
  if (btn && btn.classList.contains('loading')) {
    setBusy(btn, false);
    btn.textContent = btn.dataset.label || '🔀 Авто-раздать районы';
    showToast('Назначены зоны по дорогам', 'success');
  }
});

socket.on('route_step', (data) => {
  const { tractor_id: tractorId, polyline } = data;
  if (!routeLayers.has(tractorId)) {
    const tractor = getTractorById(tractorId);
    routeLayers.set(tractorId, L.polyline([], {
      color: tractor ? tractor.color : '#38bdf8',
      weight: 4,
    }).addTo(map));
  }
  const layer = routeLayers.get(tractorId);
  (polyline || []).forEach(([lat, lon]) => layer.addLatLng([lat, lon]));
});

socket.on('tractor_done', (info) => {
  appendLog('✅ Один из тракторов завершил маршрут');
  if (info && info.tractor && info.length) {
    const tractor = getTractorById(info.tractor);
    if (tractor) {
      tractor.length = Number(info.length);
      renderLegend();
    }
  }
});

socket.on('clear_routes', () => {
  routeLayers.forEach((layer) => map.removeLayer(layer));
  routeLayers.clear();
  if (kmlLayer) {
    map.removeLayer(kmlLayer);
    kmlLayer = null;
  }
});

socket.on('routes_ready', () => {
  setSpinner(false);
  disableDuringBuild(false);
  fetch('/routes_grid.kml')
    .then((res) => res.text())
    .then((kmlText) => {
      if (kmlLayer) map.removeLayer(kmlLayer);
      const parser = new DOMParser();
      const kml = parser.parseFromString(kmlText, 'text/xml');
      kmlLayer = new L.KML(kml);
      map.addLayer(kmlLayer);
      if (kmlLayer.getBounds().isValid()) map.fitBounds(kmlLayer.getBounds());
      appendLog('📍 Итоговый KML добавлен на карту');
    });
});

socket.on('build_status', (payload) => {
  if (!payload) return;
  setSpinner(Boolean(payload.running));
  if (!payload.running) disableDuringBuild(false);
});

socket.on('build_done', (data) => {
  setSpinner(false);
  disableDuringBuild(false);
  if (!data?.success) {
    appendLog('[ROUTE ERROR] Построение завершилось с ошибкой');
    showToast('Маршрутизация завершилась с ошибкой', 'error');
  } else {
    showToast('Маршруты готовы', 'success');
  }
});

function loadGridFromSettings() {
  if (state.mode === 'grid') {
    fetchGrid();
  }
}

function initialize() {
  buildTractors();
  if (state.mode === 'grid') {
    fetchGrid();
  } else {
    loadRoadsLayer();
  }
  if (gridError) appendLog(`[GRID ERROR] ${gridError}`);
  if (!hasGoogleKey) appendLog('⚠️ GOOGLE_API_KEY не найден — маршруты будут строиться прямыми линиями');
  if (!hasYandexKey) appendLog('ℹ️ YANDEX_API_KEY отсутствует, выбирайте источник Google');
  loadRoadsLayer({ silent: true });
}

initialize();

window.addEventListener('resize', () => {
  if (window.innerWidth >= 992) sidebar.classList.remove('open');
});

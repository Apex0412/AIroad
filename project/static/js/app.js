/* global L, io, initialSettings, gridError, tractorColors, hasGoogleKey, hasYandexKey */
const state = {
  assignments: {},
  tractors: [],
  currentTractor: null,
  mode: initialSettings.mode || 'grid',
  advanced: Boolean(initialSettings.advanced),
  streetSource: initialSettings.street_source || 'google',
  building: false,
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

const spinner = document.getElementById('spinner');
const gridMeta = document.getElementById('grid-meta');
const advancedToggle = document.querySelector('input[name="advanced"]');
const logEl = document.getElementById('log');

const sectorLayers = new Map();
const routeLayers = new Map();
const roadLayers = new Map();
let kmlLayer = null;
let roadsLayer = null;

const socket = io();

function setSpinner(active) {
  state.building = active;
  spinner.classList.toggle('active', active);
}

function classifyMessage(message) {
  if (!message) return 'info';
  const text = message.toLowerCase();
  if (text.includes('error') || text.includes('ошибка') || text.includes('❌')) return 'error';
  if (text.includes('⚠️') || text.includes('warn')) return 'warn';
  if (text.includes('✅') || text.includes('готов') || text.includes('done') || text.includes('[done]')) return 'success';
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
    });
  }
  state.currentTractor = state.tractors[0]?.id || null;
  const select = document.getElementById('tractor-select');
  select.innerHTML = '';
  state.tractors.forEach((tractor) => {
    const option = document.createElement('option');
    option.value = tractor.id;
    option.textContent = tractor.name;
    select.appendChild(option);
  });
}

function getTractorById(id) {
  return state.tractors.find((t) => t.id === id);
}

function styleForTractor(id) {
  const tractor = getTractorById(id);
  if (!tractor) return { color: '#94a3b8', fillColor: '#1f2937' };
  return { color: '#cbd5f5', fillColor: tractor.color };
}

function updateLayerTooltip(layer, sectorId, tractorId) {
  const tractor = getTractorById(tractorId);
  if (!tractor) {
    layer.unbindTooltip();
    return;
  }
  layer.bindTooltip(
    `${tractor.name}<br>Сектор: ${sectorId}`,
    { sticky: true, opacity: 0.85 },
  );
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
  layer.setStyle({
    color: tractorId ? style.color : '#1e3352',
    weight: 1,
  });
}

function loadGrid() {
  fetch('/grid')
    .then((res) => {
      if (!res.ok) {
        return res.json().then((data) => {
          throw new Error(data.error || 'Ошибка загрузки сетки');
        });
      }
      return res.json();
    })
    .then((data) => {
      if (window.gridLayer) {
        window.gridLayer.remove();
      }
      resetAssignments();
      sectorLayers.clear();
      window.gridLayer = L.geoJSON(data, {
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
      if (window.gridLayer.getBounds().isValid()) {
        map.fitBounds(window.gridLayer.getBounds());
      }
    })
    .catch((err) => appendLog(`[GRID ERROR] ${err.message}`));
}

function loadRoads() {
  fetch('/roads')
    .then((res) => {
      if (!res.ok) throw new Error('Дороги недоступны');
      return res.json();
    })
    .then((data) => {
      if (roadsLayer) {
        roadsLayer.remove();
      }
      roadLayers.clear();
      roadsLayer = L.geoJSON(data, {
        style: { color: '#64748b', weight: 2 },
        onEachFeature(feature, layer) {
          roadLayers.set(feature.properties.id, layer);
          layer.bindTooltip(feature.properties.name || 'Без названия', { opacity: 0.8 });
        },
      }).addTo(map);
    })
    .catch(() => appendLog('[ROADS] ⚠️ Дороги не загружены'));
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
  };
  fetch('/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
    .then((res) => res.json())
    .then((data) => {
      if (data.error) throw new Error(data.error);
      appendLog('[SETTINGS] Настройки обновлены');
      loadGrid();
      buildTractors();
    })
    .catch((err) => appendLog(`[SETTINGS ERROR] ${err.message}`));
}

function autoAssign() {
  const params = new URLSearchParams({
    mode: state.mode,
    advanced: state.advanced,
    streetSource: state.streetSource,
  });
  fetch(`/auto_assign?${params.toString()}`, { method: 'POST' })
    .then((res) => res.json())
    .then((data) => {
      if (data.error) throw new Error(data.error);
      appendLog(`🔀 Автораспределение завершено (${data.assigned})`);
      if (Array.isArray(data.summary)) {
        data.summary.forEach((item) => {
          appendLog(`[ASSIGN] ${item.tractor}: ${item.cells} клеток, ${item.area_km2.toFixed(2)} км², дорог ${Math.round(item.roads_m)} м`);
        });
      }
    })
    .catch((err) => appendLog(`[ASSIGN ERROR] ${err.message}`));
}

function buildRoutes() {
  appendLog('🚀 Запуск построения маршрутов');
  setSpinner(true);
  fetch('/build_routes', { method: 'POST' })
    .then((res) => res.json())
    .then((data) => {
      if (data.error) throw new Error(data.error);
    })
    .catch((err) => {
      appendLog(`[ROUTE ERROR] ${err.message}`);
      setSpinner(false);
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
  const modeInput = document.querySelector(`input[name="mode"][value="${state.mode}"]`);
  if (modeInput) modeInput.checked = true;
  if (advancedToggle) advancedToggle.checked = state.advanced;
  const streetInput = document.querySelector(`input[name="street-source"][value="${state.streetSource}"]`);
  if (streetInput) streetInput.checked = true;
  if (state.tractors.length !== initialSettings.n_units) {
    buildTractors();
  }
});

socket.on('assignments', (data) => {
  resetAssignments();
  Object.entries(data).forEach(([sectorId, tractorId]) => applyAssignment(sectorId, tractorId));
  appendLog('[ASSIGN] Назначения обновлены');
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
});

function handleLogEvent(data) {
  if (data && data.message) {
    appendLog(data.message);
  }
}

socket.on('progress', handleLogEvent);
socket.on('log', handleLogEvent);

socket.on('route_step', (data) => {
  const { tractor_id: tractorId, coords } = data;
  if (!routeLayers.has(tractorId)) {
    const tractor = getTractorById(tractorId);
    routeLayers.set(tractorId, L.polyline([], {
      color: tractor ? tractor.color : '#38bdf8',
      weight: 4,
    }).addTo(map));
  }
  const layer = routeLayers.get(tractorId);
  coords.forEach(([lat, lon]) => layer.addLatLng([lat, lon]));
});

socket.on('tractor_done', () => appendLog('✅ Один из тракторов завершил маршрут'));

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

socket.on('build_done', (data) => {
  setSpinner(false);
  if (!data?.success) {
    appendLog('[ROUTE ERROR] Построение завершилось с ошибкой');
  }
});

socket.on('grid_ready', (data) => {
  if (data && typeof data.cells === 'number') {
    gridMeta.textContent = `Сетка: ${data.cells}`;
  }
});

socket.on('grid_error', (payload) => {
  if (!payload || !payload.message) {
    appendLog('[GRID] Сетка готова к работе');
    return;
  }
  gridMeta.textContent = 'Сетка: —';
  appendLog(`[GRID ERROR] ${payload.message}`);
});

buildTractors();
loadGrid();
loadRoads();

if (gridError) {
  appendLog(`[GRID ERROR] ${gridError}`);
}
if (!hasGoogleKey) {
  appendLog('⚠️ GOOGLE_MAPS_API_KEY не найден — маршруты будут строиться прямыми линиями');
}
if (!hasYandexKey) {
  appendLog('ℹ️ YANDEX_GEOCODER_API_KEY отсутствует, выбирайте источник Google');
}

document.getElementById('tractor-select').addEventListener('change', (event) => {
  state.currentTractor = event.target.value;
});

document.getElementById('clear-selected').addEventListener('click', () => {
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
});

document.getElementById('reset-all').addEventListener('click', () => {
  resetAssignments();
  socket.emit('assignments_reset', {});
});

document.getElementById('save-build').addEventListener('click', buildRoutes);

document.getElementById('download-kml').addEventListener('click', downloadKml);

document.getElementById('auto-assign').addEventListener('click', autoAssign);

document.getElementById('clear-routes').addEventListener('click', clearRoutes);

document.getElementById('apply-settings').addEventListener('click', submitSettings);

Array.from(document.querySelectorAll('input[name="mode"]')).forEach((el) => {
  el.addEventListener('change', () => {
    state.mode = el.value;
    appendLog(`[MODE] Переключено на ${state.mode === 'grid' ? 'сетку' : 'дороги'}`);
  });
});

if (advancedToggle) {
  advancedToggle.addEventListener('change', () => {
    state.advanced = advancedToggle.checked;
    appendLog(`[ADVANCED] ${state.advanced ? 'Включено' : 'Отключено'}`);
  });
}

Array.from(document.querySelectorAll('input[name="street-source"]')).forEach((el) => {
  el.addEventListener('change', () => {
    state.streetSource = el.value;
    appendLog(`[STREET] Источник улиц: ${state.streetSource}`);
  });
});

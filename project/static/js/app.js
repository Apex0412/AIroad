/* global L, io, initialSettings, gridError, tractorColors, hasGoogleKey, hasYandexKey */
const state = {
  assignments: {},
  tractors: [],
  currentTractor: null,
  mode: initialSettings.mode || 'grid',
  advanced: Boolean(initialSettings.advanced),
  streetSource: initialSettings.street_source || 'google',
};

const map = L.map('map').setView([initialSettings.center_lat, initialSettings.center_lon], 13);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '&copy; OpenStreetMap contributors'
}).addTo(map);

const advancedToggle = document.querySelector('input[name="advanced"]');

const sectorLayers = new Map();
const routeLayers = new Map();
const roadLayers = new Map();
let kmlLayer = null;
let roadsLayer = null;

const socket = io();

function appendLog(message) {
  const logEl = document.getElementById('log');
  const time = new Date().toLocaleTimeString();
  const color = message.includes('Ошибка') || message.includes('❌') ? '#f87171'
    : message.includes('✅') ? '#4ade80'
    : message.includes('🚀') ? '#60a5fa'
    : '#e2e8f0';
  const div = document.createElement('div');
  div.style.color = color;
  div.textContent = `[${time}] ${message}`;
  logEl.appendChild(div);
  logEl.scrollTop = logEl.scrollHeight;
}

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
  if (!tractor) return { color: '#64748b', fillColor: '#0f172a' };
  return { color: '#94a3b8', fillColor: tractor.color };
}

function applyAssignment(sectorId, tractorId) {
  state.assignments[sectorId] = tractorId;
  const layer = sectorLayers.get(sectorId);
  if (layer) {
    const style = styleForTractor(tractorId);
    layer.setStyle({ color: style.color, fillColor: style.fillColor, fillOpacity: 0.45 });
  }
}

function resetAssignments() {
  Object.keys(state.assignments).forEach((sectorId) => {
    const layer = sectorLayers.get(sectorId);
    if (layer) {
      layer.setStyle({ color: '#475569', fillColor: '#1f2937', fillOpacity: 0.2 });
    }
  });
  state.assignments = {};
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
        onEachFeature(feature, layer) {
          const { id } = feature.properties;
          layer.setStyle({ color: '#475569', weight: 1, fillOpacity: 0.2 });
          layer.on('click', () => {
            if (!state.currentTractor) return;
            applyAssignment(id, state.currentTractor);
            socket.emit('assign_sector', { sector_id: id, tractor_id: state.currentTractor });
          });
          sectorLayers.set(id, layer);
        },
      }).addTo(map);
      if (window.gridLayer.getBounds().isValid()) {
        map.fitBounds(window.gridLayer.getBounds());
      }
    })
    .catch((err) => appendLog(err.message));
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
        style: { color: '#94a3b8', weight: 2 },
        onEachFeature(feature, layer) {
          roadLayers.set(feature.properties.id, layer);
        },
      }).addTo(map);
    })
    .catch(() => appendLog('Дороги не загружены'));
}

function submitSettings() {
  const gridCells = Number(document.getElementById('grid-cells').value || initialSettings.grid_cells);
  const payload = {
    grid_cells: gridCells,
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
      appendLog('Настройки обновлены');
      loadGrid();
    })
    .catch((err) => appendLog(`Ошибка настроек: ${err.message}`));
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
      appendLog(`Автораспределение завершено (${data.assigned})`);
      if (Array.isArray(data.summary)) {
        data.summary.forEach((item) => {
          appendLog(`${item.tractor}: ${item.cells} клеток, ${item.area_km2.toFixed(2)} км², дорог ${Math.round(item.roads_m)} м`);
        });
      }
    })
    .catch((err) => appendLog(`Ошибка автораспределения: ${err.message}`));
}

function buildRoutes() {
  appendLog('🚀 Старт построения маршрутов');
  fetch('/build_routes', { method: 'POST' })
    .then((res) => res.json())
    .then((data) => {
      if (data.error) throw new Error(data.error);
    })
    .catch((err) => appendLog(`Ошибка запуска маршрутов: ${err.message}`));
}

function downloadKml() {
  window.open('/routes_grid.kml', '_blank');
}

function clearRoutes() {
  fetch('/clear_routes', { method: 'POST' })
    .then((res) => res.json())
    .then((data) => {
      if (data.error) throw new Error(data.error);
      appendLog('Маршруты очищены');
    })
    .catch((err) => appendLog(`Ошибка очистки маршрутов: ${err.message}`));
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
  map.setView([initialSettings.center_lat, initialSettings.center_lon], map.getZoom());
});

socket.on('assignments', (data) => {
  resetAssignments();
  Object.entries(data).forEach(([sectorId, tractorId]) => applyAssignment(sectorId, tractorId));
  appendLog('Назначения обновлены');
});

socket.on('roads_assignment', (data) => {
  appendLog('Назначения дорог обновлены');
  roadLayers.forEach((layer) => layer.setStyle({ color: '#94a3b8', weight: 2 }));
  Object.entries(data).forEach(([roadId, tractorId]) => {
    const layer = roadLayers.get(roadId);
    if (!layer) return;
    const tractor = getTractorById(tractorId);
    const color = tractor ? tractor.color : '#38bdf8';
    layer.setStyle({ color, weight: 3 });
  });
});

socket.on('progress', (data) => appendLog(data.message));

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

socket.on('grid_error', (payload) => {
  if (!payload || !payload.message) {
    appendLog('Сетка готова к работе');
    return;
  }
  appendLog(`Ошибка сетки: ${payload.message}`);
});

buildTractors();
loadGrid();
loadRoads();

document.getElementById('tractor-select').addEventListener('change', (event) => {
  state.currentTractor = event.target.value;
});

document.getElementById('clear-selected').addEventListener('click', () => {
  const target = state.currentTractor;
  Object.entries(state.assignments).forEach(([sectorId, tractorId]) => {
    if (tractorId === target) {
      delete state.assignments[sectorId];
      const layer = sectorLayers.get(sectorId);
      if (layer) layer.setStyle({ color: '#475569', fillColor: '#1f2937', fillOpacity: 0.2 });
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
  });
});

if (advancedToggle) {
  advancedToggle.addEventListener('change', () => {
    state.advanced = advancedToggle.checked;
  });
}

Array.from(document.querySelectorAll('input[name="street-source"]')).forEach((el) => {
  el.addEventListener('change', () => {
    state.streetSource = el.value;
  });
});

if (gridError) {
  appendLog(`Ошибка сетки: ${gridError}`);
}
if (!hasGoogleKey) {
  appendLog('⚠️ GOOGLE_MAPS_API_KEY не найден — маршруты будут строиться прямыми линиями');
}
if (!hasYandexKey) {
  appendLog('ℹ️ YANDEX_GEOCODER_API_KEY отсутствует, выбирайте источник Google');
}

/*
 * ANCHOR Live Map — map-only widget for dashboards.
 *
 * Needs Leaflet 1.9.x loaded first (window.L):
 *   <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
 *   <script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
 *
 * Usage:
 *   const lm = AnchorLiveMap.mount('map-div-id' | HTMLElement, {
 *     api: 'http://100.96.155.4:30793',   // map server base URL
 *     interval: 1500,                      // poll period in ms
 *     theme: 'light' | 'dark',             // default: follows prefers-color-scheme
 *     onData: d => {},                     // raw /api/geodata payload every poll
 *     onShipClick: name => {},             // a ship marker was clicked
 *     onError: err => {},
 *   });
 *   lm.focusShip('ferry-hakata'); lm.refresh(); lm.destroy(); lm.map  // Leaflet map
 *
 * Data comes from GET {api}/api/geodata (CORS: *). Draws ships, routes,
 * base stations (coloured by disaster impact), typhoon/tsunami hazards and
 * fleet-ai collision warnings (red line between the pair).
 */
(function (root) {
  'use strict';
  const PALETTE = {
    light: {real: '#111418', moving: '#1f6feb', still: '#8a94a3', poa: '#2da44e', route: '#1f6feb', warn: '#d4a72c',
            bad: '#cf222e', typhoon: '#e5534b', tsunami: '#0969da', panel: '#fff', text: '#1b1f24', line: '#e3e6ea'},
    dark:  {real: '#111418', moving: '#58a6ff', still: '#8b949e', poa: '#3fb950', route: '#58a6ff', warn: '#e3b341',
            bad: '#ff7b72', typhoon: '#ff7b72', tsunami: '#58a6ff', panel: '#161b22', text: '#e6edf3', line: '#30363d'},
  };
  const IMPACT_LABEL = {outer: 'degraded', core: 'severe', flooded: 'flooded', down: 'down'};
  const REAL = 'ship-001';
  const ll = c => [c[1], c[0]];
  const esc = s => String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));

  function injectCss() {
    if (document.getElementById('anchor-livemap-css')) return;
    const s = document.createElement('style');
    s.id = 'anchor-livemap-css';
    s.textContent = `
.alm-lbl{background:var(--alm-panel);color:var(--alm-text);border:1px solid var(--alm-line);box-shadow:none;font-size:11px;padding:1px 5px}
.alm-lbl:before{display:none}
.alm-hz{background:transparent;border:0;box-shadow:none;font-weight:700;font-size:13px;color:var(--alm-text);text-shadow:0 0 3px var(--alm-panel),0 0 3px var(--alm-panel)}
.alm-hz:before{display:none}`;
    document.head.appendChild(s);
  }

  function mount(target, opts) {
    if (!root.L) throw new Error('AnchorLiveMap: load Leaflet first');
    opts = Object.assign({api: '', interval: 1500}, opts || {});
    const el = typeof target === 'string' ? document.getElementById(target) : target;
    const api = opts.api.replace(/\/$/, '');
    const dark = opts.theme ? opts.theme === 'dark' : !!(root.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches);
    const P = PALETTE[dark ? 'dark' : 'light'];
    injectCss();
    el.style.setProperty('--alm-panel', P.panel); el.style.setProperty('--alm-text', P.text); el.style.setProperty('--alm-line', P.line);

    const map = L.map(el).setView([34.6, 129.6], 8);
    // Esri Ocean: English place names
    L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Ocean/World_Ocean_Base/MapServer/tile/{z}/{y}/{x}',
      {maxZoom: 13, attribution: 'Tiles &copy; Esri, GEBCO, NOAA, Garmin'}).addTo(map);
    L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Ocean/World_Ocean_Reference/MapServer/tile/{z}/{y}/{x}',
      {maxZoom: 13}).addTo(map);
    const layers = {hz: L.layerGroup().addTo(map), poa: L.layerGroup().addTo(map), route: L.layerGroup().addTo(map),
                    ue: L.layerGroup().addTo(map), cf: L.layerGroup().addTo(map)};
    const ueMarkers = {}, routes = {};
    let fitted = false, timer = null, stopped = false;

    const shipColor = u => u.assetName === REAL ? P.real : (u.velocity ? P.moving : P.still);
    const poaColor = p => !p.impact ? P.poa : (p.impact.level === 'outer' ? P.warn : P.bad);

    function drawPoas(poas) {
      layers.poa.clearLayers();
      for (const p of poas || []) {
        const c = poaColor(p), i = p.impact, at = ll(p.location.coordinates);
        L.circle(at, {radius: p.radius || 0, color: c, weight: 1, opacity: i ? .8 : .45, fillOpacity: i ? .08 : .04, interactive: false}).addTo(layers.poa);
        const tip = `<b>${esc(p.assetName)}</b>` + (i
          ? `<br>${esc(i.label)}: latency ${i.netChar.latency} ms · loss ${i.netChar.packetLoss}% · DL ${i.netChar.throughputDl} Mbps`
          : '<br>Normal');
        L.circleMarker(at, {radius: i ? 6 : 4, color: c, fillColor: c, fillOpacity: 1, weight: 1}).bindTooltip(tip).addTo(layers.poa);
      }
    }

    function drawHazards(hz) {
      layers.hz.clearLayers();
      for (const h of hz) {
        const c = ll(h.center);
        if (h.type === 'typhoon') {
          L.circle(c, {radius: h.radiusKm * 1000, color: P.typhoon, weight: 2, fillColor: P.typhoon, fillOpacity: .12, dashArray: '6 4'}).addTo(layers.hz);
          L.circle(c, {radius: h.coreRadiusKm * 1000, color: P.typhoon, weight: 1, fillColor: P.typhoon, fillOpacity: .25, interactive: false}).addTo(layers.hz);
          if (h.track) L.polyline(h.track.map(ll), {color: P.typhoon, weight: 2, opacity: .8, dashArray: '2 6'}).addTo(layers.hz);
          L.circleMarker(c, {radius: 3, color: P.typhoon, fillOpacity: 1}).bindTooltip('🌀 ' + esc(h.name), {permanent: true, direction: 'top', className: 'alm-hz'}).addTo(layers.hz);
        } else {
          L.circle(c, {radius: h.maxRadiusKm * 1000, color: P.tsunami, weight: 1, fill: false, dashArray: '4 6'}).addTo(layers.hz);
          L.circle(c, {radius: h.quakeRadiusKm * 1000, color: P.bad, weight: 1, fillColor: P.bad, fillOpacity: .15}).addTo(layers.hz);
          if (h.waveRadiusKm > 0) L.circle(c, {radius: h.waveRadiusKm * 1000, color: P.tsunami, weight: 3, fillColor: P.tsunami, fillOpacity: .08}).addTo(layers.hz);
          L.circleMarker(c, {radius: 4, color: P.bad, fillOpacity: 1}).bindTooltip('🌊 ' + esc(h.name), {permanent: true, direction: 'top', className: 'alm-hz'}).addTo(layers.hz);
        }
      }
    }

    function drawShips(ues, hz) {
      const seen = new Set(), inHz = {};
      for (const h of hz) for (const s of h.ships || []) (inHz[s] = inHz[s] || []).push(h.type);
      for (const u of ues) {
        const n = u.assetName; seen.add(n);
        const pos = ll(u.location.coordinates), col = shipColor(u);
        const tip = `<b>${esc(n)}</b><br>${pos[0].toFixed(4)}, ${pos[1].toFixed(4)}<br>speed ${u.velocity || 0} m/s` +
          `<br>base station ${(u.poaInRange || []).map(esc).join(', ') || '-'}` + (inHz[n] ? '<br>⚠ Inside a disaster zone' : '');
        if (!ueMarkers[n]) {
          ueMarkers[n] = L.circleMarker(pos, {radius: n === REAL ? 8 : 6, weight: 2, color: '#fff', fillColor: col, fillOpacity: 1})
            .bindTooltip(esc(n), {permanent: true, direction: 'right', offset: [8, 0], className: 'alm-lbl'})
            .bindPopup(tip).addTo(layers.ue);
          ueMarkers[n].on('click', () => opts.onShipClick && opts.onShipClick(n));
        } else {
          ueMarkers[n].setLatLng(pos).setStyle({fillColor: col, color: inHz[n] ? P.bad : '#fff'}).setPopupContent(tip);
        }
        const coords = u.path && u.path.coordinates, key = coords ? JSON.stringify(coords) : '';
        if (routes[n] && routes[n].key !== key) { layers.route.removeLayer(routes[n].line); delete routes[n]; }
        if (coords && !routes[n]) routes[n] = {key, line: L.polyline(coords.map(ll),
          {color: n === REAL ? P.real : P.route, weight: 2, opacity: .55, dashArray: u.velocity ? null : '4 4'}).addTo(layers.route)};
      }
      for (const n in ueMarkers) if (!seen.has(n)) { layers.ue.removeLayer(ueMarkers[n]); delete ueMarkers[n]; }
      for (const n in routes) if (!seen.has(n)) { layers.route.removeLayer(routes[n].line); delete routes[n]; }
      if (!fitted && ues.length) { map.fitBounds(L.latLngBounds(ues.map(u => ll(u.location.coordinates))).pad(.2)); fitted = true; }
    }

    function drawConflicts(cf, ues) {
      layers.cf.clearLayers();
      const pos = Object.fromEntries(ues.map(u => [u.assetName, ll(u.location.coordinates)]));
      const fmt = c => [c.cpaNm != null ? `CPA ${c.cpaNm} nm` : '', c.tcpaMin != null ? `TCPA ${c.tcpaMin} min` : '',
                        c.level || c.severity || ''].filter(Boolean).join(' · ');
      for (const c of (cf && cf.items) || []) {
        if (!pos[c.a] || !pos[c.b]) continue;
        L.polyline([pos[c.a], pos[c.b]], {color: P.bad, weight: 4, opacity: .9})
          .bindTooltip(`<b>${esc(c.a)} ↔ ${esc(c.b)}</b><br>${esc(fmt(c))}`, {sticky: true}).addTo(layers.cf);
        for (const n of [c.a, c.b]) L.circleMarker(pos[n], {radius: 12, color: P.bad, weight: 2, fill: false, interactive: false}).addTo(layers.cf);
      }
    }

    async function refresh() {
      try {
        const r = await fetch(api + '/api/geodata', {cache: 'no-store'});
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || r.status);
        if (stopped) return;
        const hz = d.hazards || [], ues = d.ues || [];
        drawHazards(hz); drawPoas(d.poas); drawShips(ues, hz); drawConflicts(d.conflicts, ues);
        opts.onData && opts.onData(d);
      } catch (e) {
        opts.onError ? opts.onError(e) : console.warn('AnchorLiveMap:', e);
      }
    }

    function loop() { refresh().finally(() => { if (!stopped) timer = setTimeout(loop, opts.interval); }); }
    loop();

    return {
      map,
      refresh,
      focusShip(name) {
        const m = ueMarkers[name]; if (!m) return false;
        map.setView(m.getLatLng(), Math.max(map.getZoom(), 10)); m.openPopup(); return true;
      },
      destroy() { stopped = true; clearTimeout(timer); map.remove(); },
    };
  }

  root.AnchorLiveMap = {mount};
})(typeof window !== 'undefined' ? window : this);

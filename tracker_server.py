# tracker_server.py
import os, time, math, sqlite3, requests
from typing import Dict, Any, List, Tuple
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS
from geopy.distance import geodesic

# GTFS-RT opcional (no usado directamente aquí)
_HAS_GTFS = True
try:
    from google.transit import gtfs_realtime_pb2  # type: ignore
except Exception:
    _HAS_GTFS = False

app = Flask(__name__)
CORS(app)

# ========= Config / Estado =========
DESTINO = (-33.4624, -70.6550)            # Paradero por defecto
OCUPACION: Dict[str, Dict[str, Any]] = {} # Ocupación por bus (último valor)
BUSES: Dict[str, Dict[str, Any]] = {}     # Micros simuladas: lat,lon,speed,route,idx,arrived
ORS_API_KEY = os.getenv("ORS_API_KEY", "").strip()  # si lo pones, se usa OpenRouteService

DB = "ocupacion.sqlite"
def init_db():
    con = sqlite3.connect(DB); cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS ocupacion(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bus_id TEXT, ts TEXT, count INTEGER, status TEXT, capacity INTEGER, pct REAL
    )""")
    con.commit(); con.close()
init_db()

# ========= HTML (UI) =========
INDEX_HTML = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Tracker móvil → servidor</title>
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <style>
    body { font-family: Arial, sans-serif; padding: 12px; max-width: 1100px; margin:auto; }
    h1 { font-size: 1.3rem; margin-bottom: 6px; }
    h2 { font-size: 1.05rem; margin: 14px 0 8px; }
    #status { margin: 8px 0; color: #333; }
    input, button { padding:8px; margin:4px 0; width:100%; box-sizing:border-box; }
    .row { display:flex; gap:8px; }
    .row > * { flex:1; }
    .grid-2 { display:grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .card { border:1px solid #ddd; border-radius:8px; padding:10px; }
    #map{height:360px;border-radius:8px;margin-top:8px;}
    table { width:100%; border-collapse: collapse; }
    th, td { padding: 6px; border-bottom:1px solid #eee; text-align:left; }
    small.mono { font-family: ui-monospace, Menlo, Consolas, monospace; }
  </style>
</head>
<body>
  <h1>Tracker móvil → servidor</h1>
  <div id="status">Estado: esperando</div>

  <div class="card">
    <h2>Identificación y destino (paradero)</h2>
    <div class="row">
      <div>
        <label>Destino lat</label>
        <input id="destLat" />
      </div>
      <div>
        <label>Destino lon</label>
        <input id="destLon" />
      </div>
    </div>
    <button id="setDestBtn">Establecer destino</button>
    <small class="mono">Define el paradero primero, luego crea la(s) micro(s).</small>
  </div>

  <div class="card">
    <h2>Simulador de micros (ruta automática)</h2>
    <div class="row">
      <div>
        <label>Bus ID</label>
        <input id="busId" placeholder="bus001" />
      </div>
      <div>
        <label>Velocidad (km/h)</label>
        <input id="speed" value="25" />
      </div>
    </div>
    <div class="row">
      <div>
        <label>Origen lat</label>
        <input id="srcLat" placeholder="-33.46" />
      </div>
      <div>
        <label>Origen lon</label>
        <input id="srcLon" placeholder="-70.68" />
      </div>
    </div>
    <div class="row">
      <button id="startSimBtn">Iniciar simulación (con ruta)</button>
      <button id="stopSimBtn">Detener simulación</button>
    </div>
    <small class="mono">La ruta se genera automáticamente (ORS si hay ORS_API_KEY, si no OSRM público).</small>
  </div>

  <div class="card">
    <h2>Mapa</h2>
    <div id="map"></div>
  </div>

  <div class="card">
    <h2>Próximas llegadas al paradero</h2>
    <div id="arrivals"></div>
  </div>

  <div class="grid-2">
    <div class="card">
      <h2>Ocupación (desde detector)</h2>
      <div id="occ"></div>
    </div>

    <div class="card">
      <h2>RED (no oficial) — Próximos buses por paradero</h2>
      <div class="row">
        <input id="stopId" placeholder="PA433" />
        <button id="fetchStopBtn">Consultar</button>
      </div>
      <div id="stopData"></div>
    </div>
  </div>

<script>
(function(){
  const statusEl    = document.getElementById('status');
  const arrivalsEl  = document.getElementById('arrivals');
  const setDestBtn  = document.getElementById('setDestBtn');
  const destLat     = document.getElementById('destLat');
  const destLon     = document.getElementById('destLon');

  const busIdEl     = document.getElementById('busId');
  const speedEl     = document.getElementById('speed');
  const srcLatEl    = document.getElementById('srcLat');
  const srcLonEl    = document.getElementById('srcLon');
  const startSimBtn = document.getElementById('startSimBtn');
  const stopSimBtn  = document.getElementById('stopSimBtn');

  const stopIdEl    = document.getElementById('stopId');
  const fetchStopBtn= document.getElementById('fetchStopBtn');
  const stopDataEl  = document.getElementById('stopData');

  const occEl       = document.getElementById('occ');

  // ---- Mapa ----
  let map = L.map('map', { zoomControl: true });
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19 }).addTo(map);
  let destMarker = L.marker([0,0], {title:'Destino (paradero)'}).addTo(map);
  let busMarkers   = {}; // bus_id -> marker
  let busPolylines = {}; // bus_id -> polyline

  // Cargar destino inicial
  fetch('/get_destination').then(r=>r.json()).then(d=>{
    const lat = d.destino[0], lon = d.destino[1];
    destLat.value = lat; destLon.value = lon;
    destMarker.setLatLng([lat, lon]);
    map.setView([lat, lon], 13);
  });

  setDestBtn.onclick = () => {
    const lat = parseFloat(destLat.value), lon = parseFloat(destLon.value);
    if(isNaN(lat) || isNaN(lon)){ alert('Destino inválido'); return; }
    fetch('/set_destination', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({lat, lon})
    }).then(r=>r.json()).then(_=>{
      destMarker.setLatLng([lat, lon]); map.setView([lat, lon], 13);
    });
  };

  // ---- Simulador (ruta automática al crear) ----
  startSimBtn.onclick = async () => {
    const bus_id = (busIdEl.value || 'bus001').trim();
    const speed  = parseFloat(speedEl.value || '25');
    const lat    = parseFloat(srcLatEl.value), lon = parseFloat(srcLonEl.value);
    if(isNaN(lat) || isNaN(lon)){ alert('Origen inválido'); return; }
    const res = await fetch('/sim/start', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({bus_id, lat, lon, speed_kmh: speed})
    });
    const j = await res.json();
    if(!j.ok){ alert('Error: ' + (j.error||'no se pudo iniciar')); return; }
    if (j.points && j.points.length >= 2) {
      drawRoute(bus_id, j.points);
    } else {
      // sin ruta (fallback recto) -> quita polilínea si existía
      if (busPolylines[bus_id]) { map.removeLayer(busPolylines[bus_id]); delete busPolylines[bus_id]; }
    }
  };

  stopSimBtn.onclick = async () => {
    const bus_id = (busIdEl.value || 'bus001').trim();
    await fetch('/sim/stop', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({bus_id})});
    if (busMarkers[bus_id])   { map.removeLayer(busMarkers[bus_id]);   delete busMarkers[bus_id]; }
    if (busPolylines[bus_id]) { map.removeLayer(busPolylines[bus_id]); delete busPolylines[bus_id]; }
  };

  function drawRoute(bus_id, points){
    if (busPolylines[bus_id]) map.removeLayer(busPolylines[bus_id]);
    busPolylines[bus_id] = L.polyline(points, {weight:4, opacity:0.7}).addTo(map);
    map.fitBounds(busPolylines[bus_id].getBounds().pad(0.3));
  }

  // ---- REFRESH de buses simulados (mapa + tabla de llegadas + resumen) ----
  async function refreshBuses(){
    try{
      const r = await fetch('/sim/buses');
      const j = await r.json();
      if(!j.ok) return;

      // Actualiza destino (por si cambió)
      const dest = j.destino; destMarker.setLatLng([dest[0], dest[1]]);

      // Marcadores en el mapa
      for(const b of j.buses){
        const id = b.bus_id, lat = b.lat, lon = b.lon;
        if(!busMarkers[id]){
          busMarkers[id] = L.marker([lat, lon], {title: id}).addTo(map);
        } else {
          busMarkers[id].setLatLng([lat, lon]);
        }
        busMarkers[id].bindTooltip(`${id}<br>Dist: ${b.distance_km.toFixed(2)} km<br>ETA: ${b.eta_min.toFixed(1)} min`, {permanent:false});
      }
      // Limpia marcadores que ya no existen
      for(const id of Object.keys(busMarkers)){
        if(!j.buses.find(x => x.bus_id === id)){
          map.removeLayer(busMarkers[id]); delete busMarkers[id];
        }
      }

      // Tabla de próximas llegadas
      const orden = [...j.buses].sort((a,b)=>a.eta_min - b.eta_min);
      let html = '<table><tr><th>Bus</th><th>Distancia</th><th>ETA</th><th>Estado</th></tr>';
      if(orden.length === 0){
        html += '<tr><td colspan="4"><i>Sin buses simulados</i></td></tr>';
        statusEl.textContent = "Estado: sin buses simulados";
      } else {
        const next = orden[0];
        statusEl.textContent = `Próxima llegada: ${next.bus_id} a ${next.distance_km.toFixed(2)} km (${next.eta_min.toFixed(1)} min)`;
        for(const b of orden){
          html += `<tr>
            <td><b>${b.bus_id}</b></td>
            <td>${b.distance_km.toFixed(2)} km</td>
            <td>${b.eta_min.toFixed(1)} min</td>
            <td>${b.arrived ? "En paradero" : (b.has_route ? "En ruta (con ruta)" : "En ruta (recta)")}</td>
          </tr>`;
        }
      }
      html += '</table>';
      arrivalsEl.innerHTML = html;

    }catch(e){ /* silencioso */ }
  }
  setInterval(refreshBuses, 1000);
  refreshBuses();

  // ---- Ocupación (cruza con simulación para Dist/ETA si existe ese bus) ----
  async function refreshOcc(){
    try{
      const [rOcc, rSim] = await Promise.all([fetch('/occupancy/list'), fetch('/sim/buses')]);
      const data = await rOcc.json();
      const sim  = await rSim.json();
      const busesSim = sim.ok ? sim.buses : [];

      let html = '<table><tr><th>Bus</th><th>Count</th><th>%</th><th>Status</th><th>Dist</th><th>ETA</th><th>TS</th></tr>';
      const keys = Object.keys(data);
      if(keys.length===0) html += '<tr><td colspan="7"><i>Sin datos aún</i></td></tr>';
      for (const [bus, v] of Object.entries(data)){
        const cap = v.capacity || 40;
        const pct = v.pct ?? Math.min(100, Math.round((v.count/cap)*100));
        const color = pct <= 50 ? '#2ecc71' : (pct <= 80 ? '#f1c40f' : '#e74c3c');

        const simRow = busesSim.find(b => b.bus_id === bus);
        const distTxt = simRow ? `${simRow.distance_km.toFixed(2)} km` : '—';
        const etaTxt  = simRow ? `${simRow.eta_min.toFixed(1)} min`    : '—';

        html += `<tr>
          <td><b>${bus}</b></td>
          <td>${v.count}</td>
          <td><span style="color:${color}">${pct.toFixed(0)}%</span></td>
          <td>${v.status}</td>
          <td>${distTxt}</td>
          <td>${etaTxt}</td>
          <td><small>${v.ts}</small></td>
        </tr>`;
      }
      html += '</table>';
      occEl.innerHTML = html;
    }catch(e){
      occEl.innerHTML = '<i>Error cargando ocupación</i>';
    }
  }
  setInterval(refreshOcc, 5000); refreshOcc();

  // ---- Fallback Red (no oficial) ----
  fetchStopBtn.onclick = async () => {
    const s = (stopIdEl.value || '').trim();
    if(!s){ alert('Ingresa stop_id, ej. PA433'); return; }
    try{
      const r = await fetch('/red/arrivals/' + encodeURIComponent(s));
      const j = await r.json();
      stopDataEl.innerHTML = '<pre>'+JSON.stringify(j.data || j, null, 2)+'</pre>';
    }catch(e){ stopDataEl.innerHTML = '<i>Error consultando paradero</i>'; }
  };

})();
</script>
</body>
</html>
"""

# ========= Utilidades de ruta =========
def _route_generate_osrm(src_lat: float, src_lon: float, dst_lat: float, dst_lon: float) -> List[Tuple[float,float]]:
    url = f"https://router.project-osrm.org/route/v1/driving/{src_lon},{src_lat};{dst_lon},{dst_lat}?overview=full&geometries=geojson"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    coords = data["routes"][0]["geometry"]["coordinates"]  # [lon,lat]
    return [(lat, lon) for lon, lat in coords]

def _route_generate_ors(src_lat: float, src_lon: float, dst_lat: float, dst_lon: float) -> List[Tuple[float,float]]:
    url = "https://api.openrouteservice.org/v2/directions/driving-car"
    params = {"api_key": ORS_API_KEY, "start": f"{src_lon},{src_lat}", "end": f"{dst_lon},{dst_lat}"}
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    coords = data["features"][0]["geometry"]["coordinates"]  # [lon,lat]
    return [(lat, lon) for lon, lat in coords]

# ========= Endpoints básicos =========
@app.route("/")
def index():
    return render_template_string(INDEX_HTML)

@app.route("/get_destination")
def get_destination():
    return jsonify({"destino": DESTINO})

@app.route("/set_destination", methods=["POST"])
def set_destination():
    global DESTINO
    data = request.get_json(force=True)
    DESTINO = (float(data["lat"]), float(data["lon"]))
    return jsonify({"message": "destino actualizado", "destino": DESTINO})

# ========= Ocupación (desde detector) =========
@app.route("/occupancy", methods=["POST"])
def occupancy():
    data = request.get_json(force=True, silent=True) or {}
    bus_id = data.get("bus_id")
    count  = data.get("count")
    status = data.get("status")
    ts     = data.get("ts")
    cap    = int(data.get("capacity", 40))
    if not bus_id or count is None or not status or not ts:
        return jsonify({"ok": False, "error": "payload incompleto"}), 400

    pct = min(100.0, (int(count)/cap)*100.0)
    OCUPACION[str(bus_id)] = {
        "count": int(count), "status": str(status),
        "ts": str(ts), "capacity": cap, "pct": pct
    }

    con = sqlite3.connect(DB); cur = con.cursor()
    cur.execute("INSERT INTO ocupacion(bus_id, ts, count, status, capacity, pct) VALUES(?,?,?,?,?,?)",
                (bus_id, ts, int(count), status, cap, pct))
    con.commit(); con.close()
    return jsonify({"ok": True})

@app.route("/occupancy/list", methods=["GET"])
def occupancy_list():
    return jsonify(OCUPACION)

# ========= Simulador de micros =========
def _advance_along_route(bus: Dict[str, Any], step_km: float):
    """Avanza siguiendo la 'route' del bus (lista de [lat,lon])."""
    route: List[Tuple[float,float]] = bus.get("route") or []
    if not route or len(route) < 2:
        return False  # no hay ruta

    idx = int(bus.get("idx", 0))
    lat, lon = bus["lat"], bus["lon"]

    # Si recién parte, colócalo en el primer punto si está lejos
    if idx == 0 and geodesic((lat,lon), route[0]).km > 0.01 and not bus.get("placed"):
        lat, lon = route[0]; bus["placed"] = True

    while step_km > 0 and idx < len(route)-1:
        nlat, nlon = route[idx+1]
        dist_km = geodesic((lat,lon),(nlat,nlon)).km
        if dist_km < 1e-6:
            idx += 1
            continue
        if step_km >= dist_km:
            lat, lon = nlat, nlon
            step_km -= dist_km
            idx += 1
        else:
            frac = step_km / dist_km
            lat  = lat + (nlat - lat) * frac
            lon  = lon + (nlon - lon) * frac
            step_km = 0

    bus["lat"], bus["lon"], bus["idx"] = lat, lon, idx
    if idx >= len(route)-1:
        bus["arrived"] = True
    return True

def _advance_straight(bus: Dict[str, Any], destino: tuple, step_km: float):
    """Movimiento recto hacia el destino (fallback)."""
    lat, lon = bus["lat"], bus["lon"]
    lat2, lon2 = destino
    km_per_deg_lat = 110.574
    km_per_deg_lon = 111.320 * math.cos(math.radians(lat if lat else (lat2 or 0)))

    dlat = lat2 - lat
    dlon = lon2 - lon
    vx_km = dlon * km_per_deg_lon
    vy_km = dlat * km_per_deg_lat
    dist_km = math.hypot(vx_km, vy_km)
    if dist_km < 0.02:
        bus["lat"], bus["lon"] = lat2, lon2
        bus["arrived"] = True
        return

    ux, uy = vx_km / dist_km, vy_km / dist_km
    move_km = min(step_km, dist_km)
    lon += (move_km * ux) / km_per_deg_lon
    lat += (move_km * uy) / km_per_deg_lat
    bus["lat"], bus["lon"] = lat, lon

def _advance_bus(bus: Dict[str, Any], destino: tuple):
    """Avanza la micro según dt y speed_kmh; usa ruta si existe."""
    now = time.time()
    dt = now - bus.get("t", now)
    bus["t"] = now
    if dt <= 0:
        return
    speed = float(bus.get("speed_kmh", 25.0))
    if speed <= 0:
        return
    step_km = speed * dt / 3600.0
    used_route = _advance_along_route(bus, step_km)
    if not used_route:
        _advance_straight(bus, destino, step_km)

def _generate_route(src_lat: float, src_lon: float, dst_lat: float, dst_lon: float) -> List[Tuple[float,float]]:
    """Genera ruta con ORS (si hay ORS_API_KEY) o OSRM público."""
    if ORS_API_KEY:
        try:
            return _route_generate_ors(src_lat, src_lon, dst_lat, dst_lon)
        except Exception:
            pass
    # Fallback OSRM
    return _route_generate_osrm(src_lat, src_lon, dst_lat, dst_lon)

@app.route("/sim/start", methods=["POST"])
def sim_start():
    data = request.get_json(force=True)
    bus_id = str(data.get("bus_id", "bus001"))
    lat    = float(data["lat"]); lon = float(data["lon"])
    speed  = float(data.get("speed_kmh", 25.0))

    # Crea/actualiza bus
    BUSES[bus_id] = {"lat": lat, "lon": lon, "speed_kmh": speed, "t": time.time(), "arrived": False, "route": None, "idx": 0}

    # Genera ruta automáticamente (si falla, va en recta)
    points: List[Tuple[float,float]] = []
    try:
        points = _generate_route(lat, lon, DESTINO[0], DESTINO[1])
        if points and len(points) >= 2:
            BUSES[bus_id]["route"]  = points
            BUSES[bus_id]["idx"]    = 0
            BUSES[bus_id]["placed"] = False
    except Exception as e:
        # Sin ruta -> seguirá en recta como fallback
        print("WARN: no se pudo generar ruta:", e)

    return jsonify({"ok": True, "bus_id": bus_id, "points": points})

@app.route("/sim/stop", methods=["POST"])
def sim_stop():
    data = request.get_json(force=True, silent=True) or {}
    bus_id = str(data.get("bus_id", ""))
    if bus_id in BUSES:
        del BUSES[bus_id]
    return jsonify({"ok": True})

@app.route("/sim/buses", methods=["GET"])
def sim_buses():
    out = []
    for bus_id, bus in list(BUSES.items()):
        _advance_bus(bus, DESTINO)
        dist_km = geodesic((bus["lat"], bus["lon"]), DESTINO).km
        speed   = max(float(bus.get("speed_kmh", 25.0)), 1e-6)
        eta_min = (dist_km / speed) * 60.0
        out.append({
            "bus_id": bus_id,
            "lat": bus["lat"], "lon": bus["lon"],
            "speed_kmh": bus.get("speed_kmh", 25.0),
            "distance_km": dist_km, "eta_min": eta_min,
            "arrived": bool(bus.get("arrived", False)),
            "has_route": bool(bus.get("route"))
        })
    return jsonify({"ok": True, "destino": DESTINO, "buses": out})

# ========= Red (no oficial) =========
@app.route("/red/arrivals/<stop_id>", methods=["GET"])
def red_arrivals(stop_id: str):
    try:
        url = f"https://api.xor.cl/red/bus-stop/{stop_id}"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return jsonify({"ok": True, "data": r.json()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ========= Main =========
if __name__ == "__main__":
    print("Servidor iniciado.")
    print("Abre en:  http://127.0.0.1:5000  (o http://<IP_LAN>:5000)")
    app.run(host="0.0.0.0", port=5000, debug=True)

# tracker_server.py
import os, time, math, sqlite3, requests
from typing import Dict, Any, List, Tuple, Optional
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS
from geopy.distance import geodesic

# (opcional) gtfs-realtime
_HAS_GTFS = True
try:
    from google.transit import gtfs_realtime_pb2  # type: ignore
except Exception:
    _HAS_GTFS = False

app = Flask(__name__)
CORS(app)

# ==================== Config / Estado ====================
DESTINO = (-33.0066285122585, -71.5451341716933)              # Paradero destino (editable desde la UI)
OCUPACION: Dict[str, Dict[str, Any]] = {}   # Ocupación por bus
BUSES: Dict[str, Dict[str, Any]] = {}       # Estado de buses simulados

# Ruta: ORS si hay API key; si no, OSRM público
ORS_API_KEY = os.getenv("ORS_API_KEY", "").strip()

# Paradas reales (OSM)
STOP_MATCH_DIST_M = 60.0          # distancia máx (m) de un paradero a la ruta
AUTOSTOPS_DWELL_SEC = 5           # dwell (s) por parada
STOP_RADIUS_KM = 0.02             # 20 m para considerar “llegada” a la parada

DB = "ocupacion.sqlite"
def init_db():
    con = sqlite3.connect(DB); cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS ocupacion(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bus_id TEXT, ts TEXT, count INTEGER, status TEXT, capacity INTEGER, pct REAL
    )""")
    con.commit(); con.close()
init_db()

# ==================== UI ====================
INDEX_HTML = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<title>Simulador buses → paradero</title>
<meta name="viewport" content="width=device-width,initial-scale=1" />
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  body{font-family:Arial, sans-serif; padding:12px; max-width:1100px; margin:auto}
  h1{font-size:1.25rem;margin:6px 0}
  .card{border:1px solid #ddd;border-radius:8px;padding:10px;margin:8px 0}
  .row{display:flex;gap:8px}.row>*{flex:1}
  input,button{padding:8px}
  #map{height:420px;border-radius:8px}
  table{width:100%;border-collapse:collapse}th,td{padding:6px;border-bottom:1px solid #eee;text-align:center}
  .pill{display:inline-block;padding:2px 6px;border-radius:999px;background:#eee;font-size:12px;margin-left:4px}
</style>
</head>
<body>
<h1>Simulador buses → paradero</h1>
<div id="status">Estado: listo</div>

<div class="card">
  <h3>Destino (paradero)</h3>
  <div class="row">
    <div><label>Lat</label><input id="destLat"></div>
    <div><label>Lon</label><input id="destLon"></div>
  </div>
  <button id="setDestBtn">Establecer destino</button>
</div>

<div class="card">
  <h3>Crear bus (ruta + paraderos reales OSM)</h3>
  <div class="row">
    <div><label>Bus ID</label><input id="busId" placeholder="bus001"></div>
    <div><label>Velocidad (km/h)</label><input id="speed" value="25"></div>
  </div>
  <div class="row">
    <div><label>Origen lat</label><input id="srcLat" placeholder="-33.02"></div>
    <div><label>Origen lon</label><input id="srcLon" placeholder="-71.54"></div>
  </div>
  <div class="row">
    <button id="startSimBtn">Iniciar simulación</button>
    <button id="stopSimBtn">Detener</button>
  </div>
  <small>Las paradas se obtienen de OpenStreetMap a lo largo de la ruta.</small>
</div>

<div class="card"><h3>Mapa</h3><div id="map"></div></div>

<div class="card">
  <h3>Próximas llegadas</h3>
  <div id="arrivals"></div>
</div>

<!--
<div class="card">
  <h3>RED (no oficial) — Próximos buses por paradero</h3>
  <div class="row">
    <input id="stopId" placeholder="PA433">
    <button id="fetchStopBtn">Consultar</button>
  </div>
  <div id="stopData"></div>
</div>
-->


<script>
(function(){
  const statusEl=document.getElementById('status');
  const destLat=document.getElementById('destLat'); const destLon=document.getElementById('destLon');
  const setDestBtn=document.getElementById('setDestBtn');
  const busIdEl=document.getElementById('busId'); const speedEl=document.getElementById('speed');
  const srcLatEl=document.getElementById('srcLat'); const srcLonEl=document.getElementById('srcLon');
  const startSimBtn=document.getElementById('startSimBtn'); const stopSimBtn=document.getElementById('stopSimBtn');
  const arrivalsEl=document.getElementById('arrivals');
  const stopIdEl=document.getElementById('stopId'); const fetchStopBtn=document.getElementById('fetchStopBtn'); const stopDataEl=document.getElementById('stopData');

  let map=L.map('map'); 
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19}).addTo(map);

  let destMarker=L.marker([0,0],{title:'Paradero destino'}).addTo(map);
  let busMarkers={}, polylines={}, stopMarkers={};

  fetch('/get_destination').then(r=>r.json()).then(j=>{
    const [la,lo]=j.destino; 
    destLat.value=la; 
    destLon.value=lo; 
    destMarker.setLatLng([la,lo]); 
    map.setView([la,lo],13);
  });

  setDestBtn.onclick=()=>{
    const la=parseFloat(destLat.value), lo=parseFloat(destLon.value);
    if(isNaN(la)||isNaN(lo)){alert('Destino inválido');return;}
    fetch('/set_destination',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({lat:la,lon:lo})
    }).then(()=>{
      destMarker.setLatLng([la,lo]); 
      map.setView([la,lo],13);
    });
  };

  startSimBtn.onclick=async ()=>{
    const id=(busIdEl.value||'bus001').trim(); 
    const sp=parseFloat(speedEl.value||'25');
    const la=parseFloat(srcLatEl.value), lo=parseFloat(srcLonEl.value);
    if(isNaN(la)||isNaN(lo)){alert('Origen inválido');return;}

    const res=await fetch('/sim/start',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({bus_id:id, lat:la, lon:lo, speed_kmh:sp})
    });

    const j=await res.json(); 
    if(!j.ok){alert('No se pudo iniciar');return;}

    if(j.points?.length>=2){ 
      if(polylines[id]) map.removeLayer(polylines[id]);
      polylines[id]=L.polyline(j.points,{weight:4,opacity:0.75}).addTo(map);
      map.fitBounds(polylines[id].getBounds().pad(0.3));
    }

    if(stopMarkers[id]){
      for(const m of stopMarkers[id]) map.removeLayer(m);
    }

    stopMarkers[id]=[];
    (j.auto_stops||[]).forEach(s=>{
      const mk=L.circleMarker([s[0],s[1]],{radius:5,opacity:0.9}).addTo(map);
      mk.bindTooltip(s[2] ? `🚌 ${s[2]}` : 'Paradero');
      stopMarkers[id].push(mk);
    });
  };

  stopSimBtn.onclick=async ()=>{
    const id=(busIdEl.value||'bus001').trim();
    await fetch('/sim/stop',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({bus_id:id})
    });

    if(busMarkers[id]){map.removeLayer(busMarkers[id]); delete busMarkers[id];}
    if(polylines[id]){map.removeLayer(polylines[id]); delete polylines[id];}
    if(stopMarkers[id]){
      for(const m of stopMarkers[id]) map.removeLayer(m);
      delete stopMarkers[id];
    }
  };

  async function refreshBuses(){
    try{
      const j=await (await fetch('/sim/buses')).json(); 
      if(!j.ok) return;

      const [dla,dlo]=j.destino; 
      destMarker.setLatLng([dla,dlo]);

      const list=[...j.buses].sort((a,b)=>a.eta_min-b.eta_min);

      let html=`<table>
        <tr>
          <th>Bus</th>
          <th>Dist</th>
          <th>ETA</th>
          <th>Paradas</th>
          <!-- <th>Ocupación</th> -->
          <!-- <th>%</th> -->
          <th>Status</th>
          <th>Estado</th>
        </tr>`;

      if(list.length===0){
        html+=`<tr><td colspan="8"><i>Sin buses</i></td></tr>`;
        statusEl.textContent='Estado: sin buses';
      } else {
        statusEl.textContent=`Próxima llegada: ${list[0].bus_id} en ${list[0].eta_min.toFixed(1)} min`;

        for(const b of list){
          const id=b.bus_id, la=b.lat, lo=b.lon;

          if(!busMarkers[id]) 
            busMarkers[id]=L.marker([la,lo],{title:id}).addTo(map); 
          else 
            busMarkers[id].setLatLng([la,lo]);

          let tip=`${id}<br>Dist: ${b.distance_km.toFixed(2)} km<br>ETA: ${b.eta_min.toFixed(1)} min`;
          if(b.is_dwell) tip+=`<br><span class="pill">Detenido</span>`;
          busMarkers[id].bindTooltip(tip);

          const si=b.stops_total>0?`${b.stops_next_idx}/${b.stops_total}`:'—';
          const state=b.arrived?'En paradero':(b.is_dwell?'En parada':(b.has_route?'En ruta':'Recta'));

          const occCount = b.occ_count ?? '—';
          const occPct = b.occ_pct != null ? `${b.occ_pct}%` : '—';
          const occStatus = b.occ_status ?? '—';

          html+=`
          <tr>
            <td><b>${id}</b></td>
            <td>${b.distance_km.toFixed(2)} km</td>
            <td>${b.eta_min.toFixed(1)} min</td>
            <td>${si}</td>
            <!-- <td>${occCount}</td> -->
            <!-- <td>${occPct}</td> -->
            <td>${occStatus}</td>
            <td>${state}</td>
          </tr>`;
        }
      }

      html+='</table>'; 
      arrivalsEl.innerHTML=html;

    } catch(e){}
  }

  setInterval(refreshBuses, 1000); 
  refreshBuses();

  fetchStopBtn.onclick=async()=>{
    const s=(stopIdEl.value||'').trim(); 
    if(!s){alert('Ingresa stop_id');return;}

    try{ 
      const j=await (await fetch('/red/arrivals/'+encodeURIComponent(s))).json(); 
      stopDataEl.innerHTML='<pre>'+JSON.stringify(j.data||j,null,2)+'</pre>'; 
    } catch(_){ 
      stopDataEl.textContent='Error'; 
    }
  };
})();
</script>
</body>
</html>
"""


# ==================== Rutas (ORS/OSRM) ====================
def _route_generate_osrm(src_lat: float, src_lon: float, dst_lat: float, dst_lon: float) -> List[Tuple[float,float]]:
    url = f"https://router.project-osrm.org/route/v1/driving/{src_lon},{src_lat};{dst_lon},{dst_lat}?overview=full&geometries=geojson"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    coords = r.json()["routes"][0]["geometry"]["coordinates"]  # [lon,lat]
    return [(lat, lon) for lon, lat in coords]

def _route_generate_ors(src_lat: float, src_lon: float, dst_lat: float, dst_lon: float) -> List[Tuple[float,float]]:
    url = "https://api.openrouteservice.org/v2/directions/driving-car"
    params = {"api_key": ORS_API_KEY, "start": f"{src_lon},{src_lat}", "end": f"{dst_lon},{dst_lat}"}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    coords = r.json()["features"][0]["geometry"]["coordinates"]  # [lon,lat]
    return [(lat, lon) for lon, lat in coords]

def _generate_route(src_lat: float, src_lon: float, dst_lat: float, dst_lon: float) -> List[Tuple[float,float]]:
    if ORS_API_KEY:
        try:
            return _route_generate_ors(src_lat, src_lon, dst_lat, dst_lon)
        except Exception:
            pass
    return _route_generate_osrm(src_lat, src_lon, dst_lat, dst_lon)

# ==================== Paraderos OSM a lo largo de la ruta ====================
def _bbox_for_route(route: List[Tuple[float,float]], margin_deg: float = 0.01) -> Tuple[float,float,float,float]:
    lats=[p[0] for p in route]; lons=[p[1] for p in route]
    return (min(lats)-margin_deg, min(lons)-margin_deg, max(lats)+margin_deg, max(lons)+margin_deg)  # S, W, N, E

def _overpass_fetch_bus_stops(south: float, west: float, north: float, east: float) -> List[Dict[str,Any]]:
    q = f"""
    [out:json][timeout:25];
    (
      node["highway"="bus_stop"]({south},{west},{north},{east});
      node["public_transport"="platform"]["bus"~".*"]({south},{west},{north},{east});
    );
    out body;
    """
    r = requests.post("https://overpass-api.de/api/interpreter", data={"data": q}, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("elements", [])

def _meters_per_deg(lat: float) -> Tuple[float,float]:
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 40075000.0 * math.cos(math.radians(lat)) / 360.0
    return m_per_deg_lat, m_per_deg_lon

def _project_dist_along(route: List[Tuple[float,float]], pt: Tuple[float,float]) -> Tuple[float,float]:
    """(dist_min_m, distancia_recorrida_km_al_pie) del punto respecto a la polilínea."""
    px_lat, px_lon = pt
    min_d = 1e18
    acc_km = 0.0
    best_along_km = 0.0
    for i in range(len(route)-1):
        a = route[i]; b = route[i+1]
        lat_ref = (a[0]+b[0])/2.0
        mlat, mlon = _meters_per_deg(lat_ref)
        ax, ay = (a[1]*mlon, a[0]*mlat)
        bx, by = (b[1]*mlon, b[0]*mlat)
        px, py = (px_lon*mlon, px_lat*mlat)

        vx, vy = (bx-ax, by-ay); wx, wy = (px-ax, py-ay)
        seg_len2 = vx*vx + vy*vy
        t = 0.0 if seg_len2==0 else max(0.0, min(1.0, (wx*vx + wy*vy)/seg_len2))
        projx, projy = (ax + t*vx, ay + t*vy)
        dist_m = math.hypot(px-projx, py-projy)
        if dist_m < min_d:
            min_d = dist_m
            seg_km = geodesic(a, b).km
            best_along_km = acc_km + seg_km * t
        acc_km += geodesic(a, b).km
    return min_d, best_along_km

def _polyline_total_km(route: List[Tuple[float,float]]) -> float:
    tot = 0.0
    for i in range(len(route)-1):
        tot += geodesic(route[i], route[i+1]).km
    return tot

def _osm_stops_along_route(route: List[Tuple[float,float]]) -> List[Tuple[float,float,str]]:
    """Paraderos reales (lat, lon, name) ordenados según sentido de la ruta."""
    if not route or len(route)<2:
        return []
    s,w,n,e = _bbox_for_route(route, margin_deg=0.01)
    try:
        elems = _overpass_fetch_bus_stops(s,w,n,e)
    except Exception as e:
        print("WARN Overpass:", e)
        return []

    total_km = _polyline_total_km(route)
    items = []
    for el in elems:
        lat = float(el.get("lat")); lon = float(el.get("lon"))
        name = (el.get("tags") or {}).get("name","Paradero")
        d_m, along_km = _project_dist_along(route, (lat,lon))
        if d_m <= STOP_MATCH_DIST_M and 0.0 <= along_km <= total_km:
            items.append((d_m, along_km, lat, lon, name))

    # Orden por distancia a lo largo
    items.sort(key=lambda x: x[1])

    # Deduplicación de paraderos muy cercanos
    dedup = []
    MIN_GAP_M = 80.0
    for it in items:
        if dedup and (it[1]-dedup[-1][1])*1000.0 < MIN_GAP_M:
            if it[0] < dedup[-1][0]:
                dedup[-1] = it
        else:
            dedup.append(it)

    return [(lat, lon, name) for (_, _, lat, lon, name) in dedup]

# ==================== Distancias / movimiento ====================
def _remaining_route_km(bus: Dict[str, Any]) -> Optional[float]:
    route = bus.get("route") or []
    if not route or len(route)<2:
        return None
    idx = int(bus.get("idx",0))
    lat, lon = bus["lat"], bus["lon"]
    rem = 0.0
    if idx < len(route)-1:
        rem += geodesic((lat,lon), route[idx+1]).km
        for i in range(idx+1, len(route)-1):
            rem += geodesic(route[i], route[i+1]).km
    return rem

def _advance_along_route(bus: Dict[str, Any], step_km: float):
    route = bus.get("route") or []
    if not route or len(route)<2:
        return False
    idx = int(bus.get("idx",0))
    lat, lon = bus["lat"], bus["lon"]
    if idx==0 and geodesic((lat,lon), route[0]).km>0.01 and not bus.get("placed"):
        lat, lon = route[0]
        bus["placed"]=True
    while step_km>0 and idx < len(route)-1:
        nlat,nlon = route[idx+1]
        dist_km = geodesic((lat,lon),(nlat,nlon)).km
        if dist_km < 1e-6:
            idx+=1
            continue
        if step_km >= dist_km:
            lat,lon = nlat,nlon
            step_km -= dist_km
            idx+=1
        else:
            frac = step_km/dist_km
            lat = lat+(nlat-lat)*frac
            lon = lon+(nlon-lon)*frac
            step_km=0
    bus["lat"], bus["lon"], bus["idx"] = lat, lon, idx
    if idx >= len(route)-1:
        bus["arrived"]=True
    return True

def _advance_straight(bus: Dict[str, Any], destino: tuple, step_km: float):
    lat,lon = bus["lat"], bus["lon"]
    lat2,lon2 = destino
    mlat, mlon = _meters_per_deg(lat if lat else lat2)
    dlat,dlon = (lat2-lat, lon2-lon)
    vx,vy = (dlon*mlon, dlat*mlat)
    dist_km = math.hypot(vx,vy)/1000.0
    if dist_km < 0.02:
        bus["lat"],bus["lon"]=lat2,lon2
        bus["arrived"]=True
        return
    ux,uy = (vx/(dist_km*1000), vy/(dist_km*1000))
    move_km = min(step_km, dist_km)
    lon += (move_km*1000*ux)/mlon
    lat += (move_km*1000*uy)/mlat
    bus["lat"],bus["lon"]=lat,lon

def _check_stop_and_dwell(bus: Dict[str, Any], now: float):
    """Si el bus llegó a la próxima parada, se detiene (dwell) y reinicia el reloj."""
    stops = bus.get("stops") or []
    next_idx = int(bus.get("next_stop_idx", 0))
    dwell_sec = int(bus.get("dwell_sec", AUTOSTOPS_DWELL_SEC))
    if not stops or next_idx >= len(stops):
        return

    tgt = stops[next_idx]  # (lat, lon)
    # ¿está dentro del radio de llegada?
    if geodesic((bus["lat"], bus["lon"]), (tgt[0], tgt[1])).km <= STOP_RADIUS_KM and not bus.get("is_dwell", False):
        # anclar posición exactamente en la parada
        bus["lat"], bus["lon"] = tgt[0], tgt[1]
        # activar dwell y avanzar el índice de la siguiente parada
        bus["is_dwell"] = True
        bus["dwell_until"] = now + max(0, dwell_sec)
        bus["next_stop_idx"] = next_idx + 1
        # MUY IMPORTANTE: reiniciar el reloj para que no se acumule tiempo de movimiento
        bus["t"] = now

def _advance_bus(bus: Dict[str, Any], destino: tuple):
    """Avanza el bus por su ruta o en línea recta, respetando dwell en paradas."""
    now = time.time()

    # Si está detenido por dwell, mantener el reloj actualizado y no moverlo
    if bus.get("is_dwell", False):
        bus["t"] = now  # evitar acumulación de dt mientras está detenido
        if now < float(bus.get("dwell_until", 0)):
            return
        # terminó el dwell: limpiar flags y esperar al siguiente ciclo para mover
        bus["is_dwell"] = False
        bus["dwell_until"] = None
        bus["t"] = now
        return

    # Movimiento normal
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

    # Chequear si toca detenerse en la próxima parada
    _check_stop_and_dwell(bus, now)

# ==================== Endpoints básicos ====================
@app.route("/")
def index():
    return render_template_string(INDEX_HTML)

@app.route("/get_destination")
def get_destination():
    return jsonify({"destino": DESTINO})

@app.route("/set_destination", methods=["POST"])
def set_destination():
    global DESTINO
    d = request.get_json(force=True)
    DESTINO = (float(d["lat"]), float(d["lon"]))
    return jsonify({"message":"ok","destino":DESTINO})

# ==================== Ocupación ====================
@app.route("/occupancy", methods=["POST"])
@app.route("/occupancy/update", methods=["POST"])
def occupancy_update():
    data = request.get_json(force=True)

    bus_id = data.get("bus_id")
    count = data.get("count")
    status = data.get("status", "unknown")    # <--- NUEVO
    capacity = data.get("capacity", 40)

    if not bus_id:
        return jsonify({"ok": False, "error": "bus_id missing"}), 400
    if count is None:
        return jsonify({"ok": False, "error": "count missing"}), 400

    ts = time.strftime("%Y-%m-%d %H:%M:%S")

    # --- Guardar en memoria ---
    OCUPACION[bus_id] = {
        "count": count,
        "status": status,   # <--- NUEVO
        "capacity": capacity,
        "ts": ts
    }

    # --- Guardar en SQLite ---
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute(
        "INSERT INTO ocupacion (bus_id, ts, count, status, capacity, pct) VALUES (?,?,?,?,?,?)",
        (bus_id, ts, count, status, capacity, count / capacity if capacity else None)
    )
    con.commit()
    con.close()

    return jsonify({"ok": True})


@app.route("/occupancy/list")
def occupancy_list():
    return jsonify(OCUPACION)

# ==================== Simulador ====================
@app.route("/sim/start", methods=["POST"])
def sim_start():
    d=request.get_json(force=True)
    bus_id=str(d.get("bus_id","bus001"))
    lat=float(d["lat"])
    lon=float(d["lon"])
    speed=float(d.get("speed_kmh",25.0))

    BUSES[bus_id]={"lat":lat,"lon":lon,"speed_kmh":speed,"t":time.time(),
                   "arrived":False,"route":None,"idx":0,
                   "stops":[], "stop_names":[], "next_stop_idx":0,
                   "dwell_sec":AUTOSTOPS_DWELL_SEC,"is_dwell":False,"dwell_until":None}

    # 1) Ruta
    points: List[Tuple[float,float]] = []
    try:
        points = _generate_route(lat,lon, DESTINO[0],DESTINO[1])
        if points and len(points)>=2:
            BUSES[bus_id]["route"]=points
            BUSES[bus_id]["idx"]=0
            BUSES[bus_id]["placed"]=False
    except Exception as e:
        print("WARN ruta:", e)

    # 2) Paraderos reales OSM sobre la ruta
    auto_stops: List[Tuple[float,float,str]] = []
    if points and len(points)>=2:
        try:
            auto_stops = _osm_stops_along_route(points)
        except Exception as e:
            print("WARN paraderos OSM:", e)

    if auto_stops:
        BUSES[bus_id]["stops"] = [(a[0],a[1]) for a in auto_stops]
        BUSES[bus_id]["stop_names"] = [a[2] for a in auto_stops]
        BUSES[bus_id]["next_stop_idx"] = 0

    return jsonify({"ok":True,"bus_id":bus_id,"points":points,"auto_stops":auto_stops,"dwell_sec":AUTOSTOPS_DWELL_SEC})

@app.route("/sim/stop", methods=["POST"])
def sim_stop():
    d=request.get_json(force=True, silent=True) or {}
    bus_id=str(d.get("bus_id",""))
    if bus_id in BUSES:
        del BUSES[bus_id]
    return jsonify({"ok":True})

@app.route("/sim/buses")
def sim_buses():
    out = []
    now = time.time()

    for bus_id, bus in list(BUSES.items()):
        _advance_bus(bus, DESTINO)

        dist_route = _remaining_route_km(bus)
        if dist_route is None:
            dist_km = geodesic((bus["lat"], bus["lon"]), DESTINO).km
            distance_kind = "straight"
        else:
            dist_km = max(0.0, dist_route)
            distance_kind = "route"

        speed = max(float(bus.get("speed_kmh", 25.0)), 1e-6)
        eta_min = (dist_km / speed) * 60.0

        dwell_remaining = 0.0
        if bus.get("is_dwell", False) and bus.get("dwell_until"):
            dwell_remaining = max(0.0, float(bus["dwell_until"]) - now)

        total = len(bus.get("stops") or [])
        nxt = int(bus.get("next_stop_idx", 0))
        remain = max(0, total - nxt)
        dwell_each = int(bus.get("dwell_sec", AUTOSTOPS_DWELL_SEC))

        eta_min += (dwell_remaining + remain * dwell_each) / 60.0

        # ---- OCUPACIÓN UNIDA AQUÍ ----
        occ = OCUPACION.get(bus_id, {})
        occ_count = occ.get("count")
        occ_status = occ.get("status")
        occ_capacity = occ.get("capacity", 40)

        occ_pct = None
        if occ_count is not None and occ_capacity:
            occ_pct = round((occ_count / occ_capacity) * 100)

        out.append({
            "bus_id": bus_id,
            "lat": bus["lat"],
            "lon": bus["lon"],
            "speed_kmh": bus.get("speed_kmh", 25.0),
            "distance_km": dist_km,
            "eta_min": eta_min,
            "arrived": bool(bus.get("arrived", False)),
            "has_route": bool(bus.get("route")),
            "distance_kind": distance_kind,
            "is_dwell": bus.get("is_dwell", False),
            "stops_total": total,
            "stops_next_idx": nxt,

            # 👇 CAMPOS OCUPACIÓN
            "occ_count": occ_count,
            "occ_capacity": occ_capacity if occ_count is not None else None,
            "occ_pct": occ_pct,
            "occ_status": occ_status
        })

    return jsonify({
        "ok": True,
        "destino": DESTINO,
        "buses": out
    })


# ==================== Fallback RED no oficial ====================
@app.route("/red/arrivals/<stop_id>")
def red_arrivals(stop_id:str):
    try:
        r=requests.get(f"https://api.xor.cl/red/bus-stop/{stop_id}",timeout=10)
        r.raise_for_status()
        return jsonify({"ok":True,"data":r.json()})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500

# ==================== Main ====================
if __name__=="__main__":
    print("Servidor iniciado. Abre http://127.0.0.1:5000  (o http://<IP_LAN>:5000)")
    app.run(host="0.0.0.0", port=5000, debug=True)

# tracker_server.py
from flask import Flask, request, jsonify, render_template_string
from geopy.distance import geodesic
from flask_cors import CORS
import time

app = Flask(__name__)
CORS(app)  # permite requests desde la página servida (útil en pruebas)

# destino por defecto (lat, lon) -- cámbialo al que quieras
DESTINO = (-33.4624, -70.6550)

# Guardar última info por client_id
clients = {}

INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Tracker móvil → servidor</title>
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <style>
    body { font-family: Arial, sans-serif; padding: 12px; max-width:700px; margin:auto; }
    h1 { font-size: 1.2rem; }
    #status { margin: 8px 0; color: #333; }
    #log { white-space: pre-wrap; background:#f7f7f7; padding:8px; border-radius:6px; height:200px; overflow:auto; }
    input, button { padding:8px; margin:4px 0; width:100%; box-sizing:border-box; }
    .row { display:flex; gap:8px; }
    .row > * { flex:1; }
  </style>
</head>
<body>
  <h1>Envía tu ubicación al servidor</h1>
  <div id="status">Estado: esperando</div>

  <label>Client ID (se genera por defecto):</label>
  <input id="clientId" />

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

  <p>
    <button id="startBtn">Iniciar envío de ubicación</button>
    <button id="stopBtn" disabled>Detener</button>
  </p>

  <div id="log"></div>

<script>
(function(){
  // Generar client id aleatorio si no lo pones
  function uuidv4(){ return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c){ var r = Math.random()*16|0, v = c=='x'?r:(r&0x3|0x8); return v.toString(16); }); }
  const clientInput = document.getElementById('clientId');
  if(!clientInput.value) clientInput.value = uuidv4();

  const logEl = document.getElementById('log');
  const statusEl = document.getElementById('status');
  const startBtn = document.getElementById('startBtn');
  const stopBtn = document.getElementById('stopBtn');
  const setDestBtn = document.getElementById('setDestBtn');
  const destLat = document.getElementById('destLat');
  const destLon = document.getElementById('destLon');

  // Tomar destino inicial desde el servidor (se obtiene al cargar)
  fetch('/get_destination').then(r=>r.json()).then(d=>{
    destLat.value = d.destino[0];
    destLon.value = d.destino[1];
  }).catch(()=>{ /* ignore */ });

  let watchId = null;
  let lastPos = null;

  function log(msg){
    logEl.textContent = (new Date().toLocaleTimeString()) + " — " + msg + "\\n" + logEl.textContent;
  }

  // Enviar destino al servidor
  setDestBtn.onclick = () => {
    const lat = parseFloat(destLat.value);
    const lon = parseFloat(destLon.value);
    if(isNaN(lat) || isNaN(lon)){ alert('Destino inválido'); return; }
    fetch('/set_destination', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({lat: lat, lon: lon})
    }).then(r=>r.json()).then(resp=>{
      log("Destino actualizado en servidor: " + JSON.stringify(resp.destino));
    }).catch(e=>log("Error set_destination: "+e));
  };

  // Enviar posición al servidor
  async function sendPosition(pos){
    const clientId = clientInput.value;
    const payload = {
      client_id: clientId,
      timestamp: Date.now(),
      lat: pos.coords.latitude,
      lon: pos.coords.longitude,
      accuracy: pos.coords.accuracy,
      speed: pos.coords.speed  // puede ser null
    };
    try{
      const res = await fetch('/update', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if(res.ok){
        statusEl.textContent = `Enviado — distancia ${data.distance_km.toFixed(3)} km — ETA ${data.eta_min.toFixed(1)} min`;
        log("Server: distancia=" + data.distance_km.toFixed(3) + " km, ETA=" + data.eta_min.toFixed(1) + " min");
      } else {
        log("Server error: " + JSON.stringify(data));
      }
    } catch(err){
      log("Fetch error: " + err);
    }
  }

  // Manejar watchPosition
  startBtn.onclick = () => {
    if(!("geolocation" in navigator)){ alert("Tu navegador no soporta geolocation"); return; }
    statusEl.textContent = "Pidiendo permiso de ubicación...";
    // opciones: highAccuracy puede consumir más batería pero da mejor posición
    watchId = navigator.geolocation.watchPosition(pos => {
      statusEl.textContent = "Ubicación obtenida. Enviando...";
      sendPosition(pos);
    }, err => {
      statusEl.textContent = "Error geolocation: " + err.message;
      log("Geolocation error: " + err.message);
    }, {enableHighAccuracy: true, maximumAge: 1000, timeout: 10000});
    startBtn.disabled = true;
    stopBtn.disabled = false;
    log("Iniciado watchPosition");
  };

  stopBtn.onclick = () => {
    if(watchId !== null){
      navigator.geolocation.clearWatch(watchId);
      watchId = null;
      statusEl.textContent = "Envío detenido";
      startBtn.disabled = false;
      stopBtn.disabled = true;
      log("Detenido watchPosition");
    }
  };

})();
</script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(INDEX_HTML)

@app.route('/get_destination')
def get_destination():
    return jsonify({"destino": DESTINO})

@app.route('/set_destination', methods=['POST'])
def set_destination():
    global DESTINO
    data = request.get_json(force=True)
    DESTINO = (float(data['lat']), float(data['lon']))
    return jsonify({"message":"destino actualizado", "destino": DESTINO})

@app.route('/update', methods=['POST'])
def update_location():
    """
    Recibe JSON: { client_id, timestamp, lat, lon, accuracy, speed }
    Calcula distancia y ETA y devuelve JSON con distance_km y eta_min
    """
    try:
        data = request.get_json(force=True)
        client = data.get('client_id', 'anon')
        lat = float(data['lat'])
        lon = float(data['lon'])
        timestamp = float(data.get('timestamp', time.time()*1000)) / 1000.0

        pos = (lat, lon)

        # calcular velocidad aproximada (si hay posición previa)
        prev = clients.get(client)
        speed_kmh = None
        if prev:
            prev_pos, prev_time = prev['pos'], prev['time']
            # distancia en km
            d = geodesic(prev_pos, pos).km
            dt = max(1e-3, timestamp - prev_time)  # segundos
            speed_m_s = d * 1000.0 / dt
            speed_kmh = speed_m_s * 3.6
        else:
            speed_kmh = None

        # guardar estado
        clients[client] = {'pos': pos, 'time': timestamp}

        # distancia al destino y ETA
        distance_km = geodesic(pos, DESTINO).km
        # si tenemos speed_kmh válido (no 0), usamos eso, si no asumimos 30 km/h
        speed_use = speed_kmh if (speed_kmh is not None and speed_kmh > 0.5) else 30.0
        eta_min = (distance_km / speed_use) * 60.0

        print(f"[{client}] pos={pos} distance={distance_km:.3f} km speed={speed_kmh if speed_kmh else 'N/A'} km/h eta={eta_min:.1f} min")
        return jsonify({"distance_km": distance_km, "eta_min": eta_min, "speed_kmh": speed_kmh})

    except Exception as e:
        print("Error procesando update:", e)
        return jsonify({"error": str(e)}), 400

if __name__ == '__main__':
    print("Servidor iniciado. Accede desde el móvil en http://<IP_PC>:5000")
    app.run(host='0.0.0.0', port=5000, debug=True)

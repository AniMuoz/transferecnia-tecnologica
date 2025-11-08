import requests
from datetime import datetime

# URL pública de tu ubicación en tiempo real (copiada desde Google Maps)
url_ubicacion = "https://maps.app.goo.gl/rY4qHiJtUxYJwhex9"

# Coordenadas del destino (por ejemplo, tu casa o paradero)
destino_lat, destino_lon = -33.4624, -70.6550

# Tu API Key de Google Directions (gratis, 1 minuto de configuración)
api_key = "TU_API_KEY"

def obtener_tiempo_estimado(api_key, origen, destino):
    url = (
        f"https://maps.googleapis.com/maps/api/directions/json?"
        f"origin={origen[0]},{origen[1]}&destination={destino[0]},{destino[1]}&key={api_key}"
    )
    respuesta = requests.get(url).json()
    if respuesta["status"] == "OK":
        tiempo = respuesta["routes"][0]["legs"][0]["duration"]["text"]
        distancia = respuesta["routes"][0]["legs"][0]["distance"]["text"]
        print(f"🚍 Distancia: {distancia} | ETA: {tiempo}")
    else:
        print("❌ Error obteniendo ruta:", respuesta["status"])

# 🔹 En este punto, reemplazarías la lectura del link con coordenadas reales
# (por simplicidad ahora puedes poner coordenadas fijas)
origen = (-33.4579, -70.6495)

obtener_tiempo_estimado(api_key, origen, (destino_lat, destino_lon))

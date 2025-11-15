# red_client.py
import os, requests, time
from typing import List, Dict, Any
from google.transit import gtfs_realtime_pb2

def _get(url_env: str) -> bytes:
    url = os.getenv(url_env)
    if not url:
        raise RuntimeError(f"Falta variable {url_env}")
    headers = {}
    if os.getenv("RED_API_KEY"):
        headers["Authorization"] = f"Bearer {os.getenv('RED_API_KEY')}"
    r = requests.get(url, headers=headers, timeout=10)
    r.raise_for_status()
    return r.content

def vehicle_positions() -> List[Dict[str, Any]]:
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(_get("RED_VEH_POS_URL"))
    out = []
    for e in feed.entity:
        if not e.HasField("vehicle"): 
            continue
        v = e.vehicle
        d = {
            "entity_id": e.id,
            "trip_id": v.trip.trip_id or None,
            "route_id": v.trip.route_id or None,
            "lat": getattr(v.position, "latitude", None),
            "lon": getattr(v.position, "longitude", None),
            "bearing": getattr(v.position, "bearing", None),
            "speed": getattr(v.position, "speed", None),
            "timestamp": int(getattr(v, "timestamp", 0)),
        }
        occ = getattr(v, "occupancy_status", None)  # si la agencia lo publica
        if occ is not None:
            d["occupancy_status"] = int(occ)  # 0..6 según GTFS-RT
        out.append(d)
    return out

def trip_updates() -> List[Dict[str, Any]]:
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(_get("RED_TRIP_UP_URL"))
    out = []
    for e in feed.entity:
        if not e.HasField("trip_update"):
            continue
        tu = e.trip_update
        d = {
            "trip_id": tu.trip.trip_id or None,
            "route_id": tu.trip.route_id or None,
            "stops": []
        }
        for stu in tu.stop_time_update:
            d["stops"].append({
                "stop_id": stu.stop_id,
                "arrival": int(getattr(stu.arrival, "time", 0)) if stu.HasField("arrival") else None,
                "departure": int(getattr(stu.departure, "time", 0)) if stu.HasField("departure") else None,
            })
        out.append(d)
    return out

# Fallback NO OFICIAL (mientras esperas acceso):
def arrivals_by_stop_xor(stop_code: str) -> Dict[str, Any]:
    url = f"https://api.xor.cl/red/bus-stop/{stop_code}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json()

import cv2
import os
import time
from ultralytics import YOLO
import requests

TRACKER_URL = "http://127.0.0.1:5000"  # donde corre tracker_server
BUS_ID = "TURBUS"  # ID de la micro que estás monitoreando

def estado_micro(x):
    if x <= 20:
        return "Asientos disponibles"
    if x <= 30:
        return "Pasillo disponible"
    if x > 30:
        return "Llena"

def enviar_ocupacion(bus_id, count):
    payload = {
        "bus_id": bus_id,
        "count": count,
        "status": estado_micro(count),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "capacity": 40
    }

    try:
        r = requests.post(f"{TRACKER_URL}/occupancy", json=payload)
        r.raise_for_status()
        print(f"✅ Estado enviado: {payload}")
    except Exception as e:
        print(f"❌ Error enviando ocupación: {e}")

def iniciar_deteccion(model_path='yolov8n.pt', intervalo=10, output_folder='frames_detectados'):
    """
    Detección de personas en tiempo real con YOLO, actualizando ocupación en tracker_server.
    """
    model = YOLO(model_path)
    os.makedirs(output_folder, exist_ok=True)

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("❌ No se pudo acceder a la cámara.")
        return

    last_time = time.time()
    frame_id = 0
    print("🎥 Detección iniciada... Presiona 'q' para salir.\n")

    while True:
        ret, frame = cap.read()
        if not ret:
            print("⚠️ No se pudo leer el frame de la cámara.")
            break

        current_time = time.time()
        if current_time - last_time >= intervalo:
            last_time = current_time

            results = model(frame)
            num_personas = (results[0].boxes.cls == 0).sum().item()
            print(f"[{time.strftime('%H:%M:%S')}] {num_personas} personas detectadas. {estado_micro(num_personas)}")

            # 🔹 Enviar automáticamente la ocupación al tracker_server
            enviar_ocupacion(BUS_ID, num_personas)

            annotated_frame = results[0].plot()
            save_path = os.path.join(output_folder, f"frame_{frame_id:04d}.jpg")
            cv2.imwrite(save_path, annotated_frame)
            frame_id += 1

        cv2.imshow("Detección de personas (YOLOv8)", frame)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()
    print("✅ Detección finalizada. Frames guardados en:", output_folder)

if __name__ == "__main__":
    iniciar_deteccion()

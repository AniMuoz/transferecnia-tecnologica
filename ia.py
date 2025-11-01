import cv2
import os
from ultralytics import YOLO

# Cargar modelo YOLO
model = YOLO('yolov8n.pt')  # o 'yolov8s.pt' / 'yolov9c.pt'

# Ruta del video
video_path = 'vegetita.jpg'

# Carpeta donde guardar los frames
output_folder = 'frames_detectados'
os.makedirs(output_folder, exist_ok=True)

# Abrir video
cap = cv2.VideoCapture(video_path)

fps = cap.get(cv2.CAP_PROP_FPS)         # Cuadros por segundo
frame_interval = int(fps * 10)          # Cada 10 segundos
frame_count = 0
frame_id = 0

while True:
    ret, frame = cap.read()
    if not ret:
        break

    # Procesar solo cada 10 segundos
    if frame_count % frame_interval == 0:
        # Analizar frame con YOLO
        results = model(frame)
        num_personas = (results[0].boxes.cls == 0).sum().item()

        # Mostrar conteo
        print(f"\nFrame {frame_id}: {num_personas} personas detectadas\n")

        # DIBUJAR detecciones en la imagen
        annotated_frame = results[0].plot()  # Devuelve el frame con las cajas dibujadas

        # GUARDAR el frame analizado
        save_path = os.path.join(output_folder, f"frame_{frame_id:04d}.jpg")
        cv2.imwrite(save_path, annotated_frame)
        print(f"Frame guardado en: {save_path}")

        frame_id += 1

    frame_count += 1

cap.release()
print("✅ Análisis completado. Frames guardados en:", output_folder)


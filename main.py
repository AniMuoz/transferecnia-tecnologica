import ia as ia


# Función que manejará los resultados
def procesar_deteccion(num_personas):
    print(f"📡 Procesando detección externa: {num_personas} personas")
    estado = ia.estado_micro(num_personas)
    print(estado)


# Iniciar la detección con callback activo
ia.iniciar_deteccion(model_path='yolov8n.pt', intervalo=10, callback=procesar_deteccion)



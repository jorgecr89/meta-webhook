# Usa una imagen oficial ligera de Python
FROM python:3.11-slim

# Establece variables de entorno para optimizar Python en contenedores
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

# Crea y establece el directorio de trabajo
WORKDIR /app

# Instala dependencias del sistema operativo (si fueran necesarias)
RUN apt-get update && apt-get install -y --no-install-recommends gcc && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Copia e instala las dependencias de Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia el código fuente
COPY . .

# Expone el puerto que usa FastAPI
EXPOSE 8000

# Comando para ejecutar la aplicación con Uvicorn en producción
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
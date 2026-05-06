FROM ubuntu:24.04

# Evitar interacciones en la instalación
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Instalar dependencias del sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    # --- Dependencias para PyMuPDF ---
    libmupdf-dev \
    mupdf-tools \
    # --- Dependencias críticas para OCRmyPDF ---
    ocrmypdf \
    tesseract-ocr \
    tesseract-ocr-spa \
    tesseract-ocr-eng \
    libtesseract-dev \
    ghostscript \
    unpaper \
    pngquant \
    # --- Dependencias para pdf2image (Poppler) ---
    poppler-utils \
    # --- Dependencias de compilación para paquetes de Python (lxml, pikepdf) ---
    libxml2-dev \
    libxslt1-dev \
    zlib1g-dev \
    libjpeg-dev \
    pkg-config \
    # --- Librerías para OpenCV / Pillow / Otros ---
    libgl1 \
    libglib2.0-0 \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Crear directorio de la app
WORKDIR /app

# Crear entorno virtual (Recomendado en Ubuntu 24.04 para evitar conflictos con el sistema)
RUN python3 -m venv /venv
ENV PATH="/venv/bin:$PATH"

# Copiar e instalar dependencias Python
# Asegúrate de que en requirements.txt estén: pytesseract, pillow y pdf2image
COPY app/requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copiar el código de la aplicación
COPY app/ .

# Crear directorios para uploads y outputs
RUN mkdir -p uploads outputs && chmod 777 uploads outputs

# Configurar variables de entorno para Tesseract (opcional, ayuda a localizar datos)
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata/

# Exponer el puerto
EXPOSE 5000

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5000/health || exit 1

# Ejecutar la aplicación
CMD ["python3", "app.py"]

# Documentación del Servicio: PDF Censor API

Este servicio es una aplicación web basada en **Flask** que permite cargar archivos PDF, visualizar sus páginas, buscar términos específicos y aplicar censuras permanentes (redacción de contenido) eliminando la información sensible tanto visual como internamente.

## 🚀 Características Principales
*   **Censura Real:** No solo añade cuadros negros; elimina el texto y los píxeles de imágenes subyacentes.
*   **Búsqueda Automática:** Localiza coordenadas de palabras clave para censura masiva.
*   **Limpieza de Metadatos:** Elimina autor, título y otros campos sensibles del PDF original.
*   **Gestión de Almacenamiento:** Sistema automático de limpieza de archivos temporales.
*   **Seguridad:** Validación de tipos de archivo y nombres seguros mediante `werkzeug`.

---

## 🛠️ Requisitos Técnicos
*   **Lenguaje:** Python 3.9+
*   **Librerías Core:** 
    *   `Flask`: Framework web.
    *   `PyMuPDF (fitz)`: Motor de manipulación de PDF de alto rendimiento.
    *   `Werkzeug`: Utilidades de seguridad para archivos.

---

## 📂 Estructura de Archivos
La aplicación espera la siguiente estructura mínima para funcionar:
```text
.
├── app.py              # Script principal (el código proporcionado)
├── uploads/            # Directorio temporal para archivos subidos
├── outputs/            # Directorio para PDFs procesados y previews
├── templates/          
│   └── index.html      # Interfaz de usuario (Frontend)
└── Dockerfile          # Configuración de contenedor (ver sección Docker)
```

---

## ⚙️ Configuración (Variables de Entorno)
Puedes ajustar el comportamiento del servicio sin tocar el código usando:
| Variable | Descripción | Valor por Defecto |
| :--- | :--- | :--- |
| `SECRET_KEY` | Clave para sesiones de Flask | `dev-secret-key` |
| `MAX_UPLOAD_MB` | Tamaño máximo de archivo permitido | `50` |

---

## 📡 Referencia de la API

### 1. Estado del Servicio
`GET /health`
*   **Uso:** Verificar si el contenedor está activo.
*   **Respuesta:** `{"status": "ok", "service": "pdf-censor"}`

### 2. Cargar Documento
`POST /api/upload`
*   **Cuerpo:** `multipart/form-data` con campo `file`.
*   **Retorno:** ID del archivo y metadatos básicos.
*   **Nota:** Activa la limpieza de archivos antiguos (> 1 hora).

### 3. Previsualización de Página
`GET /api/preview/<file_id>/<page_num>`
*   **Retorno:** Una imagen `PNG` de la página solicitada (zoom 150%).
*   **Seguridad:** La imagen se elimina del servidor inmediatamente después de ser enviada al navegador.

### 4. Búsqueda de Texto
`POST /api/search_text`
*   **JSON:** `{"file_id": "...", "text": "buscar"}`
*   **Retorno:** Lista de coordenadas `[x0, y0, x1, y1]` y números de página donde aparece el término.

### 5. Aplicar Censura (Procesado Final)
`POST /api/censor`
*   **JSON:**
    ```json
    {
      "file_id": "ID_RECIBIDO",
      "redactions": [{"page": 0, "rect": [x0, y0, x1, y1]}],
      "search_terms": ["palabra1"],
      "redact_metadata": true
    }
    ```
*   **Retorno:** El archivo PDF procesado como descarga adjunta.

---

## 🐳 Despliegue con Docker

Para montar este servicio en un contenedor Linux, utiliza el siguiente `Dockerfile` sugerido:
```dockerfile
# Usar imagen ligera de Python
FROM python:3.10-slim

# Instalar dependencias del sistema para PyMuPDF
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instalar dependencias de Python
COPY requirements.txt .
RUN pip install --no-cache-dir flask pymupdf werkzeug

# Crear directorios de trabajo
RUN mkdir uploads outputs templates

# Copiar el código
COPY . .

# Exponer el puerto
EXPOSE 5000

# Ejecutar con un servidor de producción (opcional, aquí directo)
CMD ["python", "app.py"]
```

---

## ⚠️ Notas de Seguridad
1.  **Persistencia:** Los archivos se almacenan localmente en el contenedor. Si el contenedor se reinicia, los archivos en `uploads/` se pierden (lo cual es deseable por privacidad).
2.  **Limpieza:** El script elimina archivos con más de **3600 segundos** de antigüedad cada vez que se sube un nuevo archivo.
3.  **Sanitización:** Se utiliza `secure_filename` para evitar ataques de salto de directorio (Path Traversal).
```

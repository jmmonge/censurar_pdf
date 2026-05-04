import os
import uuid
import json
import time
import shutil
from pathlib import Path
from flask import (
    Flask, request, jsonify, render_template,
    send_file, abort, after_this_request
)
from werkzeug.utils import secure_filename
import fitz  # PyMuPDF

# ─────────────────────────── Configuración ───────────────────────────
app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-key")
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", 50)) * 1024 * 1024
app.config["UPLOAD_FOLDER"] = Path("uploads")
app.config["OUTPUT_FOLDER"] = Path("outputs")
app.config["ALLOWED_EXTENSIONS"] = {"pdf"}

app.config["UPLOAD_FOLDER"].mkdir(exist_ok=True)
app.config["OUTPUT_FOLDER"].mkdir(exist_ok=True)

REDACTION_COLOR = (0, 0, 0)       # Negro por defecto
HIGHLIGHT_COLOR = (1, 1, 0, 0.3)  # Amarillo semitransparente para previsualización


# ─────────────────────────── Helpers ─────────────────────────────────
def allowed_file(filename: str) -> bool:
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in app.config["ALLOWED_EXTENSIONS"]
    )


def unique_path(folder: Path, suffix: str) -> Path:
    return folder / f"{uuid.uuid4().hex}{suffix}"


def clean_old_files(folder: Path, max_age_seconds: int = 3600):
    """Elimina archivos más antiguos que max_age_seconds."""
    now = time.time()
    for f in folder.iterdir():
        if f.is_file() and (now - f.stat().st_mtime) > max_age_seconds:
            f.unlink(missing_ok=True)


# ─────────────────────────── Rutas API ───────────────────────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "pdf-censor"})


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/upload", methods=["POST"])
def upload_pdf():
    """Sube un PDF y devuelve su ID temporal + número de páginas."""
    if "file" not in request.files:
        return jsonify({"error": "No se encontró ningún archivo"}), 400

    file = request.files["file"]
    if file.filename == "" or not allowed_file(file.filename):
        return jsonify({"error": "Archivo inválido. Solo se aceptan PDFs."}), 400

    clean_old_files(app.config["UPLOAD_FOLDER"])
    clean_old_files(app.config["OUTPUT_FOLDER"])

    safe_name = secure_filename(file.filename)
    file_id = uuid.uuid4().hex
    upload_path = app.config["UPLOAD_FOLDER"] / f"{file_id}_{safe_name}"
    file.save(upload_path)

    try:
        doc = fitz.open(str(upload_path))
        page_count = len(doc)
        doc.close()
    except Exception as e:
        upload_path.unlink(missing_ok=True)
        return jsonify({"error": f"PDF inválido o corrupto: {str(e)}"}), 422

    return jsonify({
        "file_id": file_id,
        "filename": safe_name,
        "pages": page_count,
        "size_kb": round(upload_path.stat().st_size / 1024, 1),
    })


@app.route("/api/preview/<file_id>/<int:page_num>")
def preview_page(file_id: str, page_num: int):
    """Renderiza una página del PDF como imagen PNG para previsualización."""
    if not file_id.isalnum():
        abort(400)

    matches = list(app.config["UPLOAD_FOLDER"].glob(f"{file_id}_*"))
    if not matches:
        abort(404)

    upload_path = matches[0]
    try:
        doc = fitz.open(str(upload_path))
        if page_num < 0 or page_num >= len(doc):
            doc.close()
            abort(404)

        page = doc[page_num]
        mat = fitz.Matrix(1.5, 1.5)  # zoom 150 %
        pix = page.get_pixmap(matrix=mat, alpha=False)
        doc.close()

        png_path = unique_path(app.config["OUTPUT_FOLDER"], ".png")
        pix.save(str(png_path))

        @after_this_request
        def remove_png(response):
            png_path.unlink(missing_ok=True)
            return response

        return send_file(str(png_path), mimetype="image/png")
    except Exception as e:
        abort(500)


@app.route("/api/search_text", methods=["POST"])
def search_text():
    """Busca texto en el PDF y devuelve sus coordenadas por página."""
    data = request.get_json(silent=True) or {}
    file_id = data.get("file_id", "")
    search_term = data.get("text", "").strip()

    if not file_id.isalnum() or not search_term:
        return jsonify({"error": "Parámetros inválidos"}), 400

    matches_list = list(app.config["UPLOAD_FOLDER"].glob(f"{file_id}_*"))
    if not matches_list:
        return jsonify({"error": "Archivo no encontrado"}), 404

    upload_path = matches_list[0]
    results = []

    try:
        doc = fitz.open(str(upload_path))
        for page_num, page in enumerate(doc):
            quads = page.search_for(search_term, quads=True)
            for quad in quads:
                rect = quad.rect
                results.append({
                    "page": page_num,
                    "rect": [rect.x0, rect.y0, rect.x1, rect.y1],
                    "text": search_term,
                })
        doc.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"matches": results, "count": len(results)})


@app.route("/api/censor", methods=["POST"])
def censor_pdf():
    """
    Aplica censuras al PDF y devuelve el archivo resultante.

    Body JSON:
    {
        "file_id": "...",
        "redactions": [
            {"page": 0, "rect": [x0, y0, x1, y1], "color": [0,0,0]},
            ...
        ],
        "search_terms": ["palabra1", "palabra2"],   # opcional
        "redact_metadata": true                      # opcional
    }
    """
    data = request.get_json(silent=True) or {}
    file_id = data.get("file_id", "")
    redactions = data.get("redactions", [])
    search_terms = data.get("search_terms", [])
    redact_metadata = data.get("redact_metadata", True)

    if not file_id.isalnum():
        return jsonify({"error": "file_id inválido"}), 400

    matches_list = list(app.config["UPLOAD_FOLDER"].glob(f"{file_id}_*"))
    if not matches_list:
        return jsonify({"error": "Archivo no encontrado. Vuelve a subirlo."}), 404

    upload_path = matches_list[0]
    output_path = unique_path(app.config["OUTPUT_FOLDER"], "_censurado.pdf")

    try:
        doc = fitz.open(str(upload_path))

        # 1. Añadir censuras manuales (rectángulos)
        for item in redactions:
            page_num = int(item.get("page", 0))
            rect_coords = item.get("rect", [])
            color = tuple(item.get("color", REDACTION_COLOR))

            if page_num < 0 or page_num >= len(doc):
                continue
            if len(rect_coords) != 4:
                continue

            page = doc[page_num]
            rect = fitz.Rect(*rect_coords)
            page.add_redact_annot(rect, fill=color)

        # 2. Censura por texto buscado
        for term in search_terms:
            term = term.strip()
            if not term:
                continue
            for page in doc:
                quads = page.search_for(term, quads=True)
                for quad in quads:
                    page.add_redact_annot(quad, fill=REDACTION_COLOR)

        # 3. Aplicar todas las censuras (elimina texto subyacente)
        for page in doc:
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_PIXELS,  # también censura imágenes
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            )

        # 4. Limpiar metadatos sensibles
        if redact_metadata:
            doc.set_metadata({
                "title": "",
                "author": "",
                "subject": "",
                "keywords": "",
                "creator": "PDF Censor",
                "producer": "PDF Censor",
            })

        # 5. Guardar con compresión
        doc.save(
            str(output_path),
            garbage=4,
            deflate=True,
            clean=True,
        )
        doc.close()

        original_name = upload_path.name.replace(file_id + "_", "")
        download_name = f"censurado_{original_name}"

        @after_this_request
        def remove_output(response):
            # Eliminar tras un tiempo prudente
            # En producción usa una tarea en background
            return response

        return send_file(
            str(output_path),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=download_name,
        )

    except Exception as e:
        output_path.unlink(missing_ok=True)
        return jsonify({"error": f"Error al censurar: {str(e)}"}), 500


@app.errorhandler(413)
def too_large(e):
    max_mb = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
    return jsonify({"error": f"Archivo demasiado grande. Máximo {max_mb} MB."}), 413


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Recurso no encontrado"}), 404


# ─────────────────────────── Entrypoint ──────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
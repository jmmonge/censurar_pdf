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
import pytesseract
from pdf2image import convert_from_path

# ─────────────────────────── Configuración ───────────────────────────
app = Flask(__name__)

# Definimos las rutas como objetos Path de una vez por todas
BASE_DIR = Path(__file__).resolve().parent
app.config['UPLOAD_FOLDER'] = BASE_DIR / "uploads"
app.config['OUTPUT_FOLDER'] = BASE_DIR / "outputs"

# Aseguramos que existan físicamente
app.config['UPLOAD_FOLDER'].mkdir(parents=True, exist_ok=True)
app.config['OUTPUT_FOLDER'].mkdir(parents=True, exist_ok=True)

app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-key")
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", 50)) * 1024 * 1024
app.config["ALLOWED_EXTENSIONS"] = {"pdf"}

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

def clean_old_files(folder_path: Path):
    """Elimina archivos con más de 1 hora de antigüedad"""
    now = time.time()
    # Al ser folder_path un objeto Path, usamos .iterdir()
    for f in folder_path.iterdir():
        if f.is_file():
            if f.stat().st_mtime < now - 3600:
                try:
                    f.unlink()
                except Exception as e:
                    print(f"Error borrando archivo viejo {f.name}: {e}")


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
    # Operación de unión de rutas con Path es con el símbolo /
    upload_path = app.config["UPLOAD_FOLDER"] / f"{file_id}_{safe_name}"
    file.save(str(upload_path))

    try:
        doc = fitz.open(str(upload_path))
        page_count = len(doc)
        doc.close()
    except Exception as e:
        if upload_path.exists():
            upload_path.unlink()
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

    # glob() funciona porque UPLOAD_FOLDER es un objeto Path
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
        mat = fitz.Matrix(1.5, 1.5)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        doc.close()

        png_path = unique_path(app.config["OUTPUT_FOLDER"], ".png")
        pix.save(str(png_path))

        @after_this_request
        def remove_png(response):
            if png_path.exists():
                png_path.unlink()
            return response

        return send_file(str(png_path), mimetype="image/png")
    except Exception as e:
        abort(500)

@app.route('/api/search_text', methods=['POST'])
def search_text():
    data = request.json
    file_id = data.get('file_id')
    search_term = data.get('text', '').lower()
    
    # Buscamos el archivo que empiece por el ID (ya que el nombre real varía)
    matches = list(app.config["UPLOAD_FOLDER"].glob(f"{file_id}_*"))
    if not matches:
        return jsonify({"error": "Archivo no encontrado"}), 404
        
    pdf_path = matches[0]
    doc = fitz.open(str(pdf_path))
    all_matches = []

    for page_num in range(len(doc)):
        page = doc[page_num]
        text_instances = page.search_for(search_term)
        
        if text_instances:
            for inst in text_instances:
                all_matches.append({
                    "page": page_num,
                    "rect": [inst.x0, inst.y0, inst.x1, inst.y1]
                })
        else:
            # OCR si no hay texto digital
            images = convert_from_path(str(pdf_path), first_page=page_num+1, last_page=page_num+1)
            if images:
                img = images[0]
                ocr_data = pytesseract.image_to_data(img, lang='spa', output_type=pytesseract.Output.DICT)
                img_w, img_h = img.size
                pdf_w, pdf_h = page.rect.width, page.rect.height
                
                for i, word_text in enumerate(ocr_data['text']):
                    if search_term in word_text.lower() and word_text.strip():
                        all_matches.append({
                            "page": page_num,
                            "rect": [
                                ocr_data['left'][i] * pdf_w / img_w,
                                ocr_data['top'][i] * pdf_h / img_h,
                                (ocr_data['left'][i] + ocr_data['width'][i]) * pdf_w / img_w,
                                (ocr_data['top'][i] + ocr_data['height'][i]) * pdf_h / img_h
                            ]
                        })
    doc.close()
    return jsonify({"count": len(all_matches), "matches": all_matches})

@app.route("/api/censor", methods=["POST"])
def censor_pdf():
    data = request.get_json(silent=True) or {}
    file_id = data.get("file_id", "")
    redactions = data.get("redactions", [])
    search_terms = data.get("search_terms", [])
    redact_metadata = data.get("redact_metadata", True)

    matches_list = list(app.config["UPLOAD_FOLDER"].glob(f"{file_id}_*"))
    if not matches_list:
        return jsonify({"error": "Archivo no encontrado"}), 404

    upload_path = matches_list[0]
    output_path = unique_path(app.config["OUTPUT_FOLDER"], "_censurado.pdf")

    try:
        doc = fitz.open(str(upload_path))
        for item in redactions:
            page_num = int(item.get("page", 0))
            rect_coords = item.get("rect", [])
            color = tuple(item.get("color", REDACTION_COLOR))
            if 0 <= page_num < len(doc) and len(rect_coords) == 4:
                page = doc[page_num]
                page.add_redact_annot(fitz.Rect(*rect_coords), fill=color)

        for term in search_terms:
            if not term.strip(): continue
            for page in doc:
                for quad in page.search_for(term.strip(), quads=True):
                    page.add_redact_annot(quad, fill=REDACTION_COLOR)

        for page in doc:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)

        if redact_metadata:
            doc.set_metadata({"creator": "PDF Censor", "producer": "PDF Censor"})

        doc.save(str(output_path), garbage=4, deflate=True, clean=True)
        doc.close()

        original_name = upload_path.name.replace(file_id + "_", "")
        return send_file(
            str(output_path),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"censurado_{original_name}"
        )
    except Exception as e:
        if output_path.exists(): output_path.unlink()
        return jsonify({"error": str(e)}), 500

@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "Archivo demasiado grande"}), 413

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
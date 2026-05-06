import os
import uuid
import json
import time
import shutil
import threading
from pathlib import Path
from flask import (
    Flask, request, jsonify, render_template,
    send_file, abort, after_this_request
)
from werkzeug.utils import secure_filename
import fitz  # PyMuPDF
import pytesseract
from pdf2image import convert_from_path
import unicodedata
import re

# ─────────────────────────── Configuración ───────────────────────────
app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
app.config['UPLOAD_FOLDER'] = BASE_DIR / "uploads"
app.config['OUTPUT_FOLDER'] = BASE_DIR / "outputs"

app.config['UPLOAD_FOLDER'].mkdir(parents=True, exist_ok=True)
app.config['OUTPUT_FOLDER'].mkdir(parents=True, exist_ok=True)

app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-key")
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", 50)) * 1024 * 1024
app.config["ALLOWED_EXTENSIONS"] = {"pdf"}

REDACTION_COLOR = (0, 0, 0)
HIGHLIGHT_COLOR = (1, 1, 0, 0.3)
SEARCH_PADDING = 1


# Estado OCR compartido entre hilos (file_id -> "running" | "done" | "error")
ocr_status: dict[str, str] = {}
ocr_events: dict[str, threading.Event] = {}
ocr_lock = threading.Lock()
ocr_cache: dict[str, list] = {}


# ─────────────────────────── Helpers ─────────────────────────────────
def allowed_file(filename: str) -> bool:
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in app.config["ALLOWED_EXTENSIONS"]
    )

def unique_path(folder: Path, suffix: str) -> Path:
    return folder / f"{uuid.uuid4().hex}{suffix}"

def clean_old_files(folder_path: Path):
    """Elimina archivos con más de 1 hora de antigüedad."""
    now = time.time()
    for f in folder_path.iterdir():
        if f.is_file() and f.stat().st_mtime < now - 3600:
            try:
                f.unlink()
            except Exception as e:
                print(f"Error borrando archivo viejo {f.name}: {e}")

def normalize_text(text: str) -> str:
    """Elimina acentos, puntos, guiones y convierte a minúsculas."""
    if not text:
        return ""
    text = text.lower()
    text = "".join(
        c for c in unicodedata.normalize('NFD', text)
        if unicodedata.category(c) != 'Mn'
    )
    text = re.sub(r'[.\-]', '', text)
    return text.strip()

def ocr_marker_path(file_id: str) -> Path:
    """Ruta del fichero bandera que indica que el OCR ya se realizó."""
    return app.config['UPLOAD_FOLDER'] / f"{file_id}.ocr_done"

def run_ocr_and_replace(file_id: str, pdf_path: Path):
    event = threading.Event()
    with ocr_lock:
        ocr_status[file_id] = "running"
        ocr_events[file_id] = event  # ← registrar evento

    tmp_output = unique_path(app.config['UPLOAD_FOLDER'], "_ocr_tmp.pdf")
    try:
        from PIL import Image
        import io

        original_doc = fitz.open(str(pdf_path))
        new_doc = fitz.open()

        for page_num in range(len(original_doc)):
            orig_page = original_doc[page_num]
            page_width = orig_page.rect.width
            page_height = orig_page.rect.height

            # Renderizar página a imagen
            mat = fitz.Matrix(2.0, 2.0)
            pix = orig_page.get_pixmap(matrix=mat, alpha=False)
            img_bytes = pix.tobytes("png")

            # Generar PDF con capa de texto via tesseract directamente
            img = Image.open(io.BytesIO(img_bytes))
            pdf_bytes = pytesseract.image_to_pdf_or_hocr(
                img,
                lang='spa',
                extension='pdf',
                config='--psm 3'
            )

            # Abrir el PDF que generó tesseract (tiene imagen + texto invisible)
            tess_doc = fitz.open("pdf", pdf_bytes)
            tess_page = tess_doc[0]

            # Crear nueva página con las dimensiones originales
            new_page = new_doc.new_page(width=page_width, height=page_height)

            # Copiar el contenido del PDF de tesseract escalado a las dimensiones originales
            new_page.show_pdf_page(
                new_page.rect,
                tess_doc,
                0,
            )
            tess_doc.close()

            print(f"[OCR] Página {page_num} procesada")

        original_doc.close()

        new_doc.save(str(tmp_output), garbage=4, deflate=True)
        new_doc.close()

        # Verificar
        verify = fitz.open(str(tmp_output))
        total_chars = sum(len(verify[p].get_text()) for p in range(len(verify)))
        verify.close()
        print(f"[OCR] Verificación: {total_chars} caracteres en PDF resultante")

        shutil.move(str(tmp_output), str(pdf_path))
        ocr_marker_path(file_id).touch()
        with ocr_lock:
            ocr_status[file_id] = "done"
        print(f"[OCR] Completado para file_id={file_id}")

    except Exception as e:
        print(f"[OCR] Error en file_id={file_id}: {e}")
        import traceback
        traceback.print_exc()
        if tmp_output.exists():
            tmp_output.unlink()
        with ocr_lock:
            ocr_status[file_id] = "error"
    finally:
        event.set()  # ← señalar siempre, tanto si termina bien como si falla

def pdf_has_real_text(pdf_path: Path, min_chars_per_page: int = 50) -> bool:
    """
    Devuelve True si el PDF tiene texto digital aprovechable.
    Un PDF escaneado sin OCR tendrá 0 o muy pocos caracteres por página.
    """
    try:
        doc = fitz.open(str(pdf_path))
        total_pages = len(doc)
        pages_with_text = 0
        for page in doc:
            text = page.get_text().strip()
            if len(text) >= min_chars_per_page:
                pages_with_text += 1
        doc.close()
        # Consideramos que tiene texto real si al menos la mitad de páginas
        # superan el umbral mínimo de caracteres
        return pages_with_text >= max(1, total_pages // 2)
    except Exception:
        return False
def search_in_ocr_pdf(file_id: str, pdf_path: Path, search_term: str) -> list:
    """
    Busca en PDF con OCR. La primera vez extrae todas las palabras con sus
    coordenadas y las guarda en caché. Las siguientes búsquedas usan la caché.
    """
    # ── Construir caché si no existe ───────────────────────────────────
    with ocr_lock:
        cached = ocr_cache.get(file_id)

    if cached is None:
        from PIL import Image
        import io
        print(f"[SEARCH] Construyendo caché OCR para {file_id}...")
        words_data = []
        doc = fitz.open(str(pdf_path))
        for page_num in range(len(doc)):
            page = doc[page_num]
            page_width = page.rect.width
            page_height = page.rect.height

            mat = fitz.Matrix(2.0, 2.0)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            img_w, img_h = img.size

            ocr_data = pytesseract.image_to_data(
                img, lang='spa',
                output_type=pytesseract.Output.DICT,
                config='--psm 3'
            )

            for i, word in enumerate(ocr_data['text']):
                if not word.strip():
                    continue
                if int(ocr_data['conf'][i]) < 30:
                    continue
                x0 = ocr_data['left'][i] * page_width / img_w
                y0 = ocr_data['top'][i] * page_height / img_h
                x1 = (ocr_data['left'][i] + ocr_data['width'][i]) * page_width / img_w
                y1 = (ocr_data['top'][i] + ocr_data['height'][i]) * page_height / img_h
                words_data.append({
                    "page": page_num,
                    "word_norm": normalize_text(word),
                    "rect": [x0, y0, x1, y1]
                })

        doc.close()
        with ocr_lock:
            ocr_cache[file_id] = words_data
        cached = words_data
        print(f"[SEARCH] Caché construida: {len(cached)} palabras")

    # ── Buscar en la caché ─────────────────────────────────────────────
    results = []
    for entry in cached:
        if search_term in entry["word_norm"]:
            r = entry["rect"]
            results.append({
                "page": entry["page"],
                "rect": [
                    max(0, r[0] - SEARCH_PADDING),
                    max(0, r[1] - SEARCH_PADDING),
                    r[2] + SEARCH_PADDING,
                    r[3] + SEARCH_PADDING,
                ]
            })
    return results
# ─────────────────────────── Rutas API ───────────────────────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "pdf-censor"})

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/upload", methods=["POST"])
def upload_pdf():
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
    file.save(str(upload_path))

    try:
        doc = fitz.open(str(upload_path))
        page_count = len(doc)
        doc.close()
    except Exception as e:
        if upload_path.exists():
            upload_path.unlink()
        return jsonify({"error": f"PDF inválido o corrupto: {str(e)}"}), 422

    # ── Detectar si necesita OCR y lanzarlo en segundo plano ──────────
    needs_ocr = not pdf_has_real_text(upload_path)
    if needs_ocr:
        t = threading.Thread(
            target=run_ocr_and_replace,
            args=(file_id, upload_path),
            daemon=True,
        )
        t.start()

    return jsonify({
        "file_id": file_id,
        "filename": safe_name,
        "pages": page_count,
        "size_kb": round(upload_path.stat().st_size / 1024, 1),
        "ocr_required": needs_ocr,  # el frontend puede mostrar aviso inmediato
    })


@app.route("/api/ocr_status/<file_id>")
def get_ocr_status(file_id: str):
    """
    Devuelve el estado del OCR para un file_id dado.
    Estados posibles: 'pending' | 'running' | 'done' | 'error'
    """
    if not file_id.isalnum():
        abort(400)
    with ocr_lock:
        status = ocr_status.get(file_id)
    if status is None:
        # Podría haberse reiniciado el servidor; comprobamos la bandera en disco
        status = "done" if ocr_marker_path(file_id).exists() else "pending"
    return jsonify({"file_id": file_id, "status": status})


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
    except Exception:
        abort(500)


@app.route('/api/search_text', methods=['POST'])
def search_text():
    data = request.json
    file_id = data.get('file_id')
    original_term = data.get('text', '').strip()
    search_term = normalize_text(original_term)

    if not search_term:
        return jsonify({"count": 0, "matches": []})

    matches = list(app.config["UPLOAD_FOLDER"].glob(f"{file_id}_*"))
    if not matches:
        return jsonify({"error": "Archivo no encontrado"}), 404

    pdf_path = matches[0]

    # ── Si el OCR está en curso, esperar a que termine ─────────────────
    with ocr_lock:
        status = ocr_status.get(file_id)
        event = ocr_events.get(file_id)

    if status == "running" and event is not None:
        print(f"[SEARCH] OCR en curso, esperando señal...")
        event.wait()  # bloquea hasta que el OCR llame a event.set()
        print(f"[SEARCH] OCR terminado, procediendo con la búsqueda")

# ── Buscar en el PDF ───────────────────────────────────────────────
    ocr_done = ocr_marker_path(file_id).exists()
    all_matches = []

    if ocr_done:
        # PDF con OCR: usar tesseract por palabra (coordenadas precisas)
        all_matches = search_in_ocr_pdf(file_id, pdf_path, search_term)
    else:
        # PDF con texto digital: search_for es suficiente y preciso
        doc = fitz.open(str(pdf_path))
        for page_num in range(len(doc)):
            page = doc[page_num]
            for variant in _search_variants(original_term):
                for rect in page.search_for(variant):
                    candidate = {
                        "page": page_num,
                        "rect": [
                            max(0, rect.x0 - SEARCH_PADDING),
                            max(0, rect.y0 - SEARCH_PADDING),
                            rect.x1 + SEARCH_PADDING,
                            rect.y1 + SEARCH_PADDING,
                        ]
                    }
                    if candidate not in all_matches:
                        all_matches.append(candidate)

            # Palabras normalizadas (para acentos que search_for no cubre)
            for w in page.get_text("words"):
                word_norm = normalize_text(w[4])
                if search_term in word_norm:
                    candidate = {
                        "page": page_num,
                        "rect": [
                            max(0, w[0] - SEARCH_PADDING),
                            max(0, w[1] - SEARCH_PADDING),
                            w[2] + SEARCH_PADDING,
                            w[3] + SEARCH_PADDING,
                        ]
                    }
                    # Deduplicar comparando coordenadas con tolerancia
                    already = any(
                        abs(m["rect"][0] - candidate["rect"][0]) < 2 and
                        abs(m["rect"][1] - candidate["rect"][1]) < 2 and
                        m["page"] == candidate["page"]
                        for m in all_matches
                    )
                    if not already:
                        all_matches.append(candidate)
        doc.close()

    # ── Si no hay resultados y el OCR no se ha hecho, lanzarlo ────────
    if not all_matches and not ocr_done and status != "running":
        t = threading.Thread(
            target=run_ocr_and_replace,
            args=(file_id, pdf_path),
            daemon=True,
        )
        t.start()
        return jsonify({
            "count": 0,
            "matches": [],
            "ocr_required": True,
            "message": "El documento no contiene texto digital. Realizando OCR, por favor espere…"
        })

    return jsonify({"count": len(all_matches), "matches": all_matches})

def _search_variants(term: str) -> list[str]:
    """
    Genera variantes del término para mejorar la tasa de acierto en search_for:
    original, sin acentos, mayúsculas, etc.
    """
    variants = {term}
    # Sin acentos
    no_accent = "".join(
        c for c in unicodedata.normalize('NFD', term)
        if unicodedata.category(c) != 'Mn'
    )
    variants.add(no_accent)
    variants.add(term.lower())
    variants.add(no_accent.lower())
    variants.add(term.upper())
    return list(variants)

@app.route("/api/censor", methods=["POST"])
def censor_pdf():
    data = request.get_json(silent=True) or {}
    file_id = data.get("file_id", "")
    redactions = data.get("redactions", [])
    search_terms = data.get("search_terms", [])

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
            if not term.strip():
                continue
            for page in doc:
                for quad in page.search_for(term.strip(), quads=True):
                    page.add_redact_annot(quad, fill=REDACTION_COLOR)

        for page in doc:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)

        empty_metadata = {
            "title": "", "author": "", "subject": "",
            "keywords": "", "creator": "", "producer": "",
            "creationDate": "", "modDate": ""
        }
        doc.set_metadata(empty_metadata)
        doc.del_xml_metadata()

        doc.save(
            str(output_path),
            garbage=4,
            deflate=True,
            clean=True,
            linear=True,
        )
        doc.close()

        original_name = upload_path.name.replace(file_id + "_", "")
        return send_file(
            str(output_path),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"censurado_{original_name}"
        )
    except Exception as e:
        if output_path.exists():
            output_path.unlink()
        return jsonify({"error": str(e)}), 500


@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "Archivo demasiado grande"}), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
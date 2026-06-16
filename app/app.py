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

import logging
from logging.handlers import RotatingFileHandler
import time
from logs_admin import logs_bp
import socket

# ─────────────────────────── Configuración ───────────────────────────
app = Flask(__name__)
app.register_blueprint(logs_bp)
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

# Configuración del Logger
log_formatter = logging.Formatter(
    '[%(asctime)s] %(levelname)s | IP: %(client_ip)s | Fichero: %(filename_src)s | Tamaño: %(filesize)s KB | Accion: %(action)s | UA: %(user_agent)s'
)

# El log se guardará en "log_uso.log"
# maxBytes=1MB, backupCount=5 (mantendrá hasta 5 archivos viejos de historial)
LOGS_DIR = os.path.join(os.path.dirname(__file__), 'logs')
os.makedirs(LOGS_DIR, exist_ok=True)
log_path = os.path.join(LOGS_DIR, 'log_uso.log')
log_handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=5, encoding='utf-8')
log_handler.setFormatter(log_formatter)
log_handler.setLevel(logging.INFO)

app_log = logging.getLogger('app_stats')
app_log.setLevel(logging.INFO)
app_log.addHandler(log_handler)

stats_log = logging.getLogger('app_stats') 
stats_log.setLevel(logging.INFO)
stats_log.addHandler(log_handler)
stats_log.propagate = False

# Estado OCR compartido entre hilos (file_id -> "running" | "done" | "error")
ocr_status: dict[str, str] = {}
ocr_events: dict[str, threading.Event] = {}
ocr_lock = threading.Lock()
ocr_cache: dict[str, list] = {}


# ─────────────────────────── Helpers ─────────────────────────────────
def registrar_evento(action, filename_src="N/A", filesize="0"):
    """Registra un evento en el log con metadatos automáticos."""
    try:
        ip = request.remote_addr if request else "Servidor"
        hostname = socket.getfqdn(ip) if ip != "Servidor" else "Servidor"
        if hostname == ip:          # no se pudo resolver
            hostname = "N/A"
    except Exception:
        ip, hostname = "N/A", "N/A"

    extra_data = {
        'client_ip': f"{ip} ({hostname})",
        'filename_src': filename_src,
        'filesize': filesize,
        'action': action,
        'user_agent': request.headers.get('User-Agent', 'Desconocido') if request else "Interno"
    }
    # Usamos stats_log, que es el que tiene el formato especial
    stats_log.info(f"Accion: {action}", extra=extra_data)

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
    term_words = search_term.split()
    term_len = len(term_words)
    results = []

    for i in range(len(cached) - term_len + 1):
        chunk = cached[i:i + term_len]
        
        # Solo comparar palabras de la misma página y consecutivas
        if len(set(e["page"] for e in chunk)) > 1:
            continue
            
        chunk_norms = [e["word_norm"] for e in chunk]
        
        # Comparación flexible: el término debe estar contenido en cada palabra
        # (cubre casos como "jimenez" encontrando "jimenez," con puntuación)
        match = all(
            term_words[j] in chunk_norms[j]
            for j in range(term_len)
        )
        
        if match:
            r_x0 = min(e["rect"][0] for e in chunk)
            r_y0 = min(e["rect"][1] for e in chunk)
            r_x1 = max(e["rect"][2] for e in chunk)
            r_y1 = max(e["rect"][3] for e in chunk)
            results.append({
                "page": chunk[0]["page"],
                "rect": [
                    max(0, r_x0 - SEARCH_PADDING),
                    max(0, r_y0 - SEARCH_PADDING),
                    r_x1 + SEARCH_PADDING,
                    r_y1 + SEARCH_PADDING,
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
    
    size_kb = round(upload_path.stat().st_size / 1024, 1)

    #Registramos el evento (ya existe size_kb)
    registrar_evento(
        action="UPLOAD", 
        filename_src=file.filename, 
        filesize=str(size_kb),
    )
 
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
        # PDF con OCR: usar caché tesseract por palabra (coordenadas precisas)
        all_matches = search_in_ocr_pdf(file_id, pdf_path, search_term)
    else:
        # PDF con texto digital
        doc = fitz.open(str(pdf_path))
        for page_num in range(len(doc)):
            page = doc[page_num]

            # Método 1: search_for con variantes (rápido, cubre mayúsculas)
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

            # Método 2: palabras normalizadas con soporte multi-palabra
            words = page.get_text("words")
            term_words = search_term.split()
            term_len = len(term_words)

            # DEBUG temporal
            print(f"[DEBUG] Buscando '{search_term}' → term_words={term_words}")
            for w in words:
                if any(t in normalize_text(w[4]) for t in term_words):
                    print(f"[DEBUG] Palabra relevante: {repr(w[4])} norm={repr(normalize_text(w[4]))} bloque={w[5]} linea={w[6]} pos={w[7]}")

            for i in range(len(words) - term_len + 1):
                chunk = words[i:i + term_len]
                chunk_norm = [normalize_text(w[4]) for w in chunk]

                match = all(
                    term_words[j] in chunk_norm[j]
                    for j in range(term_len)
                )

                if match:
                    candidate = {
                        "page": page_num,
                        "rect": [
                            max(0, min(w[0] for w in chunk) - SEARCH_PADDING),
                            max(0, min(w[1] for w in chunk) - SEARCH_PADDING),
                            max(w[2] for w in chunk) + SEARCH_PADDING,
                            max(w[3] for w in chunk) + SEARCH_PADDING,
                        ]
                    }
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

        original_name = upload_path.name.replace(file_id + "_", "")


        registrar_evento(action="DOWNLOAD",filename_src=f"censurado_{original_name}")
        doc.close()
        
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

# ─────────────────────────── Patrones automáticos ────────────────────

PATTERNS = {
    "dni_nie": re.compile(
        #r'(\b[xyz]?(?=(?:\D*\d){8}\D*$)(?:[0-9]{1,3}\.){0,3}[0-9]{1,3}[a-z]\b)|(\b[0-9]{8}[A-Z]\b)',
        r'(\b([XYZ\d])[\s\.\-]?([0-9]{1,3}(?:\.?[0-9]{3}){2}|[0-9]{1,7})[\s\.\-]?([A-Z])\b)',
        re.IGNORECASE
    ),
    "email": re.compile(
        r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b'
    ),
}
@app.route("/api/search_pattern", methods=["POST"])
def search_pattern():
    data = request.get_json(silent=True) or {}
    file_id = data.get("file_id", "")
    pattern_key = data.get("pattern", "")   # "dni_nie" | "email"

    if pattern_key not in PATTERNS:
        return jsonify({"error": "Patrón desconocido"}), 400

    matches_list = list(app.config["UPLOAD_FOLDER"].glob(f"{file_id}_*"))
    if not matches_list:
        return jsonify({"error": "Archivo no encontrado"}), 404

    pdf_path = matches_list[0]
    pattern = PATTERNS[pattern_key]
    all_matches = []

    # ── Esperar OCR si está en curso ──────────────────────────────────
    with ocr_lock:
        status = ocr_status.get(file_id)
        event = ocr_events.get(file_id)
    if status == "running" and event is not None:
        event.wait()

    try:
        doc = fitz.open(str(pdf_path))
        for page_num in range(len(doc)):
            page = doc[page_num]
            # get_text("words") devuelve (x0, y0, x1, y1, word, block, line, pos)
            words = page.get_text("words")
            for w in words:
                word_text = w[4]
                if pattern.search(word_text):
                    all_matches.append({
                        "page": page_num,
                        "rect": [
                            max(0, w[0] - SEARCH_PADDING),
                            max(0, w[1] - SEARCH_PADDING),
                            w[2] + SEARCH_PADDING,
                            w[3] + SEARCH_PADDING,
                        ],
                        "found": word_text,
                    })
        doc.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"count": len(all_matches), "matches": all_matches})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
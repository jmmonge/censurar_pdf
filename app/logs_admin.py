import os
import re
from functools import wraps
from flask import Blueprint, render_template, abort, request, Response

logs_bp = Blueprint('logs_admin', __name__, template_folder='templates')

LOG_DIR = '/app/logs'
# Define aquí tus credenciales
USER_ADMIN = "jmmonge"
PASS_ADMIN = "Rubinos22" 

def check_auth(username, password):
    """Verifica si el usuario y contraseña son correctos."""
    return username == USER_ADMIN and password == PASS_ADMIN

def requires_auth(f):
    """Decorador para proteger rutas."""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return Response(
                'No autorizado. Introduce las credenciales correctas.', 401,
                {'WWW-Authenticate': 'Basic realm="Login Requerido"'}
            )
        return f(*args, **kwargs)
    return decorated

def parse_log_line(line):
    pattern = r"\[(?P<date>.*?)\] (?P<level>\w+) \| IP: (?P<ip>.*?) \| Fichero: (?P<file>.*?) \| Tamaño: (?P<size>.*?) \| Accion: (?P<action>.*?) \| UA: (?P<ua>.*)"
    match = re.search(pattern, line)
    return match.groupdict() if match else None

# Aplicamos el decorador @requires_auth a las rutas
@logs_bp.route('/dashboard-logs')
@logs_bp.route('/dashboard-logs/<filename>')
@requires_auth
def dashboard_logs(filename=None):
    if not os.path.exists(LOG_DIR):
        return "Carpeta de logs no encontrada", 404

    files = [f for f in os.listdir(LOG_DIR) if f.endswith('.log')]
    files.sort(key=lambda x: os.path.getmtime(os.path.join(LOG_DIR, x)), reverse=True)

    selected_file = filename if filename else (files[0] if files else None)
    log_entries = []
    
    if selected_file:
        try:
            filepath = os.path.join(LOG_DIR, selected_file)
            with open(filepath, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                for line in reversed(lines):
                    parsed = parse_log_line(line)
                    if parsed:
                        log_entries.append(parsed)
        except Exception as e:
            return f"Error: {e}", 500

    return render_template('logs.html', files=files, entries=log_entries, current_file=selected_file)
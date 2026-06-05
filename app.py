from flask import Flask, render_template, request, jsonify, send_file
from werkzeug.utils import secure_filename
import os, sys, time, logging, requests
from io import BytesIO

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from model_UDTGen              import extract_udt_tags
from L5XOpt_UDT                import (optimize_and_regenerate_udt, extract_udt_definition,
                                       extract_all_udt_definitions,
                                       _topological_sort as _topo_sort,
                                       _estimate_udt_size as _estimate_size)
from L5XGen_UDT                import generate_udt_l5x_from_tags
from Attach_L5Xanalyzer        import analyze_l5x_type
from Validator_UDT             import validate_udt
from Attach_UDTVerify          import analyse_udt
from Attach_ProcessCSV         import process_csv_to_udts
from Attach_ProcessExcel       import process_excel_to_udts
from Attach_ProcessL5XProgram  import analyse_program, optimize_program

def _xml_size_label(xml_str: str) -> str:
    """Human-readable byte size of an XML/text string."""
    if not xml_str:
        return '0 B'
    b = len(xml_str.encode('utf-8')) if isinstance(xml_str, str) else len(xml_str)
    return f"{b/1024:.1f} KB" if b >= 1024 else f"{b} B"


app = Flask(__name__)
app.config.update({
    'UPLOAD_FOLDER':      'uploads',
    'MAX_CONTENT_LENGTH': 16 * 1024 * 1024,
})
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Purge any stragglers left from previous runs (crashes, kill -9, etc.).
# Files in uploads/ are temporary — they're read once and deleted in /attach.
def _purge_upload_dir(folder: str) -> None:
    try:
        for fn in os.listdir(folder):
            fp = os.path.join(folder, fn)
            if os.path.isfile(fp):
                try:
                    os.remove(fp)
                except OSError:
                    pass
    except OSError:
        pass
_purge_upload_dir(app.config['UPLOAD_FOLDER'])

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
known_udts    = {}

# Bounded TTL cache for L5X content held between attach → optimize calls.
# The previous module-level dict grew without bound and leaked across users.
# This is still in-process, single-worker state — fine for the localhost
# deployment, NOT for any multi-worker / multi-user setup.
import threading
_STORED_FILES_TTL_SEC = 30 * 60   # 30 minutes
_STORED_FILES_MAX     = 64
_stored_lock          = threading.Lock()
stored_files: dict[str, tuple[float, str]] = {}   # filename → (timestamp, content)


def _stored_files_set(name: str, content: str) -> None:
    with _stored_lock:
        now = time.time()
        # Evict expired entries
        for k in [k for k, (ts, _) in stored_files.items()
                  if now - ts > _STORED_FILES_TTL_SEC]:
            stored_files.pop(k, None)
        # Cap size — drop oldest if at limit
        while len(stored_files) >= _STORED_FILES_MAX:
            oldest = min(stored_files.items(), key=lambda kv: kv[1][0])[0]
            stored_files.pop(oldest, None)
        stored_files[name] = (now, content)


def _stored_files_get(name: str) -> str | None:
    with _stored_lock:
        entry = stored_files.get(name)
        if not entry:
            return None
        ts, content = entry
        if time.time() - ts > _STORED_FILES_TTL_SEC:
            stored_files.pop(name, None)
            return None
        return content


def _stored_files_pop(name: str) -> None:
    with _stored_lock:
        stored_files.pop(name, None)


active_model  = {'name': 'phi4'}
ALLOWED_EXTENSIONS = {'l5x', 'csv', 'xlsx', 'xlsm'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def create_response(text, is_code=False, duration=None, download=None,
                    requires_confirmation=False, confirmation_data=None):
    return {'response': {
        'text': text, 'is_code': is_code, 'duration': duration,
        'download': download, 'requires_confirmation': requires_confirmation,
        'confirmation_data': confirmation_data,
    }}

def error_response(msg, status=400):
    return jsonify(create_response(msg)), status


# ── NLP → UDT pipeline ───────────────────────────────────────────────────────
def handle_udt_generation(user_input):
    tags = extract_udt_tags(user_input)
    if not tags.get('tags'):
        return {'success': False,
                'error': "Could not extract UDT definitions. "
                         "Try: 'Create a UDT named MotorStatus with: run_status, BOOL, Motor running; speed, REAL, Speed setpoint'"}

    validation = validate_udt(tags)
    if not validation.is_valid:
        return {'success': False, 'error': 'UDT validation failed:\n' + '\n'.join(validation.errors)}

    clean = validation.fixed_data
    name  = clean.get('udt_name', 'GeneratedUDT')

    gen = generate_udt_l5x_from_tags(clean)
    if not gen.get('success'):
        return {'success': False, 'error': f"Generation failed: {gen.get('error')}"}

    # Optimizer pass
    try:
        udt_def = extract_udt_definition(gen['udt_text'])
        opt     = optimize_and_regenerate_udt(udt_def) if udt_def and 'name' in udt_def else {}
        final_l5x  = opt.get('udt_text',      gen['udt_text'])      if opt.get('success') else gen['udt_text']
        final_name = opt.get('download_name',  gen['download_name']) if opt.get('success') else gen['download_name']
    except Exception as e:
        app.logger.warning(f'[Optimizer] {e}')
        final_l5x, final_name = gen['udt_text'], gen['download_name']

    return {
        'success': True, 'udt_l5x': final_l5x, 'file_name': final_name,
        'message': f'UDT "{name}" generated — download will begin shortly.',
    }


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/favicon.ico')
def favicon():
    return app.send_static_file('favicon.ico')

@app.route('/logo')
def logo():
    p = os.path.join(app.static_folder, 'AnythingOT.png')
    return send_file(p, mimetype='image/png') if os.path.exists(p) else ('', 404)

@app.route('/active_model')
def get_active_model():
    name = active_model.get('name', '').strip()
    return jsonify({'model': name, 'is_set': bool(name)})


# ── Option 1: NLP chat ────────────────────────────────────────────────────────
@app.route('/chat', methods=['POST'])
def chat():
    start    = time.time()
    data     = request.get_json() or {}
    user_msg = data.get('message', '').strip()
    if not user_msg:
        return error_response('No message received.', 400)
    if not active_model.get('name', '').strip():
        return jsonify(create_response(
            'No Ollama model selected. Open Settings ⚙️ and choose a model first.',
            False, time.time() - start))

    result = handle_udt_generation(user_msg)
    if result['success']:
        return jsonify(create_response(
            result['message'], False, time.time() - start,
            {'file_content': result['udt_l5x'], 'file_name': result['file_name'],
             'content_type': 'application/xml'}))
    return jsonify(create_response(result['error'], False, time.time() - start))


# ── Option 2 & 3: File attach ─────────────────────────────────────────────────
@app.route('/attach', methods=['POST'])
def attach_file():
    start    = time.time()
    file     = request.files.get('file')
    if not file or not allowed_file(file.filename):
        return error_response('Only .l5x, .csv and .xlsx/.xlsm files are supported.', 400)

    filename = secure_filename(file.filename)
    ext      = filename.rsplit('.', 1)[1].lower()
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    # ── CSV batch ─────────────────────────────────────────────────────────────
    if ext == 'csv':
        with open(filepath, 'rb') as f:
            csv_bytes = f.read()
        try:    os.remove(filepath)
        except OSError: pass
        result = process_csv_to_udts(csv_bytes)
        if not result['success']:
            return jsonify(create_response(
                'CSV failed:\n' + '\n'.join(result['errors']), False, time.time() - start))
        names   = [u['udt_name'] for u in result['udts']]
        summary = f"Generated {len(result['udts'])} UDT(s): {', '.join(names)}."
        if result['warnings']:
            summary += f"\n⚠ {len(result['warnings'])} warning(s)."
        return jsonify(create_response(
            summary, False, time.time() - start,
            {'file_content_b64': result['zip_bytes'].hex(), 'file_name': 'UDTs.zip',
             'content_type': 'application/zip', 'encoding': 'hex'}))

    # ── Excel batch (one sheet = one UDT) ─────────────────────────────────────
    if ext in ('xlsx', 'xlsm'):
        with open(filepath, 'rb') as f:
            xl_bytes = f.read()
        try:    os.remove(filepath)
        except OSError: pass
        result = process_excel_to_udts(xl_bytes)
        if not result['success']:
            return jsonify(create_response(
                'Excel failed:\n' + '\n'.join(result['errors']), False, time.time() - start))
        names   = [u['udt_name'] for u in result['udts']]
        summary = f"Generated {len(result['udts'])} UDT(s) from {len(names)} sheet(s): {', '.join(names)}."
        if result['errors']:
            summary += f"\n⚠ {len(result['errors'])} sheet(s) skipped on error."
        if result['warnings']:
            summary += f"\n⚠ {len(result['warnings'])} warning(s)."
        return jsonify(create_response(
            summary, False, time.time() - start,
            {'file_content_b64': result['zip_bytes'].hex(), 'file_name': 'UDTs.zip',
             'content_type': 'application/zip', 'encoding': 'hex'}))

    # ── L5X: read content once ────────────────────────────────────────────────
    with open(filepath, 'r', encoding='utf-8') as f:
        l5x_content = f.read()
    try:    os.remove(filepath)
    except OSError: pass
    l5x_type    = analyze_l5x_type(l5x_content).lower()

    # ── Full program L5X ──────────────────────────────────────────────────────
    if l5x_type in {'controller', 'program'}:
        result = analyse_program(l5x_content)
        if result.get('error'):
            return jsonify(create_response(result['error'], False, time.time() - start))

        # Store content for deferred optimize call
        _stored_files_set(filename, l5x_content)

        n_total   = result['udt_count']
        n_issues  = result['needs_opt_count']
        cyclic    = result['cyclic']

        summary   = f"Program loaded — {n_total} UDT(s) found, {n_issues} need optimization."
        if cyclic:
            summary += f"\n⚠ {len(cyclic)} circular reference(s) skipped: {', '.join(cyclic)}."

        return jsonify(create_response(
            summary, False, time.time() - start, None, True,
            {
                'type':             'program_attachment',
                'filename':         filename,
                'controller_name':  result['controller_name'],
                'processing_order': result['processing_order'],
                'cyclic':           cyclic,
                'analyses':         result['analyses'],
                'udt_count':        n_total,
                'needs_opt_count':  n_issues,
                'options': [
                    {'label': 'Optimize all', 'action': 'optimize_program'},
                    {'label': 'Manual Command (WIP)', 'action': 'manual_command'},
                ],
            }
        ))

    # ── Single UDT L5X ────────────────────────────────────────────────────────
    if l5x_type in {'datatype', 'udt'}:
        udt = extract_udt_definition(l5x_content)
        if not (udt and 'name' in udt):
            return jsonify(create_response('UDT detected but extraction failed.',
                                           False, time.time() - start))
        udt_name            = udt['name']
        known_udts[udt_name] = udt
        _stored_files_set(filename, l5x_content)

        analysis = analyse_udt(l5x_content)
        n_issues = len(analysis.get('issues', []))
        summary  = (f"UDT \"{udt_name}\" loaded — {analysis['member_count']} member(s), "
                    f"{n_issues} issue(s) found.")

        return jsonify(create_response(
            summary, False, time.time() - start, None, True,
            {
                'type':              'udt_attachment',
                'udt_name':          udt_name,
                'udt_definition':    udt,
                'original_filename': filename,
                'analysis':          analysis,
                'options': [
                    {'label': 'Optimize',              'action': 'optimize'},
                    {'label': 'Manual Command (WIP)',   'action': 'manual_command'},
                ],
            }
        ))

    return jsonify(create_response(
        f"Unsupported L5X type '{l5x_type}'. Attach a UDT export or full controller program.",
        False, time.time() - start))


# ── Process actions ───────────────────────────────────────────────────────────
@app.route('/process_udt_attachment', methods=['POST'])
def process_udt_attachment():
    try:
        data   = request.get_json() or {}
        action = data.get('action')

        # ── Full program optimize ─────────────────────────────────────────────
        if action == 'optimize_program':
            filename = data.get('filename', '')
            content  = _stored_files_get(filename)
            if not content:
                return jsonify(create_response('Original file not found — please re-attach.'))

            result = optimize_program(content)
            if not result['success']:
                return jsonify(create_response(f"Optimization failed: {result.get('error')}"))

            changed = result['changed']
            skipped = result['skipped']
            before_b    = len(content.encode('utf-8'))
            after_b     = len(result['optimized_xml'].encode('utf-8'))
            size_before = _xml_size_label(content)
            size_after  = _xml_size_label(result['optimized_xml'])
            diff_b      = before_b - after_b
            abs_diff    = abs(diff_b)
            if abs_diff < 512:                         # < 512 B — negligible
                diff_lbl = "size unchanged"
            elif diff_b > 0:                           # file got smaller
                diff_lbl = f"-{abs_diff/1024:.1f} KB" if abs_diff >= 1024 else f"-{abs_diff} B"
            else:                                      # file got larger (whitespace etc.)
                diff_lbl = f"+{abs_diff/1024:.1f} KB (formatting)" if abs_diff >= 1024 else f"+{abs_diff} B (formatting)"
            msg     = (f"Program optimized — {len(changed)} UDT(s) updated, {len(skipped)} already optimal. "
                       f"{size_before} → {size_after} ({diff_lbl}).")
            dl_name = filename.rsplit('.', 1)[0] + '_optimized.L5X'
            _stored_files_pop(filename)   # clean up

            return jsonify(create_response(msg, download={
                'file_content': result['optimized_xml'],
                'file_name':    dl_name,
                'content_type': 'application/xml',
            }))

        # ── Single UDT optimize ───────────────────────────────────────────────
        if action == 'optimize':
            udt_def  = data.get('udt_definition')
            udt_name = udt_def.get('name', 'Unknown') if udt_def else 'Unknown'
            orig_filename = data.get('original_filename', '')
            orig_content  = (_stored_files_get(orig_filename) or '')

            # Re-parse the original file so nested UDT definitions are available.
            # This lets the optimizer embed optimized nested types as Use="Context"
            # siblings, producing a self-contained single-UDT export. Falling back
            # to the posted udt_definition keeps things working if the stored
            # content has expired.
            if orig_content:
                parsed = extract_all_udt_definitions(orig_content)
                if 'error' not in parsed and parsed.get('target') in parsed.get('udts', {}):
                    target_name = parsed['target']
                    all_udts    = parsed['udts']
                    aoi_reg     = parsed.get('aoi_registry', {})
                    reg = {}
                    for n in _topo_sort(all_udts):
                        reg[n] = _estimate_size(all_udts[n], reg, aoi_reg)
                    res = optimize_and_regenerate_udt(
                        all_udts[target_name], all_udts=all_udts,
                        udt_size_registry=reg, aoi_registry=aoi_reg,
                        aoi_context_xml=parsed.get('aoi_context_xml'),
                        embed_nested_context=True,
                    )
                    udt_name = target_name
                else:
                    res = optimize_and_regenerate_udt(udt_def)
            else:
                res = optimize_and_regenerate_udt(udt_def)

            if res.get('success'):
                size_before = _xml_size_label(orig_content) if orig_content else None
                size_after  = _xml_size_label(res['udt_text'])
                if size_before:
                    before_b = len(orig_content.encode('utf-8'))
                    after_b  = len(res['udt_text'].encode('utf-8'))
                    diff_b   = before_b - after_b
                    diff_b_abs = abs(diff_b)
                    diff_lbl = f"{diff_b_abs/1024:.1f} KB saved" if diff_b > 0 and diff_b_abs >= 1024 else (f"{diff_b_abs} B saved" if diff_b > 0 else "no size change")
                    size_msg = f"{size_before} → {size_after} ({diff_lbl})"
                else:
                    size_msg = size_after
                _stored_files_pop(orig_filename)
                return jsonify(create_response(
                    f'UDT "{udt_name}" optimized — {size_msg}. Download will begin shortly.',
                    download={'file_content': res['udt_text'],
                              'file_name':    res.get('download_name', f'{udt_name}.L5X'),
                              'content_type': 'application/xml'}))
            return jsonify(create_response(f"Optimization failed: {res.get('error')}"))

        # ── Manual command (WIP) ──────────────────────────────────────────────
        if action == 'manual_command':
            udt_def  = data.get('udt_definition', {})
            udt_name = (udt_def.get('name') if udt_def else None) or data.get('filename', 'loaded UDT')
            return jsonify(create_response(
                f'Manual command mode — "{udt_name}" is loaded. (Work in progress.)'))

        return error_response('Unsupported action.', 400)

    except Exception as e:
        app.logger.exception('Error in /process_udt_attachment')
        return jsonify(create_response(f'Error: {e}')), 500


# ── Ollama settings ───────────────────────────────────────────────────────────
@app.route('/ollama_models')
def ollama_models():
    try:
        r = requests.get('http://localhost:11434/api/tags', timeout=5)
        names = [m['name'] for m in r.json().get('models', [])]
        return jsonify({'models': names, 'active_model': active_model['name']})
    except Exception as e:
        return jsonify({'models': [], 'active_model': active_model['name'], 'error': str(e)})

@app.route('/set_model', methods=['POST'])
def set_model():
    data  = request.get_json() or {}
    model = data.get('model', '').strip()
    if not model:
        return jsonify({'success': False, 'error': 'No model name.'})
    active_model['name'] = model
    import model_UDTGen; model_UDTGen.MODEL_NAME = model
    return jsonify({'success': True, 'model': model})


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    # Hard-bind to localhost. This app has no auth, no CSRF, and reads/writes
    # engineering source files. Exposing it on a network interface would be a
    # remote attack surface. Override only if you explicitly know what you're
    # doing AND have added auth in front of it.
    HOST = os.environ.get('LLM4UDT_HOST', '127.0.0.1')
    if HOST not in ('127.0.0.1', 'localhost', '::1'):
        raise SystemExit(
            f"Refusing to bind to {HOST!r}: this app has no authentication. "
            "Set LLM4UDT_HOST=127.0.0.1 (or run behind a reverse proxy + auth)."
        )

    # debug=True enables the Werkzeug debugger, which is remote-code-execution
    # by design. Keep it off unless explicitly opted in.
    debug_mode = os.environ.get('FLASK_DEBUG', '0') == '1'

    print(f'Starting dev server on http://{HOST}:5003 (debug={debug_mode})')
    app.run(debug=debug_mode, host=HOST, port=5003)

import sys
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, render_template, request, send_file, Response, jsonify
import os
import shutil
import uuid
import queue
import threading
import time
import glob
from downloader import WebsiteDownloader, zip_directory, get_site_name, discover_pages
from design_system_generator import generate_design_system, DesignSystemGenerationError, is_available as design_system_available
from requirements_doc_generator import generate_requirements_doc, RequirementsDocGenerationError, is_available as requirements_doc_available

app = Flask(__name__)

# Base config
DOWNLOAD_FOLDER = 'downloads'
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

def cleanup_downloads_folder():
    """Remove all files and folders from downloads directory"""
    try:
        for item in os.listdir(DOWNLOAD_FOLDER):
            item_path = os.path.join(DOWNLOAD_FOLDER, item)
            if os.path.isfile(item_path):
                os.remove(item_path)
            elif os.path.isdir(item_path):
                shutil.rmtree(item_path)
        print(f"🧹 Pasta downloads limpa com sucesso")
    except Exception as e:
        print(f"⚠️ Erro ao limpar pasta downloads: {e}")

# Cleanup downloads folder on startup
cleanup_downloads_folder()

# Store for SSE messages per session
message_queues = {}
download_results = {}

def cleanup_abandoned_sessions():
    """Clean up sessions that were never downloaded after 30 minutes"""
    while True:
        time.sleep(300)  # Check every 5 minutes
        current_time = time.time()
        
        sessions_to_remove = []
        for session_id, result in list(download_results.items()):
            if result.get('status') == 'complete' and result.get('created_at'):
                age = current_time - result['created_at']
                # Remove if older than 30 minutes
                if age > 1800:
                    zip_path = result.get('zip_path')
                    if zip_path and os.path.exists(zip_path):
                        try:
                            os.remove(zip_path)
                            print(f"🗑️ Removido arquivo abandonado: {os.path.basename(zip_path)}")
                        except:
                            pass
                    sessions_to_remove.append(session_id)
        
        # Clean up memory
        for session_id in sessions_to_remove:
            if session_id in message_queues:
                del message_queues[session_id]
            if session_id in download_results:
                del download_results[session_id]

# Start cleanup thread
cleanup_thread = threading.Thread(target=cleanup_abandoned_sessions, daemon=True)
cleanup_thread.start()

@app.route('/')
def index():
    return render_template('index.html', advanced_available=design_system_available())

def _parse_login(data):
    """Extrai/valida as credenciais de login opcionais do corpo da requisição.
    Retorna (login_dict_ou_None, error_message_ou_None). Nunca loga usuário/senha."""
    login = data.get('login')
    if not login:
        return None, None
    if not isinstance(login, dict):
        return None, 'login deve ser um objeto {login_url, username, password}'
    login_url = login.get('login_url')
    username = login.get('username')
    password = login.get('password')
    if not login_url or not username or not password:
        return None, 'login requer login_url, username e password'
    if not all(isinstance(v, str) for v in (login_url, username, password)):
        return None, 'login_url, username e password devem ser strings'
    return {'login_url': login_url, 'username': username, 'password': password}, None

@app.route('/discover-pages', methods=['POST'])
def discover_pages_route():
    """Analisa a home e retorna os links internos encontrados, para o usuário escolher quais baixar"""
    data = request.get_json(silent=True) or {}
    url = data.get('url')

    if not url:
        return jsonify({'error': 'URL é obrigatória'}), 400

    login, login_error = _parse_login(data)
    if login_error:
        return jsonify({'error': login_error}), 400

    try:
        pages, truncated, login_ok, login_logs = discover_pages(url, login=login)
        return jsonify({'pages': pages, 'truncated': truncated, 'login_ok': login_ok, 'login_logs': login_logs})
    except Exception as e:
        return jsonify({'error': f'Não foi possível analisar o site: {e}'}), 500

@app.route('/start-download', methods=['POST'])
def start_download():
    """Start download process and return session ID for SSE"""
    data = request.get_json()
    url = data.get('url')
    advanced_mode = bool(data.get('advanced_mode'))
    selected_pages = data.get('selected_pages') or []

    if not url:
        return jsonify({'error': 'URL is required'}), 400

    if not isinstance(selected_pages, list) or not all(isinstance(p, str) for p in selected_pages):
        return jsonify({'error': 'selected_pages deve ser uma lista de URLs'}), 400
    selected_pages = selected_pages[:20]

    login, login_error = _parse_login(data)
    if login_error:
        return jsonify({'error': login_error}), 400

    if advanced_mode and not design_system_available():
        return jsonify({'error': 'Modo avançado indisponível: configure ANTHROPIC_API_KEY no servidor'}), 400

    # Create session
    session_id = str(uuid.uuid4())
    message_queues[session_id] = queue.Queue()
    download_results[session_id] = {'status': 'processing', 'zip_path': None, 'filename': None}

    # Start download in background thread
    thread = threading.Thread(target=process_download, args=(session_id, url, advanced_mode, selected_pages, login))
    thread.daemon = True
    thread.start()

    return jsonify({'session_id': session_id})

def process_download(session_id, url, advanced_mode=False, selected_pages=None, login=None):
    """Background download process"""
    selected_pages = selected_pages or []
    q = message_queues[session_id]
    request_id = session_id
    download_dir = os.path.join(DOWNLOAD_FOLDER, request_id)
    zip_path = os.path.join(DOWNLOAD_FOLDER, f"{request_id}.zip")

    def log_callback(message):
        q.put(message)

    try:
        # Initialize downloader with log callback
        downloader = WebsiteDownloader(url, download_dir, log_callback=log_callback, typed_assets=advanced_mode)

        # Process the site
        if advanced_mode:
            pages = [url] + selected_pages
            q.put(f"📚 Modo avançado ativado — {len(pages)} página(s) selecionada(s)")
            success = downloader.process_multi(pages, login=login)
        else:
            success = downloader.process(login=login)

        if not success:
            q.put("❌ Falha no download")
            download_results[session_id] = {'status': 'error', 'error': 'Failed to download site'}
            return

        if advanced_mode:
            q.put("🎨 Gerando Design System com IA (Claude)...")
            try:
                files = generate_design_system(
                    downloader.get_all_pages_html(),
                    downloader.get_captured_css(),
                    asset_manifest=downloader.get_asset_manifest(),
                    log_callback=log_callback,
                )
                for rel_path, content in files.items():
                    full_path = os.path.join(download_dir, rel_path)
                    os.makedirs(os.path.dirname(full_path), exist_ok=True)
                    with open(full_path, 'w', encoding='utf-8') as f:
                        f.write(content)
                q.put(f"✅ Design System gerado ({len(files)} arquivo(s))")
            except DesignSystemGenerationError as e:
                q.put(f"⚠️ Não foi possível gerar o Design System: {e}")
            except Exception as e:
                q.put(f"⚠️ Erro inesperado ao gerar Design System: {e}")

        if requirements_doc_available():
            q.put("📋 Gerando documento de requisitos (BDD) com IA...")
            try:
                doc = generate_requirements_doc(downloader.get_all_pages_html(), log_callback=log_callback)
                with open(os.path.join(download_dir, 'REQUISITOS.md'), 'w', encoding='utf-8') as f:
                    f.write(doc)
                q.put("✅ Documento de requisitos gerado (REQUISITOS.md)")
            except RequirementsDocGenerationError as e:
                q.put(f"⚠️ Não foi possível gerar o documento de requisitos: {e}")
            except Exception as e:
                q.put(f"⚠️ Erro inesperado ao gerar documento de requisitos: {e}")

        # Generate filename from site name
        site_name = get_site_name(url)
        zip_filename = f"{site_name}.zip"

        q.put("📦 Criando arquivo ZIP...")
        zip_directory(download_dir, zip_path)

        # Cleanup raw files
        shutil.rmtree(download_dir)

        q.put("🎉 Download pronto!")
        download_results[session_id] = {
            'status': 'complete',
            'zip_path': zip_path,
            'filename': zip_filename,
            'created_at': time.time()
        }

    except Exception as e:
        q.put(f"❌ Erro: {str(e)}")
        download_results[session_id] = {'status': 'error', 'error': str(e)}
        
        # Clean up any leftover files
        try:
            if os.path.exists(download_dir):
                shutil.rmtree(download_dir)
            if os.path.exists(zip_path):
                os.remove(zip_path)
        except:
            pass

@app.route('/stream/<session_id>')
def stream(session_id):
    """SSE endpoint for log streaming"""
    def generate():
        if session_id not in message_queues:
            yield f"data: ❌ Sessão não encontrada\n\n"
            return
        
        q = message_queues[session_id]
        
        while True:
            try:
                # Wait for message with timeout
                message = q.get(timeout=60)
                yield f"data: {message}\n\n"
                
                # Check if download is complete
                result = download_results.get(session_id, {})
                if result.get('status') in ['complete', 'error']:
                    # Send final status
                    yield f"event: done\ndata: {result['status']}\n\n"
                    break
                    
            except queue.Empty:
                # Send keepalive
                yield f": keepalive\n\n"
    
    return Response(generate(), mimetype='text/event-stream')

@app.route('/download-file/<session_id>')
def download_file(session_id):
    """Download the generated ZIP file and clean up immediately"""
    result = download_results.get(session_id)
    
    if not result or result['status'] != 'complete':
        return "File not ready", 404
    
    zip_path = result['zip_path']
    filename = result['filename']
    
    if not os.path.exists(zip_path):
        return "File not found", 404
    
    # Send file and clean up immediately after
    try:
        response = send_file(zip_path, as_attachment=True, download_name=filename)
        
        # Clean up in background thread to avoid blocking the response
        def cleanup():
            time.sleep(1)  # Small delay to ensure file transfer completes
            try:
                if os.path.exists(zip_path):
                    os.remove(zip_path)
                    print(f"🗑️ Arquivo ZIP removido: {filename}")
                if session_id in message_queues:
                    del message_queues[session_id]
                if session_id in download_results:
                    del download_results[session_id]
            except Exception as e:
                print(f"⚠️ Erro ao limpar arquivo: {e}")
        
        cleanup_thread = threading.Thread(target=cleanup)
        cleanup_thread.daemon = True
        cleanup_thread.start()
        
        return response
    except Exception as e:
        print(f"❌ Erro ao enviar arquivo: {e}")
        return "Error sending file", 500

if __name__ == '__main__':
    # Development server
    app.run(debug=True, port=5001, threaded=True)
else:
    # Production server (Gunicorn)
    pass

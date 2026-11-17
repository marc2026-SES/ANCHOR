"""Same operator view on ship, coastal MEC app and shore; device UI is retained."""
import hmac
import json
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from .runtime import Runtime


def make_server(runtime, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, value, content_type='application/json; charset=utf-8'):
            payload = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-M2M-RSC', '2000' if status < 300 else '4000')
            self.send_header('X-M2M-RI', self.headers.get('X-M2M-RI', ''))
            self.end_headers()
            self.wfile.write(payload)

        def authorized(self):
            return hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token)

        def do_GET(self):
            if self.path == '/':
                return self.send(200, Path(__file__).with_name('dashboard.html').read_bytes(), 'text/html; charset=utf-8')
            if self.path == '/health':
                fresh = bool(runtime.last_scan and time.time() - runtime.last_scan < max(15, runtime.settings.scan_seconds * 3))
                ready = runtime.ready and fresh
                return self.send(200 if ready else 503, {'ready': ready, 'role': runtime.role})
            if not self.authorized():
                return self.send(401, {'error': 'API token required'})
            try:
                if self.path == '/status':
                    return self.send(200, runtime.status())
                if self.path.startswith('/batches/'):
                    return self.send(200, runtime.batch_status(unquote(self.path[len('/batches/'):])) )
                self.send(404, {'error': 'Not found'})
            except ValueError as exc:
                self.send(400, {'error': str(exc)})
            except Exception as exc:
                self.send(503, {'error': str(exc)})

        def do_POST(self):
            if self.path != '/notify' and not self.authorized():
                return self.send(401, {'error': 'API token required'})
            try:
                size = int(self.headers.get('Content-Length', 0))
                if not 0 < size <= 65536:
                    return self.send(413, {'error': 'Invalid body size'})
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError('Expected object')
                if self.path == '/notify':
                    # Never ingest notification bodies as authoritative measurements.
                    if not body.get('m2m:sgn', {}).get('vrq'):
                        with runtime.lock:
                            runtime.notifications += 1
                        runtime.wake.set()
                    return self.send(200, {})
                if self.path == '/commands':
                    if runtime.role != 'shore':
                        return self.send(405, {'error': 'Commands are issued at shore'})
                    return self.send(201, runtime.submit_command(body))
                if self.path == '/incidents/ack':
                    return self.send(200, runtime.acknowledge(body['incidentId'], body['operator']))
                if self.path == '/batches':
                    name = body['batchId']
                    if not isinstance(name, str) or not 1 <= len(name) <= 80:
                        raise ValueError('Invalid batchId')
                    return self.send(201, runtime.capture_batch(name))
                self.send(404, {'error': 'Not found'})
            except (ValueError, KeyError, TypeError) as exc:
                self.send(400, {'error': str(exc)})
            except Exception as exc:
                self.send(503, {'error': str(exc)})

    cfg = runtime.settings
    if not (cfg.tls_cert and cfg.tls_key):
        raise ValueError('API listeners require TLS certificates')
    server = ThreadingHTTPServer((cfg.bind, cfg.port), Handler)
    from .tls import secure_server
    return secure_server(server, cfg.tls_cert, cfg.tls_key)


def serve(settings):
    token = Path(settings.token_file).read_text().strip()
    if len(token) < 24:
        raise ValueError('Generate a token with anchor --config ... init-token first')
    runtime = Runtime(settings)
    httpd = make_server(runtime, token)
    def ready_loop():
        while not runtime.done.is_set():
            try:
                runtime.local.get(runtime.journal.path('events'))
                runtime.ready = True
                return
            except Exception as exc:
                runtime.error('startup: run bootstrap if resources are missing', exc)
                runtime.done.wait(3)
    threading.Thread(target=ready_loop, daemon=True).start()
    runtime.start()
    def stop(*_):
        runtime.done.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        httpd.serve_forever(poll_interval=0.2)
    finally:
        runtime.stop()
        httpd.server_close()

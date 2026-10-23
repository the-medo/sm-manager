import json
import logging
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .collector import Collector, cleanup_plan

STATIC = Path(__file__).parent / "static"


def handler_for(collector, config):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SMManager"

        def log_message(self, format, *args):
            logging.getLogger("http").debug(format, *args)

        def respond(self, status, payload, content_type="application/json"):
            body = json.dumps(payload, separators=(",", ":")).encode() if content_type == "application/json" else payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            # Host validation blocks DNS rebinding. Never enable wildcard hosts or CORS.
            host = self.headers.get("Host", "").lower()
            allowed = {str(h).lower() for h in config["allowed_hosts"]}
            allowed |= {f"{h}:{config['port']}" for h in list(allowed)}
            if host not in allowed:
                return self.respond(403, {"error": "Host not allowed"})
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                return self.respond(403, {"error": "Cross-site access denied"})
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://{host}", f"https://{host}"}:
                return self.respond(403, {"error": "Origin not allowed"})
            url = urlsplit(self.path)
            if url.path.startswith("/api/"):
                snapshot = collector.get_snapshot()
                if url.path == "/api/health":
                    healthy = bool(snapshot and not snapshot["stale"])
                    return self.respond(200 if healthy else 503, {"status": "ok" if healthy else "warming" if not snapshot else "stale", "last_sample": snapshot["timestamp"] if snapshot else None})
                if not snapshot:
                    return self.respond(503, {"error": "Taking the first sample. Please wait a moment."})
                if url.path == "/api/snapshot":
                    return self.respond(200, snapshot)
                if url.path == "/api/history":
                    query = parse_qs(url.query)
                    try:
                        hours = int(query.get("hours", ["24"])[0])
                        entity = query.get("entity", ["host"])[0]
                        if len(entity) > 100 or not 1 <= hours <= 24:
                            raise ValueError()
                    except ValueError:
                        return self.respond(400, {"error": "Use hours=1..24 and a valid entity"})
                    return self.respond(200, collector.history.query(entity, hours))
                if url.path.startswith("/api/cleanup-plan/"):
                    plan = cleanup_plan(snapshot, url.path.rsplit("/", 1)[-1])
                    return self.respond(200 if plan else 404, plan or {"error": "Worktree not found"})
                return self.respond(404, {"error": "Not found"})
            name = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css", "/favicon.svg": "favicon.svg"}.get(url.path)
            if not name:
                return self.respond(404, {"error": "Not found"})
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            return self.respond(200, (STATIC / name).read_bytes(), mime + ("; charset=utf-8" if mime.startswith("text/") else ""))

    return Handler


def serve(config):
    collector = Collector(config)
    server = ThreadingHTTPServer(("127.0.0.1", config["port"]), handler_for(collector, config))
    thread = threading.Thread(target=collector.run, name="collector", daemon=True)
    thread.start()
    logging.info("SM Manager listening at http://127.0.0.1:%s", config["port"])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        collector.stop_event.set()
        thread.join(timeout=30)
        if not thread.is_alive():
            collector.history.close()

"""Loopback-only editor with authenticated same-origin JSON actions."""
from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
import threading
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import web_service
from .config import runtime_data_dir
from .db import connect

STATIC = Path(__file__).parent / "static"
MAX_BODY = 64 * 1024


class EditorServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, address: tuple[str, int], db_path: Path | None = None):
        if address[0] not in {"127.0.0.1", "localhost"}:
            raise ValueError("Редактор доступен только через 127.0.0.1 или localhost")
        self.db_path = Path(db_path or runtime_data_dir() / "app.sqlite3").resolve()
        db = connect(self.db_path)
        db.close()
        self.token = secrets.token_urlsafe(32)
        self.action_lock = threading.Lock()
        super().__init__(("127.0.0.1", address[1]), EditorHandler)

    def database(self):
        # Schema/migrations run once at startup, never on every HTTP request.
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db


class EditorHandler(BaseHTTPRequestHandler):
    server: EditorServer

    def log_message(self, *_args):
        # Avoid logging request bodies, exported text or credential-bearing URLs.
        pass

    def _send(self, status: int, body: bytes, kind: str = "application/json; charset=utf-8", headers=None):
        self.send_response(status)
        for key, value in {"Content-Type": kind, "Content-Length": str(len(body)), "Cache-Control": "no-store",
                           "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                           "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
                           **(headers or {})}.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, value):
        self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def _host_allowed(self):
        port = self.server.server_port
        return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _authenticated(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            value = cookie["editor_session"].value
            return hmac.compare_digest(value, self.server.token)
        except (KeyError, ValueError, TypeError, CookieError):
            return False

    def do_GET(self):
        if not self._host_allowed():
            return self._json(403, {"error": "Недопустимый Host"})
        path = urlsplit(self.path).path
        if path != "/" and not self._authenticated():
            return self._json(403, {"error": "Откройте редактор с главной страницы"})
        try:
            if path == "/":
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8",
                                  {"Set-Cookie": f"editor_session={self.server.token}; HttpOnly; SameSite=Strict; Path=/"})
            if path in {"/app.js", "/style.css"}:
                return self._send(200, (STATIC / path[1:]).read_bytes(),
                                  "text/javascript; charset=utf-8" if path.endswith("js") else "text/css; charset=utf-8")
            if path == "/api/session":
                return self._json(200, {"csrf": self.server.token})
            if path == "/api/doctor":
                from .diagnostics import run_doctor
                return self._json(200, run_doctor(self.server.db_path))
            db = self.server.database()
            try:
                db.execute("BEGIN")
                if path == "/api/state":
                    return self._json(200, web_service.state(db))
                if path.startswith("/api/drafts/") and path[len("/api/drafts/"):].isdigit():
                    return self._json(200, web_service.draft_detail(db, int(path.split("/")[-1])))
                if path.startswith("/api/export/") and path[len("/api/export/"):].isdigit():
                    format = parse_qs(urlsplit(self.path).query).get("format", ["json"])[0]
                    body, kind, filename = web_service.export_draft(db, int(path.split("/")[-1]), format)
                    return self._send(200, body, kind, {"Content-Disposition": f'attachment; filename="{filename}"'})
                return self._json(404, {"error": "Страница не найдена"})
            finally:
                db.close()
        except (ValueError, KeyError, TypeError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception:
            self._json(500, {"error": "Не удалось прочитать данные. Запустите doctor в терминале."})

    def do_POST(self):
        if not self._host_allowed() or not self._authenticated():
            return self._json(403, {"error": "Недопустимая сессия"})
        origin = self.headers.get("Origin")
        allowed_origins = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
        supplied_token = self.headers.get("X-Editor-CSRF", "")
        valid_token = supplied_token.isascii() and hmac.compare_digest(supplied_token, self.server.token)
        if (origin is not None and origin not in allowed_origins) or not valid_token:
            return self._json(403, {"error": "Недопустимый источник запроса"})
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self._json(415, {"error": "Ожидается application/json"})
        if self.headers.get("Transfer-Encoding"):
            return self._json(400, {"error": "Transfer-Encoding не поддерживается"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                return self._json(413, {"error": "Размер запроса должен быть от 1 до 65536 байт"})
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Ожидается JSON-объект")
            path = urlsplit(self.path).path
            with self.server.action_lock:
                if path == "/api/backup":
                    from .backup import create_backup
                    return self._json(200, create_backup(self.server.db_path))
                if not path.startswith("/api/actions/"):
                    return self._json(404, {"error": "Действие не найдено"})
                db = self.server.database()
                try:
                    result = web_service.perform(db, path[len("/api/actions/"):], data)
                    self._json(200, result)
                finally:
                    db.close()
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception:
            self._json(500, {"error": "Действие не завершено. Обновите карточку и проверьте журнал."})


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False) -> None:
    import webbrowser
    server = EditorServer((host, port))
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"Локальный редактор: {url}\nОстановить: Ctrl+C", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

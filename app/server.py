"""HTTP 服务（仅标准库）：JSON API + 静态页面 + 本地照片。
每个请求独立 SQLite 连接；写冲突由 BEGIN IMMEDIATE + busy_timeout 串行化。
"""
import base64
import json
import os
import re
import secrets
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import queries as Q
from . import services as svc
from .adapter_delivery import MockDeliveryAdapter
from .db import connect

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

_state = {"db": None, "photos": None}
_registry_lock = threading.Lock()


def _adapter(conn):
    codes = [r["expected_code"] for r in conn.execute(
        "SELECT expected_code FROM delivery_note_lines")]
    return MockDeliveryAdapter(codes)


class Handler(BaseHTTPRequestHandler):
    server_version = "CigTrace/1.0"

    # ------------------------------------------------------------ 基础
    def log_message(self, fmt, *args):
        pass  # 静默；错误由响应体体现

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, path, ctype):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise svc.DomainError("bad_json", "请求体不是合法 JSON", 400)

    def _conn(self):
        return connect(_state["db"])

    def _current_user(self, conn):
        token = self.headers.get("X-Auth-Token", "")
        if not token:
            cookie = self.headers.get("Cookie", "")
            m = re.search(r"token=([A-Za-z0-9]+)", cookie)
            token = m.group(1) if m else ""
        if not token:
            return None, None
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token=?", (token,)).fetchone()
        return (dict(row), token) if row else (None, None)

    def _require(self, conn, role=None):
        user, token = self._current_user(conn)
        if not user:
            raise svc.DomainError("unauthorized", "请先登录", 401)
        if role and user["role"] != role:
            raise svc.DomainError("forbidden",
                                  f"需要{('店长' if role=='manager' else '店员')}角色，"
                                  f"当前账号「{user['name']}」是"
                                  f"{'店长' if user['role']=='manager' else '店员'}", 403)
        return user, token

    # ------------------------------------------------------------ 路由
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/" or path == "/index.html":
                return self._send_static(os.path.join(WEB_DIR, "index.html"),
                                         "text/html; charset=utf-8")
            if path.startswith("/static/"):
                name = path.rsplit("/", 1)[-1]
                ctype = {"app.js": "application/javascript; charset=utf-8",
                         "styles.css": "text/css; charset=utf-8"}.get(
                    name, "application/octet-stream")
                return self._send_static(os.path.join(WEB_DIR, name), ctype)
            if path.startswith("/photos/"):
                name = os.path.basename(path)  # 防目录穿越
                return self._send_static(os.path.join(_state["photos"], name),
                                         "image/svg+xml" if name.endswith(".svg")
                                         else "application/octet-stream")
            if path.startswith("/api/"):
                return self._api_get(path, parse_qs(parsed.query))
            self.send_error(404)
        except svc.DomainError as e:
            self._send_json({"ok": False, "error": {"code": e.code,
                                                    "message": str(e)}}, e.status)
        except Exception as e:  # noqa: BLE001
            self._send_json({"ok": False, "error": {"code": "internal",
                                                    "message": str(e)}}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        conn = self._conn()
        try:
            if not path.startswith("/api/"):
                self.send_error(404)
                return
            self._api_post(conn, path)
        except svc.DomainError as e:
            self._send_json({"ok": False, "error": {"code": e.code,
                                                    "message": str(e)}}, e.status)
        except Exception as e:  # noqa: BLE001
            self._send_json({"ok": False, "error": {"code": "internal",
                                                    "message": str(e)}}, 500)
        finally:
            conn.close()

    # ------------------------------------------------------------ GET API
    def _api_get(self, path, query):
        conn = self._conn()
        try:
            user, _ = self._require(conn)
            if path == "/api/me":
                return self._send_json({"ok": True, "user": _public_user(user)})
            if path == "/api/notes":
                rows = conn.execute(
                    "SELECT * FROM delivery_notes WHERE store_id=? ORDER BY delivered_at",
                    (user["store_id"],)).fetchall()
                return self._send_json({"ok": True, "notes": [dict(r) for r in rows]})
            if path == "/api/trace":
                code = (query.get("code", [""])[0] or "").strip()
                if not code:
                    raise svc.DomainError("bad_param", "请输入串码")
                data = Q.trace(conn, code)
                if data is None:
                    raise svc.DomainError("not_found", f"查无串码 {code}", 404)
                if data["package"]["store_id"] != user["store_id"]:
                    raise svc.DomainError("foreign_store",
                                          "该串码不属于本店，仅本店条包可追溯", 403)
                return self._send_json({"ok": True, "data": data})
            if path == "/api/returns":
                return self._send_json(
                    {"ok": True, "returns": Q.returns_queue(conn, user["store_id"])})
            if path == "/api/quarantine":
                return self._send_json(
                    {"ok": True, "packages": Q.quarantine(conn, user["store_id"])})
            if path == "/api/inventory":
                return self._send_json(
                    {"ok": True, "groups": Q.inventory_grouped(conn, user["store_id"])})
            if path == "/api/diff":
                return self._send_json(
                    {"ok": True, "data": Q.inventory_diff(conn, user["store_id"])})
            if path == "/api/recent-scans":
                return self._send_json({"ok": True, "scans": Q.scan_recent(conn)})
            self.send_error(404)
        finally:
            conn.close()

    # ------------------------------------------------------------ POST API
    def _api_post(self, conn, path):
        if path == "/api/login":
            data = self._read_json()
            row = conn.execute("SELECT * FROM users WHERE username=?",
                               (data.get("username", ""),)).fetchone()
            if not row or row["password"] != data.get("password", ""):
                raise svc.DomainError("login_failed", "用户名或密码错误", 401)
            token = secrets.token_hex(16)
            conn.execute("INSERT INTO sessions(token,user_id,created_at) VALUES (?,?,?)",
                         (token, row["id"], datetime.now().isoformat(timespec="seconds")))
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Set-Cookie", f"token={token}; Path=/; HttpOnly; SameSite=Lax")
            body = json.dumps({"ok": True, "token": token,
                               "user": _public_user(dict(row))}, ensure_ascii=False)
            self.send_header("Content-Length", str(len(body.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            return

        user, token = self._require(conn)
        data = self._read_json()

        if path == "/api/logout":
            conn.execute("DELETE FROM sessions WHERE token=?", (token,))
            return self._send_json({"ok": True})

        if path == "/api/receive":
            res = svc.receive_scan(
                conn, note_id=data["note_id"], code=data["code"].strip().upper(),
                actor=user["id"], location=(data.get("location") or "").strip() or None,
                barcode=(data.get("barcode") or "").strip() or None,
                damaged=bool(data.get("damaged")))
            return self._send_json(res, 200 if res["ok"] else 200)

        if path == "/api/sell":
            return self._send_json(svc.sell(
                conn, code=data["code"].strip().upper(), actor=user["id"]))

        if path == "/api/returns":
            photo_path = None
            if data.get("photo_data"):
                photo_path = self._save_photo(data["code"].strip().upper(),
                                              data["photo_data"])
            res = svc.request_return(
                conn, code=data["code"].strip().upper(),
                reason=(data.get("reason") or "").strip(), actor=user["id"],
                photo_path=photo_path)
            return self._send_json(res)

        m = re.fullmatch(r"/api/returns/(\d+)/review", path)
        if m:
            self._require(conn, "manager")
            return self._send_json(svc.review_return(
                conn, return_id=int(m.group(1)), approved=bool(data.get("approved")),
                actor=user["id"], note=data.get("note", "")))

        m = re.fullmatch(r"/api/returns/(\d+)/receipt", path)
        if m:
            self._require(conn, "manager")
            return self._send_json(svc.carrier_receipt(
                conn, return_id=int(m.group(1)), actor=user["id"],
                adapter=_adapter(conn)))

        self.send_error(404)

    def _save_photo(self, code, photo_data):
        """前端以 data:image/...;base64, 提交，解码后存本地。"""
        m = re.fullmatch(r"data:image/(\w+);base64,(.*)", photo_data, re.S)
        ext = m.group(1) if m else "svg"
        raw = base64.b64decode(m.group(2) if m else photo_data)
        os.makedirs(_state["photos"], exist_ok=True)
        name = f"ret-{code}-{secrets.token_hex(4)}.{ext}"
        with open(os.path.join(_state["photos"], name), "wb") as f:
            f.write(raw)
        return os.path.join(_state["photos"], name)


def _public_user(u):
    return {"id": u["id"], "username": u["username"], "name": u["name"],
            "role": u["role"], "role_label": "店长" if u["role"] == "manager" else "店员",
            "store_id": u["store_id"]}


def run(db_path, photos_dir, port=8000):
    _state["db"] = db_path
    _state["photos"] = photos_dir
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"服务已启动：http://127.0.0.1:{port} （Ctrl+C 停止）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()

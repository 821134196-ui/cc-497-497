"""HTTP 服务：JSON API + 五个静态页面。仅依赖 Python 标准库。

启动：python3 -m traceapp.app --db data/app.db --photos data/photos --port 8000
"""
import argparse
import base64
import hashlib
import hmac
import json
import re
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db, domain
from .util import now

STATIC_DIR = Path(__file__).parent / "static"
SECRET = b"cigarette-trace-demo-secret-2026"

STATE = {}  # db_path / photo_dir / conn 在 main 中注入


# ---------------------------------------------------------------- 会话

def sign(value: str) -> str:
    sig = hmac.new(SECRET, value.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{value}.{sig}"


def unsign(token: str):
    if not token or token.count(".") != 1:
        return None
    value, sig = token.split(".", 1)
    if hmac.compare_digest(sig, hmac.new(SECRET, value.encode(),
                                         hashlib.sha256).hexdigest()[:24]):
        return value
    return None


# ---------------------------------------------------------------- 业务封装

def current_user(conn, handler):
    cookie_header = handler.headers.get("Cookie", "")
    cookie = SimpleCookie()
    cookie.load(cookie_header)
    morsel = cookie.get("session")
    if not morsel:
        return None
    uid = unsign(morsel.value)
    if not uid:
        return None
    return conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def save_photo(photo_dir: Path, serial: str, data_b64: str, name: str) -> str:
    photo_dir.mkdir(parents=True, exist_ok=True)
    ext = ".jpg"
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", name or "photo.jpg")
    m = re.search(r"\.(jpe?g|png|webp)$", safe_name.lower())
    if m:
        ext = "." + m.group(1).lstrip(".")
        if ext == ".jpeg":
            ext = ".jpg"
    raw = base64.b64decode(data_b64, validate=True)
    if len(raw) > 5 * 1024 * 1024:
        raise domain.DomainError("照片不能超过 5MB。")
    fname = f"{re.sub(r'[^A-Za-z0-9_-]', '_', serial)}-{now().replace(':','').replace(' ','_')}{ext}"
    path = photo_dir / fname
    path.write_bytes(raw)
    return str(path)


# ---------------------------------------------------------------- HTTP Handler

class Handler(BaseHTTPRequestHandler):
    server_version = "CigTrace/1.0"

    # --- 基础工具 ---
    @property
    def conn(self):
        return STATE["conn"]

    def log_message(self, fmt, *args):
        pass  # 静默；测试与演示更干净

    def _send_json(self, obj, status=200, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, data, extra_headers=None):
        self._send_json({"ok": True, "data": data}, extra_headers=extra_headers)

    def _err(self, message, status=400, code="error"):
        self._send_json({"ok": False, "error": message, "code": code}, status=status)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode())
        except Exception:
            raise ValueError("请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def _require_user(self):
        user = current_user(self.conn, self)
        if user is None:
            raise _HttpError(401, "未登录或会话已失效，请重新登录。", "unauthorized")
        return user

    # --- 路由 ---
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                self._api_get(path, qs)
            elif path.startswith("/photos/"):
                self._serve_photo(path[len("/photos/"):])
            else:
                self._serve_page(path)
        except _HttpError as e:
            self._err(e.message, e.status, e.code)
        except domain.DomainError as e:
            self._err(str(e))
        except domain.AuthError as e:
            self._err(str(e), 403, "forbidden")
        except Exception as e:  # noqa: BLE001
            self._err(f"服务器错误：{e}", 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            self._api_post(path)
        except _HttpError as e:
            self._err(e.message, e.status, e.code)
        except domain.DomainError as e:
            self._err(str(e))
        except domain.AuthError as e:
            self._err(str(e), 403, "forbidden")
        except ValueError as e:
            self._err(str(e))
        except Exception as e:  # noqa: BLE001
            self._err(f"服务器错误：{e}", 500)

    # --- 静态页面 ---
    def _serve_page(self, path):
        if path in ("", "/"):
            path = "/receive.html"
        if not path.startswith("/"):
            path = "/" + path
        file_path = (STATIC_DIR / path.lstrip("/")).resolve()
        if not str(file_path).startswith(str(STATIC_DIR.resolve())) or not file_path.is_file():
            self._err("页面不存在", 404, "not_found")
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
        }.get(file_path.suffix, "application/octet-stream")
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_photo(self, name):
        photo_dir = Path(STATE["photo_dir"]).resolve()
        file_path = (photo_dir / name).resolve()
        if not str(file_path).startswith(str(photo_dir)) or not file_path.is_file():
            self._err("照片不存在", 404, "not_found")
            return
        ctype = {".jpg": "image/jpeg", ".png": "image/png",
                 ".webp": "image/webp"}.get(file_path.suffix, "application/octet-stream")
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --- GET API ---
    def _return_rows(self, conn, store_id, statuses):
        if isinstance(statuses, str):
            statuses = (statuses,)
        marks = ",".join("?" for _ in statuses)
        rows = conn.execute(
            f"""SELECT * FROM return_requests
                WHERE store_id=? AND status IN ({marks}) ORDER BY id DESC""",
            (store_id, *statuses)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            pack = conn.execute("SELECT * FROM packs WHERE serial=?", (r["serial"],)).fetchone()
            d["pack"] = domain._pack_dict(pack)
            out.append(d)
        return out

    def _api_get(self, path, qs):
        conn = self.conn

        if path == "/api/me":
            user = current_user(conn, self)
            self._ok(None if user is None else {
                "id": user["id"], "username": user["username"],
                "display_name": user["display_name"], "role": user["role"],
                "role_text": "店长" if user["role"] == "manager" else "店员",
                "store_id": user["store_id"],
            })
            return

        user = self._require_user()
        store_id = user["store_id"]

        if path == "/api/pending-returns":
            self._ok(domain.pending_returns(conn, store_id))
        elif path == "/api/returns-outbound":
            self._ok(self._return_rows(conn, store_id, "outbound"))
        elif path == "/api/returns-finished":
            self._ok(self._return_rows(conn, store_id, ("closed", "rejected")))
        elif path == "/api/quarantine":
            self._ok(domain.quarantine_list(conn, store_id))
        elif path == "/api/blocked-events":
            self._ok(domain.blocked_events(conn, store_id))
        elif path == "/api/inventory":
            self._ok(domain.inventory_groups(conn, store_id))
        elif path == "/api/stock-diff":
            self._ok(domain.stock_diff_view(conn, store_id))
        elif path == "/api/brands":
            rows = conn.execute("SELECT * FROM brands ORDER BY name").fetchall()
            self._ok([dict(r) for r in rows])
        elif path == "/api/trace":
            serial = (qs.get("serial") or [""])[0].strip()
            if not serial:
                raise _HttpError(400, "请提供串码")
            self._ok(domain.trace_pack(conn, serial))
        elif path == "/api/scan-preview":
            serial = (qs.get("serial") or [""])[0].strip()
            if not serial:
                raise _HttpError(400, "请提供串码")
            pack = conn.execute("SELECT * FROM packs WHERE serial=?", (serial,)).fetchone()
            line = domain._find_line(conn, serial)
            last_ev = conn.execute(
                "SELECT * FROM flow_events WHERE serial=? ORDER BY id DESC LIMIT 1",
                (serial,)).fetchone()
            self._ok({
                "exists": pack is not None,
                "pack": domain._pack_dict(pack),
                "line": dict(line) if line else None,
                "last_event": dict(last_ev) if last_ev else None,
                "belongs_here": bool(line and line["note_store_id"] == store_id),
            })
        elif path == "/api/delivery-lines":
            # 本店配送单中尚未扫码的串码（供收货页演示快速填充）
            rows = conn.execute(
                """SELECT l.serial, l.batch, l.location, l.note_id,
                          b.name AS brand_name, b.spec AS brand_spec, b.barcode
                   FROM delivery_lines l
                   JOIN delivery_notes n ON n.id=l.note_id
                   JOIN brands b ON b.id=l.brand_id
                   WHERE n.store_id=?
                     AND l.serial NOT IN (SELECT serial FROM packs WHERE serial=l.serial)
                   ORDER BY l.note_id, l.serial""",
                (store_id,)).fetchall()
            self._ok([dict(r) for r in rows])
        else:
            raise _HttpError(404, "接口不存在", "not_found")

    # --- POST API ---
    def _api_post(self, path):
        conn = self.conn
        data = self._read_json()

        if path == "/api/login":
            username = (data.get("username") or "").strip()
            user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if not user:
                raise domain.DomainError("账号不存在，演示账号：zhang / wang / zhao / qian")
            self._ok({"id": user["id"], "display_name": user["display_name"],
                      "role": user["role"]},
                     extra_headers=[("Set-Cookie",
                                     f"session={sign(user['id'])}; HttpOnly; Path=/; SameSite=Lax")])
            return

        if path == "/api/logout":
            self._ok({"logged_out": True},
                     extra_headers=[("Set-Cookie",
                                     "session=; HttpOnly; Path=/; Max-Age=0; SameSite=Lax")])
            return

        user = self._require_user()
        uid = user["id"]

        if path == "/api/receive":
            self._ok(domain.scan_receive(
                conn, serial=(data.get("serial") or "").strip(), user_id=uid,
                scanned_barcode=(data.get("barcode") or "").strip(),
                scanned_batch=(data.get("batch") or "").strip(),
                location=(data.get("location") or "").strip(),
                damaged=bool(data.get("damaged"))))
        elif path == "/api/sales":
            self._ok(domain.mark_sold(conn, serial=(data.get("serial") or "").strip(),
                                      user_id=uid))
        elif path == "/api/returns":
            photo_path = None
            if data.get("photo_data"):
                photo_path = save_photo(Path(STATE["photo_dir"]),
                                        (data.get("serial") or "").strip(),
                                        data["photo_data"], data.get("photo_name") or "photo.jpg")
            self._ok(domain.create_return(
                conn, serial=(data.get("serial") or "").strip(), user_id=uid,
                reason=(data.get("reason") or "").strip(),
                detail=(data.get("detail") or "").strip(),
                photo_path=photo_path))
        elif re.fullmatch(r"/api/returns/\d+/review", path):
            rid = int(path.split("/")[3])
            self._ok(domain.review_return(
                conn, request_id=rid, user_id=uid,
                decision=(data.get("decision") or "").strip(),
                note=(data.get("note") or "").strip()))
        elif re.fullmatch(r"/api/returns/\d+/carrier", path):
            rid = int(path.split("/")[3])
            self._ok(domain.call_carrier(conn, request_id=rid, user_id=uid))
        elif path == "/api/anomaly/dispose":
            self._ok(domain.dispose_anomaly(
                conn, serial=(data.get("serial") or "").strip(), user_id=uid,
                action=(data.get("action") or "").strip(),
                note=(data.get("note") or "").strip()))
        elif path == "/api/stock-count":
            self._ok(domain.record_stock_count(
                conn, brand_id=(data.get("brand_id") or "").strip(),
                physical_qty=int(data.get("physical_qty")),
                user_id=uid, note=(data.get("note") or "").strip()))
        else:
            raise _HttpError(404, "接口不存在", "not_found")


class _HttpError(Exception):
    def __init__(self, status, message, code="error"):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


def build_server(db_path: str, photo_dir: str, port: int = 0):
    STATE["conn"] = db.connect(db_path)
    db.init_db(STATE["conn"])
    STATE["photo_dir"] = photo_dir
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return server


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/app.db")
    ap.add_argument("--photos", default="data/photos")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    server = build_server(args.db, args.photos, args.port)
    actual_port = server.server_address[1]
    print(f"卷烟条包追溯系统运行中： http://127.0.0.1:{actual_port}/")
    print("按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()

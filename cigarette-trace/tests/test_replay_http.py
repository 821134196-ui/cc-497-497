"""配送方回执重放幂等测试 + HTTP 接口层集成测试。"""
import json
import urllib.request
import urllib.error

from tests.support import FixtureCase
from traceapp import app as webapp
from traceapp import domain


def _approved_return(conn, ids, serial, detail="破损"):
    domain.scan_receive(conn, serial=serial, user_id=ids["clerk"])
    rid = domain.create_return(conn, serial=serial, user_id=ids["clerk"],
                               reason="damaged", detail=detail)["request_id"]
    domain.review_return(conn, request_id=rid, user_id=ids["mgr"],
                         decision="approved")
    return rid


class ReceiptReplayTest(FixtureCase):
    def test_replay_returns_same_decision_and_txn(self):
        rid = _approved_return(self.conn, self.ids, "RETA")
        first = domain.call_carrier(self.conn, request_id=rid, user_id=self.ids["mgr"])
        second = domain.call_carrier(self.conn, request_id=rid, user_id=self.ids["mgr"])
        third = domain.call_carrier(self.conn, request_id=rid, user_id=self.ids["mgr"])

        self.assertEqual(first["result"], "receipt")
        self.assertEqual(second["result"], "replay")
        self.assertEqual(third["result"], "replay")
        self.assertEqual(first["txn_id"], second["txn_id"])
        self.assertEqual(first["decision"], second["decision"])

        # 回执表只有一行
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM carrier_receipts WHERE request_id=?",
            (rid,)).fetchone()["c"]
        self.assertEqual(n, 1)

        # 条包只有一次出库状态变更，事件中 carrier_receipt 仅一条，replay 有两条
        types = [r["event_type"] for r in self.conn.execute(
            "SELECT event_type FROM flow_events WHERE serial='RETA' ORDER BY id")]
        self.assertEqual(types.count("carrier_receipt"), 1)
        self.assertEqual(types.count("carrier_receipt_replay"), 2)
        self.assertEqual(self.pack_status("RETA")[0], "RETURNED")

    def test_rejected_serial_then_replay_stable(self):
        # 串码以 X 结尾 -> 模拟适配器必判拒收
        self.conn.execute(
            "INSERT INTO delivery_lines(note_id,serial,brand_id,batch,location) "
            "VALUES('N1','ENDEX','B1','BA','A-01')")
        self.conn.commit()
        rid = _approved_return(self.conn, self.ids, "ENDEX")
        first = domain.call_carrier(self.conn, request_id=rid, user_id=self.ids["mgr"])
        second = domain.call_carrier(self.conn, request_id=rid, user_id=self.ids["mgr"])
        self.assertEqual(first["decision"], "rejected")
        self.assertEqual(second["decision"], "rejected")
        self.assertEqual(first["txn_id"], second["txn_id"])
        # 未售条包拒收后回在库，且只回一次
        self.assertEqual(self.pack_status("ENDEX"), ("IN_STOCK", None))
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM carrier_receipts WHERE request_id=?",
            (rid,)).fetchone()["c"]
        self.assertEqual(n, 1)


class HttpIntegrationTest(FixtureCase):
    """启动真实 HTTP 服务（随机端口），验证会话/角色/接口行为。"""

    def setUp(self):
        super().setUp()
        self.server = webapp.build_server(
            self.ids["db_path"], self.ids["photo_dir"], port=0)
        self.port = self.server.server_address[1]
        import threading
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def req(self, method, path, body=None, cookie=None):
        data = None
        headers = {"Content-Type": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
        if cookie:
            headers["Cookie"] = cookie
        r = urllib.request.Request(self.base + path, data=data, headers=headers,
                                   method=method)
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read().decode()), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode()), e.headers

    def login(self, username):
        status, data, headers = self.req("POST", "/api/login", {"username": username})
        self.assertEqual(status, 200)
        return headers["Set-Cookie"].split(";")[0]

    def test_unauthenticated_rejected(self):
        status, data, _ = self.req("GET", "/api/inventory")
        self.assertEqual(status, 401)
        self.assertFalse(data["ok"])

    def test_tampered_cookie_rejected(self):
        status, _, _ = self.req("GET", "/api/inventory",
                                cookie="session=u-mgr.deadbeefdeadbeef")
        self.assertEqual(status, 401)

    def test_full_flow_over_http(self):
        clerk = self.login("clerk")
        mgr = self.login("mgr")

        # 店员收货
        s, d, _ = self.req("POST", "/api/receive", {"serial": "SR001"}, clerk)
        self.assertEqual(s, 200); self.assertTrue(d["ok"])

        # 店员重复扫描被业务拦截
        s, d, _ = self.req("POST", "/api/receive", {"serial": "SR001"}, clerk)
        self.assertEqual(s, 400); self.assertIn("重复扫描", d["error"])

        # 店员不能复核
        s, d, _ = self.req("POST", "/api/returns",
                           {"serial": "SR001", "reason": "damaged"}, clerk)
        self.assertTrue(d["ok"])
        pending = self.req("GET", "/api/pending-returns", cookie=mgr)[1]["data"]
        rid = pending[0]["id"]
        s, d, _ = self.req("POST", f"/api/returns/{rid}/review",
                           {"decision": "approved"}, clerk)
        self.assertEqual(s, 403)

        # 店长复核 + 两次索取回执（第二次重放）
        self.req("POST", f"/api/returns/{rid}/review",
                 {"decision": "approved"}, mgr)
        s, d1, _ = self.req("POST", f"/api/returns/{rid}/carrier", {}, mgr)
        s, d2, _ = self.req("POST", f"/api/returns/{rid}/carrier", {}, mgr)
        self.assertEqual(d1["data"]["result"], "receipt")
        self.assertEqual(d2["data"]["result"], "replay")
        self.assertEqual(d1["data"]["txn_id"], d2["data"]["txn_id"])

        # 页面可访问
        for page in ("/receive.html", "/trace.html", "/returns.html",
                     "/quarantine.html", "/stock.html", "/styles.css", "/app.js"):
            with urllib.request.urlopen(self.base + page) as resp:
                self.assertEqual(resp.status, 200)

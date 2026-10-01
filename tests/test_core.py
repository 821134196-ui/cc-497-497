"""核心约束测试：
test_uniqueness      两人并发扫同一串码，只形成一笔库存（主键 + IMMEDIATE 事务）
test_state_reversal  已售条包不能被直接改回在库；驳回维持已售
test_roles           店员/店长职责分离
test_receipt_replay  配送回执幂等、可重放，绝不重复出库；拒收隔离
test_overdue         超 7 天退货期限被系统直接拒绝
test_inbound_chain   无原始收货链路的条包退货被拦截
test_anomalies       破损 / 串码不符 / 非本店入库 分别处置
"""
import threading
import unittest
from datetime import datetime, timedelta

from app import services as svc
from app.adapter_delivery import MockDeliveryAdapter
from app.db import connect
from tests.helpers import (CLERK, CLERK2, MANAGER, NOTE, cleanup, make_db)

NOW = "2026-09-28T10:00:00"
REGISTRY = ["TST260001", "TST260002", "TST260003", "TST260004", "TST260010"]


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = make_db()

    def tearDown(self):
        cleanup(self.conn, self.path)

    def adapter(self, dispute=None):
        return MockDeliveryAdapter(REGISTRY, dispute=dispute or {})

    def receive(self, code="TST260001", **kw):
        kw.setdefault("now", NOW)
        return svc.receive_scan(self.conn, note_id=NOTE, code=code,
                                actor=CLERK, location="A-01", **kw)

    def sell_then_request(self, code, adapter_dispute=None, approve=True):
        self.receive(code)
        svc.sell(self.conn, code=code, actor=CLERK, now=NOW)
        r = svc.request_return(self.conn, code=code, reason="测试退货",
                               actor=CLERK, now=NOW)
        if approve:
            svc.review_return(self.conn, return_id=r["return_id"], approved=True,
                              actor=MANAGER, now=NOW)
        return r


class TestUniqueness(BaseCase):
    def test_concurrent_same_code_single_stock(self):
        """两人并发扫描同一串码：恰好一笔入库，另一笔被识别为重复扫描。"""
        results = []
        barrier = threading.Barrier(2)

        def worker():
            conn = connect(self.path)
            barrier.wait()  # 最大化竞争：同时进入
            try:
                res = svc.receive_scan(conn, note_id=NOTE, code="TST260002",
                                       actor=CLERK, location="A-01", now=NOW)
                results.append(res["result"])
            except Exception as e:  # 只允许“成功/重复”两种结局
                results.append("EXC:" + type(e).__name__)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(results), ["duplicate", "ok"], results)
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM packages WHERE code='TST260002'").fetchone()["c"]
        self.assertEqual(n, 1, "同一串码只能有一条库存记录")
        n_stock = self.conn.execute(
            "SELECT COUNT(*) c FROM packages WHERE code='TST260002' AND state='in_stock'"
        ).fetchone()["c"]
        self.assertEqual(n_stock, 1)
        dup = self.conn.execute(
            "SELECT COUNT(*) c FROM scan_events WHERE code='TST260002' AND result='duplicate'"
        ).fetchone()["c"]
        self.assertEqual(dup, 1, "重复扫描必须留痕一次")

    def test_concurrent_return_requests(self):
        """两人并发对同一已售条包发起退货：只允许一笔进行中。"""
        self.receive("TST260003")
        svc.sell(self.conn, code="TST260003", actor=CLERK, now=NOW)
        outcomes = []
        barrier = threading.Barrier(2)

        def worker():
            conn = connect(self.path)
            barrier.wait()
            try:
                svc.request_return(conn, code="TST260003", reason="并发退货",
                                   actor=CLERK, now=NOW)
                outcomes.append("ok")
            except svc.DomainError as e:
                outcomes.append(e.code)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(outcomes), ["active_return_exists", "ok"], outcomes)
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM returns WHERE code='TST260003' AND status='pending'"
        ).fetchone()["c"]
        self.assertEqual(n, 1)


class TestStateReversal(BaseCase):
    def test_sold_cannot_be_set_back_to_in_stock(self):
        self.receive("TST260001")
        svc.sell(self.conn, code="TST260001", actor=CLERK, now=NOW)
        with self.assertRaises(svc.DomainError) as cm:
            svc.direct_set_state(self.conn, code="TST260001",
                                 to_state=svc.S_IN_STOCK, actor=MANAGER,
                                 note="尝试把已售改回在库")
        self.assertEqual(cm.exception.code, "illegal_transition")
        st = self.conn.execute(
            "SELECT state FROM packages WHERE code='TST260001'").fetchone()["state"]
        self.assertEqual(st, svc.S_SOLD, "拦截后状态必须仍是已售")

    def test_rejected_return_keeps_sold(self):
        r = self.sell_then_request("TST260001", approve=False)
        svc.review_return(self.conn, return_id=r["return_id"], approved=False,
                          actor=MANAGER, note="不满足条件", now=NOW)
        st = self.conn.execute(
            "SELECT state FROM packages WHERE code='TST260001'").fetchone()["state"]
        self.assertEqual(st, svc.S_SOLD, "店长驳回：维持已售，绝不回到在库")

    def test_quarantined_and_outbound_are_terminal(self):
        # 出库后不可再改
        r = self.sell_then_request("TST260001")
        svc.carrier_receipt(self.conn, return_id=r["return_id"], actor=MANAGER,
                            adapter=self.adapter(), now=NOW)
        with self.assertRaises(svc.DomainError):
            svc.direct_set_state(self.conn, code="TST260001",
                                 to_state=svc.S_IN_STOCK, actor=MANAGER)
        # 隔离态不可销售
        self.receive("TST260004", damaged=True)
        with self.assertRaises(svc.DomainError):
            svc.sell(self.conn, code="TST260004", actor=CLERK, now=NOW)


class TestRoles(BaseCase):
    def test_only_clerk_can_request(self):
        self.receive("TST260001")
        svc.sell(self.conn, code="TST260001", actor=CLERK, now=NOW)
        with self.assertRaises(svc.DomainError) as cm:
            svc.request_return(self.conn, code="TST260001", reason="店长越权发起",
                               actor=MANAGER, now=NOW)
        self.assertEqual(cm.exception.code, "forbidden")

    def test_only_manager_can_review(self):
        self.receive("TST260001")
        svc.sell(self.conn, code="TST260001", actor=CLERK, now=NOW)
        r = svc.request_return(self.conn, code="TST260001", reason="店员自审？",
                               actor=CLERK, now=NOW)
        with self.assertRaises(svc.DomainError) as cm:
            svc.review_return(self.conn, return_id=r["return_id"], approved=True,
                              actor=CLERK, now=NOW)
        self.assertEqual(cm.exception.code, "forbidden")

    def test_only_manager_can_request_receipt(self):
        r = self.sell_then_request("TST260001")
        with self.assertRaises(svc.DomainError) as cm:
            svc.carrier_receipt(self.conn, return_id=r["return_id"], actor=CLERK,
                                adapter=self.adapter(), now=NOW)
        self.assertEqual(cm.exception.code, "forbidden")

    def test_cannot_touch_other_store(self):
        # 二店店员扫本店（S01）配送单 -> 拒绝
        with self.assertRaises(svc.DomainError) as cm:
            svc.receive_scan(self.conn, note_id=NOTE, code="TST260001",
                             actor=CLERK2, location="X", now=NOW)
        self.assertEqual(cm.exception.code, "foreign_note")
        # S01 的店长不能复核属于 S02 的条包（构造一笔 S02 退货）
        conn2 = connect(self.path)
        svc.receive_scan(conn2, note_id="DN-T-2", code="TST260010",
                         actor=CLERK2, location="B-01", now=NOW)
        svc.sell(conn2, code="TST260010", actor=CLERK2, now=NOW)
        r2 = svc.request_return(conn2, code="TST260010", reason="二店退货",
                                actor=CLERK2, now=NOW)
        conn2.close()
        with self.assertRaises(svc.DomainError) as cm:
            svc.review_return(self.conn, return_id=r2["return_id"], approved=True,
                              actor=MANAGER, now=NOW)
        self.assertEqual(cm.exception.code, "forbidden")


class TestReceiptReplay(BaseCase):
    def test_accept_replay_is_idempotent(self):
        r = self.sell_then_request("TST260001")
        first = svc.carrier_receipt(self.conn, return_id=r["return_id"],
                                    actor=MANAGER, adapter=self.adapter(), now=NOW)
        self.assertTrue(first["ok"])
        self.assertFalse(first["replayed"])

        # 重复调用（模拟适配器故障重试 / 人工再次点按）
        second = svc.carrier_receipt(self.conn, return_id=r["return_id"],
                                     actor=MANAGER, adapter=self.adapter(), now=NOW)
        self.assertTrue(second["replayed"], "第二次必须命中回执重放")
        self.assertEqual(first["receipt_no"], second["receipt_no"])

        st = self.conn.execute(
            "SELECT state FROM packages WHERE code='TST260001'").fetchone()["state"]
        self.assertEqual(st, svc.S_OUTBOUND)
        n_receipt = self.conn.execute(
            "SELECT COUNT(*) c FROM receipt_log WHERE return_id=?",
            (r["return_id"],)).fetchone()["c"]
        n_out_flow = self.conn.execute(
            "SELECT COUNT(*) c FROM flows WHERE code='TST260001' AND action='carrier_accept'"
        ).fetchone()["c"]
        self.assertEqual(n_receipt, 1, "回执只能有一条")
        self.assertEqual(n_out_flow, 1, "出库流水只能有一次")

    def test_refused_replay_and_quarantine(self):
        r = self.sell_then_request("TST260004")
        dispute = {"TST260004": "批次与配送登记不符"}
        first = svc.carrier_receipt(self.conn, return_id=r["return_id"],
                                    actor=MANAGER,
                                    adapter=self.adapter(dispute), now=NOW)
        self.assertFalse(first["ok"])
        st = self.conn.execute(
            "SELECT state, anomaly_type FROM packages WHERE code='TST260004'"
        ).fetchone()
        self.assertEqual(st["state"], svc.S_QUARANTINED)
        self.assertEqual(st["anomaly_type"], "delivery_refused")

        second = svc.carrier_receipt(self.conn, return_id=r["return_id"],
                                     actor=MANAGER,
                                     adapter=self.adapter(dispute), now=NOW)
        self.assertTrue(second["replayed"])
        self.assertFalse(second["ok"], "重放必须维持拒收结论")
        n_refuse = self.conn.execute(
            "SELECT COUNT(*) c FROM flows WHERE code='TST260004' AND action='carrier_refuse'"
        ).fetchone()["c"]
        self.assertEqual(n_refuse, 1)

    def test_receipt_requires_approval(self):
        self.receive("TST260001")
        svc.sell(self.conn, code="TST260001", actor=CLERK, now=NOW)
        r = svc.request_return(self.conn, code="TST260001", reason="未复核就要回执",
                               actor=CLERK, now=NOW)
        with self.assertRaises(svc.DomainError) as cm:
            svc.carrier_receipt(self.conn, return_id=r["return_id"], actor=MANAGER,
                                adapter=self.adapter(), now=NOW)
        self.assertEqual(cm.exception.code, "wrong_status")


class TestReturnRules(BaseCase):
    def test_overdue_blocked(self):
        self.receive("TST260001")
        sold = (datetime.fromisoformat(NOW) - timedelta(days=9)).isoformat(timespec="seconds")
        svc.sell(self.conn, code="TST260001", actor=CLERK, now=sold)
        res = svc.request_return(self.conn, code="TST260001", reason="超期退货",
                                 actor=CLERK, now=NOW)
        self.assertFalse(res["ok"])
        self.assertEqual(res["result"], "overdue")
        st = self.conn.execute(
            "SELECT state FROM packages WHERE code='TST260001'").fetchone()["state"]
        self.assertEqual(st, svc.S_SOLD, "超期拒绝后条包仍为已售，不进待审")
        status = self.conn.execute(
            "SELECT status FROM returns WHERE id=?", (res["return_id"],)
        ).fetchone()["status"]
        self.assertEqual(status, "rejected")

    def test_boundary_seven_days_allowed(self):
        self.receive("TST260002")
        sold = (datetime.fromisoformat(NOW) - timedelta(days=7)).isoformat(timespec="seconds")
        svc.sell(self.conn, code="TST260002", actor=CLERK, now=sold)
        res = svc.request_return(self.conn, code="TST260002", reason="第 7 天",
                                 actor=CLERK, now=NOW)
        self.assertTrue(res["ok"], "持有恰好 7 天应当允许")

    def test_inbound_chain_required(self):
        """无正常收货流水（例如手工落库的条包）退货必须被拦截。"""
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute(
            "INSERT INTO packages(code, spec_id, batch, store_id, expected_store, "
            "note_id, state, location, received_at, sold_at) "
            "VALUES ('TST269999','SP01','B9999','S01','S01',?,'sold','柜台',?,?)",
            (NOTE, NOW, NOW))
        self.conn.execute("COMMIT")
        with self.assertRaises(svc.DomainError) as cm:
            svc.request_return(self.conn, code="TST269999", reason="链路缺失",
                               actor=CLERK, now=NOW)
        self.assertEqual(cm.exception.code, "no_inbound_chain")

    def test_only_sold_can_return(self):
        self.receive("TST260001")  # 仍在库
        with self.assertRaises(svc.DomainError) as cm:
            svc.request_return(self.conn, code="TST260001", reason="在库就想退",
                               actor=CLERK, now=NOW)
        self.assertEqual(cm.exception.code, "wrong_state")


class TestAnomalies(BaseCase):
    def test_damaged_quarantined_not_stocked(self):
        res = self.receive("TST260001", damaged=True)
        self.assertEqual(res["result"], "damaged")
        row = self.conn.execute(
            "SELECT state, anomaly_type FROM packages WHERE code='TST260001'").fetchone()
        self.assertEqual(row["state"], svc.S_QUARANTINED)
        self.assertEqual(row["anomaly_type"], "damaged")

    def test_code_mismatch(self):
        res = self.receive("TST260099")  # 计划中不存在
        self.assertEqual(res["result"], "code_mismatch")

    def test_foreign_store(self):
        res = svc.receive_scan(self.conn, note_id=NOTE, code="TST260010",
                               actor=CLERK, location="A-01", now=NOW)
        self.assertEqual(res["result"], "foreign_store")
        row = self.conn.execute(
            "SELECT state, expected_store FROM packages WHERE code='TST260010'").fetchone()
        self.assertEqual(row["state"], svc.S_QUARANTINED)
        self.assertEqual(row["expected_store"], "S02")

    def test_barcode_mismatch(self):
        res = self.receive("TST260001", barcode="6900000009999")
        self.assertEqual(res["result"], "code_mismatch")
        self.assertIn("barcode_expected", res["anomaly_detail"])

    def test_duplicate_after_quarantine(self):
        # 异常码再扫一次也只能是重复，不能借第二次扫描“洗白”入库
        self.receive("TST260001", damaged=True)
        again = self.receive("TST260001")
        self.assertEqual(again["result"], "duplicate")
        row = self.conn.execute(
            "SELECT state FROM packages WHERE code='TST260001'").fetchone()
        self.assertEqual(row["state"], svc.S_QUARANTINED)


if __name__ == "__main__":
    unittest.main(verbosity=2)

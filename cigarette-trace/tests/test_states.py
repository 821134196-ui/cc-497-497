"""状态逆转拦截 + 五类异常处置测试。"""
from tests.support import FixtureCase
from traceapp import db, domain


class AnomalyRoutingTest(FixtureCase):
    def test_normal_receive(self):
        r = domain.scan_receive(self.conn, serial="SR001", user_id=self.ids["clerk"])
        self.assertEqual(r["result"], "received")
        self.assertEqual(self.pack_status("SR001"), (db.STATUS_IN_STOCK, None))

    def test_damaged_quarantined(self):
        r = domain.scan_receive(self.conn, serial="SR001", user_id=self.ids["clerk"],
                                damaged=True)
        self.assertEqual(r["result"], "quarantined")
        self.assertEqual(r["anomaly"], "damaged")
        self.assertEqual(self.pack_status("SR001"),
                         (db.STATUS_QUARANTINED, "damaged"))

    def test_code_mismatch_barcode(self):
        r = domain.scan_receive(self.conn, serial="SR001", user_id=self.ids["clerk"],
                                scanned_barcode="6970000009999")
        self.assertEqual(r["result"], "quarantined")
        self.assertEqual(r["anomaly"], "code_mismatch")

    def test_code_mismatch_batch(self):
        r = domain.scan_receive(self.conn, serial="SR001", user_id=self.ids["clerk"],
                                scanned_batch="WRONG-BATCH")
        self.assertEqual(r["anomaly"], "code_mismatch")

    def test_foreign_store_quarantined(self):
        # S002 配送单的串码，被 S001 店员扫描
        r = domain.scan_receive(self.conn, serial="FOREIGN1",
                                user_id=self.ids["clerk"])
        self.assertEqual(r["anomaly"], "foreign_store")
        # 主档门店仍标记在扫描门店以便隔离可见，但处置时禁止转本店库存
        self.assertEqual(self.pack_status("FOREIGN1"),
                         (db.STATUS_QUARANTINED, "foreign_store"))

    def test_unknown_serial_rejected_no_pack(self):
        with self.assertRaises(domain.DomainError):
            domain.scan_receive(self.conn, serial="GHOST999",
                                user_id=self.ids["clerk"])
        self.assertEqual(self.pack_count("GHOST999"), 0)
        # 拦截事件必须已落库留痕（不能随错误一起回滚）
        ev = self.conn.execute(
            "SELECT event_type FROM flow_events WHERE serial='GHOST999'").fetchall()
        self.assertEqual([r["event_type"] for r in ev], ["receive_reject_unknown"])

    def test_duplicate_block_event_persisted(self):
        domain.scan_receive(self.conn, serial="SRDUP", user_id=self.ids["clerk"])
        with self.assertRaises(domain.DomainError):
            domain.scan_receive(self.conn, serial="SRDUP", user_id=self.ids["clerk"])
        ev = self.conn.execute(
            "SELECT event_type FROM flow_events WHERE serial='SRDUP' ORDER BY id").fetchall()
        self.assertIn("receive_duplicate_blocked", [r["event_type"] for r in ev])

    def test_expired_block_event_persisted(self):
        domain.scan_receive(self.conn, serial="OLD1", user_id=self.ids["clerk"])
        with self.assertRaises(domain.DomainError):
            domain.create_return(self.conn, serial="OLD1",
                                 user_id=self.ids["clerk"], reason="damaged")
        ev = self.conn.execute(
            "SELECT event_type FROM flow_events WHERE serial='OLD1'").fetchall()
        self.assertIn("return_expired_blocked", [r["event_type"] for r in ev])

    def test_expired_return_blocked(self):
        # OLD1 挂在很久以前的配送单上：先正常收货，再发起退货应被拦截
        domain.scan_receive(self.conn, serial="OLD1", user_id=self.ids["clerk"])
        with self.assertRaisesRegex(domain.DomainError, "超过退货期限"):
            domain.create_return(self.conn, serial="OLD1",
                                 user_id=self.ids["clerk"], reason="damaged")
        # 状态保持在库，没有退货申请
        self.assertEqual(self.pack_status("OLD1"), (db.STATUS_IN_STOCK, None))
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM return_requests WHERE serial='OLD1'").fetchone()["c"]
        self.assertEqual(n, 0)

    def test_foreign_pack_cannot_be_released_to_stock(self):
        domain.scan_receive(self.conn, serial="FOREIGN1", user_id=self.ids["clerk"])
        with self.assertRaisesRegex(domain.DomainError, "非本店"):
            domain.dispose_anomaly(self.conn, serial="FOREIGN1",
                                   user_id=self.ids["mgr"], action="to_stock")
        self.assertEqual(self.pack_status("FOREIGN1"),
                         (db.STATUS_QUARANTINED, "foreign_store"))


class StateReversalTest(FixtureCase):
    def _receive_then_sell(self, serial="SOLD1"):
        domain.scan_receive(self.conn, serial=serial, user_id=self.ids["clerk"])
        domain.mark_sold(self.conn, serial=serial, user_id=self.ids["clerk"])
        self.assertEqual(self.pack_status(serial), (db.STATUS_SOLD, None))

    def test_sold_cannot_be_directly_back_to_stock(self):
        self._receive_then_sell()
        # 直接走状态机白名单：SOLD -> IN_STOCK 必须被拦截
        from traceapp.domain import _check_transition
        with self.assertRaisesRegex(domain.DomainError, "状态逆转被拦截"):
            _check_transition(db.STATUS_SOLD, db.STATUS_IN_STOCK)

    def test_sold_return_goes_to_quarantine_not_stock(self):
        self._receive_then_sell()
        r = domain.create_return(self.conn, serial="SOLD1",
                                 user_id=self.ids["clerk"], reason="mismatch",
                                 detail="顾客退回 @reject")
        self.assertIn("request_id", r)
        self.assertEqual(self.pack_status("SOLD1"),
                         (db.STATUS_QUARANTINED, "return_verify"))

        # 店长复核通过 -> RETURNING（仍未回在库）
        rid = r["request_id"]
        domain.review_return(self.conn, request_id=rid, user_id=self.ids["mgr"],
                             decision="approved")
        self.assertEqual(self.pack_status("SOLD1"), (db.STATUS_RETURNING, None))

        # 配送方拒收（detail 含 @reject）：曾售出 -> 保留隔离，绝不回在库
        rcpt = domain.call_carrier(self.conn, request_id=rid, user_id=self.ids["mgr"])
        self.assertEqual(rcpt["decision"], "rejected")
        status, anomaly = self.pack_status("SOLD1")
        self.assertEqual(status, db.STATUS_QUARANTINED)
        self.assertTrue(anomaly)  # 保留隔离标记
        self.assertNotEqual(anomaly, None)

    def test_sold_pack_rejected_review_keeps_quarantine(self):
        self._receive_then_sell()
        r = domain.create_return(self.conn, serial="SOLD1",
                                 user_id=self.ids["clerk"], reason="other")
        domain.review_return(self.conn, request_id=r["request_id"],
                             user_id=self.ids["mgr"], decision="rejected")
        status, anomaly = self.pack_status("SOLD1")
        self.assertEqual(status, db.STATUS_QUARANTINED,
                         "售退条包即使复核驳回也不能回在库")

    def test_manager_cannot_release_sold_pack_to_stock(self):
        self._receive_then_sell()
        r = domain.create_return(self.conn, serial="SOLD1",
                                 user_id=self.ids["clerk"], reason="mismatch")
        with self.assertRaisesRegex(domain.DomainError, "曾售出"):
            domain.dispose_anomaly(self.conn, serial="SOLD1",
                                   user_id=self.ids["mgr"], action="to_stock")

    def test_unsold_rejected_return_can_return_to_stock(self):
        # 未售条包被店长驳回退货：可以回在库
        domain.scan_receive(self.conn, serial="RETA", user_id=self.ids["clerk"])
        r = domain.create_return(self.conn, serial="RETA",
                                 user_id=self.ids["clerk"], reason="damaged")
        domain.review_return(self.conn, request_id=r["request_id"],
                             user_id=self.ids["mgr"], decision="rejected")
        self.assertEqual(self.pack_status("RETA"), (db.STATUS_IN_STOCK, None))

    def test_accepted_receipt_removes_from_store(self):
        domain.scan_receive(self.conn, serial="RETA", user_id=self.ids["clerk"])
        r = domain.create_return(self.conn, serial="RETA",
                                 user_id=self.ids["clerk"], reason="damaged")
        rid = r["request_id"]
        domain.review_return(self.conn, request_id=rid, user_id=self.ids["mgr"],
                             decision="approved")
        rcpt = domain.call_carrier(self.conn, request_id=rid, user_id=self.ids["mgr"])
        self.assertEqual(rcpt["decision"], "accepted")
        self.assertEqual(self.pack_status("RETA"), (db.STATUS_RETURNED, None))

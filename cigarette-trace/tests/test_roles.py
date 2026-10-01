"""角色分离测试：店员发起、店长复核、跨店隔离。"""
from tests.support import FixtureCase
from traceapp import domain


class RoleSeparationTest(FixtureCase):
    def setUp(self):
        super().setUp()
        domain.scan_receive(self.conn, serial="RETA", user_id=self.ids["clerk"])
        self.rid = domain.create_return(
            self.conn, serial="RETA", user_id=self.ids["clerk"],
            reason="damaged", detail="破损")["request_id"]

    def test_clerk_cannot_review(self):
        with self.assertRaises(domain.AuthError):
            domain.review_return(self.conn, request_id=self.rid,
                                 user_id=self.ids["clerk"], decision="approved")

    def test_clerk_cannot_call_carrier(self):
        domain.review_return(self.conn, request_id=self.rid,
                             user_id=self.ids["mgr"], decision="approved")
        with self.assertRaises(domain.AuthError):
            domain.call_carrier(self.conn, request_id=self.rid,
                                user_id=self.ids["clerk"])

    def test_clerk_cannot_dispose_anomaly(self):
        domain.scan_receive(self.conn, serial="SR001", user_id=self.ids["clerk"],
                            damaged=True)
        with self.assertRaises(domain.AuthError):
            domain.dispose_anomaly(self.conn, serial="SR001",
                                   user_id=self.ids["clerk"], action="to_stock")

    def test_clerk_cannot_stock_count(self):
        with self.assertRaises(domain.AuthError):
            domain.record_stock_count(self.conn, brand_id="B1",
                                      physical_qty=1, user_id=self.ids["clerk"])

    def test_manager_cannot_receive(self):
        with self.assertRaises(domain.AuthError):
            domain.scan_receive(self.conn, serial="SR002",
                                user_id=self.ids["mgr"])

    def test_manager_cannot_create_return(self):
        with self.assertRaises(domain.AuthError):
            domain.create_return(self.conn, serial="RETA",
                                 user_id=self.ids["mgr"], reason="damaged")

    def test_cross_store_manager_cannot_review(self):
        with self.assertRaises(domain.AuthError):
            domain.review_return(self.conn, request_id=self.rid,
                                 user_id=self.ids["mgr2"], decision="approved")

    def test_cross_store_clerk_cannot_return(self):
        # RETA 属 S001，S002 店员不可发起
        with self.assertRaises(domain.DomainError):
            domain.create_return(self.conn, serial="RETA",
                                 user_id=self.ids["clerk2"], reason="damaged")

    def test_double_review_blocked(self):
        domain.review_return(self.conn, request_id=self.rid,
                             user_id=self.ids["mgr"], decision="approved")
        with self.assertRaisesRegex(domain.DomainError, "不能重复复核"):
            domain.review_return(self.conn, request_id=self.rid,
                                 user_id=self.ids["mgr"], decision="rejected")

    def test_carrier_before_approval_blocked(self):
        # 新建一个仍 pending 的申请
        domain.scan_receive(self.conn, serial="REJX", user_id=self.ids["clerk"])
        rid2 = domain.create_return(self.conn, serial="REJX",
                                    user_id=self.ids["clerk"],
                                    reason="damaged")["request_id"]
        with self.assertRaisesRegex(domain.DomainError, "尚未复核通过"):
            domain.call_carrier(self.conn, request_id=rid2, user_id=self.ids["mgr"])

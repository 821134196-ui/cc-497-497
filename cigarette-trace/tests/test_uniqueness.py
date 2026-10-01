"""唯一性测试：两人（多线程）并发扫描同一串码只能形成一笔库存。"""
import sqlite3
import threading
from unittest import mock

from tests.support import FixtureCase
from traceapp import db, domain
from traceapp.util import now


class UniquenessTest(FixtureCase):
    def test_sequential_duplicate_scan_blocked(self):
        domain.scan_receive(self.conn, serial="SRDUP", user_id=self.ids["clerk"])
        self.assertEqual(self.pack_count("SRDUP"), 1)
        with self.assertRaises(domain.DomainError):
            domain.scan_receive(self.conn, serial="SRDUP", user_id=self.ids["clerk"])
        self.assertEqual(self.pack_count("SRDUP"), 1)

    def test_concurrent_same_serial_single_stock(self):
        n = 16
        barrier = threading.Barrier(n)
        results = []
        lock = threading.Lock()

        def worker(i):
            import tempfile
            from pathlib import Path
            # 每个线程使用自己的连接（模拟两人各自终端），指向同一数据库文件
            conn = db.connect(self.ids["db_path"])
            barrier.wait()  # 尽量同时撞击
            try:
                domain.scan_receive(conn, serial="SR001",
                                    user_id=self.ids["clerk"], location=f"L-{i}")
                with lock:
                    results.append(("ok", i))
            except domain.DomainError as e:
                with lock:
                    results.append(("blocked", i))
            finally:
                conn.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        oks = [r for r in results if r[0] == "ok"]
        blocked = [r for r in results if r[0] == "blocked"]
        self.assertEqual(len(oks), 1, f"应有且仅有一笔成功，实际 {len(oks)}")
        self.assertEqual(len(blocked), n - 1)
        self.assertEqual(self.pack_count("SR001"), 1,
                         "packs 表中同串码必须只有一行")
        row = self.conn.execute("SELECT * FROM packs WHERE serial='SR001'").fetchone()
        self.assertEqual(row["status"], db.STATUS_IN_STOCK)

    def test_database_unique_constraint_is_final_guard(self):
        """绕过领域层直接并发插入时，UNIQUE(serial) 仍然兜底。"""
        now_s = now()
        errors = []

        def insert():
            try:
                with db.write_tx(self.conn) as c:
                    c.execute(
                        "INSERT INTO packs(serial,brand_id,barcode,batch,location,"
                        "store_id,note_id,status,received_at,created_at) "
                        "VALUES('SR002','B1','x','BA','A-01','S001','N1','IN_STOCK',?,?)",
                        (now_s, now_s))
            except sqlite3.IntegrityError:
                errors.append("integrity")

        t1 = threading.Thread(target=insert)
        t2 = threading.Thread(target=insert)
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(self.pack_count("SR002"), 1)
        self.assertEqual(len(errors), 1)

    def test_concurrent_different_serials_both_succeed(self):
        barrier = threading.Barrier(2)
        out = {}

        def worker(serial):
            conn = db.connect(self.ids["db_path"])
            barrier.wait()
            try:
                domain.scan_receive(conn, serial=serial, user_id=self.ids["clerk"])
                out[serial] = "ok"
            except Exception as e:  # noqa: BLE001
                out[serial] = str(e)
            finally:
                conn.close()

        t1 = threading.Thread(target=worker, args=("SR002",))
        t2 = threading.Thread(target=worker, args=("SR003",))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(out, {"SR002": "ok", "SR003": "ok"})
        self.assertEqual(self.pack_count("SR002"), 1)
        self.assertEqual(self.pack_count("SR003"), 1)

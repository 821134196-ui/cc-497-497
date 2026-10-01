"""测试夹具：在临时目录构建最小但完整的演示数据。"""
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from traceapp import db
from traceapp.util import today, now


def build_fixture(window_days=30):
    """返回 (conn, tmpdir, ids)。每个用例独立临时库。"""
    tmp = tempfile.mkdtemp(prefix="cigtrace-test-")
    db_path = str(Path(tmp) / "test.db")
    photo_dir = str(Path(tmp) / "photos")
    conn = db.connect(db_path)
    db.init_db(conn)

    c = conn
    c.execute("INSERT INTO stores VALUES('S001','测试甲店')")
    c.execute("INSERT INTO stores VALUES('S002','测试乙店')")
    c.execute("INSERT INTO store_settings VALUES('S001',?)", (window_days,))
    c.execute("INSERT INTO store_settings VALUES('S002',?)", (window_days,))
    c.execute("INSERT INTO users VALUES('u-clerk','clerk','店员甲','clerk','S001')")
    c.execute("INSERT INTO users VALUES('u-mgr','mgr','店长甲','manager','S001')")
    c.execute("INSERT INTO users VALUES('u-clerk2','clerk2','店员乙','clerk','S002')")
    c.execute("INSERT INTO users VALUES('u-mgr2','mgr2','店长乙','manager','S002')")
    c.execute("INSERT INTO brands VALUES('B1','鹤鸣','硬盒','6970000001017')")
    c.execute("INSERT INTO brands VALUES('B2','星河','软盒','6970000002021')")

    fresh = (today() - timedelta(days=2)).strftime("%Y-%m-%d") + " 09:00:00"
    old = (today() - timedelta(days=window_days + 100)).strftime("%Y-%m-%d") + " 09:00:00"
    c.execute("INSERT INTO delivery_notes VALUES('N1','S001','云鹤物流',?)", (fresh,))
    c.execute("INSERT INTO delivery_notes VALUES('NOLD','S001','云鹤物流',?)", (old,))
    c.execute("INSERT INTO delivery_notes VALUES('N2','S002','云鹤物流',?)", (fresh,))

    # S001 待收串码
    for serial in ["SR001", "SR002", "SR003", "SRDUP", "SOLD1", "RETA", "REJX"]:
        c.execute("INSERT INTO delivery_lines(note_id,serial,brand_id,batch,location) "
                  "VALUES('N1',?,'B1','BA','A-01')", (serial,))
    # 老批次串码：仅挂超期配送单
    c.execute("INSERT INTO delivery_lines(note_id,serial,brand_id,batch,location) "
              "VALUES('NOLD','OLD1','B1','BOLD','A-09')")
    # S002 的串码（非本店演练）
    c.execute("INSERT INTO delivery_lines(note_id,serial,brand_id,batch,location) "
              "VALUES('N2','FOREIGN1','B2','BB','B-02')")
    conn.commit()

    ids = {
        "db_path": db_path, "photo_dir": photo_dir,
        "clerk": "u-clerk", "mgr": "u-mgr",
        "clerk2": "u-clerk2", "mgr2": "u-mgr2",
        "store1": "S001", "store2": "S002",
    }
    return conn, tmp, ids


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.conn, self.tmp, self.ids = build_fixture()

    def tearDown(self):
        self.conn.close()

    def pack_status(self, serial):
        row = self.conn.execute("SELECT status, anomaly FROM packs WHERE serial=?",
                                (serial,)).fetchone()
        return None if row is None else (row["status"], row["anomaly"])

    def pack_count(self, serial):
        return self.conn.execute("SELECT COUNT(*) c FROM packs WHERE serial=?",
                                 (serial,)).fetchone()["c"]

"""测试公共夹具：内存级临时 SQLite + 最小主数据。"""
import os
import tempfile

from app.db import connect, init_db

CLERK = "u_clerk"
MANAGER = "u_mgr"
CLERK2 = "u_clerk2"
STORE = "S01"
STORE2 = "S02"
NOTE = "DN-T-1"


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    init_db(path)
    conn = connect(path)
    conn.executescript(f"""
    INSERT INTO stores(id,name) VALUES ('{STORE}','测试一店'),('{STORE2}','测试二店');
    INSERT INTO users(id,username,password,name,role,store_id) VALUES
      ('{CLERK}','clerk','x','店员甲','clerk','{STORE}'),
      ('{MANAGER}','mgr','x','店长甲','manager','{STORE}'),
      ('{CLERK2}','clerk2','x','店员乙','clerk','{STORE2}');
    INSERT INTO specs(id,brand,spec_name,barcode,code_prefix) VALUES
      ('SP01','虚构山','软白','6900000000011','TST'),
      ('SP02','虚构海','蓝星','6900000000028','TSX');
    INSERT INTO delivery_notes(id,store_id,carrier,delivered_at)
      VALUES ('{NOTE}','{STORE}','模拟物流','2026-09-20T09:00:00');
    INSERT INTO delivery_note_lines(note_id,expected_code,spec_id) VALUES
      ('{NOTE}','TST260001','SP01'),('{NOTE}','TST260002','SP01'),
      ('{NOTE}','TST260003','SP01'),('{NOTE}','TST260004','SP01');
    INSERT INTO delivery_notes(id,store_id,carrier,delivered_at)
      VALUES ('DN-T-2','{STORE2}','模拟物流','2026-09-20T09:00:00');
    INSERT INTO delivery_note_lines(note_id,expected_code,spec_id) VALUES
      ('DN-T-2','TST260010','SP01');
    """)
    return conn, path


def cleanup(conn, path):
    conn.close()
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            os.remove(p)

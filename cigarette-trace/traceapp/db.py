"""SQLite 数据层：建表、连接管理、全局写锁。

每次条包流转（收货/隔离/退货申请/复核/回执/处置/库存调整/销售）
都在 flow_events 中留痕，保证可追溯。
"""
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;

-- 门店
CREATE TABLE IF NOT EXISTS stores (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL
);

-- 用户（店员 / 店长）
CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    username    TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('clerk','manager')),
    store_id    TEXT NOT NULL REFERENCES stores(id)
);

-- 虚构品牌规格
CREATE TABLE IF NOT EXISTS brands (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,            -- 虚构品牌名
    spec        TEXT NOT NULL,            -- 规格
    barcode     TEXT NOT NULL             -- 商品条码（规格级，非条包串码）
);

-- 配送单
CREATE TABLE IF NOT EXISTS delivery_notes (
    id          TEXT PRIMARY KEY,
    store_id    TEXT NOT NULL REFERENCES stores(id),
    carrier     TEXT NOT NULL,
    delivered_at TEXT NOT NULL
);

-- 配送单明细：计划配送的串码（核对非本店入库用）
CREATE TABLE IF NOT EXISTS delivery_lines (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id     TEXT NOT NULL REFERENCES delivery_notes(id),
    serial      TEXT NOT NULL,
    brand_id    TEXT NOT NULL REFERENCES brands(id),
    batch       TEXT NOT NULL,
    location    TEXT NOT NULL,
    UNIQUE(note_id, serial)
);

-- 退货期限（按配送单签收日期 +N 天）
CREATE TABLE IF NOT EXISTS store_settings (
    store_id        TEXT PRIMARY KEY REFERENCES stores(id),
    return_window_days INTEGER NOT NULL DEFAULT 30
);

-- 条包主档。serial 全库唯一，是并发扫描唯一性的最终防线。
CREATE TABLE IF NOT EXISTS packs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    serial      TEXT NOT NULL UNIQUE,
    brand_id    TEXT REFERENCES brands(id),
    barcode     TEXT,
    batch       TEXT,
    location    TEXT,                     -- 最近一次流转位置
    store_id    TEXT REFERENCES stores(id),
    note_id     TEXT REFERENCES delivery_notes(id),
    status      TEXT NOT NULL,            -- 见 STATUS_*
    anomaly     TEXT,                     -- 隔离原因，NULL=正常
    received_at TEXT,
    sold_at     TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_packs_status ON packs(status);
CREATE INDEX IF NOT EXISTS idx_packs_brand ON packs(brand_id);

-- 退货申请
CREATE TABLE IF NOT EXISTS return_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    serial      TEXT NOT NULL,
    store_id    TEXT NOT NULL,
    reason      TEXT NOT NULL,            -- damaged / mismatch / other
    detail      TEXT DEFAULT '',
    photo_path  TEXT,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','approved','rejected','outbound','closed')),
    created_by  TEXT NOT NULL REFERENCES users(id),
    created_at  TEXT NOT NULL,
    reviewed_by TEXT REFERENCES users(id),
    reviewed_at TEXT,
    review_note TEXT DEFAULT '',
    carrier_txn TEXT,                     -- 配送方回执流水号
    receipt_at  TEXT,
    receipt_decision TEXT                 -- accepted / rejected
);
CREATE INDEX IF NOT EXISTS idx_rr_status ON return_requests(status);

-- 配送方回执（重放时以 txn_id 去重）
CREATE TABLE IF NOT EXISTS carrier_receipts (
    txn_id      TEXT PRIMARY KEY,
    request_id  INTEGER NOT NULL REFERENCES return_requests(id),
    serial      TEXT NOT NULL,
    decision    TEXT NOT NULL,            -- accepted / rejected
    message     TEXT DEFAULT '',
    received_at TEXT NOT NULL
);

-- 库存差异快照
CREATE TABLE IF NOT EXISTS stock_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id    TEXT NOT NULL,
    brand_id    TEXT NOT NULL,
    system_qty  INTEGER NOT NULL,
    physical_qty INTEGER NOT NULL,
    diff        INTEGER NOT NULL,
    note        TEXT DEFAULT '',
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- 流转事件（append-only 审计链）
CREATE TABLE IF NOT EXISTS flow_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    serial      TEXT NOT NULL,
    store_id    TEXT,
    event_type  TEXT NOT NULL,
    from_status TEXT,
    to_status   TEXT,
    location    TEXT,
    actor       TEXT,
    detail      TEXT DEFAULT '',
    ref_id      TEXT,                      -- 关联单号（退货申请/回执）
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_flow_serial ON flow_events(serial, id);
CREATE INDEX IF NOT EXISTS idx_flow_store ON flow_events(store_id, id);
"""

# ---- 条包状态 ----
STATUS_IN_STOCK = "IN_STOCK"          # 在库
STATUS_SOLD = "SOLD"                  # 已售
STATUS_QUARANTINED = "QUARANTINED"    # 异常隔离
STATUS_RETURNING = "RETURNING"        # 退货出库在途（已复核，待回执）
STATUS_RETURNED = "RETURNED"          # 配送方已受理出库
STATUS_RECV_REJECTED = "RECV_REJECTED"  # 收货拒收（从未入库）

# 允许的状态逆转（白名单）。关键：SOLD 不可直接回到 IN_STOCK。
ALLOWED_TRANSITIONS = {
    STATUS_IN_STOCK:      {STATUS_SOLD, STATUS_QUARANTINED},
    STATUS_SOLD:          {STATUS_QUARANTINED},   # 售退先入隔离核对，绝不直接回在库
    STATUS_QUARANTINED:   {STATUS_IN_STOCK, STATUS_RETURNING, STATUS_RECV_REJECTED},
    STATUS_RETURNING:     {STATUS_RETURNED, STATUS_IN_STOCK},  # 拒收可在库，受理则出库
    STATUS_RETURNED:      set(),
    STATUS_RECV_REJECTED: set(),
}


_write_lock = threading.RLock()


def get_lock():
    """全局写锁：同一进程内把读-校验-写串行化。"""
    return _write_lock


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: sqlite3.Connection):
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def write_tx(conn: sqlite3.Connection):
    """序列化写事务：应用锁 + BEGIN IMMEDIATE，双保险防并发双入库。"""
    with _write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def log_event(conn, serial, event_type, actor, *, from_status=None, to_status=None,
              location=None, detail="", ref_id=None, store_id=None, ts=None):
    from .util import now
    conn.execute(
        """INSERT INTO flow_events
           (serial, store_id, event_type, from_status, to_status, location, actor, detail, ref_id, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (serial, store_id, event_type, from_status, to_status, location, actor,
         detail, str(ref_id) if ref_id is not None else None, ts or now()),
    )

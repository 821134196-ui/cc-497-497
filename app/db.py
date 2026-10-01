"""SQLite 连接与建表脚本。所有写操作在 BEGIN IMMEDIATE 事务中完成，
packages.code 主键 + 活跃退货部分唯一索引共同保证并发下的唯一性。"""
import os
import sqlite3

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS stores (
    id      TEXT PRIMARY KEY,
    name    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id        TEXT PRIMARY KEY,
    username  TEXT NOT NULL UNIQUE,
    password  TEXT NOT NULL,              -- 演示用明文，勿用于生产
    name      TEXT NOT NULL,
    role      TEXT NOT NULL CHECK (role IN ('clerk','manager')),
    store_id  TEXT NOT NULL REFERENCES stores(id)
);

CREATE TABLE IF NOT EXISTS specs (
    id         TEXT PRIMARY KEY,           -- 品牌规格编号（虚构）
    brand      TEXT NOT NULL,              -- 虚构品牌
    spec_name  TEXT NOT NULL,              -- 规格
    barcode    TEXT NOT NULL UNIQUE,       -- 盒码/条码（虚构 13 位）
    code_prefix TEXT NOT NULL UNIQUE       -- 条包串码前 3 位
);

CREATE TABLE IF NOT EXISTS delivery_notes (
    id           TEXT PRIMARY KEY,         -- 配送单号
    store_id     TEXT NOT NULL REFERENCES stores(id),
    carrier      TEXT NOT NULL,            -- 配送方
    delivered_at TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'delivered'
);

CREATE TABLE IF NOT EXISTS delivery_note_lines (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id       TEXT NOT NULL REFERENCES delivery_notes(id),
    expected_code TEXT NOT NULL UNIQUE,    -- 本单应到条包串码
    spec_id       TEXT NOT NULL REFERENCES specs(id)
);

CREATE TABLE IF NOT EXISTS packages (
    code           TEXT PRIMARY KEY,       -- 完整条包串码（全局唯一）
    spec_id        TEXT REFERENCES specs(id),
    batch          TEXT,                   -- 批次
    store_id       TEXT NOT NULL,          -- 实际扫描所在门店
    expected_store TEXT,                   -- 串码归属门店（非本店入库时不同）
    note_id        TEXT,                   -- 实际匹配到的配送单
    state          TEXT NOT NULL,          -- 见 services.STATES
    location       TEXT,                   -- 最近一次流转位置/库位
    received_at    TEXT,
    sold_at        TEXT,
    anomaly_type   TEXT,                   -- damaged/code_mismatch/foreign_store/delivery_refused
    anomaly_detail TEXT,                   -- JSON：预期 vs 实际
    resolved       INTEGER NOT NULL DEFAULT 0,
    photo_path     TEXT
);

CREATE TABLE IF NOT EXISTS flows (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT NOT NULL,
    ts         TEXT NOT NULL,
    action     TEXT NOT NULL,              -- 每次流转动作
    from_state TEXT,
    to_state   TEXT,
    actor      TEXT,
    location   TEXT,
    note       TEXT,
    ref_type   TEXT,
    ref_id     TEXT,
    photo_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_flows_code ON flows(code, id);

-- 每次扫码尝试都落库（含重复/异常被拒的尝试）
CREATE TABLE IF NOT EXISTS scan_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    code    TEXT NOT NULL,
    context TEXT NOT NULL,                 -- receive/sell/return...
    result  TEXT NOT NULL,                 -- ok/duplicate/damaged/code_mismatch/foreign_store...
    reason  TEXT,
    actor   TEXT,
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS idx_scan_ts ON scan_events(id);

CREATE TABLE IF NOT EXISTS returns (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    code             TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN
                        ('pending','approved','rejected','outbound','refused')),
    reason           TEXT,
    clerk_id         TEXT NOT NULL,
    manager_id       TEXT,
    requested_at     TEXT NOT NULL,
    reviewed_at      TEXT,
    sold_at_snapshot TEXT,
    days_held        INTEGER,
    review_note      TEXT,
    photo_path       TEXT
);
-- 同一条包不允许同时存在两笔进行中的退货（并发申请的数据库级兜底）
CREATE UNIQUE INDEX IF NOT EXISTS ux_returns_active
    ON returns(code) WHERE status IN ('pending','approved');

-- 配送回执：一笔退货至多一条回执，重放直接读回原结果
CREATE TABLE IF NOT EXISTS receipt_log (
    return_id   INTEGER PRIMARY KEY REFERENCES returns(id),
    receipt_no  TEXT NOT NULL,
    accepted    INTEGER NOT NULL,
    reason      TEXT,
    payload     TEXT,
    synced_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);
"""


def connect(path):
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()

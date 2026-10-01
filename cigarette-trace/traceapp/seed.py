"""演示数据导入：虚构品牌与编号，包含正常串码和全部异常场景串码。

用法：python3 -m traceapp.seed --db data/app.db
"""
import argparse
import base64
from pathlib import Path

from . import db
from .util import now

# 1x1 灰色 PNG（占位照片，仅为本地文件演示）
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

# ---- 虚构品牌规格 ----
BRANDS = [
    ("B-HM", "鹤鸣", "84mm 硬盒·20支", "6970000001017"),
    ("B-XH", "星河", "84mm 软盒·20支", "6970000002021"),
    ("B-QL", "青岚", "97mm 细支·20支", "6970000003038"),
]

STORES = [
    ("S001", "梧桐路直营店"),
    ("S002", "临江路加盟店"),
]

USERS = [
    # id, 登录名, 显示名, 角色, 门店
    ("u-zhang", "zhang", "张小曼（店员）", "clerk", "S001"),
    ("u-wang", "wang", "王建国（店长）", "manager", "S001"),
    ("u-zhao", "zhao", "赵丽（店员）", "clerk", "S002"),
    ("u-qian", "qian", "孙磊（店长）", "manager", "S002"),
]

NOTES = [
    # id, 门店, 配送方, 签收时间
    ("N-2609-01", "S001", "云鹤物流", "2026-09-01 09:12:00"),
    ("N-2608-01", "S001", "云鹤物流", "2026-08-01 10:05:00"),   # 已过 30 天退货期限
    ("N-2609-02", "S002", "云鹤物流", "2026-09-05 14:40:00"),
]

# 配送单明细 (note_id, serial, brand_id, batch, location)
LINES = [
    # --- S001 正常批次 ---
    ("N-2609-01", "HM260901001", "B-HM", "B2608A", "A-01-01"),
    ("N-2609-01", "HM260901002", "B-HM", "B2608A", "A-01-01"),
    ("N-2609-01", "HM260901003", "B-HM", "B2608A", "A-01-02"),
    ("N-2609-01", "HM260901004", "B-HM", "B2608A", "A-01-02"),  # 破损
    ("N-2609-01", "HM260901DUP", "B-HM", "B2608A", "A-01-03"),  # 重复扫描目标
    ("N-2609-01", "HM260901008", "B-HM", "B2608A", "A-01-03"),  # 已出库闭环
    ("N-2609-01", "XH260901001", "B-XH", "X2608C", "B-02-01"),
    ("N-2609-01", "XH260901002", "B-XH", "X2608C", "B-02-01"),
    ("N-2609-01", "XH260901003", "B-XH", "X2608C", "B-02-02"),
    ("N-2609-01", "XH260901004", "B-XH", "X2608C", "B-02-02"),  # 串码不符
    ("N-2609-01", "XH260901005", "B-XH", "X2608C", "B-02-03"),  # 已售
    ("N-2609-01", "XH260901006", "B-XH", "X2608C", "B-02-03"),  # 退货在途（未售）
    ("N-2609-01", "QL260901001", "B-QL", "Q2608E", "C-03-01"),
    ("N-2609-01", "QL260901002", "B-QL", "Q2608E", "C-03-01"),
    ("N-2609-01", "QL260901003", "B-QL", "Q2608E", "C-03-02"),  # 退货待审
    ("N-2609-01", "QL2609010X", "B-QL", "Q2608E", "C-03-02"),  # 已售退货在途，X 结尾→配送方拒收
    # --- 尚未扫码收货（收货页正常/异常实时演练） ---
    ("N-2609-01", "HM260901005", "B-HM", "B2608A", "A-01-03"),
    ("N-2609-01", "HM260901006", "B-HM", "B2608A", "A-01-04"),
    ("N-2609-01", "XH260901007", "B-XH", "X2608C", "B-02-04"),  # 可改扫条码触发串码不符
    # --- S001 老批次（超期） ---
    ("N-2608-01", "HM260801001", "B-HM", "B2607S", "A-04-01"),
    # --- S002 批次（其中 QL260902001 被拿到 S001 扫→非本店入库） ---
    ("N-2609-02", "HM260902001", "B-HM", "B2608A", "A-11-01"),
    ("N-2609-02", "QL260902001", "B-QL", "Q2608E", "C-12-01"),
]

BRAND_BY_ID = {b[0]: b for b in BRANDS}


def seed(db_path: str, photo_dir: str):
    photo_path = Path(photo_dir)
    photo_path.mkdir(parents=True, exist_ok=True)

    conn = db.connect(db_path)
    db.init_db(conn)

    conn.executescript("""
        DELETE FROM flow_events;
        DELETE FROM carrier_receipts;
        DELETE FROM return_requests;
        DELETE FROM stock_snapshots;
        DELETE FROM packs;
        DELETE FROM delivery_lines;
        DELETE FROM delivery_notes;
        DELETE FROM brands;
        DELETE FROM users;
        DELETE FROM stores;
        DELETE FROM store_settings;
        DELETE FROM sqlite_sequence;
    """)

    c = conn
    for sid, name in STORES:
        c.execute("INSERT INTO stores(id,name) VALUES(?,?)", (sid, name))
        c.execute("INSERT INTO store_settings(store_id,return_window_days) VALUES(?,30)", (sid,))
    for uid, uname, dname, role, sid in USERS:
        c.execute("INSERT INTO users(id,username,display_name,role,store_id) VALUES(?,?,?,?,?)",
                  (uid, uname, dname, role, sid))
    for bid, name, spec, barcode in BRANDS:
        c.execute("INSERT INTO brands(id,name,spec,barcode) VALUES(?,?,?,?)",
                  (bid, name, spec, barcode))
    for nid, sid, carrier, dt in NOTES:
        c.execute("INSERT INTO delivery_notes(id,store_id,carrier,delivered_at) VALUES(?,?,?,?)",
                  (nid, sid, carrier, dt))
    for nid, serial, bid, batch, loc in LINES:
        c.execute("INSERT INTO delivery_lines(note_id,serial,brand_id,batch,location) VALUES(?,?,?,?,?)",
                  (nid, serial, bid, batch, loc))
    conn.commit()

    # ---- 占位照片 ----
    photo_files = {}
    for serial in ("QL260901003", "QL2609010X", "HM260901008"):
        p = photo_path / f"{serial}-seed.png"
        p.write_bytes(_PNG)
        photo_files[serial] = str(p)

    def add_pack(serial, store_id, status, *, anomaly=None, barcode=None,
                 received_at="2026-09-01 09:15:00", sold_at=None, loc_override=None):
        line = next(l for l in LINES if l[1] == serial)
        _, _, bid, batch, location = line
        location = loc_override or location
        c.execute(
            """INSERT INTO packs(serial,brand_id,barcode,batch,location,store_id,note_id,
                                 status,anomaly,received_at,sold_at,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (serial, bid, barcode or BRAND_BY_ID[bid][3], batch, location,
             store_id, line[0], status, anomaly, received_at, sold_at, now()))

    def ev(serial, etype, actor, **kw):
        db.log_event(c, serial, etype, actor, **kw)

    # ===== 正常在库（S001） =====
    normal = ["HM260901001", "HM260901002", "HM260901003", "HM260901DUP",
              "XH260901001", "XH260901002", "XH260901003",
              "QL260901001", "QL260901002",
              "HM260801001"]  # 老批次但仍在库（退货将超期拦截）
    for s in normal:
        recv = "2026-08-01 10:20:00" if s.startswith("HM2608") else "2026-09-01 09:15:00"
        add_pack(s, "S001", "IN_STOCK", received_at=recv)
        ev(s, "receive", "张小曼（店员）", to_status="IN_STOCK",
           location=next(l for l in LINES if l[1] == s)[4],
           detail=f"按配送单 {next(l for l in LINES if l[1] == s)[0]} 正常收货入库。",
           ts=recv)

    # S002 正常在库
    add_pack("HM260902001", "S002", "IN_STOCK", received_at="2026-09-05 14:50:00")
    ev("HM260902001", "receive", "赵丽（店员）", to_status="IN_STOCK",
       location="A-11-01", detail="按配送单 N-2609-02 正常收货入库。", ts="2026-09-05 14:50:00")

    # ===== 破损：收货隔离 =====
    add_pack("HM260901004", "S001", "QUARANTINED", anomaly="damaged")
    ev("HM260901004", "receive_quarantine", "张小曼（店员）",
       to_status="QUARANTINED", location="A-01-02",
       detail="收货即发现外盒挤压破损，隔离待处置。", ts="2026-09-01 09:16:00")

    # ===== 串码不符：实物条码/批次与单据不符 =====
    add_pack("XH260901004", "S001", "QUARANTINED", anomaly="code_mismatch",
             barcode="6979999999008")
    # 直接改批次模拟扫描值与单据不一致
    c.execute("UPDATE packs SET batch='X2699Z' WHERE serial='XH260901004'")
    ev("XH260901004", "receive_quarantine", "张小曼（店员）",
       to_status="QUARANTINED", location="B-02-02",
       detail="串码不符：条码不符（扫描 6979999999008 / 单据 6970000002021）；"
              "批次不符（扫描 X2699Z / 单据 X2608C），隔离待核。",
       ts="2026-09-01 09:17:00")

    # ===== 非本店入库：S002 的串码被送到 S001 扫 =====
    add_pack("QL260902001", "S001", "QUARANTINED", anomaly="foreign_store",
             loc_override="待处理暂存区")
    ev("QL260902001", "receive_quarantine", "张小曼（店员）",
       to_status="QUARANTINED", location="待处理暂存区",
       detail="非本店入库：该串码属于门店 S002（临江路加盟店），"
              "本门店 S001 不得入库，先行隔离。", ts="2026-09-06 08:30:00")

    # ===== 已售（状态逆转拦截演示） =====
    add_pack("XH260901005", "S001", "SOLD", sold_at="2026-09-20 16:10:00")
    ev("XH260901005", "sale", "前台收银", from_status="IN_STOCK", to_status="SOLD",
       location="B-02-03", detail="前台售出。", ts="2026-09-20 16:10:00")

    # ===== 退货待审：店员已发起，等店长复核 =====
    add_pack("QL260901003", "S001", "QUARANTINED", anomaly="return_verify")
    ev("QL260901003", "return_request", "张小曼（店员）",
       from_status="IN_STOCK", to_status="QUARANTINED", location="C-03-02",
       detail="店员发起退货申请（damaged）。库内退货：入库链路 配送单 N-2609-01，进入隔离待复核。",
       ts="2026-09-28 11:00:00")
    c.execute(
        """INSERT INTO return_requests
           (serial,store_id,reason,detail,photo_path,status,created_by,created_at)
           VALUES(?,?,?,?,?, 'pending', 'u-zhang', ?)""",
        ("QL260901003", "S001", "damaged", "条盒边角受潮，申请退货。",
         photo_files["QL260901003"], "2026-09-28 11:00:00"))

    # ===== 退货在途（未售）：店长已复核，待配送方回执，默认受理 =====
    add_pack("XH260901006", "S001", "RETURNING", anomaly=None,
             loc_override="退货暂存区")
    ev("XH260901006", "return_request", "张小曼（店员）",
       from_status="IN_STOCK", to_status="QUARANTINED", location="B-02-03",
       detail="店员发起退货申请（damaged）。库内退货：入库链路 配送单 N-2609-01，进入隔离待复核。",
       ts="2026-09-27 15:00:00")
    ev("XH260901006", "return_approved", "王建国（店长）",
       from_status="QUARANTINED", to_status="RETURNING",
       detail="店长复核通过，待配送方回执。", ts="2026-09-27 15:30:00")
    c.execute(
        """INSERT INTO return_requests
           (serial,store_id,reason,detail,photo_path,status,created_by,created_at,
            reviewed_by,reviewed_at,review_note)
           VALUES(?,?,?,?,?, 'outbound', 'u-zhang', ?, 'u-wang', ?, ?)""",
        ("XH260901006", "S001", "damaged", "外包装破损 @accept",
         None, "2026-09-27 15:00:00", "2026-09-27 15:30:00", "情况属实，同意退货。"))

    # ===== 已售退货在途（X 结尾→配送方将拒收，且因曾售出永不回在库） =====
    add_pack("QL2609010X", "S001", "RETURNING", anomaly=None,
             sold_at="2026-09-18 19:20:00", loc_override="退货暂存区")
    ev("QL2609010X", "sale", "前台收银", from_status="IN_STOCK", to_status="SOLD",
       location="C-03-02", detail="前台售出。", ts="2026-09-18 19:20:00")
    ev("QL2609010X", "return_request", "张小曼（店员）",
       from_status="SOLD", to_status="QUARANTINED", location="C-03-02",
       detail="店员发起退货申请（mismatch）。售退条包：已核对原始入库链路 配送单 N-2609-01，"
              "进入隔离待店长复核。", ts="2026-09-26 10:00:00")
    ev("QL2609010X", "return_approved", "王建国（店长）",
       from_status="QUARANTINED", to_status="RETURNING",
       detail="店长复核通过，待配送方回执。", ts="2026-09-26 10:20:00")
    c.execute(
        """INSERT INTO return_requests
           (serial,store_id,reason,detail,photo_path,status,created_by,created_at,
            reviewed_by,reviewed_at,review_note)
           VALUES(?,?,?,?,?, 'outbound', 'u-zhang', ?, 'u-wang', ?, ?)""",
        ("QL2609010X", "S001", "mismatch", "顾客退回，疑为串码不符",
         photo_files["QL2609010X"], "2026-09-26 10:00:00",
         "2026-09-26 10:20:00", "已核对入库链路，送配送方判定。"))

    # ===== 已闭环：配送方受理、最终出库 =====
    add_pack("HM260901008", "S001", "RETURNED", anomaly=None,
             loc_override="配送方收货点")
    ev("HM260901008", "return_request", "张小曼（店员）",
       from_status="IN_STOCK", to_status="QUARANTINED", location="A-01-03",
       detail="店员发起退货申请（damaged）。库内退货：入库链路 配送单 N-2609-01，进入隔离待复核。",
       ts="2026-09-10 09:00:00")
    ev("HM260901008", "return_approved", "王建国（店长）",
       from_status="QUARANTINED", to_status="RETURNING",
       detail="店长复核通过，待配送方回执。", ts="2026-09-10 09:30:00")
    ev("HM260901008", "carrier_receipt", "王建国（店长）",
       from_status="RETURNING", to_status="RETURNED",
       detail="配送方受理，最终出库完成。流水号 CR000004-SEED000001。",
       ts="2026-09-11 10:00:00")
    c.execute(
        """INSERT INTO return_requests
           (serial,store_id,reason,detail,photo_path,status,created_by,created_at,
            reviewed_by,reviewed_at,review_note,carrier_txn,receipt_at,receipt_decision)
           VALUES(?,?,?,?,?, 'closed', 'u-zhang', ?, 'u-wang', ?, ?, ?, ?, 'accepted')""",
        ("HM260901008", "S001", "damaged", "运输受潮",
         photo_files["HM260901008"], "2026-09-10 09:00:00",
         "2026-09-10 09:30:00", "同意退货。",
         "CR000004-SEED000001", "2026-09-11 10:00:00"))
    c.execute(
        """INSERT INTO carrier_receipts(txn_id,request_id,serial,decision,message,received_at)
           VALUES(?,?,?,?,?,?)""",
        ("CR000004-SEED000001", 4, "HM260901008", "accepted",
         "配送方核验：符合退货条件，已收回条包，退货出库完成。", "2026-09-11 10:00:00"))

    # ===== 库存差异：鹤鸣系统 5 条，上次盘点实物 4 条，差异 -1 =====
    c.execute(
        """INSERT INTO stock_snapshots(store_id,brand_id,system_qty,physical_qty,diff,note,created_by,created_at)
           VALUES('S001','B-HM',5,4,-1,'货架缝隙找到条包前的待复盘记录','u-wang','2026-09-29 18:00:00')""")
    ev("BRAND:B-HM", "stock_count", "王建国（店长）",
       detail="盘点：系统 5 / 实物 4 / 差异 -1。货架缝隙找到条包前的待复盘记录。",
       ref_id=1, ts="2026-09-29 18:00:00")

    conn.commit()
    conn.close()

    print("演示数据已导入：", db_path)
    print("登录账号（S001 梧桐路直营店）：zhang 店员 / wang 店长")
    print("登录账号（S002 临江路加盟店）：zhao 店员 / qian 店长")
    print("""
演示串码速查：
  正常收货（可在收货页扫配送单新串码；库内已有的见库存页）
  重复扫描      HM260901DUP / HM260901001（再扫一次即拦截）
  破损隔离      HM260901004（异常隔离页）
  串码不符      XH260901004（异常隔离页，左右对比单据与实物）
  非本店入库    QL260902001（S002 的串码，在 S001 隔离）
  超过退货期限  HM260801001（8 月 1 日批次，发起退货即拦截）
  已售不可回库  XH260901005（已售）
  退货待审      QL260901003（店员已发起，等 wang 复核）
  配送方受理    XH260901006（复核通过后索取回执 → accepted 出库）
  配送方拒收    QL2609010X（已售 + X 结尾 → 拒收且永不回在库）
  已闭环出库    HM260901008
提示：退货说明里写 @accept 或 @reject 可直接指定模拟配送方结论；串码以 X 结尾必被拒收。""")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/app.db")
    ap.add_argument("--photos", default="data/photos")
    args = ap.parse_args()
    seed(args.db, args.photos)


if __name__ == "__main__":
    main()

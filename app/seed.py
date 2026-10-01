"""演示数据：虚构品牌与编号，导入正常与异常串码，并跑通各状态。
执行：python3 -m app.cli init
"""
import os
from datetime import datetime, timedelta

from . import services as svc
from .adapter_delivery import MockDeliveryAdapter
from .db import connect, init_db

STORE_A = "S01"
STORE_B = "S02"

SPECS = [
    ("SP01", "苍山牌", "软白（20支/条）", "6901234000011", "YHY"),
    ("SP02", "星桥牌", "蓝星（20支/条）", "6901234000028", "XQY"),
    ("SP03", "鹭岛牌", "海风（细支/条）", "6901234000035", "LDS"),
]

NOTE_A = "DN-2609-18"
NOTE_B = "DN-2609-19"

# 本单 A 计划：0001-0006 苍山，01-03 星桥，01-02 鹭岛
LINES_A = [f"YHY26{n:04d}" for n in range(1, 7)] + \
          [f"XQY26{n:04d}" for n in range(1, 4)] + \
          [f"LDS26{n:04d}" for n in range(1, 3)]
# 门店 B 的计划单：S01 扫到其中 YHY260010 即“非本店入库”
LINES_B = ["YHY260010", "XQY260010", "LDS260010"]

CLERK_A = "u_clerk"
MANAGER_A = "u_mgr"


def _write_photo(photo_dir, name, title, subtitle, tint="#d9d9d9"):
    """生成一张本地 SVG 占位照片（无需第三方库）。"""
    os.makedirs(photo_dir, exist_ok=True)
    path = os.path.join(photo_dir, name)
    svg = f"""<svg xmlns='http://www.w3.org/2000/svg' width='320' height='200'>
<rect width='100%' height='100%' fill='{tint}'/>
<rect x='10' y='10' width='300' height='180' fill='none' stroke='#8c8c8c' stroke-width='2'/>
<text x='160' y='92' font-size='20' text-anchor='middle' fill='#434343'
 font-family='sans-serif'>{title}</text>
<text x='160' y='124' font-size='13' text-anchor='middle' fill='#595959'
 font-family='sans-serif'>{subtitle}（演示占位照片）</text>
</svg>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg)
    return path


def seed(db_path, photo_dir):
    if os.path.exists(db_path):
        os.remove(db_path)
    for suffix in ("-wal", "-shm"):
        p = db_path + suffix
        if os.path.exists(p):
            os.remove(p)
    init_db(db_path)
    conn = connect(db_path)
    now = datetime.now().replace(microsecond=0)
    iso = lambda dt: dt.isoformat(timespec="seconds")

    conn.executescript("""
    INSERT INTO stores(id,name) VALUES ('S01','云鹤烟酒便利店（演示总店）');
    INSERT INTO stores(id,name) VALUES ('S02','南桥社区烟酒店（演示二店）');
    INSERT INTO users(id,username,password,name,role,store_id) VALUES
      ('u_clerk','clerk','clerk123','王小蔓','clerk','S01'),
      ('u_mgr','manager','mgr123','刘店长','manager','S01'),
      ('u_clerk2','clerk2','clerk123','陈小芸','clerk','S02');
    """)
    conn.executemany(
        "INSERT INTO specs(id,brand,spec_name,barcode,code_prefix) VALUES (?,?,?,?,?)",
        SPECS)
    conn.execute(
        "INSERT INTO delivery_notes(id,store_id,carrier,delivered_at) VALUES (?,?,?,?)",
        (NOTE_A, STORE_A, "云商物流（模拟）", iso(now - timedelta(days=12))))
    conn.executemany(
        "INSERT INTO delivery_note_lines(note_id,expected_code,spec_id) VALUES (?,?,?)",
        [(NOTE_A, c, {"Y": "SP01", "X": "SP02", "L": "SP03"}[c[0]]) for c in LINES_A])
    conn.execute(
        "INSERT INTO delivery_notes(id,store_id,carrier,delivered_at) VALUES (?,?,?,?)",
        (NOTE_B, STORE_B, "云商物流（模拟）", iso(now - timedelta(days=1))))
    conn.executemany(
        "INSERT INTO delivery_note_lines(note_id,expected_code,spec_id) VALUES (?,?,?)",
        [(NOTE_B, c, {"Y": "SP01", "X": "SP02", "L": "SP03"}[c[0]]) for c in LINES_B])

    R = lambda code, **kw: svc.receive_scan(
        conn, note_id=NOTE_A, code=code, actor=CLERK_A,
        location=kw.pop("location", "A区货架-01"), **kw)

    # —— 正常条包 ——
    for code in ["YHY260001", "YHY260002", "YHY260003", "XQY260001", "XQY260002"]:
        R(code)
    # 0006 走完整个退货出库链路（早入库、早售出）
    svc.receive_scan(conn, note_id=NOTE_A, code="YHY260006", actor=CLERK_A,
                     location="A区货架-02", now=iso(now - timedelta(days=10)))
    # 0004 配送方将拒收（批次争议）
    R("YHY260004", location="A区货架-02")

    # —— 异常 1：破损 ——
    photo_dmg = _write_photo(photo_dir, "YHY260005.svg", "破损照片 YHY260005",
                             "外箱挤压 边角破损", tint="#e8d5d5")
    R("YHY260005", damaged=True, location="异常隔离区")
    conn.execute("UPDATE packages SET photo_path=? WHERE code='YHY260005'",
                 (photo_dmg,))

    # —— 异常 2：串码不符（配送计划中查无此码）——
    R("YHY260099", location="异常隔离区")

    # —— 异常 3：非本店入库（码属于 S02 的配送单）——
    R("YHY260010", location="异常隔离区")

    # 鹭岛 0001 早入库早售出 → 超期退货
    svc.receive_scan(conn, note_id=NOTE_A, code="LDS260001", actor=CLERK_A,
                     location="B区货架-01", now=iso(now - timedelta(days=12)))
    svc.sell(conn, code="LDS260001", actor=CLERK_A, now=iso(now - timedelta(days=9)))

    # —— 销售（近几天）——
    svc.sell(conn, code="YHY260001", actor=CLERK_A, now=iso(now - timedelta(days=2)))
    svc.sell(conn, code="YHY260002", actor=CLERK_A, now=iso(now - timedelta(days=2)))
    svc.sell(conn, code="XQY260001", actor=CLERK_A, now=iso(now - timedelta(days=1)))
    svc.sell(conn, code="XQY260002", actor=CLERK_A, now=iso(now - timedelta(days=3)))
    svc.sell(conn, code="YHY260004", actor=CLERK_A, now=iso(now - timedelta(days=2)))
    svc.sell(conn, code="YHY260006", actor=CLERK_A, now=iso(now - timedelta(days=8)))

    # —— 退货链路演示 ——
    # a) 待店长复核
    photo_ret = _write_photo(photo_dir, "return-YHY260002.svg",
                             "退货照片 YHY260002", "顾客退回 外包装完好",
                             tint="#d5e0e8")
    svc.request_return(conn, code="YHY260002", reason="顾客反馈口味不适，包装完好",
                       actor=CLERK_A, photo_path=photo_ret)
    # b) 复核通过，待配送回执
    r2 = svc.request_return(conn, code="XQY260002", reason="顾客购错规格",
                            actor=CLERK_A)
    svc.review_return(conn, return_id=r2["return_id"], approved=True,
                      actor=MANAGER_A, note="包装完好，同意")
    # c) 配送方接收 → 最终出库
    r3 = svc.request_return(conn, code="YHY260006", reason="顾客退回（历史单演示）",
                            actor=CLERK_A, now=iso(now - timedelta(days=7)))
    svc.review_return(conn, return_id=r3["return_id"], approved=True,
                      actor=MANAGER_A, note="链路齐全", now=iso(now - timedelta(days=7)))
    # d) 配送方拒收 → 异常隔离（delivery_refused）
    r4 = svc.request_return(conn, code="YHY260004", reason="顾客怀疑串码异常",
                            actor=CLERK_A)
    svc.review_return(conn, return_id=r4["return_id"], approved=True,
                      actor=MANAGER_A, note="先同意，交配送核验")

    # 配送回执：0006 接收（最终出库），0004 因批次争议被拒（异常隔离）
    adapter = MockDeliveryAdapter(LINES_A + LINES_B)
    svc.carrier_receipt(conn, return_id=r3["return_id"], actor=MANAGER_A,
                        adapter=adapter, now=iso(now - timedelta(days=7)))
    svc.carrier_receipt(conn, return_id=r4["return_id"], actor=MANAGER_A,
                        adapter=adapter)

    # e) 超期退货 → 系统直接拒绝（条包维持已售）
    svc.request_return(conn, code="LDS260001", reason="顾客很久后要求退货",
                       actor=CLERK_A)

    conn.close()
    return {
        "accounts": [("店员", "clerk / clerk123"), ("店长", "manager / mgr123"),
                     ("二店店员", "clerk2 / clerk123")],
        "codes": {
            "正常在库": ["YHY260003"],
            "已售": ["YHY260001", "XQY260001", "LDS260001"],
            "退货待店长复核": ["YHY260002"],
            "复核通过待配送回执": ["XQY260002"],
            "破损（隔离）": ["YHY260005"],
            "串码不符（隔离）": ["YHY260099"],
            "非本店入库（隔离）": ["YHY260010（属 S02 配送单 " + NOTE_B + "）"],
            "配送拒收隔离": ["YHY260004"],
            "已出库": ["YHY260006"],
            "超期退货被拒（维持已售）": ["LDS260001（售出 9 天）"],
            "重复扫描演示": ["再扫一次 YHY260003 → 提示重复，不形成第二笔库存"],
            "应到未扫（库存差异）": ["XQY260003", "LDS260002"],
        },
        "note": NOTE_A,
    }

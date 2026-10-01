"""领域服务：收货扫描、销售、退货申请/复核/配送回执、异常隔离与追溯。

关键约束：
* 所有写操作在 BEGIN IMMEDIATE 事务内完成；packages.code 主键 + 退货部分唯一索引兜底并发。
* 状态机 ALLOWED_TRANSITIONS 是唯一的状态变更入口，已售条包无法被直接改回在库。
* 每次流转写 flows；每次扫码尝试（含被拒）写 scan_events。
"""
import json
from datetime import datetime, timedelta

RETURN_WINDOW_DAYS = 7

# 状态机
S_IN_STOCK = "in_stock"               # 在库
S_SOLD = "sold"                       # 已售
S_RETURN_PENDING = "return_pending"   # 退货待店长复核
S_RETURN_APPROVED = "return_approved" # 复核通过，待配送回执
S_OUTBOUND = "outbound"               # 配送方收回，最终出库
S_QUARANTINED = "quarantined"         # 异常隔离

ALLOWED_TRANSITIONS = {
    S_IN_STOCK: {S_SOLD},
    S_SOLD: {S_RETURN_PENDING},
    S_RETURN_PENDING: {S_RETURN_APPROVED, S_SOLD},       # 复核通过 / 驳回维持已售
    S_RETURN_APPROVED: {S_OUTBOUND, S_QUARANTINED},      # 配送方接收 / 拒收隔离
    S_QUARANTINED: set(),
    S_OUTBOUND: set(),
}

ANOMALY_LABELS = {
    "damaged": "破损",
    "code_mismatch": "串码不符",
    "foreign_store": "非本店入库",
    "delivery_refused": "配送方拒收回执",
}


class DomainError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _rollback(conn):
    """安全回滚：部分被拒分支已提交审计流水后再抛业务异常，此时无事务可回滚。"""
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    except Exception:
        pass


def _scan_event(conn, code, context, result, reason, actor, detail=None):
    conn.execute(
        "INSERT INTO scan_events(ts, code, context, result, reason, actor, detail) "
        "VALUES (?,?,?,?,?,?,?)",
        (_now(), code, context, result, reason, actor,
         json.dumps(detail, ensure_ascii=False) if detail else None),
    )


def _flow(conn, *, code, action, from_state, to_state, actor, location=None,
          note=None, ref_type=None, ref_id=None, photo_path=None):
    conn.execute(
        "INSERT INTO flows(code, ts, action, from_state, to_state, actor, location, "
        "note, ref_type, ref_id, photo_path) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (code, _now(), action, from_state, to_state, actor, location, note,
         ref_type, ref_id, photo_path),
    )


def _transition(conn, code, to_state):
    row = conn.execute("SELECT state FROM packages WHERE code=?", (code,)).fetchone()
    if row is None:
        raise DomainError("not_found", f"条包 {code} 不存在", 404)
    cur = row["state"]
    if to_state not in ALLOWED_TRANSITIONS.get(cur, set()):
        raise DomainError(
            "illegal_transition",
            f"状态逆转被拦截：{code} 当前为「{cur}」，不允许直接改为「{to_state}」；"
            f"允许的下一状态：{sorted(ALLOWED_TRANSITIONS.get(cur, set())) or '无'}",
            409,
        )
    return cur


def _package_brief(row):
    return {k: row[k] for k in row.keys()} if row else None


# ---------------------------------------------------------------- 收货扫描

def receive_scan(conn, *, note_id, code, actor, location, barcode=None,
                 damaged=False, now=None):
    """按配送单扫描收货。返回 (ok, result_dict)。异常同样落库为隔离条包。"""
    now = now or _now()
    user = conn.execute("SELECT * FROM users WHERE id=?", (actor,)).fetchone()
    if user is None:
        raise DomainError("no_actor", "操作员不存在", 403)
    note = conn.execute("SELECT * FROM delivery_notes WHERE id=?", (note_id,)).fetchone()
    if note is None:
        raise DomainError("note_not_found", f"配送单 {note_id} 不存在", 404)
    if note["store_id"] != user["store_id"]:
        raise DomainError("foreign_note", "不能扫描其他门店的配送单", 403)

    conn.execute("BEGIN IMMEDIATE")
    try:
        # 1) 重复扫描：串码全局唯一，已存在即拒绝，不产生第二笔库存
        existing = conn.execute("SELECT * FROM packages WHERE code=?", (code,)).fetchone()
        if existing:
            _scan_event(conn, code, "receive", "duplicate",
                        f"重复扫描：该串码状态为 {existing['state']}", user["id"],
                        {"note_id": note_id})
            conn.execute("COMMIT")
            return {"ok": False, "result": "duplicate",
                    "message": f"重复扫描：{code} 已于 {existing['received_at']} 入库，"
                               f"当前状态「{existing['state']}」，未重复形成库存",
                    "package": _package_brief(existing)}

        line = conn.execute(
            "SELECT l.*, n.store_id AS line_store_id, n.id AS line_note_id "
            "FROM delivery_note_lines l JOIN delivery_notes n ON n.id=l.note_id "
            "WHERE l.expected_code=?", (code,)).fetchone()

        expected_spec = None
        anomaly = None
        detail = {}
        if line is None:
            anomaly = "code_mismatch"
            detail = {"expected": f"配送单 {note_id} 计划内串码", "actual": code,
                      "plan": "全网配送计划中查无此串码"}
        elif line["line_note_id"] != note_id and line["line_store_id"] == user["store_id"]:
            anomaly = "code_mismatch"
            other = conn.execute("SELECT spec_id FROM specs WHERE id=?",
                                 (line["spec_id"],)).fetchone()
            detail = {"expected": f"本单 {note_id} 计划串码",
                      "actual": f"{code} 属于本店另一张配送单 {line['line_note_id']}"}
        elif line["line_store_id"] != user["store_id"]:
            anomaly = "foreign_store"
            other_store = conn.execute("SELECT name FROM stores WHERE id=?",
                                       (line["line_store_id"],)).fetchone()
            detail = {"expected": f"{user['store_id']} 本店配送单",
                      "actual": f"{code} 归属门店「{other_store['name']}」"
                                f"（配送单 {line['line_note_id']}）"}

        # 条码与规格核对（扫了条码才比对）
        spec = None
        if line is not None:
            spec = conn.execute("SELECT * FROM specs WHERE id=?",
                                (line["spec_id"],)).fetchone()
            expected_spec = spec["id"]
            if barcode and spec and barcode != spec["barcode"]:
                anomaly = "code_mismatch"
                detail["barcode_expected"] = spec["barcode"]
                detail["barcode_actual"] = barcode

        if damaged and anomaly is None:
            anomaly = "damaged"
            detail = {"expected": "外包装完好", "actual": "扫描时标记为破损"}

        if anomaly:
            conn.execute(
                "INSERT INTO packages(code, spec_id, batch, store_id, expected_store, "
                "note_id, state, location, received_at, anomaly_type, anomaly_detail) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (code, expected_spec, _batch_of(code), user["store_id"],
                 line["line_store_id"] if line else user["store_id"],
                 line["line_note_id"] if line else note_id,
                 S_QUARANTINED, location or "异常隔离区", now,
                 anomaly, json.dumps(detail, ensure_ascii=False)),
            )
            _flow(conn, code=code, action="receive_quarantine", from_state=None,
                  to_state=S_QUARANTINED, actor=user["id"], location=location,
                  note=ANOMALY_LABELS[anomaly],
                  ref_type="delivery_note", ref_id=note_id)
            _scan_event(conn, code, "receive", anomaly,
                        ANOMALY_LABELS[anomaly], user["id"], detail)
            conn.execute("COMMIT")
            return {"ok": False, "result": anomaly,
                    "message": f"{ANOMALY_LABELS[anomaly]}：{code} 已转入异常隔离区，"
                               f"未计入在库库存",
                    "anomaly_detail": detail,
                    "package": _package_brief(
                        conn.execute("SELECT * FROM packages WHERE code=?",
                                     (code,)).fetchone())}

        # 正常入库
        conn.execute(
            "INSERT INTO packages(code, spec_id, batch, store_id, expected_store, "
            "note_id, state, location, received_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (code, spec["id"], _batch_of(code), user["store_id"], user["store_id"],
             note_id, S_IN_STOCK, location or "待上架", now),
        )
        _flow(conn, code=code, action="receive", from_state=None,
              to_state=S_IN_STOCK, actor=user["id"], location=location,
              note=f"按配送单 {note_id} 收货入库",
              ref_type="delivery_note", ref_id=note_id)
        _scan_event(conn, code, "receive", "ok", "正常入库", user["id"],
                    {"note_id": note_id, "location": location})
        conn.execute("COMMIT")
        return {"ok": True, "result": "ok",
                "message": f"入库成功：{code}（{spec['brand']} {spec['spec_name']}）"
                           f"已置于 {location or '待上架'}",
                "package": _package_brief(
                    conn.execute("SELECT * FROM packages WHERE code=?",
                                 (code,)).fetchone())}
    except Exception:
        _rollback(conn)
        raise


def _batch_of(code):
    """虚构批次：由串码派生，演示用。"""
    return "B" + code[-4:] + "26"


# ---------------------------------------------------------------- 销售

def sell(conn, *, code, actor, now=None):
    now = now or _now()
    user = conn.execute("SELECT * FROM users WHERE id=?", (actor,)).fetchone()
    conn.execute("BEGIN IMMEDIATE")
    try:
        pkg = conn.execute("SELECT * FROM packages WHERE code=?", (code,)).fetchone()
        if pkg is None:
            _scan_event(conn, code, "sell", "not_found", "串码不存在", actor)
            conn.execute("COMMIT")
            raise DomainError("not_found", f"条包 {code} 不存在", 404)
        if pkg["store_id"] != user["store_id"]:
            conn.execute("COMMIT")
            raise DomainError("foreign_store", "不能销售其他门店的条包", 403)
        cur = _transition(conn, code, S_SOLD)  # 仅在库可售
        conn.execute("UPDATE packages SET state=?, sold_at=? WHERE code=?",
                     (S_SOLD, now, code))
        _flow(conn, code=code, action="sell", from_state=cur, to_state=S_SOLD,
              actor=actor, location=pkg["location"], note="门店销售出库（柜台）")
        _scan_event(conn, code, "sell", "ok", "销售", actor)
        conn.execute("COMMIT")
        return {"ok": True, "message": f"{code} 已登记销售，状态「已售」"}
    except Exception:
        _rollback(conn)
        raise


# ---------------------------------------------------------------- 退货

def request_return(conn, *, code, reason, actor, photo_path=None, now=None):
    now = now or _now()
    user = conn.execute("SELECT * FROM users WHERE id=?", (actor,)).fetchone()
    if user is None or user["role"] != "clerk":
        raise DomainError("forbidden", "只有店员可以发起退货申请", 403)

    conn.execute("BEGIN IMMEDIATE")
    try:
        pkg = conn.execute("SELECT * FROM packages WHERE code=?", (code,)).fetchone()
        if pkg is None:
            _scan_event(conn, code, "return", "not_found", "串码不存在", actor)
            conn.execute("COMMIT")
            raise DomainError("not_found", f"条包 {code} 不存在", 404)
        if pkg["store_id"] != user["store_id"]:
            conn.execute("COMMIT")
            raise DomainError("foreign_store", "该条包不属本店，不能发起退货", 403)

        # 核对原始入库链路：必须存在一笔正常收货流水
        chain = conn.execute(
            "SELECT * FROM flows WHERE code=? AND action='receive' ORDER BY id LIMIT 1",
            (code,)).fetchone()
        if chain is None:
            _scan_event(conn, code, "return", "no_inbound_chain",
                        "缺少原始入库链路", actor)
            conn.execute("COMMIT")
            raise DomainError("no_inbound_chain",
                              f"{code} 缺少原始入库链路（无正常收货流水），退货被拦截", 409)

        if pkg["state"] != S_SOLD:
            # 并发或重复发起：已有进行中的退货时给出明确结论
            active = conn.execute(
                "SELECT id FROM returns WHERE code=? AND status IN ('pending','approved') "
                "LIMIT 1", (code,)).fetchone()
            if active:
                _scan_event(conn, code, "return", "active_return_exists",
                            "已存在进行中的退货申请", actor)
                conn.execute("COMMIT")
                raise DomainError("active_return_exists",
                                  f"{code} 已存在进行中的退货申请（#{active['id']}），"
                                  f"不能重复发起", 409)
            _scan_event(conn, code, "return", "wrong_state",
                        f"当前状态 {pkg['state']}", actor)
            conn.execute("COMMIT")
            raise DomainError("wrong_state",
                              f"只有「已售」条包可申请退货，{code} 当前为「{pkg['state']}」", 409)

        sold_at = datetime.fromisoformat(pkg["sold_at"])
        days_held = (datetime.fromisoformat(now) - sold_at).days

        # 超期：落审计行并直接拒绝，与店长驳回分别处置
        if days_held > RETURN_WINDOW_DAYS:
            cur = conn.execute(
                "INSERT INTO returns(code, status, reason, clerk_id, requested_at, "
                "sold_at_snapshot, days_held, review_note, photo_path) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (code, "rejected", reason, actor, now, pkg["sold_at"], days_held,
                 f"系统拦截：售出已 {days_held} 天，超过 {RETURN_WINDOW_DAYS} 天退货期限",
                 photo_path),
            ).lastrowid
            _flow(conn, code=code, action="return_overdue_blocked",
                  from_state=S_SOLD, to_state=S_SOLD, actor=actor,
                  note=f"超过退货期限（{days_held} 天），申请被系统拒绝",
                  ref_type="return", ref_id=str(cur), photo_path=photo_path)
            _scan_event(conn, code, "return", "overdue",
                        f"超过退货期限 {days_held} 天", actor)
            conn.execute("COMMIT")
            return {"ok": False, "result": "overdue", "return_id": cur,
                    "message": f"超过退货期限：{code} 售出已 {days_held} 天"
                               f"（限 {RETURN_WINDOW_DAYS} 天），系统直接拒绝并留痕"}

        try:
            rid = conn.execute(
                "INSERT INTO returns(code, status, reason, clerk_id, requested_at, "
                "sold_at_snapshot, days_held, photo_path) VALUES (?,?,?,?,?,?,?,?)",
                (code, "pending", reason, actor, now, pkg["sold_at"], days_held,
                 photo_path),
            ).lastrowid
        except Exception as e:
            raise DomainError("active_return_exists",
                              f"{code} 已存在进行中的退货申请，不能重复发起", 409) from e

        new_state = S_RETURN_PENDING
        conn.execute("UPDATE packages SET state=? WHERE code=?", (new_state, code))
        _flow(conn, code=code, action="return_request", from_state=S_SOLD,
              to_state=new_state, actor=actor, note=f"店员发起退货：{reason}",
              ref_type="return", ref_id=str(rid), photo_path=photo_path)
        _scan_event(conn, code, "return", "ok", "退货申请已提交，待店长复核", actor)
        conn.execute("COMMIT")
        return {"ok": True, "return_id": rid, "days_held": days_held,
                "message": f"退货申请已提交（持有 {days_held} 天），等待店长复核"}
    except Exception:
        _rollback(conn)
        raise


def review_return(conn, *, return_id, approved, actor, note="", now=None):
    now = now or _now()
    user = conn.execute("SELECT * FROM users WHERE id=?", (actor,)).fetchone()
    if user is None or user["role"] != "manager":
        raise DomainError("forbidden", "只有店长可以复核退货", 403)

    conn.execute("BEGIN IMMEDIATE")
    try:
        ret = conn.execute("SELECT * FROM returns WHERE id=?", (return_id,)).fetchone()
        if ret is None:
            conn.execute("COMMIT")
            raise DomainError("not_found", "退货单不存在", 404)
        pkg = conn.execute("SELECT * FROM packages WHERE code=?", (ret["code"],)).fetchone()
        if pkg["store_id"] != user["store_id"]:
            conn.execute("COMMIT")
            raise DomainError("forbidden", "不能复核其他门店的退货", 403)
        if ret["status"] != "pending":
            conn.execute("COMMIT")
            raise DomainError("wrong_status",
                              f"退货单当前为「{ret['status']}」，不能重复复核", 409)

        if approved:
            cur = _transition(conn, ret["code"], S_RETURN_APPROVED)
            conn.execute("UPDATE packages SET state=? WHERE code=?",
                         (S_RETURN_APPROVED, ret["code"]))
            conn.execute(
                "UPDATE returns SET status='approved', manager_id=?, reviewed_at=?, "
                "review_note=? WHERE id=?", (actor, now, note, return_id))
            _flow(conn, code=ret["code"], action="return_approve", from_state=cur,
                  to_state=S_RETURN_APPROVED, actor=actor,
                  note=f"店长复核通过：{note or '同意退货'}",
                  ref_type="return", ref_id=str(return_id))
            msg = "店长复核通过，已向配送方请求回执"
        else:
            cur = _transition(conn, ret["code"], S_SOLD)  # 驳回：维持已售，绝不回到在库
            conn.execute("UPDATE packages SET state=? WHERE code=?",
                         (S_SOLD, ret["code"]))
            conn.execute(
                "UPDATE returns SET status='rejected', manager_id=?, reviewed_at=?, "
                "review_note=? WHERE id=?", (actor, now, note, return_id))
            _flow(conn, code=ret["code"], action="return_reject", from_state=cur,
                  to_state=S_SOLD, actor=actor,
                  note=f"店长复核驳回：{note or '不同意退货'}，条包维持已售",
                  ref_type="return", ref_id=str(return_id))
            msg = "店长复核驳回，条包维持「已售」，未回到在库"
        conn.execute("COMMIT")
        return {"ok": True, "message": msg}
    except Exception:
        _rollback(conn)
        raise


def carrier_receipt(conn, *, return_id, actor, adapter, now=None):
    """请求配送方回执。回执结果持久化在 receipt_log；重复调用/重放直接读回原结果，
    绝不产生第二次出库。"""
    now = now or _now()
    user = conn.execute("SELECT * FROM users WHERE id=?", (actor,)).fetchone()
    if user is None or user["role"] != "manager":
        raise DomainError("forbidden", "只有店长可以向配送方确认回执", 403)

    conn.execute("BEGIN IMMEDIATE")
    try:
        ret = conn.execute("SELECT * FROM returns WHERE id=?", (return_id,)).fetchone()
        if ret is None:
            conn.execute("COMMIT")
            raise DomainError("not_found", "退货单不存在", 404)
        pkg = conn.execute("SELECT * FROM packages WHERE code=?", (ret["code"],)).fetchone()
        if pkg["store_id"] != user["store_id"]:
            conn.execute("COMMIT")
            raise DomainError("forbidden", "不能处理其他门店的退货", 403)

        cached = conn.execute("SELECT * FROM receipt_log WHERE return_id=?",
                              (return_id,)).fetchone()
        if cached:
            conn.execute("COMMIT")
            return {"ok": bool(cached["accepted"]), "replayed": True,
                    "receipt_no": cached["receipt_no"], "reason": cached["reason"],
                    "message": f"回执重放：该退货已有回执（{cached['receipt_no']}），"
                               f"结果维持「{'接收' if cached['accepted'] else '拒收'}」，"
                               f"未重复出库"}

        if ret["status"] != "approved":
            conn.execute("COMMIT")
            raise DomainError("wrong_status",
                              f"退货单当前为「{ret['status']}」，需店长复核通过后才能请求回执",
                              409)

        receipt = adapter.request_return({
            "code": ret["code"], "spec_id": pkg["spec_id"],
            "batch": pkg["batch"], "note_id": pkg["note_id"],
            "days_held": ret["days_held"], "reason": ret["reason"],
        })
        conn.execute(
            "INSERT INTO receipt_log(return_id, receipt_no, accepted, reason, payload, "
            "synced_at) VALUES (?,?,?,?,?,?)",
            (return_id, receipt["receipt_no"], 1 if receipt["accepted"] else 0,
             receipt.get("reason"), json.dumps(receipt, ensure_ascii=False), now))

        if receipt["accepted"]:
            cur = _transition(conn, ret["code"], S_OUTBOUND)
            conn.execute("UPDATE packages SET state=?, location=? WHERE code=?",
                         (S_OUTBOUND, "配送车辆（已交接）", ret["code"]))
            conn.execute("UPDATE returns SET status='outbound' WHERE id=?", (return_id,))
            _flow(conn, code=ret["code"], action="carrier_accept", from_state=cur,
                  to_state=S_OUTBOUND, actor=actor,
                  location="配送车辆（已交接）",
                  note=f"配送方回执接收 {receipt['receipt_no']}：{receipt.get('reason','')}",
                  ref_type="return", ref_id=str(return_id))
            msg = f"配送方回执「接收」（{receipt['receipt_no']}），条包最终出库"
        else:
            cur = _transition(conn, ret["code"], S_QUARANTINED)
            conn.execute(
                "UPDATE packages SET state=?, location=?, anomaly_type='delivery_refused', "
                "anomaly_detail=? WHERE code=?",
                (S_QUARANTINED, "异常隔离区",
                 json.dumps({"receipt_no": receipt["receipt_no"],
                             "reason": receipt.get("reason")}, ensure_ascii=False),
                 ret["code"]))
            conn.execute("UPDATE returns SET status='refused' WHERE id=?", (return_id,))
            _flow(conn, code=ret["code"], action="carrier_refuse", from_state=cur,
                  to_state=S_QUARANTINED, actor=actor, location="异常隔离区",
                  note=f"配送方回执拒收 {receipt['receipt_no']}：{receipt.get('reason','')}",
                  ref_type="return", ref_id=str(return_id))
            msg = (f"配送方回执「拒收」（{receipt['receipt_no']}："
                   f"{receipt.get('reason')}），条包转入异常隔离区")
        conn.execute("COMMIT")
        return {"ok": bool(receipt["accepted"]), "replayed": False,
                "receipt_no": receipt["receipt_no"], "message": msg}
    except Exception:
        _rollback(conn)
        raise


def direct_set_state(conn, *, code, to_state, actor, note="手工改状态"):
    """供管理端/测试调用的唯一直接改状态入口：同样受状态机约束。
    已售 → 在库 不在 ALLOWED_TRANSITIONS 中，必然被拦截。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = _transition(conn, code, to_state)
        conn.execute("UPDATE packages SET state=? WHERE code=?", (to_state, code))
        _flow(conn, code=code, action="manual_state_change", from_state=cur,
              to_state=to_state, actor=actor, note=note)
        conn.execute("COMMIT")
        return {"ok": True}
    except Exception:
        _rollback(conn)
        raise

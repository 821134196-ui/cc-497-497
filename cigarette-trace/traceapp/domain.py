"""领域逻辑：收货扫描、异常隔离、退货流程、状态机、库存差异。

关键规则：
1. serial 全库唯一 + 序列化写事务，两人并发扫描同一串码只能形成一笔库存。
2. SOLD 状态不可直接回到 IN_STOCK：售退条包先进 QUARANTINED 核对入库链路。
3. 店员只能发起退货申请，店长才能复核；配送方回执决定最终是否出库。
4. 配送回执按退货申请幂等：重放返回首次结论，不重复出库。
"""
import sqlite3

from . import db
from .adapter import make_txn_id, simulate_carrier_decision
from .util import now, within_return_window


class DomainError(Exception):
    """业务规则错误（非法操作、校验失败等）。"""


class AuthError(Exception):
    """角色或身份不满足。"""


# 异常原因 -> 中文文案
ANOMALY_TEXTS = {
    "damaged": "破损",
    "code_mismatch": "串码不符",
    "duplicate": "重复扫描",
    "foreign_store": "非本店入库",
    "return_expired": "超过退货期限",
    "return_verify": "退货核对中",
    "return_rejected_keep": "退货驳回保留隔离",
    "carrier_rejected": "配送方拒收退回",
}


# ---------------------------------------------------------------- 辅助查询

def _get_user(conn, user_id):
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        raise AuthError("用户不存在")
    return row


def require_role(user, role):
    if user["role"] != role:
        raise AuthError(f"需要{ '店长' if role == 'manager' else '店员' }权限，"
                        f"当前身份为{ '店长' if user['role'] == 'manager' else '店员' }")


def _get_pack(conn, serial):
    return conn.execute("SELECT * FROM packs WHERE serial=?", (serial,)).fetchone()


def _find_line(conn, serial):
    return conn.execute(
        """SELECT l.*, n.store_id AS note_store_id, n.delivered_at, n.carrier,
                  b.name AS brand_name, b.spec AS brand_spec, b.barcode AS line_barcode
           FROM delivery_lines l
           JOIN delivery_notes n ON n.id = l.note_id
           JOIN brands b ON b.id = l.brand_id
           WHERE l.serial=?""",
        (serial,),
    ).fetchone()


def _return_window(conn, store_id):
    row = conn.execute(
        "SELECT return_window_days FROM store_settings WHERE store_id=?", (store_id,)
    ).fetchone()
    return row["return_window_days"] if row else 30


def _was_sold(conn, serial):
    """该条包是否曾售出（即使已进入隔离也不能直接回在库）。"""
    return conn.execute(
        "SELECT 1 FROM flow_events WHERE serial=? AND event_type='sale' LIMIT 1",
        (serial,),
    ).fetchone() is not None


def _check_transition(cur_status, new_status):
    if new_status not in db.ALLOWED_TRANSITIONS.get(cur_status, set()):
        raise DomainError(
            f"状态逆转被拦截：{cur_status} 不允许直接变更为 {new_status}。"
            f"已售条包不能被直接改回在库。"
        )


# ---------------------------------------------------------------- 收货扫描

def scan_receive(conn, *, serial, user_id, scanned_barcode="", scanned_batch="",
                 location="", damaged=False):
    """门店按配送单扫码收货。返回 dict 描述处置结果。"""
    user = _get_user(conn, user_id)
    require_role(user, "clerk")

    block = None  # 需先提交拦截事件、再在事务外抛出的业务错误
    result = None
    with db.write_tx(conn) as c:
        existing = _get_pack(c, serial)
        if existing is not None:
            # 重复扫描：已有主档（任何状态）都不能再次入库。
            # 事件必须落库留痕，故先随事务提交，事务外再抛错。
            db.log_event(c, serial, "receive_duplicate_blocked", user["display_name"],
                         from_status=existing["status"], to_status=existing["status"],
                         location=location, store_id=user["store_id"],
                         detail=f"重复扫描拦截：条包已在库/已处置，所属门店 {existing['store_id']}")
            block = (f"重复扫描：串码 {serial} 已存在（状态 {existing['status']}），"
                     f"不能形成第二笔库存。")
        else:
            line = _find_line(c, serial)
            if line is None:
                db.log_event(c, serial, "receive_reject_unknown", user["display_name"],
                             location=location, store_id=user["store_id"],
                             detail="配送单无此串码，来源不明，拒收。")
                block = f"拒收：串码 {serial} 不在任何配送单中，来源不明。"
            elif line["note_store_id"] != user["store_id"]:
                # 非本店入库：隔离，等待退回/转运
                pack = _insert_pack(c, line, user, db.STATUS_QUARANTINED,
                                    anomaly="foreign_store", location=location)
                db.log_event(c, serial, "receive_quarantine", user["display_name"],
                             to_status=db.STATUS_QUARANTINED, location=location,
                             detail=f"非本店入库：该串码属于门店 {line['note_store_id']}，"
                                    f"本门店 {user['store_id']} 不得入库，先行隔离。")
                result = {"result": "quarantined", "anomaly": "foreign_store",
                          "text": ANOMALY_TEXTS["foreign_store"], "pack": _pack_dict(pack)}
            else:
                # 串码不符：实物条码/批次与配送单链路记录不一致
                mismatch_parts = []
                if scanned_barcode and scanned_barcode != line["line_barcode"]:
                    mismatch_parts.append(
                        f"条码不符（扫描 {scanned_barcode} / 单据 {line['line_barcode']}）")
                if scanned_batch and scanned_batch != line["batch"]:
                    mismatch_parts.append(
                        f"批次不符（扫描 {scanned_batch} / 单据 {line['batch']}）")

                if mismatch_parts:
                    pack = _insert_pack(c, line, user, db.STATUS_QUARANTINED,
                                        anomaly="code_mismatch", location=location,
                                        override_barcode=scanned_barcode,
                                        override_batch=scanned_batch)
                    db.log_event(c, serial, "receive_quarantine", user["display_name"],
                                 to_status=db.STATUS_QUARANTINED, location=location,
                                 detail="串码不符：" + "；".join(mismatch_parts) + "，隔离待核。")
                    result = {"result": "quarantined", "anomaly": "code_mismatch",
                              "text": ANOMALY_TEXTS["code_mismatch"], "pack": _pack_dict(pack)}
                elif damaged:
                    pack = _insert_pack(c, line, user, db.STATUS_QUARANTINED,
                                        anomaly="damaged", location=location)
                    db.log_event(c, serial, "receive_quarantine", user["display_name"],
                                 to_status=db.STATUS_QUARANTINED, location=location,
                                 detail="收货即发现破损，隔离待处置。")
                    result = {"result": "quarantined", "anomaly": "damaged",
                              "text": ANOMALY_TEXTS["damaged"], "pack": _pack_dict(pack)}
                else:
                    # 正常收货入库
                    pack = _insert_pack(c, line, user, db.STATUS_IN_STOCK, location=location)
                    db.log_event(c, serial, "receive", user["display_name"],
                                 to_status=db.STATUS_IN_STOCK, location=location,
                                 detail=f"按配送单 {line['note_id']} 正常收货入库。",
                                 ref_id=line["note_id"])
                    result = {"result": "received", "pack": _pack_dict(pack)}

    if block:
        raise DomainError(block)
    return result


def _insert_pack(c, line, user, status, *, anomaly=None, location=None,
                 override_barcode=None, override_batch=None):
    loc = location or line["location"]
    try:
        cur = c.execute(
            """INSERT INTO packs
               (serial, brand_id, barcode, batch, location, store_id, note_id,
                status, anomaly, received_at, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (line["serial"], line["brand_id"],
             override_barcode or line["line_barcode"],
             override_batch or line["batch"],
             loc, user["store_id"], line["note_id"], status, anomaly, now(), now()),
        )
    except sqlite3.IntegrityError:
        # UNIQUE(serial) 并发兜底：两个线程同时通过前置检查时，只有一个能插入
        raise DomainError(f"重复扫描（唯一约束拦截）：串码 {line['serial']} 已被另一笔扫描登记。")
    return c.execute("SELECT * FROM packs WHERE id=?", (cur.lastrowid,)).fetchone()


# ---------------------------------------------------------------- 销售

def mark_sold(conn, *, serial, user_id):
    """模拟前台售出：IN_STOCK -> SOLD。"""
    user = _get_user(conn, user_id)
    with db.write_tx(conn) as c:
        pack = _get_pack(c, serial)
        if pack is None:
            raise DomainError("串码不存在，无法销售。")
        if pack["store_id"] != user["store_id"]:
            raise DomainError("非本店条包，不能销售。")
        _check_transition(pack["status"], db.STATUS_SOLD)
        c.execute("UPDATE packs SET status=?, sold_at=? WHERE id=?",
                  (db.STATUS_SOLD, now(), pack["id"]))
        db.log_event(c, serial, "sale", user["display_name"],
                     from_status=pack["status"], to_status=db.STATUS_SOLD,
                     location=pack["location"], detail="前台售出。")
        return {"result": "sold", "pack": _pack_dict(_get_pack(c, serial))}


# ---------------------------------------------------------------- 退货申请

def create_return(conn, *, serial, user_id, reason, detail="", photo_path=None):
    """店员发起退货（售退/库内退货）。售退必须核对原始入库链路。"""
    user = _get_user(conn, user_id)
    require_role(user, "clerk")
    if reason not in ("damaged", "mismatch", "other"):
        raise DomainError("退货原因不合法。")

    block = None
    result = None
    with db.write_tx(conn) as c:
        pack = _get_pack(c, serial)
        if pack is None:
            raise DomainError("串码不存在，无法发起退货。")
        if pack["store_id"] != user["store_id"]:
            raise DomainError("非本店条包，不能发起退货。")

        line = _find_line(c, serial)
        if line is None:
            raise DomainError("缺少原始入库链路（无配送单记录），售退条包核对不通过，禁止退货。")

        # 超过退货期限：拦截事件要留痕，随事务提交后在事务外抛错
        window = _return_window(c, user["store_id"])
        if not within_return_window(line["delivered_at"], window):
            db.log_event(c, serial, "return_expired_blocked", user["display_name"],
                         from_status=pack["status"], to_status=pack["status"],
                         store_id=user["store_id"],
                         detail=f"超过退货期限：配送单 {line['note_id']} 签收于 "
                                f"{line['delivered_at'][:10]}，期限 {window} 天。")
            block = (f"超过退货期限：该条包配送单签收于 {line['delivered_at'][:10]}，"
                     f"本店退货期限 {window} 天，不能发起退货。")
        else:
            # 已有未结案申请则不允许重复发起
            dup = c.execute(
                "SELECT 1 FROM return_requests WHERE serial=? AND status IN ('pending','approved','outbound') LIMIT 1",
                (serial,)).fetchone()
            if dup:
                raise DomainError("该串码已存在处理中的退货申请，不能重复发起。")

            sold = _was_sold(c, serial)
            if sold:
                # 已售条包：只能先进隔离核对，绝不直接改回在库
                if pack["status"] == db.STATUS_SOLD:
                    _check_transition(pack["status"], db.STATUS_QUARANTINED)
                    c.execute("UPDATE packs SET status=?, anomaly=? WHERE id=?",
                              (db.STATUS_QUARANTINED, "return_verify", pack["id"]))
                    new_status = db.STATUS_QUARANTINED
                else:
                    new_status = pack["status"]
                chain_detail = "售退条包：已核对原始入库链路 " \
                               f"配送单 {line['note_id']}（{line['delivered_at'][:10]}，" \
                               f"批次 {line['batch']}，库位 {line['location']}），进入隔离待店长复核。"
            else:
                if pack["status"] == db.STATUS_IN_STOCK:
                    _check_transition(pack["status"], db.STATUS_QUARANTINED)
                    c.execute("UPDATE packs SET status=?, anomaly=? WHERE id=?",
                              (db.STATUS_QUARANTINED, "return_verify", pack["id"]))
                    new_status = db.STATUS_QUARANTINED
                else:
                    new_status = pack["status"]
                chain_detail = f"库内退货：入库链路 配送单 {line['note_id']}，进入隔离待复核。"

            cur = c.execute(
                """INSERT INTO return_requests
                   (serial, store_id, reason, detail, photo_path, status,
                    created_by, created_at)
                   VALUES (?,?,?,?,?, 'pending', ?, ?)""",
                (serial, user["store_id"], reason, detail, photo_path,
                 user["id"], now()),
            )
            req_id = cur.lastrowid
            db.log_event(c, serial, "return_request", user["display_name"],
                         from_status=pack["status"], to_status=new_status,
                         location=pack["location"],
                         detail=f"店员发起退货申请 #{req_id}（{reason}）。{chain_detail}",
                         ref_id=req_id)
            result = {"result": "requested", "request_id": req_id}

    if block:
        raise DomainError(block)
    return result


# ---------------------------------------------------------------- 店长复核

def review_return(conn, *, request_id, user_id, decision, note=""):
    """店长复核：approve 进入退货出库在途；reject 驳回。"""
    user = _get_user(conn, user_id)
    require_role(user, "manager")
    if decision not in ("approved", "rejected"):
        raise DomainError("复核结论不合法。")

    with db.write_tx(conn) as c:
        req = c.execute("SELECT * FROM return_requests WHERE id=?", (request_id,)).fetchone()
        if req is None:
            raise DomainError("退货申请不存在。")
        if req["store_id"] != user["store_id"]:
            raise AuthError("不能复核其他门店的退货申请。")
        if req["status"] != "pending":
            raise DomainError(f"申请 #{request_id} 当前状态 {req['status']}，不能重复复核。")

        pack = _get_pack(c, req["serial"])

        if decision == "approved":
            _check_transition(pack["status"], db.STATUS_RETURNING)
            c.execute("UPDATE packs SET status=?, anomaly=NULL WHERE id=?",
                      (db.STATUS_RETURNING, pack["id"]))
            c.execute(
                "UPDATE return_requests SET status='outbound', reviewed_by=?, "
                "reviewed_at=?, review_note=? WHERE id=?",
                (user["id"], now(), note, request_id))
            db.log_event(c, req["serial"], "return_approved", user["display_name"],
                         from_status=pack["status"], to_status=db.STATUS_RETURNING,
                         detail=f"店长复核通过（#{request_id}），待配送方回执。{note}",
                         ref_id=request_id)
            return {"result": "approved", "request_id": request_id}

        # rejected：售退条包即使驳回也不能回在库，继续隔离
        sold = _was_sold(c, req["serial"])
        if not sold and pack["status"] == db.STATUS_QUARANTINED:
            c.execute("UPDATE packs SET status=?, anomaly=NULL WHERE id=?",
                      (db.STATUS_IN_STOCK, pack["id"]))
            new_status = db.STATUS_IN_STOCK
        else:
            c.execute("UPDATE packs SET anomaly=? WHERE id=?",
                      ("return_rejected_keep", pack["id"]))
            new_status = pack["status"]
        c.execute(
            "UPDATE return_requests SET status='rejected', reviewed_by=?, "
            "reviewed_at=?, review_note=? WHERE id=?",
            (user["id"], now(), note, request_id))
        db.log_event(c, req["serial"], "return_rejected", user["display_name"],
                     from_status=pack["status"], to_status=new_status,
                     detail=(f"店长复核驳回（#{request_id}）。"
                             + ("该条包曾售出，按规则保留隔离，不回在库。" if sold else "")
                             + f" {note}"),
                     ref_id=request_id)
        return {"result": "rejected", "request_id": request_id}


# ---------------------------------------------------------------- 配送方回执

def call_carrier(conn, *, request_id, user_id):
    """请求配送方回执（模拟适配器）。按申请幂等，可安全重放。"""
    user = _get_user(conn, user_id)
    require_role(user, "manager")

    with db.write_tx(conn) as c:
        req = c.execute("SELECT * FROM return_requests WHERE id=?", (request_id,)).fetchone()
        if req is None:
            raise DomainError("退货申请不存在。")
        if req["store_id"] != user["store_id"]:
            raise AuthError("不能操作其他门店的退货申请。")
        if req["status"] not in ("outbound", "closed"):
            raise DomainError(f"申请 #{request_id} 状态 {req['status']}，尚未复核通过，不能索取回执。")

        pack = _get_pack(c, req["serial"])

        # 已有回执 => 重放：原样返回，不再改动任何状态
        prior = c.execute("SELECT * FROM carrier_receipts WHERE request_id=?",
                          (request_id,)).fetchone()
        if prior:
            db.log_event(c, req["serial"], "carrier_receipt_replay", user["display_name"],
                         from_status=pack["status"], to_status=pack["status"],
                         detail=f"回执重放：返回首次结论 {prior['decision']}（{prior['txn_id']}），状态不变。",
                         ref_id=request_id)
            return {"result": "replay", "decision": prior["decision"],
                    "txn_id": prior["txn_id"], "message": prior["message"]}

        decision, message, hint = simulate_carrier_decision(req["serial"], req["detail"] or "")
        txn_id = make_txn_id(request_id, req["serial"], hint)
        ts = now()
        c.execute(
            """INSERT INTO carrier_receipts
               (txn_id, request_id, serial, decision, message, received_at)
               VALUES (?,?,?,?,?,?)""",
            (txn_id, request_id, req["serial"], decision, message, ts))
        c.execute(
            "UPDATE return_requests SET status='closed', carrier_txn=?, receipt_at=?, "
            "receipt_decision=? WHERE id=?",
            (txn_id, ts, decision, request_id))

        sold = _was_sold(c, req["serial"])
        if decision == "accepted":
            _check_transition(pack["status"], db.STATUS_RETURNED)
            c.execute("UPDATE packs SET status=?, anomaly=NULL, location=? WHERE id=?",
                      (db.STATUS_RETURNED, "配送方收货点", pack["id"]))
            new_status = db.STATUS_RETURNED
            detail = f"配送方受理，最终出库完成。流水号 {txn_id}。{message}"
        else:
            # 配送方拒收：未售过的可回在库；售退条包不能回在库，保留隔离
            if not sold and pack["status"] == db.STATUS_RETURNING:
                c.execute("UPDATE packs SET status=?, anomaly=NULL, location=? WHERE id=?",
                          (db.STATUS_IN_STOCK, "门店库房", pack["id"]))
                new_status = db.STATUS_IN_STOCK
            else:
                c.execute("UPDATE packs SET status=?, anomaly=? WHERE id=?",
                          (db.STATUS_QUARANTINED, "carrier_rejected", pack["id"]))
                new_status = db.STATUS_QUARANTINED
            detail = f"配送方拒收退回。流水号 {txn_id}。{message}" + (
                " 该条包曾售出，按规则保留隔离，不回在库。" if sold else "")

        db.log_event(c, req["serial"], "carrier_receipt", user["display_name"],
                     from_status=pack["status"], to_status=new_status,
                     detail=detail, ref_id=request_id)
        return {"result": "receipt", "decision": decision,
                "txn_id": txn_id, "message": message}


# ---------------------------------------------------------------- 异常隔离处置

def dispose_anomaly(conn, *, serial, user_id, action, note=""):
    """店长按核查结论处置隔离条包：to_stock 解除隔离 / refuse 拒收退回。"""
    user = _get_user(conn, user_id)
    require_role(user, "manager")
    if action not in ("to_stock", "refuse"):
        raise DomainError("处置动作不合法。")

    with db.write_tx(conn) as c:
        pack = _get_pack(c, serial)
        if pack is None:
            raise DomainError("串码不存在。")
        if pack["store_id"] != user["store_id"]:
            raise AuthError("不能处置其他门店的条包。")
        if pack["status"] != db.STATUS_QUARANTINED:
            raise DomainError(f"条包状态 {pack['status']}，不在隔离中，无需处置。")

        sold = _was_sold(c, serial)
        if action == "to_stock":
            if sold:
                raise DomainError("状态逆转拦截：该条包曾售出，不能解除隔离改回在库。")
            if pack["anomaly"] in ("foreign_store",):
                raise DomainError("非本店入库条包不能转入本店库存，请走拒收/转运。")
            c.execute("UPDATE packs SET status=?, anomaly=NULL WHERE id=?",
                      (db.STATUS_IN_STOCK, pack["id"]))
            db.log_event(c, serial, "anomaly_release", user["display_name"],
                         from_status=db.STATUS_QUARANTINED, to_status=db.STATUS_IN_STOCK,
                         detail=f"核查无问题，解除隔离回在库。{note}")
            return {"result": "to_stock"}

        c.execute("UPDATE packs SET status=?, anomaly=? WHERE id=?",
                  (db.STATUS_RECV_REJECTED, pack["anomaly"], pack["id"]))
        db.log_event(c, serial, "anomaly_refuse", user["display_name"],
                     from_status=db.STATUS_QUARANTINED, to_status=db.STATUS_RECV_REJECTED,
                     detail=f"店长处决不入库，拒收挂账。{note}")
        return {"result": "refused"}


# ---------------------------------------------------------------- 库存差异

def record_stock_count(conn, *, brand_id, physical_qty, user_id, note=""):
    """录入实物盘点数，与系统在库数比对并保存差异快照。"""
    user = _get_user(conn, user_id)
    require_role(user, "manager")
    with db.write_tx(conn) as c:
        brand = c.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
        if not brand:
            raise DomainError("品牌规格不存在。")
        sysrow = c.execute(
            "SELECT COUNT(*) AS q FROM packs WHERE store_id=? AND brand_id=? AND status='IN_STOCK'",
            (user["store_id"], brand_id)).fetchone()
        system_qty = sysrow["q"]
        diff = physical_qty - system_qty
        cur = c.execute(
            """INSERT INTO stock_snapshots
               (store_id, brand_id, system_qty, physical_qty, diff, note, created_by, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (user["store_id"], brand_id, system_qty, physical_qty, diff,
             note, user["id"], now()))
        db.log_event(c, f"BRAND:{brand_id}", "stock_count", user["display_name"],
                     detail=f"盘点：系统 {system_qty} / 实物 {physical_qty} / 差异 {diff:+d}。{note}",
                     ref_id=cur.lastrowid)
        return {"result": "counted", "system_qty": system_qty,
                "physical_qty": physical_qty, "diff": diff, "snapshot_id": cur.lastrowid}


# ---------------------------------------------------------------- 查询视图

STATUS_TEXTS = {
    "IN_STOCK": "在库",
    "SOLD": "已售",
    "QUARANTINED": "异常隔离",
    "RETURNING": "退货在途",
    "RETURNED": "已出库",
    "RECV_REJECTED": "收货拒收",
}


def _pack_dict(row):
    if row is None:
        return None
    d = dict(row)
    d["status_text"] = STATUS_TEXTS.get(d["status"], d["status"])
    d["anomaly_text"] = ANOMALY_TEXTS.get(d["anomaly"] or "", d["anomaly"] or "")
    return d


def trace_pack(conn, serial):
    """条包追溯：主档 + 原始入库链路 + 完整流转事件。"""
    pack = _get_pack(conn, serial)
    line = _find_line(conn, serial)
    events = conn.execute(
        "SELECT * FROM flow_events WHERE serial=? ORDER BY id", (serial,)).fetchall()
    result = {
        "pack": _pack_dict(pack),
        "inbound_chain": dict(line) if line else None,
        "events": [dict(e) for e in events],
    }
    if pack:
        reqs = conn.execute(
            "SELECT * FROM return_requests WHERE serial=? ORDER BY id", (serial,)).fetchall()
        result["return_requests"] = [dict(r) for r in reqs]
    return result


def pending_returns(conn, store_id):
    rows = conn.execute(
        """SELECT r.*, u.display_name AS creator_name
           FROM return_requests r JOIN users u ON u.id=r.created_by
           WHERE r.store_id=? AND r.status='pending'
           ORDER BY r.id""",
        (store_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        pack = _get_pack(conn, r["serial"])
        d["pack"] = _pack_dict(pack)
        out.append(d)
    return out


def quarantine_list(conn, store_id):
    rows = conn.execute(
        """SELECT p.*, b.name AS brand_name, b.spec AS brand_spec
           FROM packs p LEFT JOIN brands b ON b.id=p.brand_id
           WHERE p.store_id=? AND p.status='QUARANTINED'
           ORDER BY p.id""",
        (store_id,)).fetchall()
    out = []
    for p in rows:
        d = _pack_dict(p)
        d["brand_name"] = p["brand_name"]
        d["brand_spec"] = p["brand_spec"]
        line = _find_line(conn, p["serial"])
        d["line"] = dict(line) if line else None
        out.append(d)
    return out


def inventory_groups(conn, store_id):
    """库存按规格分组，并可展开到条包。"""
    groups = conn.execute(
        """SELECT b.id AS brand_id, b.name AS brand_name, b.spec AS brand_spec,
                  b.barcode AS barcode, COUNT(p.id) AS qty
           FROM brands b
           LEFT JOIN packs p ON p.brand_id=b.id AND p.store_id=? AND p.status='IN_STOCK'
           GROUP BY b.id
           HAVING qty > 0
           ORDER BY b.name""",
        (store_id,)).fetchall()
    result = []
    for g in groups:
        d = dict(g)
        packs = conn.execute(
            """SELECT * FROM packs WHERE store_id=? AND brand_id=? AND status='IN_STOCK'
               ORDER BY serial""",
            (store_id, g["brand_id"])).fetchall()
        d["packs"] = [_pack_dict(p) for p in packs]
        result.append(d)
    return result


def blocked_events(conn, store_id, limit=30):
    """未形成库存/未放行成功的拦截事件：重复扫描、超期退货、来源不明拒收。"""
    rows = conn.execute(
        """SELECT * FROM flow_events
           WHERE store_id=?
             AND event_type IN
                 ('receive_duplicate_blocked','receive_reject_unknown','return_expired_blocked')
           ORDER BY id DESC LIMIT ?""",
        (store_id, limit)).fetchall()
    return [dict(r) for r in rows]


def stock_diff_view(conn, store_id):
    brands = conn.execute(
        """SELECT b.id, b.name, b.spec, b.barcode,
                  (SELECT COUNT(*) FROM packs p WHERE p.store_id=? AND p.brand_id=b.id
                     AND p.status='IN_STOCK') AS system_qty
           FROM brands b ORDER BY b.name""",
        (store_id,)).fetchall()
    latest = {}
    snaps = conn.execute(
        """SELECT s.*, b.name AS brand_name, b.spec AS brand_spec, u.display_name AS counter
           FROM stock_snapshots s
           JOIN brands b ON b.id=s.brand_id
           JOIN users u ON u.id=s.created_by
           WHERE s.store_id=? ORDER BY s.id DESC""",
        (store_id,)).fetchall()
    for s in snaps:
        latest.setdefault(s["brand_id"], dict(s))
    return {"brands": [dict(b) for b in brands],
            "snapshots": [dict(s) for s in snaps],
            "latest_by_brand": latest}

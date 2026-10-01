"""只读查询：条包追溯、退货待审、异常隔离、库存差异/分组库存。"""
from .services import ANOMALY_LABELS

STATE_LABELS = {
    "in_stock": "在库",
    "sold": "已售",
    "return_pending": "退货待审",
    "return_approved": "复核通过待回执",
    "outbound": "已出库",
    "quarantined": "异常隔离",
}


def _spec_map(conn):
    return {r["id"]: dict(r) for r in conn.execute("SELECT * FROM specs")}


def _store_map(conn):
    return {r["id"]: dict(r) for r in conn.execute("SELECT * FROM stores")}


def _user_map(conn):
    return {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, username, name, role FROM users")}


def trace(conn, code):
    pkg = conn.execute("SELECT * FROM packages WHERE code=?", (code,)).fetchone()
    if pkg is None:
        return None
    users = _user_map(conn)
    flows = []
    for f in conn.execute("SELECT * FROM flows WHERE code=? ORDER BY id", (code,)):
        d = dict(f)
        d["actor_name"] = users.get(f["actor"], {}).get("name", f["actor"])
        d["from_state_label"] = STATE_LABELS.get(f["from_state"], f["from_state"] or "—")
        d["to_state_label"] = STATE_LABELS.get(f["to_state"], f["to_state"] or "—")
        flows.append(d)
    rets = [dict(r) for r in conn.execute(
        "SELECT * FROM returns WHERE code=? ORDER BY id", (code,))]
    receipts = []
    if rets:
        ids = [r["id"] for r in rets]
        q = ",".join("?" * len(ids))
        receipts = [dict(r) for r in conn.execute(
            f"SELECT * FROM receipt_log WHERE return_id IN ({q})", ids)]
    pkgd = dict(pkg)
    pkgd["state_label"] = STATE_LABELS.get(pkg["state"], pkg["state"])
    pkgd["anomaly_label"] = ANOMALY_LABELS.get(pkg["anomaly_type"]) if pkg["anomaly_type"] else None
    spec = conn.execute("SELECT * FROM specs WHERE id=?", (pkg["spec_id"],)).fetchone()
    return {"package": pkgd, "spec": dict(spec) if spec else None,
            "flows": flows, "returns": rets, "receipts": receipts}


def returns_queue(conn, store_id):
    users = _user_map(conn)
    rows = conn.execute(
        "SELECT r.*, p.spec_id, p.batch FROM returns r "
        "JOIN packages p ON p.code=r.code WHERE p.store_id=? ORDER BY "
        "CASE r.status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END, r.id",
        (store_id,)).fetchall()
    specs = _spec_map(conn)
    out = []
    for r in rows:
        d = dict(r)
        d["clerk_name"] = users.get(r["clerk_id"], {}).get("name")
        d["manager_name"] = users.get(r["manager_id"], {}).get("name") if r["manager_id"] else None
        sp = specs.get(r["spec_id"])
        d["spec_text"] = f"{sp['brand']} {sp['spec_name']}" if sp else "（规格未知）"
        d["status_label"] = {"pending": "待店长复核", "approved": "待配送回执",
                             "rejected": "已驳回/拒绝", "outbound": "已出库",
                             "refused": "配送拒收隔离"}[r["status"]]
        rc = conn.execute("SELECT * FROM receipt_log WHERE return_id=?",
                          (r["id"],)).fetchone()
        d["receipt"] = dict(rc) if rc else None
        out.append(d)
    return out


def quarantine(conn, store_id):
    specs = _spec_map(conn)
    rows = conn.execute(
        "SELECT * FROM packages WHERE state='quarantined' AND store_id=? "
        "ORDER BY anomaly_type, code", (store_id,)).fetchall()
    out = []
    for p in rows:
        d = dict(p)
        sp = specs.get(p["spec_id"])
        d["spec_text"] = f"{sp['brand']} {sp['spec_name']}" if sp else "（规格未知/无计划）"
        d["anomaly_label"] = ANOMALY_LABELS.get(p["anomaly_type"], p["anomaly_type"])
        out.append(d)
    return out


def inventory_grouped(conn, store_id):
    """以规格分组，组内可展开到条包。"""
    specs = _spec_map(conn)
    groups = {}
    for p in conn.execute(
            "SELECT * FROM packages WHERE store_id=? ORDER BY spec_id, code",
            (store_id,)):
        g = groups.setdefault(p["spec_id"] or "UNKNOWN", {
            "spec_id": p["spec_id"],
            "spec_text": (lambda sp: f"{sp['brand']} {sp['spec_name']}" if sp
                          else "规格未知/无计划")(specs.get(p["spec_id"])),
            "counts": {"in_stock": 0, "sold": 0, "return_pending": 0,
                       "return_approved": 0, "outbound": 0, "quarantined": 0},
            "packages": []})
        g["counts"][p["state"]] = g["counts"].get(p["state"], 0) + 1
        d = dict(p)
        d["state_label"] = STATE_LABELS.get(p["state"], p["state"])
        g["packages"].append(d)
    return sorted(groups.values(), key=lambda g: g["spec_text"])


def inventory_diff(conn, store_id):
    """库存差异：按配送单逐行核对 应到/实到/在库/异常/已出库。"""
    specs = _spec_map(conn)
    stores = _store_map(conn)
    notes = conn.execute(
        "SELECT * FROM delivery_notes WHERE store_id=? ORDER BY id", (store_id,)).fetchall()
    result = []
    for n in notes:
        lines = conn.execute(
            "SELECT * FROM delivery_note_lines WHERE note_id=? ORDER BY id",
            (n["id"],)).fetchall()
        items = []
        arrived = missing = in_stock = quarantined = outbound = sold = 0
        for ln in lines:
            pkg = conn.execute("SELECT * FROM packages WHERE code=?",
                               (ln["expected_code"],)).fetchone()
            sp = specs.get(ln["spec_id"])
            if pkg is None:
                status = "missing"            # 应到未扫
                missing += 1
            else:
                arrived += 1
                status = pkg["state"]
                in_stock += pkg["state"] == "in_stock"
                quarantined += pkg["state"] == "quarantined"
                outbound += pkg["state"] == "outbound"
                sold += pkg["state"] in ("sold", "return_pending", "return_approved")
            items.append({"expected_code": ln["expected_code"],
                          "spec_text": f"{sp['brand']} {sp['spec_name']}" if sp else "?",
                          "status": status,
                          "status_label": STATE_LABELS.get(status, {
                              "missing": "应到未扫"}.get(status, status)),
                          "anomaly_type": pkg["anomaly_type"] if pkg else None,
                          "anomaly_label": ANOMALY_LABELS.get(pkg["anomaly_type"])
                                           if pkg and pkg["anomaly_type"] else None})
        # 非本店/无计划却扫进来的条包（计划外入库）
        extras = conn.execute(
            "SELECT code, anomaly_type FROM packages WHERE store_id=? AND state='quarantined' "
            "AND (note_id IS NULL OR note_id NOT IN (SELECT id FROM delivery_notes WHERE store_id=?))",
            (store_id, store_id)).fetchall()
        result.append({
            "note_id": n["id"], "carrier": n["carrier"],
            "delivered_at": n["delivered_at"],
            "expected": len(lines), "arrived": arrived, "missing": missing,
            "in_stock": in_stock, "quarantined": quarantined,
            "outbound": outbound, "sold": sold,
            "items": items,
            "extra_count": len(extras),
        })
    return {"store": stores.get(store_id), "notes": result}


def scan_recent(conn, limit=15):
    rows = conn.execute(
        "SELECT * FROM scan_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]

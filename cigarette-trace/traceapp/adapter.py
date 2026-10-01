"""模拟配送方回执适配器。

真实环境中配送方接口不可控，这里用本地确定性规则模拟：
- 串码以 'A' 结尾或 request.detail 含 @accept 标记 => accepted（最终出库）
- 串码以 'X' 结尾或 detail 含 @reject 标记 => rejected（退回门店）
- 其它 => accepted（默认受理）

同一退货申请反复请求（重放）时，返回首次结论且 txn_id 不变，
由领域层保证幂等。
"""
import hashlib


def simulate_carrier_decision(serial: str, detail: str = "") -> tuple:
    """返回 (decision, message, txn_hint)。txn_hint 用于派生稳定流水号。"""
    if "@reject" in detail or serial.endswith("X"):
        return ("rejected",
                "配送方核验：包装完好且无退货授权，不予受理，条包退回门店。",
                "rej")
    # 默认受理（含 '@accept' 标记或以 A 结尾的串码，演示更直观）
    return ("accepted",
            "配送方核验：符合退货条件，已收回条包，退货出库完成。",
            "acc")


def make_txn_id(request_id: int, serial: str, hint: str) -> str:
    """稳定流水号：同一申请无论重放多少次都相同。"""
    digest = hashlib.sha1(f"{request_id}|{serial}|{hint}".encode()).hexdigest()[:10].upper()
    return f"CR{request_id:06d}-{digest}"

"""配送方模拟适配器（离线、确定性）。

真实环境中这里会调用配送方的退货交接接口；演示中用内置的配送登记册判断：
* 串码在配送登记册内且不在批次争议清单 -> 接收
* 批次争议清单中的串码 -> 拒收（例如批次与配送方登记不符）

同一退货的幂等/重放由 services.carrier_receipt 持久化 receipt_log 保证，
适配器本身无状态，重复调用必然返回相同结果。
"""

# 配送方登记：批次与门店登记不一致的争议串码（演示数据，虚构）
BATCH_DISPUTE = {
    "YHY260004": "批次与配送方登记不符（门店 B400226 / 配送 B400299）",
}


class MockDeliveryAdapter:
    def __init__(self, registry_codes, dispute=None):
        self.registry = set(registry_codes)
        self.dispute = dispute or BATCH_DISPUTE

    def request_return(self, payload):
        code = payload["code"]
        receipt_no = f"RC-{code[-6:]}-01"
        if code not in self.registry:
            return {"receipt_no": receipt_no, "accepted": False,
                    "carrier": "云商物流（模拟）",
                    "reason": "配送登记册中无此串码的出库记录，拒绝接收"}
        if code in self.dispute:
            return {"receipt_no": receipt_no, "accepted": False,
                    "carrier": "云商物流（模拟）", "reason": self.dispute[code]}
        return {"receipt_no": receipt_no, "accepted": True,
                "carrier": "云商物流（模拟）",
                "reason": "封签与批次核验通过，同意收回"}

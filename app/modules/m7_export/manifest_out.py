"""运行清单导出：run_id、输入 sha256、规则版本、参数、计数与合计。"""
from __future__ import annotations

import json
from decimal import Decimal

from ...manifest import build_manifest


def manifest_text(run: dict, reconciliation: dict | None = None,
                  settlement: dict | None = None) -> str:
    payload = build_manifest(run)
    if reconciliation:
        payload["totals"] = {
            key: format(value, "f") if isinstance(value, Decimal) else value
            for key, value in reconciliation["totals"].items()
        }
        payload["counts"].update(reconciliation["counts"])
    if settlement:
        payload["settlement"] = {
            "plan_id": settlement["plan_id"],
            "policy": settlement["policy"],
            "status": settlement["status"],
            "summary": settlement["summary"],
            "vouchers": [
                {
                    "txn_no": row["txn_no"],
                    "account": row["account"],
                    "original": row["original"],
                    "applied_before": row["applied_before"],
                    "applied_now": row["applied_now"],
                    "remaining": row["remaining"],
                    "status": row["status"],
                    "carryover": row.get("carryover", False),
                    "legs": row["legs"],
                }
                for row in settlement["vouchers"]
            ],
            "allocations": [
                {
                    "bank_txn_no": item["bank_txn_no"],
                    "ledger_txn_no": item["ledger_txn_no"],
                    "amount": item["amount"],
                    "carryover": bool(item.get("carryover")),
                }
                for item in settlement["allocations"]
            ],
        }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)

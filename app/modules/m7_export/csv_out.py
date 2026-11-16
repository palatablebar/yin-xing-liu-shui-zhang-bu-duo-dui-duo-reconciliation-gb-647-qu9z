"""报告 CSV：合计、匹配组、未匹配、隔离四块，金额按十进制字符串原样输出。

大整数业务号不做数值转换，避免科学计数法与精度丢失。
"""
from __future__ import annotations

import csv
import io
from decimal import Decimal

from ... import reasons
from ..m5_matching.subset import TIER_NAMES

HEADER = ["区块", "编号", "层级", "银行笔数", "账簿笔数", "银行流水", "账簿凭证",
          "银行金额", "账簿金额", "差额", "手续费", "原因"]


def _money(value) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def report_text(records: list[dict], matches: list[dict], reconciliation: dict,
                settlement: dict | None = None) -> str:
    totals = reconciliation["totals"]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(HEADER)

    for key, label in (
        ("bank_total", "银行合计"),
        ("ledger_total", "账簿合计"),
        ("bank_fee", "手续费合计"),
        ("bank_matched", "银行匹配"),
        ("bank_unmatched", "银行未匹配"),
        ("ledger_matched", "账簿匹配"),
        ("ledger_unmatched", "账簿未匹配"),
        ("match_gap", "匹配差额"),
    ):
        writer.writerow(["合计", label, "", "", "", "", "", _money(totals[key]), "", "", "", ""])

    for group in matches:
        writer.writerow([
            "匹配组",
            group["match_id"],
            TIER_NAMES.get(int(group["tier"]), ""),
            len(group["bank_ids"]),
            len(group["ledger_ids"]),
            " ".join(group["bank_ids"]),
            " ".join(group["ledger_ids"]),
            _money(Decimal(group["bank_sum"])),
            _money(Decimal(group["ledger_sum"])),
            _money(Decimal(group["diff"])),
            _money(Decimal(group["fee_alloc"])),
            "",
        ])

    for item in reconciliation["differences"]:
        if item["kind"] == "容差":
            continue
        writer.writerow([
            item["kind"],
            item["ref"],
            item["side"],
            "",
            "",
            "",
            "",
            _money(item["amount"]),
            "",
            "",
            "",
            reasons.label(item["reason"]),
        ])

    if settlement:
        for row in settlement["vouchers"]:
            writer.writerow([
                "凭证核销",
                row["txn_no"],
                "结转" if row.get("carryover") else "",
                "",
                "",
                " ".join(leg["bank_txn_no"] for leg in row["legs"]),
                row["txn_no"],
                _money(Decimal(row["original"])),
                _money(Decimal(row["applied_now"])),
                _money(Decimal(row["remaining"])),
                "",
                "{} 已核销{} 剩余{}".format(
                    row["status"], _money(Decimal(row["applied_before"])
                                          + Decimal(row["applied_now"])),
                    _money(Decimal(row["remaining"]))),
            ])
        for item in settlement["allocations"]:
            writer.writerow([
                "核销分录",
                item["plan_id"],
                "",
                "",
                "",
                item["bank_txn_no"],
                item["ledger_txn_no"],
                _money(Decimal(item["amount"])),
                "",
                "",
                "",
                "结转尾款" if item.get("carryover") else "匹配组",
            ])

    for record in records:
        if record["status"] != "quarantine":
            continue
        writer.writerow([
            "隔离",
            record["txn_no"] or "-",
            "银行" if record["side"] == "bank" else "账簿",
            "",
            "",
            "",
            "",
            record.get("raw_amount", ""),
            "",
            "",
            "",
            f"{record['reason']} {reasons.label(record['reason'])}",
        ])

    return "\ufeff" + buffer.getvalue()

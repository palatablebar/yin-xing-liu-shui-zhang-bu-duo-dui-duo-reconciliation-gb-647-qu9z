"""核销方案：以 M5 匹配组为组合来源，只决定「整笔等待」还是「允许部分核销」。

为什么组合不在这里重新算：
- 一对多、多对一以及确定性择一已经在 M5 求过一次；核销层若再算一套，页面会出现
  两套互相矛盾的组合。本模块只消费 ``matches``，不生成组合。
- 它负责的是另外三件事：把已确认金额记成分录、留下可追踪的尾款、跨批次延续尾款。

两种策略只差一步：
- strict（整笔等待）：只有当组内每张凭证都能被整张结清时才记账；只要有一张凭证
  会剩下尾款，整组先不记，等后续结算一起结清。本次记账金额偏少，但不产生尾款。
- partial（允许部分核销）：组内先整笔结清、剩余额度再部分核销，剩余成为尾款，
  继续参与后续结算。本次记账金额更多、更及时，但会留下尾款。

两种策略的整笔结清顺序完全相同，区别只在 strict 会把「会产生凭证尾款」的整组
暂缓；partial 绝不会为凑额度把一张本来能整笔结清的凭证拆碎。

配对与边界：
- 组内先做整笔结清（金额大的优先，同额按日期先到先结），再做一次部分核销；
- 每条凭证最多被 ``max_legs`` 次结算共同承担，已达上限的凭证先等下一轮；
- 容差不用于抹尾：原始金额 − 已核销金额 = 剩余金额恒等，尾差正是需要监督的尾款；
- 尾款按「账户 + 业务号 + 原始金额」跨批次延续；同一批重跑命中同一计划，不重复记账。

本模块是纯函数：不读写存储，不修改传入行，原始金额只作为减数出现。
"""
from __future__ import annotations

import hashlib
from decimal import Decimal

POLICIES = {"strict": "整笔等待", "partial": "允许部分核销"}
ZERO = Decimal("0.00")


def ref(account: str, txn_no: str, original) -> tuple[str, str, str]:
    """核销台账的业务身份：账户 + 业务号 + 原始金额。"""
    return (account or "", txn_no or "", format(Decimal(str(original)), "f"))


def _order(row: dict) -> tuple:
    return (row.get("posted_at") or "", row.get("txn_no") or "")


def _prior_state(prior_allocations) -> tuple[dict, dict, dict]:
    """按业务身份汇总历史已核销金额与已占用笔数。"""
    bank: dict[tuple, Decimal] = {}
    ledger: dict[tuple, Decimal] = {}
    legs: dict[tuple, int] = {}
    for item in prior_allocations:
        amount = Decimal(str(item["amount"]))
        bank_key = ref(item.get("account", ""), item["bank_txn_no"],
                       item.get("bank_original", "0"))
        ledger_key = ref(item.get("account", ""), item["ledger_txn_no"],
                         item.get("ledger_original", "0"))
        bank[bank_key] = bank.get(bank_key, ZERO) + amount
        ledger[ledger_key] = ledger.get(ledger_key, ZERO) + amount
        legs[ledger_key] = legs.get(ledger_key, 0) + 1
    return bank, ledger, legs


def plan(groups: list[dict], settlements: list[dict], vouchers: list[dict],
         policy: str, prior_allocations=(), max_legs: int = 8,
         carryovers: list[dict] | None = None) -> dict:
    """按匹配组生成核销分配；历史已核销金额不会被再次使用。

    ``carryovers`` 是上期留下、本期未重导的凭证尾款（只含账户、方向、原始额、
    已核销额、日期）。本期匹配组用完后仍有剩余额度的结算款，可以继续结清这些
    尾款，这就是「尾款继续参与后续结算」。

    往期未用尽的**结算款**不做跨期自动配对：那等于绕开 M5 另造组合。它留在原计划
    的「未用结算款」里可见，由会计决定补导或人工处理。
    """
    if policy not in POLICIES:
        raise ValueError(f"未知核销策略：{policy!r}")
    carryovers = carryovers or []

    prior_bank, prior_ledger, prior_legs = _prior_state(prior_allocations)
    settlement_by_id = {row["txn_no"]: row for row in settlements}
    voucher_by_id = {row["txn_no"]: row for row in vouchers}
    settle_left = {
        row["txn_no"]: max(
            row["amount"] - prior_bank.get(ref(row["account"], row["txn_no"], row["amount"]), ZERO),
            ZERO,
        )
        for row in settlements
    }
    voucher_left = {
        row["txn_no"]: max(
            row["amount"] - prior_ledger.get(ref(row["account"], row["txn_no"], row["amount"]), ZERO),
            ZERO,
        )
        for row in vouchers
    }
    legs = {
        row["txn_no"]: prior_legs.get(ref(row["account"], row["txn_no"], row["amount"]), 0)
        for row in vouchers
    }

    allocations: list[dict] = []
    deferred: list[str] = []

    for group in sorted(groups, key=lambda item: (int(item["tier"]), item["match_id"])):
        bank_rows = [settlement_by_id[key] for key in group["bank_ids"]
                     if key in settlement_by_id]
        ledger_rows = [voucher_by_id[key] for key in group["ledger_ids"]
                       if key in voucher_by_id]
        if len(bank_rows) != len(group["bank_ids"]) or len(ledger_rows) != len(group["ledger_ids"]):
            continue

        group_legs = _group_legs(bank_rows, ledger_rows, settle_left, voucher_left,
                                 legs, max_legs)
        if not group_legs:
            continue

        if policy == "strict":
            # 整笔等待：会留下凭证尾款的组整组暂缓，尾款等后续结算一并结清。
            tail = any(
                voucher_left[row["txn_no"]] - sum(
                    (leg["amount"] for leg in group_legs if leg["ledger_txn_no"] == row["txn_no"]),
                    ZERO,
                ) > 0
                for row in ledger_rows
            )
            if tail:
                deferred.append(group["match_id"])
                continue

        for leg in group_legs:
            leg["seq"] = len(allocations)
            leg["match_id"] = group["match_id"]
            leg["carryover"] = False
            allocations.append(leg)
            settle_left[leg["bank_txn_no"]] -= leg["amount"]
            voucher_left[leg["ledger_txn_no"]] -= leg["amount"]
            legs[leg["ledger_txn_no"]] = legs.get(leg["ledger_txn_no"], 0) + 1

    carryover_legs = _settle_carryovers(
        carryovers, settlements, policy, settle_left, voucher_left, prior_legs,
        max_legs, len(allocations),
    )
    allocations.extend(carryover_legs)

    return _outcome(groups, settlements, vouchers, policy, allocations, deferred,
                    settle_left, voucher_left, prior_bank, prior_ledger,
                    legs, max_legs, carryovers)


def _settle_carryovers(carryovers, settlements, policy, settle_left, voucher_left,
                       prior_legs, max_legs, offset) -> list[dict]:
    """用本期尚未用完的结算款继续结清上期尾款；strict 不做部分核销。"""
    out: list[dict] = []
    pool = sorted(
        (item for item in carryovers
         if Decimal(str(item["remaining"])) > 0),
        key=lambda item: (item.get("posted_at") or "", item["txn_no"]),
    )
    bank_rows = sorted(settlements, key=_order)
    for item in pool:
        key = ref(item["account"], item["txn_no"], item["original"])
        remaining = Decimal(str(item["remaining"]))
        if prior_legs.get(key, 0) >= max_legs:
            continue
        for bank in bank_rows:
            if remaining <= 0:
                break
            capacity = settle_left.get(bank["txn_no"], ZERO)
            if capacity <= 0:
                continue
            if bank["account"] != item["account"] \
                    or int(bank["direction"]) != int(item["direction"]):
                continue
            take = min(capacity, remaining)
            if policy == "strict" and take < remaining:
                continue
            if take <= 0:
                continue
            out.append(
                {
                    "seq": offset + len(out),
                    "account": bank["account"],
                    "direction": int(bank["direction"]),
                    "bank_txn_no": bank["txn_no"],
                    "ledger_txn_no": item["txn_no"],
                    "amount": take,
                    "bank_original": bank["amount"],
                    "ledger_original": Decimal(str(item["original"])),
                    "bank_posted_at": bank.get("posted_at", ""),
                    "ledger_posted_at": item.get("posted_at", ""),
                    "match_id": None,
                    "carryover": True,
                }
            )
            settle_left[bank["txn_no"]] = capacity - take
            remaining -= take
    return out


def _group_legs(bank_rows, ledger_rows, settle_left, voucher_left, legs, max_legs) -> list[dict]:
    """组内先整笔结清、再部分核销；不修改传入状态。

    整笔优先保证 partial 不会为了凑额度把一张本来能整笔结清的凭证拆碎；
    容得下多张时优先金额大的，同额按凭证日期先到先结。
    """
    banks = sorted(bank_rows, key=_order)
    ledgers = sorted(ledger_rows, key=_order)
    bank_left = {row["txn_no"]: settle_left[row["txn_no"]] for row in banks}
    ledger_left = {row["txn_no"]: voucher_left[row["txn_no"]] for row in ledgers}
    leg_count = {row["txn_no"]: legs.get(row["txn_no"], 0) for row in ledgers}
    out: list[dict] = []

    def usable(ledger) -> bool:
        return (ledger_left[ledger["txn_no"]] > 0
                and leg_count[ledger["txn_no"]] < max_legs)

    def emit(bank, ledger, take: Decimal) -> None:
        out.append(
            {
                "account": bank["account"],
                "direction": int(bank["direction"]),
                "bank_txn_no": bank["txn_no"],
                "ledger_txn_no": ledger["txn_no"],
                "amount": take,
                "bank_original": bank["amount"],
                "ledger_original": ledger["amount"],
                "bank_posted_at": bank.get("posted_at", ""),
                "ledger_posted_at": ledger.get("posted_at", ""),
            }
        )
        bank_left[bank["txn_no"]] -= take
        ledger_left[ledger["txn_no"]] -= take
        leg_count[ledger["txn_no"]] += 1

    for bank in banks:
        # 整笔结清：优先金额最大且容得下的凭证。
        while bank_left[bank["txn_no"]] > 0:
            pool = [ledger for ledger in ledgers
                    if usable(ledger)
                    and ledger_left[ledger["txn_no"]] <= bank_left[bank["txn_no"]]]
            if not pool:
                break
            target = min(
                pool,
                key=lambda ledger: (-ledger_left[ledger["txn_no"]],) + _order(ledger),
            )
            emit(bank, target, ledger_left[target["txn_no"]])

        # 部分核销：剩余额度落到剩余额最接近的一张，最多一条分录。
        if bank_left[bank["txn_no"]] > 0:
            pool = [ledger for ledger in ledgers
                    if usable(ledger)
                    and ledger_left[ledger["txn_no"]] > bank_left[bank["txn_no"]]]
            if pool:
                target = min(
                    pool,
                    key=lambda ledger: (ledger_left[ledger["txn_no"]],) + _order(ledger),
                )
                emit(bank, target, bank_left[bank["txn_no"]])
    return out


def _outcome(groups, settlements, vouchers, policy, allocations, deferred,
             settle_left, voucher_left, prior_bank, prior_ledger, legs, max_legs,
             carryovers=()) -> dict:
    applied_now = {row["txn_no"]: ZERO for row in vouchers}
    for item in allocations:
        # 结转的往期凭证不在本批次有效行里，单独记在 carryover_state。
        if item["ledger_txn_no"] in applied_now:
            applied_now[item["ledger_txn_no"]] += item["amount"]

    carryover_state = {}
    for item in carryovers:
        key = ref(item["account"], item["txn_no"], item["original"])
        before = prior_ledger.get(key, ZERO)
        applied = sum(
            (leg["amount"] for leg in allocations
             if leg["ledger_txn_no"] == item["txn_no"]
             and leg.get("carryover")),
            ZERO,
        )
        original = Decimal(str(item["original"]))
        remaining = max(original - before - applied, ZERO)
        carryover_state[item["txn_no"]] = {
            "status": "settled" if remaining == 0 and (before > 0 or applied > 0)
            else "partial" if before > 0 or applied > 0 else "waiting",
            "original": format(original, "f"),
            "applied_before": format(before, "f"),
            "applied_now": format(applied, "f"),
            "remaining": format(remaining, "f"),
            "legs": sum(
                1 for leg in allocations
                if leg["ledger_txn_no"] == item["txn_no"] and leg.get("carryover")),
            "carryover": True,
            "account": item["account"],
            "posted_at": item.get("posted_at", ""),
        }

    settled, partial, waiting, over = [], [], [], []
    state = {}
    for voucher in vouchers:
        key = voucher["txn_no"]
        before = prior_ledger.get(ref(voucher["account"], key, voucher["amount"]), ZERO)
        applied = applied_now[key]
        remaining = voucher_left[key]
        if before + applied > voucher["amount"]:
            status = "over_applied"
            over.append(key)
        elif (before > 0 or applied > 0) and remaining == 0:
            status = "settled"
            settled.append(key)
        elif before > 0 or applied > 0:
            status = "partial"
            partial.append(key)
        else:
            status = "waiting"
            waiting.append(key)
        state[key] = {
            "status": status,
            "original": format(voucher["amount"], "f"),
            "applied_before": format(before, "f"),
            "applied_now": format(applied, "f"),
            "remaining": format(remaining, "f"),
            "legs": legs.get(key, 0),
        }

    posted = sum((item["amount"] for item in allocations), ZERO)
    residual = sum((value for value in voucher_left.values() if value > 0), ZERO)
    unallocated = sum((value for value in settle_left.values() if value > 0), ZERO)
    return {
        "allocations": allocations,
        "deferred_groups": deferred,
        "summary": {
            "policy": policy,
            "policy_label": POLICIES[policy],
            "allocation_count": len(allocations),
            "posted_amount": format(posted, "f"),
            "settled_vouchers": len(settled),
            "partial_vouchers": len(partial),
            "waiting_vouchers": len(waiting),
            "over_applied_vouchers": len(over),
            "voucher_count": len(vouchers),
            "settlement_count": len(settlements),
            "group_count": len(groups),
            "deferred_groups": len(deferred),
            "voucher_residual_amount": format(residual, "f"),
            "unallocated_settlement_amount": format(unallocated, "f"),
            "max_legs": max_legs,
        },
        "voucher_state": state,
        "carryover_state": carryover_state,
        "settlement_state": {
            row["txn_no"]: {
                "original": format(row["amount"], "f"),
                "applied_before": format(
                    prior_bank.get(ref(row["account"], row["txn_no"], row["amount"]), ZERO), "f"),
                "applied_now": format(
                    sum((leg["amount"] for leg in allocations
                         if leg["bank_txn_no"] == row["txn_no"]), ZERO), "f"),
                "remaining": format(settle_left[row["txn_no"]], "f"),
            }
            for row in settlements
        },
    }


def digest(policy: str, allocations: list[dict]) -> str:
    """方案指纹：同一策略 + 同一分配明细得到同一指纹。"""
    payload = policy + "|" + ";".join(
        f"{item['account']}|{item['bank_txn_no']}>{item['ledger_txn_no']}={item['amount']}"
        for item in allocations
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def source_digest(run_id: str, policy: str, groups: list[dict],
                  settlements: list[dict], vouchers: list[dict],
                  max_legs: int) -> str:
    parts = [run_id, policy, str(max_legs)]
    for group in sorted(groups, key=lambda item: item["match_id"]):
        parts.append("g|{}|{}|{}".format(
            group["match_id"], ",".join(group["bank_ids"]), ",".join(group["ledger_ids"])))
    parts += [
        "b|{}|{}|{}|{}".format(row["account"], row["txn_no"], row["amount"], row["direction"])
        for row in sorted(settlements, key=_order)
    ]
    parts += [
        "v|{}|{}|{}|{}".format(row["account"], row["txn_no"], row["amount"], row["direction"])
        for row in sorted(vouchers, key=_order)
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def compare(groups: list[dict], settlements: list[dict], vouchers: list[dict],
            prior_allocations, max_legs: int = 8, carryovers=None) -> dict:
    """并排给出两种策略，让复核人看清「多记多少」与「留下多少尾款」。"""
    return {
        policy: plan(groups, settlements, vouchers, policy, prior_allocations,
                     max_legs, carryovers)
        for policy in ("strict", "partial")
    }

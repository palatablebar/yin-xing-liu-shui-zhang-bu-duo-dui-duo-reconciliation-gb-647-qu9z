"""核销服务：把当前批次的有效流水/凭证交给核销模型，比较策略并原子记账。

读取范围只限当前批次的 ``valid`` 行；``quarantine`` 行永不进入核销，原始记录也
不会被改写。历史核销分录从台账读取，用于延续尾款与防止重复核销。
"""
from __future__ import annotations

from decimal import Decimal

from ...errors import DomainError
from ...manifest import utc_now_iso
from . import model

ZERO = Decimal("0.00")


def _rows(store, run_id: str, side: str) -> list[dict]:
    rows = []
    for record in store.records(run_id, side=side, status="valid"):
        rows.append(
            {
                "txn_no": record["txn_no"],
                "account": record["account"],
                "posted_at": record["posted_at"],
                "direction": int(record["direction"]),
                "amount": Decimal(str(record["amount"])),
                "memo": record.get("memo", ""),
            }
        )
    return rows


def _carryovers(registered: list[dict], prior: list[dict],
                current_vouchers: list[dict]) -> list[dict]:
    """历史仍未结清的凭证尾款：本期没有重导的那些，才需要结转。

    以凭证登记表为准，而不是只从分录反推；这样「整笔等待」中一分未记的凭证
    也会在下一期继续出现，不会凭空消失。
    """
    applied: dict[tuple, Decimal] = {}
    for item in prior:
        key = model.ref(item["account"], item["ledger_txn_no"], item["ledger_original"])
        applied[key] = applied.get(key, ZERO) + Decimal(str(item["amount"]))
    current = {(row["account"], row["txn_no"], format(row["amount"], "f"))
               for row in current_vouchers}
    out = []
    for row in registered:
        key = (row["account"], row["txn_no"], row["original"])
        if key in current:
            continue
        remaining = Decimal(row["original"]) - applied.get(key, ZERO)
        if remaining <= 0:
            continue
        out.append(
            {
                "txn_no": row["txn_no"],
                "account": row["account"],
                "direction": int(row["direction"]),
                "original": row["original"],
                "posted_at": row.get("posted_at", ""),
                "remaining": format(remaining, "f"),
            }
        )
    return sorted(out, key=lambda item: (item["posted_at"], item["txn_no"]))


def _vocabulary(store, run_id: str, exclude_plan_id: str | None = None) -> dict:
    """当前批次的有效两侧行 + M5 已求出的匹配组 + 历史分录与结转尾款。

    ``exclude_plan_id`` 用于「已记账后重新比较两种策略」：把本批已落的计划
    从历史里排除，看到的才是落到这份流水上的真实选择，而不是零分配。
    """
    settlements = _rows(store, run_id, "bank")
    vouchers = _rows(store, run_id, "ledger")
    prior = store.prior_allocations(exclude_plan_id=exclude_plan_id)
    return {
        "settlements": settlements,
        "vouchers": vouchers,
        "groups": store.matches(run_id),
        "prior": prior,
        "carryovers": _carryovers(store.registered_vouchers(), prior, vouchers),
    }


def _plan_from(vocab: dict, policy: str, settings) -> dict:
    return model.plan(
        vocab["groups"], vocab["settlements"], vocab["vouchers"], policy,
        vocab["prior"], settings.partial_max_legs, vocab["carryovers"],
    )


def quote(store, run: dict | None, settings) -> dict:
    """并排比较两种策略：复核人先看「即时记账多少、留下多少尾款」。"""
    if not run:
        return {"run_id": None, "policies": {}, "recommended": settings.settlement_policy}
    posted = store.posted_plan(run["run_id"])
    vocab = _vocabulary(store, run["run_id"],
                        exclude_plan_id=posted["plan_id"] if posted else None)
    outcomes = model.compare(vocab["groups"], vocab["settlements"], vocab["vouchers"],
                             vocab["prior"], settings.partial_max_legs,
                             vocab["carryovers"])
    return {
        "run_id": run["run_id"],
        "max_legs": settings.partial_max_legs,
        "recommended": settings.settlement_policy,
        "posted_plan_id": posted["plan_id"] if posted else None,
        "posted_policy": posted["policy"] if posted else None,
        "policies": {key: {"summary": value["summary"]} for key, value in outcomes.items()},
    }


def _build_plan(run: dict, policy: str, outcome: dict, settings) -> dict:
    source = model.source_digest(
        run["run_id"], policy, outcome["groups"], outcome["settlements"],
        outcome["vouchers"], settings.partial_max_legs,
    )
    return {
        "run_id": run["run_id"],
        "policy": policy,
        "source_digest": source,
        "summary": outcome["summary"],
        "allocations": outcome["allocations"],
        "max_legs": settings.partial_max_legs,
        "rule_version": settings.rule_version,
        "created_at": utc_now_iso(),
    }


def _prepare(store, run: dict, settings, policy: str | None) -> dict:
    policy = policy or settings.settlement_policy
    if policy not in model.POLICIES:
        raise DomainError("E_POLICY", "policy 只能是 strict 或 partial", 400)
    vocab = _vocabulary(store, run["run_id"])
    outcome = _plan_from(vocab, policy, settings)
    outcome["groups"] = vocab["groups"]
    outcome["settlements"] = vocab["settlements"]
    outcome["vouchers"] = vocab["vouchers"]
    return outcome


def post(store, run: dict | None, settings, policy: str | None = None) -> dict:
    """记账：同一批次 + 同一方案重试返回首次结果，不重复写分录。"""
    if not run:
        raise DomainError("E_NO_RUN", "还没有批次", 404)
    outcome = _prepare(store, run, settings, policy)
    plan = _build_plan(run, outcome["summary"]["policy"], outcome, settings)
    # 凭证登记与计划、分录同一事务：要么全落，要么全不落。
    record, created = store.post_plan(plan, outcome["vouchers"])
    payload = detail(store, record, settings)
    payload["created"] = created
    return payload


def revoke(store, settings, plan_id: str) -> dict:
    record = store.get_plan(plan_id)
    if not record:
        raise DomainError("E_NO_PLAN", "核销计划不存在", 404)
    if record["status"] != "posted":
        raise DomainError("E_PLAN_REVOKED", "该计划已撤销", 409)
    store.revoke_plan(plan_id)
    return detail(store, store.get_plan(plan_id), settings)


def _prior_by_ref(store, exclude_plan_id: str | None = None) -> dict:
    """历史已核销金额，按 (账户, 业务号, 原始金额) 归集；不绑批次。"""
    totals: dict[tuple, Decimal] = {}
    legs: dict[tuple, list[dict]] = {}
    for item in store.prior_allocations(exclude_plan_id=exclude_plan_id):
        key = (item["account"], item["ledger_txn_no"], item["ledger_original"])
        totals[key] = totals.get(key, ZERO) + Decimal(str(item["amount"]))
        legs.setdefault(key, []).append(item)
    return {"totals": totals, "legs": legs}


def _voucher_row(row: dict, by_voucher: dict, prior: dict, carryover: bool = False) -> dict:
    """一张凭证的核销全景：原始额、已核销（含往期）、剩余、承担它的每条结算。"""
    key = (row["account"], row["txn_no"], str(row["amount"]))
    original = Decimal(str(row["amount"]))
    before = prior["totals"].get(key, ZERO)
    applied = sum(
        (Decimal(str(item["amount"])) for item in by_voucher.get(row["txn_no"], [])),
        ZERO,
    )
    remaining = original - before - applied
    if remaining < 0:
        remaining = ZERO
    legs = [
        {
            "plan_id": item["plan_id"],
            "bank_txn_no": item["bank_txn_no"],
            "amount": format(Decimal(str(item["amount"])), "f"),
        }
        for item in sorted(
            by_voucher.get(row["txn_no"], []) + prior["legs"].get(key, []),
            key=lambda item: (item.get("bank_posted_at", ""), item["bank_txn_no"],
                              item["plan_id"]),
        )
    ]
    return {
        "txn_no": row["txn_no"],
        "account": row["account"],
        "posted_at": row["posted_at"],
        "direction": int(row["direction"]),
        "original": format(original, "f"),
        "applied_before": format(before, "f"),
        "applied_now": format(applied, "f"),
        "remaining": format(remaining, "f"),
        "status": ("settled" if remaining == 0 and (applied > 0 or before > 0)
                   else "partial" if applied > 0 or before > 0
                   else "waiting"),
        "carryover": carryover,
        "legs": legs,
    }


def detail(store, record: dict, settings) -> dict:
    """单份计划的完整台账：每张原始凭证的金额、已核销、剩余、承担它的结算。"""
    allocations = store.allocations(record["plan_id"])
    for item in allocations:
        item["carryover"] = bool(item.get("carryover"))
    run = store.get_run(record["run_id"])
    rows = store.records(record["run_id"]) if run else []
    valid = [row for row in rows if row["status"] == "valid"]

    by_voucher: dict[str, list[dict]] = {}
    by_settlement: dict[str, list[dict]] = {}
    for item in allocations:
        by_voucher.setdefault(item["ledger_txn_no"], []).append(item)
        by_settlement.setdefault(item["bank_txn_no"], []).append(item)

    prior = _prior_by_ref(store, exclude_plan_id=record["plan_id"])

    voucher_rows = []
    seen_vouchers = set()
    for row in valid:
        if row["side"] != "ledger":
            continue
        seen_vouchers.add(row["txn_no"])
        voucher_rows.append(
            _voucher_row(row, by_voucher, prior)
        )

    # 结转的往期尾款：不在本批次有效行里，但本计划的分录涉及它，也必须出现在台账上。
    for item in allocations:
        txn_no = item["ledger_txn_no"]
        if txn_no in seen_vouchers:
            continue
        seen_vouchers.add(txn_no)
        voucher_rows.append(
            _voucher_row(
                {
                    "txn_no": txn_no,
                    "account": item["account"],
                    "posted_at": item.get("ledger_posted_at", ""),
                    "direction": int(item["direction"]),
                    "amount": item["ledger_original"],
                },
                by_voucher,
                prior,
                carryover=True,
            )
        )

    settlement_rows = []
    for row in valid:
        if row["side"] != "bank":
            continue
        original = Decimal(str(row["amount"]))
        applied = sum(
            (Decimal(str(item["amount"])) for item in by_settlement.get(row["txn_no"], [])),
            ZERO,
        )
        settlement_rows.append(
            {
                "txn_no": row["txn_no"],
                "account": row["account"],
                "posted_at": row["posted_at"],
                "direction": int(row["direction"]),
                "original": format(original, "f"),
                "applied_now": format(applied, "f"),
                "unallocated": format(original - applied, "f"),
                "vouchers": [
                    {
                        "ledger_txn_no": item["ledger_txn_no"],
                        "amount": format(Decimal(str(item["amount"])), "f"),
                    }
                    for item in by_settlement.get(row["txn_no"], [])
                ],
            }
        )

    return {
        "plan_id": record["plan_id"],
        "run_id": record["run_id"],
        "policy": record["policy"],
        "policy_label": model.POLICIES.get(record["policy"], record["policy"]),
        "status": record["status"],
        "created_at": record["created_at"],
        "max_legs": record["max_legs"],
        "summary": record["summary"],
        "allocations": allocations,
        "vouchers": voucher_rows,
        "settlements": settlement_rows,
        "generated_at": utc_now_iso(),
    }


def ledger_payload(store, run: dict | None, settings) -> dict:
    """台账总览：已落地计划 + 每张凭证的核销进度（未记账时给出策略预览）。"""
    if not run:
        return {"run_id": None, "plan": None, "plans": [], "vouchers": []}
    posted = store.posted_plan(run["run_id"])
    plans = [
        {
            "plan_id": item["plan_id"],
            "run_id": item["run_id"],
            "policy": item["policy"],
            "policy_label": model.POLICIES.get(item["policy"], item["policy"]),
            "status": item["status"],
            "created_at": item["created_at"],
            "posted_amount": item["summary"].get("posted_amount", "0.00"),
        }
        for item in store.list_plans(20)
    ]
    if not posted:
        vocab = _vocabulary(store, run["run_id"])
        preview = _plan_from(vocab, settings.settlement_policy, settings)
        rows = [
            {
                "txn_no": row["txn_no"],
                "account": row["account"],
                "posted_at": row["posted_at"],
                "original": preview["voucher_state"][row["txn_no"]]["original"],
                "applied_before": preview["voucher_state"][row["txn_no"]]["applied_before"],
                "applied_now": preview["voucher_state"][row["txn_no"]]["applied_now"],
                "remaining": preview["voucher_state"][row["txn_no"]]["remaining"],
                "status": preview["voucher_state"][row["txn_no"]]["status"],
                "carryover": False,
                "legs": [],
            }
            for row in vocab["vouchers"]
        ]
        for txn_no, state in preview["carryover_state"].items():
            rows.append(
                {
                    "txn_no": txn_no,
                    "account": state["account"],
                    "posted_at": state["posted_at"],
                    "original": state["original"],
                    "applied_before": state["applied_before"],
                    "applied_now": state["applied_now"],
                    "remaining": state["remaining"],
                    "status": state["status"],
                    "carryover": True,
                    "legs": [],
                }
            )
        return {
            "run_id": run["run_id"],
            "plan": None,
            "plans": plans,
            "vouchers": rows,
            "summary": preview["summary"],
            "preview_policy": settings.settlement_policy,
            "carryovers": vocab["carryovers"],
        }
    payload = detail(store, posted, settings)
    payload.update({"plans": plans, "plan": posted["plan_id"], "preview_policy": None})
    return payload

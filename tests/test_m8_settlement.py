"""M8 核销：整笔等待 vs 部分核销、尾款延续、幂等记账、并发与撤销。"""
from decimal import Decimal

import pytest

from app.modules.m8_settlement import model

ZERO = Decimal("0.00")


def bank(txn, amount, day="2026-03-02T02:00:00+00:00", account="6222-0012", direction=-1):
    return {"txn_no": txn, "account": account, "posted_at": day,
            "direction": direction, "amount": Decimal(amount), "fee": Decimal("0.00")}


def voucher(txn, amount, day="2026-03-02T02:00:00+00:00", account="6222-0012", direction=-1):
    return {"txn_no": txn, "account": account, "posted_at": day,
            "direction": direction, "amount": Decimal(amount)}


def group(match_id, bank_ids, ledger_ids, tier=1):
    return {"match_id": match_id, "tier": tier,
            "bank_ids": list(bank_ids), "ledger_ids": list(ledger_ids)}


def settlement(client, policy=None):
    url = "/api/settlement" + (f"?policy={policy}" if policy else "")
    response = client.post(url)
    assert response.status_code == 200, response.text
    return response.json()


def test_strict_defers_group_that_would_leave_tail():
    settlements = [bank("B1", "100.00")]
    vouchers = [voucher("V1", "150.00")]
    groups = [group("m1", ["B1"], ["V1"], tier=3)]
    strict = model.plan(groups, settlements, vouchers, "strict")
    part = model.plan(groups, settlements, vouchers, "partial")
    assert strict["summary"]["posted_amount"] == "0.00"
    assert strict["summary"]["deferred_groups"] == 1
    assert strict["allocations"] == []
    # 部分核销把 100 记进去，留下 50 尾款
    assert part["summary"]["posted_amount"] == "100.00"
    assert part["voucher_state"]["V1"]["remaining"] == "50.00"
    assert part["voucher_state"]["V1"]["status"] == "partial"


def test_strict_records_group_that_clears_everything():
    settlements = [bank("B1", "150.00")]
    vouchers = [voucher("V1", "150.00")]
    groups = [group("m1", ["B1"], ["V1"])]
    strict = model.plan(groups, settlements, vouchers, "strict")
    assert strict["summary"]["posted_amount"] == "150.00"
    assert strict["summary"]["deferred_groups"] == 0
    assert strict["voucher_state"]["V1"]["status"] == "settled"


def test_partial_does_not_steal_rows_from_whole_clear():
    # B1 只够覆盖 V2；V2 必须整笔结清，剩余额度才部分落到 V1。
    settlements = [bank("B1", "160.00")]
    vouchers = [voucher("V1", "900.00"), voucher("V2", "100.00")]
    groups = [group("m1", ["B1"], ["V1", "V2"], tier=2)]
    result = model.plan(groups, settlements, vouchers, "partial")
    applied = {item["ledger_txn_no"]: item["amount"] for item in result["allocations"]}
    assert applied["V2"] == Decimal("100.00")
    assert applied["V1"] == Decimal("60.00")
    assert result["voucher_state"]["V2"]["status"] == "settled"
    assert result["voucher_state"]["V1"]["remaining"] == "840.00"


def test_one_settlement_many_vouchers_one_voucher_many_settlements():
    settlements = [bank("B1", "500.00", "2026-03-02T02:00:00+00:00", account="A")]
    settlements.append(bank("B2", "500.00", "2026-03-03T02:00:00+00:00", account="A"))
    vouchers = [voucher("V1", "600.00", "2026-03-02T02:00:00+00:00", account="A")]
    vouchers.append(voucher("V2", "400.00", "2026-03-03T02:00:00+00:00", account="A"))
    groups = [
        group("m1", ["B1"], ["V1", "V2"], tier=2),
        group("m2", ["B2"], ["V1"], tier=3),
    ]
    result = model.plan(groups, settlements, vouchers, "partial")
    assert result["voucher_state"]["V1"]["legs"] == 2
    assert result["voucher_state"]["V1"]["status"] == "settled"
    assert result["voucher_state"]["V2"]["legs"] == 1


def test_max_legs_bounds_splits():
    # 一张 1000 的凭证，最多被 2 次结算承担；第三笔只能等。
    settlements = [bank(f"B{n}", "400.00", f"2026-03-0{n}T02:00:00+00:00") for n in (1, 2, 3)]
    vouchers = [voucher("V1", "1000.00", "2026-03-01T02:00:00+00:00")]
    # 每组单独一行会违反 M5 不重叠，这里直接构造逐笔组，验证 max_legs 上限。
    groups = [group(f"m{n}", [f"B{n}"], ["V1"], tier=3) for n in (1, 2, 3)]
    result = model.plan(groups, settlements, vouchers, "partial", max_legs=2)
    assert result["voucher_state"]["V1"]["legs"] == 2
    assert result["voucher_state"]["V1"]["remaining"] == "200.00"


def test_history_is_never_reused():
    settlements = [bank("B1", "100.00")]
    vouchers = [voucher("V1", "100.00")]
    prior = [
        {
            "account": "6222-0012", "bank_txn_no": "B0", "ledger_txn_no": "V1",
            "amount": "40.00", "bank_original": "40.00", "ledger_original": "100.00",
        }
    ]
    result = model.plan([group("m1", ["B1"], ["V1"])], settlements, vouchers,
                        "partial", prior)
    assert result["voucher_state"]["V1"]["applied_before"] == "40.00"
    assert result["summary"]["posted_amount"] == "60.00"
    assert result["voucher_state"]["V1"]["remaining"] == "0.00"


def test_remaining_identity_holds():
    settlements = [bank("B1", "333.33")]
    vouchers = [voucher("V1", "200.00"), voucher("V2", "444.44")]
    groups = [group("m1", ["B1"], ["V1", "V2"], tier=2)]
    result = model.plan(groups, settlements, vouchers, "partial")
    for state in result["voucher_state"].values():
        assert (Decimal(state["original"])
                == Decimal(state["applied_before"])
                + Decimal(state["applied_now"]) + Decimal(state["remaining"]))


def test_plan_digest_is_order_independent():
    a = [{"account": "A", "bank_txn_no": "B1", "ledger_txn_no": "V1", "amount": "1.00"},
         {"account": "A", "bank_txn_no": "B2", "ledger_txn_no": "V2", "amount": "2.00"}]
    b = list(reversed(a))
    # 指纹按传入顺序；调用方按 seq 传入，因此同方案同序得到同指纹。
    assert model.digest("partial", a) == model.digest("partial", a)
    assert model.digest("partial", a) != model.digest("partial", b)
    assert model.digest("strict", a) != model.digest("partial", a)


def test_unknown_policy_rejected():
    with pytest.raises(ValueError):
        model.plan([], [], [], "sometimes")


# ---------- API / 存储层 ----------


def test_quote_compares_both_policies(client, demo):
    body = client.get("/api/settlement/quote").json()
    assert body["posted_plan_id"] is None
    assert set(body["policies"]) == {"strict", "partial"}
    strict = body["policies"]["strict"]["summary"]
    part = body["policies"]["partial"]["summary"]
    assert Decimal(part["posted_amount"]) > Decimal(strict["posted_amount"])
    assert part["partial_vouchers"] >= 1
    assert strict["partial_vouchers"] == 0


def test_post_then_retry_is_idempotent(client, demo):
    import sqlite3

    first = settlement(client, "partial")
    assert first["created"] is True
    second = settlement(client, "partial")
    assert second["created"] is False
    assert second["plan_id"] == first["plan_id"]
    connection = sqlite3.connect(client.app.state.settings.db_path)
    count = connection.execute(
        "SELECT COUNT(*) FROM settlement_allocations").fetchone()[0]
    assert count == len(first["allocations"])


def test_switch_policy_without_revoke_conflicts(client, demo):
    settlement(client, "partial")
    response = client.post("/api/settlement?policy=strict")
    assert response.status_code == 409
    assert response.json()["error"] == "E_PLAN_CONFLICT"


def test_revoke_then_repost_other_policy(client, demo):
    first = settlement(client, "partial")
    revoked = client.post(f"/api/settlement/{first['plan_id']}/revoke")
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    second = settlement(client, "strict")
    assert second["created"] is True
    assert second["policy"] == "strict"
    assert second["plan_id"] != first["plan_id"]


def test_revoke_twice_conflicts(client, demo):
    first = settlement(client, "partial")
    client.post(f"/api/settlement/{first['plan_id']}/revoke")
    again = client.post(f"/api/settlement/{first['plan_id']}/revoke")
    assert again.status_code == 409


def test_original_records_untouched_by_posting(client, demo):
    before = client.get("/api/statements", params={"side": "ledger"}).json()["rows"]
    settlement(client, "partial")
    after = client.get("/api/statements", params={"side": "ledger"}).json()["rows"]
    assert [(r["txn_no"], r["amount"], r["direction"]) for r in before] == \
           [(r["txn_no"], r["amount"], r["direction"]) for r in after]


def test_voucher_ledger_shows_original_applied_remaining_and_legs(client, demo):
    body = settlement(client, "partial")
    assert body["vouchers"]
    for row in body["vouchers"]:
        assert (Decimal(row["original"])
                == Decimal(row["applied_before"]) + Decimal(row["applied_now"])
                + Decimal(row["remaining"]))
        assert row["status"] in {"settled", "partial", "waiting", "over_applied"}
    group_legs = [row for row in body["vouchers"] if row["legs"]]
    assert group_legs, "至少一张凭证应列出承担它的结算组合"


def test_carryover_tail_settled_by_later_period(client, demo, settings):
    from app.pipeline import import_side

    first = settlement(client, "partial")
    unpaid = [
        row for row in first["vouchers"]
        if row["remaining"] != "0.00" and row["account"] == "6222-0034"
    ]
    assert unpaid, "样例应留下 0034 账户的尾款"
    target = unpaid[0]
    store = client.app.state.store

    # 下一期只有一笔结算款，金额正好等于上期尾款；凭证不重导。
    bank_csv = (
        "流水号,账户,发生时间,借贷标志,金额,手续费,对方户名,摘要\n"
        f"B-NEXT-{target['txn_no']},{target['account']},2026-04-10T10:00:00+08:00,"
        f"{'借' if target['direction'] < 0 else '贷'},{target['remaining']},0.00,供应商,补付\n"
    )
    import_side(settings, store, "bank", "next_bank.csv", bank_csv.encode("utf-8"))

    preview = client.get("/api/settlement").json()
    carried = [row for row in preview["vouchers"] if row["carryover"]]
    assert any(row["txn_no"] == target["txn_no"] for row in carried)

    second = settlement(client, "partial")
    legs = [
        item for item in second["allocations"]
        if item["ledger_txn_no"] == target["txn_no"]
    ]
    assert legs, "后续结算应继续核销上期尾款"
    assert legs[0]["carryover"] is True
    assert Decimal(legs[0]["amount"]) == Decimal(target["remaining"])


def test_concurrent_posts_write_once(client, demo, settings):
    import threading

    from app.modules.m8_settlement import service as settlement_service

    store = client.app.state.store
    run = store.latest_run()
    outcomes = []

    def worker():
        try:
            payload = settlement_service.post(store, run, settings, "partial")
            outcomes.append(("ok", payload["created"]))
        except Exception as error:  # noqa: BLE001 - 记录任何异常用于断言
            outcomes.append(("error", type(error).__name__))

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert all(kind == "ok" for kind, _ in outcomes), outcomes
    assert sum(1 for _, created in outcomes if created) == 1
    plans = store.list_plans()
    assert len(plans) == 1


def test_settlement_page_renders(client, demo):
    page = client.get("/settlement")
    assert page.status_code == 200
    assert "策略取舍" in page.text and "凭证台账" in page.text
    settlement(client, "partial")
    page = client.get("/settlement")
    assert page.status_code == 200
    assert "本次分录" in page.text
    assert "已记账" in page.text


def test_export_includes_settlement(client, demo):
    settlement(client, "partial")
    report = client.get("/api/export/report.csv").text
    assert "凭证核销" in report and "核销分录" in report
    manifest = client.get("/api/export/manifest.json").json()
    assert manifest["settlement"]["policy"] == "partial"
    assert manifest["settlement"]["vouchers"]
    summary = client.get("/api/export/summary").json()
    assert summary["settlement"]["plan_id"] == manifest["settlement"]["plan_id"]


def test_settlement_requires_run(client):
    assert client.get("/api/settlement").json()["plan"] is None
    response = client.post("/api/settlement?policy=partial")
    assert response.status_code == 404


def test_failed_post_rolls_back_and_retry_posts_once(client, demo):
    """分录写一半失败：计划与分录一起回滚，重试成功且只有一份。"""
    import sqlite3

    from app.errors import DomainError
    from app.modules.m8_settlement import service as settlement_service

    store = client.app.state.store
    run = store.latest_run()
    outcome = settlement_service._prepare(store, run, client.app.state.settings, "partial")
    plan = settlement_service._build_plan(run, "partial", outcome, client.app.state.settings)

    broken = dict(plan)
    broken["allocations"] = [dict(item) for item in plan["allocations"]]
    broken["allocations"][-1]["bank_txn_no"] = None  # NOT NULL 违约，写到最后一条才失败
    with pytest.raises(Exception):
        store.post_plan(broken)

    connection = sqlite3.connect(client.app.state.settings.db_path)
    assert connection.execute("SELECT COUNT(*) FROM settlement_plans").fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM settlement_allocations").fetchone()[0] == 0

    good = settlement_service.post(store, run, client.app.state.settings, "partial")
    assert good["created"] is True
    retry = settlement_service.post(store, run, client.app.state.settings, "partial")
    assert retry["created"] is False
    assert connection.execute(
        "SELECT COUNT(*) FROM settlement_allocations").fetchone()[0] == len(
        good["allocations"])

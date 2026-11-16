"""导出服务：报告 CSV 与运行清单 JSON。"""
from __future__ import annotations

from ...errors import DomainError
from ..m6_diff import differences
from ..m8_settlement import service as settlement_service
from . import csv_out, manifest_out


def _reconciliation(store, run: dict, settings) -> dict:
    return differences.reconcile(store.records(run["run_id"]), store.matches(run["run_id"]), settings)


def _settlement(store, run: dict, settings) -> dict | None:
    posted = store.posted_plan(run["run_id"])
    return settlement_service.detail(store, posted, settings) if posted else None


def report(store, run: dict | None, settings) -> str:
    if not run:
        raise DomainError("E_NO_RUN", "还没有批次", 404)
    result = _reconciliation(store, run, settings)
    return csv_out.report_text(store.records(run["run_id"]), store.matches(run["run_id"]),
                               result, _settlement(store, run, settings))


def manifest(store, run: dict | None, settings) -> str:
    if not run:
        raise DomainError("E_NO_RUN", "还没有批次", 404)
    result = _reconciliation(store, run, settings)
    return manifest_out.manifest_text(run, result, _settlement(store, run, settings))


def summary(store, run: dict | None, settings) -> dict:
    if not run:
        return {"run_id": None, "counts": {}, "totals": {}}
    result = _reconciliation(store, run, settings)
    posted = store.posted_plan(run["run_id"])
    return {
        "run_id": run["run_id"],
        "counts": result["counts"],
        "totals": {key: format(value, "f") for key, value in result["totals"].items()},
        "settlement": {
            "plan_id": posted["plan_id"],
            "policy": posted["policy"],
            "status": posted["status"],
            "summary": posted["summary"],
        } if posted else None,
    }

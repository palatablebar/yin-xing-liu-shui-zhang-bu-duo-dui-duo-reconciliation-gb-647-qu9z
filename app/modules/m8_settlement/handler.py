"""核销入口：比较策略、落地记账、查看台账、撤销重记。

记账是幂等的：同一批次同一方案重复 POST 返回首次计划，不新增分录；
同一批次换策略 POST 返回 409，必须先撤销旧计划，避免同一批流水被记两次。
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from ...errors import DomainError
from . import service

router = APIRouter(prefix="/api/settlement", tags=["核销"])


def _run(request: Request) -> dict | None:
    return request.app.state.store.latest_run()


@router.get("/quote")
def get_quote(request: Request):
    return service.quote(request.app.state.store, _run(request),
                         request.app.state.settings)


@router.get("")
def get_settlement(request: Request):
    return service.ledger_payload(request.app.state.store, _run(request),
                                  request.app.state.settings)


@router.post("")
def post_settlement(request: Request, policy: str | None = None):
    store = request.app.state.store
    return service.post(store, _run(request), request.app.state.settings, policy)


@router.get("/{plan_id}")
def get_plan(request: Request, plan_id: str):
    store = request.app.state.store
    record = store.get_plan(plan_id)
    if not record:
        raise DomainError("E_NO_PLAN", "核销计划不存在", 404)
    return service.detail(store, record, request.app.state.settings)


@router.post("/{plan_id}/revoke")
def revoke_plan(request: Request, plan_id: str):
    store = request.app.state.store
    return service.revoke(store, request.app.state.settings, plan_id)

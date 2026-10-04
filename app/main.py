"""FastAPI 应用：家庭储能时段调度。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.audit import audit_telemetry
from app.models import (
    DispatchRequest,
    DispatchResponse,
    ErrorResponse,
    RescheduleRequest,
    RescheduleResponse,
    SettlementRequest,
    SettlementResponse,
    TelemetryAuditRequest,
    TelemetryAuditResponse,
)
from app.reschedule import RescheduleError, execute_reschedule
from app.settlement import SettlementError, execute_settlement
from app.solver import InfeasibleError, solve_dispatch

app = FastAPI(
    title="家庭储能时段调度 API",
    description=(
        "输入半小时电价/基础负荷/光伏预测与电池参数，"
        "按 0.1kWh 离散 SOC 做动态规划，求满足功率、SOC 与末端约束的最低总成本计划；"
        "另提供 /telemetry/audit 对半小时实测记录做电芯能量守恒与购电量核对"
        "（只审计标记异常，不修正读数、不生成调度计划）；"
        "/dispatch/settlement 对已下发计划做执行结算：重算校验计划快照、"
        "复用遥测审计并核对 SOC 轨迹，仅对首个异常/断点之前的连续有效区间计费；"
        "/dispatch/reschedule 在计划执行中途按实测状态续排：重算校验快照、"
        "遥测审计核对已执行前缀后，从实测末态出发按剩余时段的新预测重排，"
        "并给出可直接交给结算接口的余段调度输入与计划快照。"
    ),
    version="1.3.0",
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """把 Pydantic 校验错误整理成一条可读的中文原因。"""
    reasons = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p not in ("body",))
        msg = err.get("msg", "参数不合法")
        reasons.append(f"{loc + ': ' if loc else ''}{msg}")
    reason = "；".join(reasons) if reasons else "请求参数不合法"
    return JSONResponse(
        status_code=422,
        content={"error": "invalid_input", "reason": reason},
    )


@app.exception_handler(InfeasibleError)
async def infeasible_exception_handler(request: Request, exc: InfeasibleError):
    return JSONResponse(
        status_code=422,
        content={"error": "infeasible", "reason": str(exc)},
    )


@app.exception_handler(SettlementError)
async def settlement_error_handler(request: Request, exc: SettlementError):
    return JSONResponse(
        status_code=422,
        content={"error": exc.code, "reason": str(exc)},
    )


@app.exception_handler(RescheduleError)
async def reschedule_error_handler(request: Request, exc: RescheduleError):
    return JSONResponse(
        status_code=422,
        content={"error": exc.code, "reason": str(exc)},
    )


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    return JSONResponse(
        status_code=422,
        content={"error": "invalid_input", "reason": str(exc)},
    )


@app.get("/healthz", tags=["meta"])
async def healthz():
    return {"status": "ok"}


@app.post(
    "/dispatch",
    response_model=DispatchResponse,
    responses={422: {"model": ErrorResponse, "description": "非法输入或问题无解"}},
    tags=["dispatch"],
)
async def dispatch(req: DispatchRequest) -> DispatchResponse:
    result = solve_dispatch(
        prices=req.prices,
        base_load_kw=req.base_load_kw,
        pv_kw=req.pv_kw,
        capacity_kwh=req.capacity_kwh,
        max_charge_kw=req.max_charge_kw,
        max_discharge_kw=req.max_discharge_kw,
        charge_efficiency=req.charge_efficiency,
        discharge_efficiency=req.discharge_efficiency,
        initial_soc_kwh=req.initial_soc_kwh,
        min_terminal_soc_kwh=req.min_terminal_soc_kwh,
        degradation_cost=req.degradation_cost_per_kwh,
    )
    return DispatchResponse.model_validate(result)


@app.post(
    "/telemetry/audit",
    response_model=TelemetryAuditResponse,
    responses={422: {"model": ErrorResponse, "description": "非法输入"}},
    tags=["telemetry"],
)
async def telemetry_audit(req: TelemetryAuditRequest) -> TelemetryAuditResponse:
    """核对实测记录的能量守恒与购电量一致性；异常记录照常返回 200 与分项残差。"""
    result = audit_telemetry(
        charge_efficiency=req.charge_efficiency,
        discharge_efficiency=req.discharge_efficiency,
        energy_tolerance_kwh=req.energy_tolerance_kwh,
        records=req.records,
    )
    return TelemetryAuditResponse.model_validate(result)


@app.post(
    "/dispatch/settlement",
    response_model=SettlementResponse,
    responses={422: {"model": ErrorResponse, "description": "非法输入或结算整体拒绝"}},
    tags=["settlement"],
)
async def dispatch_settlement(req: SettlementRequest) -> SettlementResponse:
    """计划执行结算：重算校验快照 → 遥测审计 → SOC 轨迹核对 → 有效前缀计费。

    - 计划快照与重算不一致（plan_snapshot_mismatch）、记录长度不符
      （settlement_length_mismatch）、无有效区间（no_valid_interval）均整体拒绝；
    - 存在异常/SOC 断点时不报错，返回 status=partially_settled 与首个异常下标。
    """
    result = execute_settlement(
        dispatch_input=req.dispatch.model_dump(),
        plan_snapshot=req.plan_snapshot.model_dump(),
        settlement_tolerance_kwh=req.settlement_tolerance_kwh,
        records=req.records,
    )
    return SettlementResponse.model_validate(result)


@app.post(
    "/dispatch/reschedule",
    response_model=RescheduleResponse,
    responses={422: {"model": ErrorResponse, "description": "非法输入或续排整体拒绝"}},
    tags=["dispatch"],
)
async def dispatch_reschedule(req: RescheduleRequest) -> RescheduleResponse:
    """计划中途续排：核对快照与前缀实测，从实测末态按新预测重排剩余时段。

    - 快照篡改（plan_snapshot_mismatch）、前缀审计异常或首条 SOC 脱节
      （prefix_audit_failed）、前缀末态不在 0.1kWh 网格（prefix_end_soc_off_grid）、
      原末端 SOC 约束已无法满足（infeasible）均整体拒绝；
    - 已执行前缀只要实测自洽即保留原记录（即使偏离旧计划），余段从实测末态重排，
      SOC 与前缀末态衔接；余段调度输入与计划快照可直接交给 /dispatch/settlement。
    """
    result = execute_reschedule(
        dispatch_input=req.dispatch.model_dump(),
        plan_snapshot=req.plan_snapshot.model_dump(),
        audit_tolerance_kwh=req.audit_tolerance_kwh,
        records=req.records,
        remaining_prices=req.remaining_prices,
        remaining_base_load_kw=req.remaining_base_load_kw,
        remaining_pv_kw=req.remaining_pv_kw,
    )
    return RescheduleResponse.model_validate(result)

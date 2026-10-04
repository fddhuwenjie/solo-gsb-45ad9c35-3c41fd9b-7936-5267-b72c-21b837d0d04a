"""计划中途续排：依据实测状态重排剩余时段。

流程（实测记录只读，不修正任何读数）：
1. 用现有求解器对原 `/dispatch` 输入重算，逐字段比对计划快照（与结算同一套防篡改核对）；
2. 复用遥测审计核对已执行前缀，并要求首条首态 SOC 在容差内衔接原初始 SOC——
   已执行动作即使偏离旧计划，只要实测自洽即保留原记录；
3. 前缀末态 SOC 必须落在 0.1kWh 网格上，作为余段重排的初始状态；
4. 以实测末态为起点，沿用原电池参数与原末端 SOC 约束，按剩余时段的新电价/负荷/光伏
   预测重新求解；原末端约束已无法满足时整体拒绝；
5. 汇总：前缀按结算口径（实测购电量×原电价、实测吞吐量×衰减单价）计费，余段按计划
   口径计预测费用；响应携带可直接交给 /dispatch/settlement 的余段调度输入与计划快照。

整体拒绝（HTTP 422）的情形：
- 计划快照与重算结果不一致（plan_snapshot_mismatch）；
- 前缀为空或覆盖全部时段、剩余预测长度不符（invalid_input，请求模型层拦截）；
- 前缀遥测审计异常，或首条 SOC 与原初始值脱节（prefix_audit_failed）；
- 前缀末态 SOC 不在 0.1kWh 网格上（prefix_end_soc_off_grid）；
- 从实测末态出发，原末端 SOC 约束已无法满足（infeasible）。
"""

from __future__ import annotations

from typing import List, Sequence

from app.audit import audit_telemetry
from app.models import TelemetryRecord
from app.settlement import _verify_snapshot
from app.solver import (
    PERIOD_HOURS,
    SOC_STEP_KWH,
    InfeasibleError,
    _snap_steps,
    solve_dispatch,
)


class RescheduleError(Exception):
    """续排整体拒绝：code 决定 HTTP 错误体中的 error 字段。"""

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code


def _round6(value: float) -> float:
    """保留 6 位小数；+0.0 把 -0.0 归一为 0.0。"""
    return round(value, 6) + 0.0


def execute_reschedule(
    dispatch_input: dict,
    plan_snapshot: dict,
    audit_tolerance_kwh: float,
    records: Sequence[TelemetryRecord],
    remaining_prices: Sequence[float],
    remaining_base_load_kw: Sequence[float],
    remaining_pv_kw: Sequence[float],
) -> dict:
    """执行中途续排，返回续排报告（见模块文档字符串）。"""
    # 1. 重算并核对快照
    recalculated = solve_dispatch(
        prices=dispatch_input["prices"],
        base_load_kw=dispatch_input["base_load_kw"],
        pv_kw=dispatch_input["pv_kw"],
        capacity_kwh=dispatch_input["capacity_kwh"],
        max_charge_kw=dispatch_input["max_charge_kw"],
        max_discharge_kw=dispatch_input["max_discharge_kw"],
        charge_efficiency=dispatch_input["charge_efficiency"],
        discharge_efficiency=dispatch_input["discharge_efficiency"],
        initial_soc_kwh=dispatch_input["initial_soc_kwh"],
        min_terminal_soc_kwh=dispatch_input["min_terminal_soc_kwh"],
        degradation_cost=dispatch_input["degradation_cost_per_kwh"],
    )
    _verify_snapshot(recalculated, plan_snapshot)

    n = len(recalculated["periods"])
    k = len(records)
    m = n - k

    prices = dispatch_input["prices"]
    eta_c = dispatch_input["charge_efficiency"]
    eta_d = dispatch_input["discharge_efficiency"]
    degradation_cost = dispatch_input["degradation_cost_per_kwh"]
    tol = audit_tolerance_kwh

    # 长度复核（请求模型层通常已拦截，服务层再守一道）
    if not 1 <= k < n:
        raise RescheduleError(
            "reschedule_length_mismatch",
            f"已执行前缀长度 {k} 必须落在 [1, {n}) 区间：至少一段且须留有待执行时段",
        )
    if not (
        len(remaining_prices) == len(remaining_base_load_kw) == len(remaining_pv_kw) == m
    ):
        raise RescheduleError(
            "reschedule_length_mismatch",
            f"剩余时段预测长度须均为 {m}（计划 {n} 段 − 已执行前缀 {k} 段），"
            f"当前电价/负荷/光伏分别为 {len(remaining_prices)}、"
            f"{len(remaining_base_load_kw)}、{len(remaining_pv_kw)}",
        )

    # 2. 复用遥测审计核对前缀（电芯能量守恒、购电量平衡、相邻记录 SOC 衔接）
    audit_report = audit_telemetry(
        charge_efficiency=eta_c,
        discharge_efficiency=eta_d,
        energy_tolerance_kwh=tol,
        records=records,
    )
    if audit_report["anomalous"]:
        idxs = audit_report["anomalous_records"]
        detail = "；".join(audit_report["records"][idxs[0]]["reasons"])
        raise RescheduleError(
            "prefix_audit_failed",
            f"已执行前缀未通过遥测审计，异常记录下标 {idxs}；第 {idxs[0]} 条：{detail}",
        )

    # 2b. 首条首态 SOC 须在容差内衔接原初始 SOC（审计只覆盖记录之间的衔接）
    initial_soc = float(dispatch_input["initial_soc_kwh"])
    origin_gap = records[0].soc_start_kwh - initial_soc
    if abs(origin_gap) > tol:
        raise RescheduleError(
            "prefix_audit_failed",
            f"首条记录首态 SOC {records[0].soc_start_kwh}kWh 与原初始 SOC {initial_soc}kWh "
            f"相差 {origin_gap:+.6f}kWh，超容差 {tol}kWh，前缀无法衔接原计划起点",
        )

    # 3. 前缀末态 SOC 须落在 0.1kWh 网格上（余段 DP 的初始状态）
    prefix_end_soc = records[-1].soc_end_kwh
    try:
        end_steps = _snap_steps(prefix_end_soc, "前缀末态 SOC")
    except ValueError as exc:
        raise RescheduleError(
            "prefix_end_soc_off_grid",
            f"{exc}，无法作为余段重排的初始状态",
        ) from exc
    remaining_initial_soc = _round6(end_steps * SOC_STEP_KWH)

    # 4. 以实测末态为起点、原末端 SOC 约束不变，按新预测重排剩余时段
    remaining_dispatch = {
        "prices": list(remaining_prices),
        "base_load_kw": list(remaining_base_load_kw),
        "pv_kw": list(remaining_pv_kw),
        "capacity_kwh": dispatch_input["capacity_kwh"],
        "max_charge_kw": dispatch_input["max_charge_kw"],
        "max_discharge_kw": dispatch_input["max_discharge_kw"],
        "charge_efficiency": eta_c,
        "discharge_efficiency": eta_d,
        "initial_soc_kwh": remaining_initial_soc,
        "min_terminal_soc_kwh": dispatch_input["min_terminal_soc_kwh"],
        "degradation_cost_per_kwh": degradation_cost,
    }
    try:
        remaining_plan = solve_dispatch(
            prices=remaining_dispatch["prices"],
            base_load_kw=remaining_dispatch["base_load_kw"],
            pv_kw=remaining_dispatch["pv_kw"],
            capacity_kwh=remaining_dispatch["capacity_kwh"],
            max_charge_kw=remaining_dispatch["max_charge_kw"],
            max_discharge_kw=remaining_dispatch["max_discharge_kw"],
            charge_efficiency=eta_c,
            discharge_efficiency=eta_d,
            initial_soc_kwh=remaining_initial_soc,
            min_terminal_soc_kwh=remaining_dispatch["min_terminal_soc_kwh"],
            degradation_cost=degradation_cost,
        )
    except InfeasibleError as exc:
        raise InfeasibleError(
            f"自实测末态 SOC {remaining_initial_soc}kWh 重排剩余 {m} 个时段无解，"
            f"原末端 SOC 约束已无法满足：{exc}"
        ) from exc

    # 5. 前缀按结算口径计费，余段按计划口径预测，组装原时间轴明细
    periods: List[dict] = []
    prefix_grid = 0.0
    prefix_throughput = 0.0
    prefix_energy_cost = 0.0
    prefix_deg_cost = 0.0

    for i, rec in enumerate(records):
        # 实测吞吐量（电芯侧）= 充入电芯 + 电芯放出；实测能量费按原电价计
        throughput = rec.charge_ac_energy_kwh * eta_c + rec.discharge_ac_energy_kwh / eta_d
        energy_cost = prices[i] * rec.grid_purchase_energy_kwh
        deg_cost = degradation_cost * throughput
        prefix_grid += rec.grid_purchase_energy_kwh
        prefix_throughput += throughput
        prefix_energy_cost += energy_cost
        prefix_deg_cost += deg_cost
        periods.append(
            {
                "index": i,
                "status": "executed",
                "base_load_kw": rec.base_load_kw,
                "pv_kw": rec.pv_kw,
                "charge_ac_energy_kwh": rec.charge_ac_energy_kwh,
                "discharge_ac_energy_kwh": rec.discharge_ac_energy_kwh,
                "grid_purchase_energy_kwh": rec.grid_purchase_energy_kwh,
                "soc_start_kwh": rec.soc_start_kwh,
                "soc_end_kwh": rec.soc_end_kwh,
                "battery_throughput_kwh": _round6(throughput),
                "energy_cost_yuan": _round6(energy_cost),
                "degradation_cost_yuan": _round6(deg_cost),
                "total_cost_yuan": _round6(energy_cost + deg_cost),
            }
        )

    for j, planned in enumerate(remaining_plan["periods"]):
        entry = dict(planned)
        entry["index"] = k + j  # 原时间轴下标
        entry["status"] = "scheduled"
        periods.append(entry)

    rem_summary = remaining_plan["summary"]
    prefix_total = prefix_energy_cost + prefix_deg_cost
    projected_energy = prefix_energy_cost + rem_summary["total_energy_cost_yuan"]
    projected_deg = prefix_deg_cost + rem_summary["total_degradation_cost_yuan"]
    projected_total = projected_energy + projected_deg
    original_total = float(recalculated["summary"]["total_cost_yuan"])

    return {
        "num_periods": n,
        "executed_periods": k,
        "scheduled_periods": m,
        "period_hours": PERIOD_HOURS,
        "audit_tolerance_kwh": tol,
        "prefix_end_soc_kwh": _round6(prefix_end_soc),
        "periods": periods,
        "summary": {
            "executed_periods": k,
            "scheduled_periods": m,
            "prefix_end_soc_kwh": _round6(prefix_end_soc),
            "actual_prefix_grid_purchase_kwh": _round6(prefix_grid),
            "actual_prefix_battery_throughput_kwh": _round6(prefix_throughput),
            "actual_prefix_energy_cost_yuan": _round6(prefix_energy_cost),
            "actual_prefix_degradation_cost_yuan": _round6(prefix_deg_cost),
            "actual_prefix_total_cost_yuan": _round6(prefix_total),
            "scheduled_grid_purchase_kwh": rem_summary["total_grid_purchase_kwh"],
            "scheduled_battery_throughput_kwh": rem_summary["battery_throughput_kwh"],
            "scheduled_energy_cost_yuan": rem_summary["total_energy_cost_yuan"],
            "scheduled_degradation_cost_yuan": rem_summary["total_degradation_cost_yuan"],
            "scheduled_total_cost_yuan": rem_summary["total_cost_yuan"],
            "scheduled_terminal_soc_kwh": rem_summary["terminal_soc_kwh"],
            "projected_energy_cost_yuan": _round6(projected_energy),
            "projected_degradation_cost_yuan": _round6(projected_deg),
            "projected_total_cost_yuan": _round6(projected_total),
            "original_plan_total_cost_yuan": original_total,
            "projected_cost_delta_yuan": _round6(projected_total - original_total),
        },
        "prefix_audit": {
            "num_records": k,
            "energy_tolerance_kwh": tol,
            "anomalous": False,
            "anomalous_records": [],
            "initial_soc_gap_kwh": _round6(origin_gap),
        },
        "remaining_dispatch": remaining_dispatch,
        "remaining_plan_snapshot": remaining_plan,
    }

"""请求 / 响应模型与输入合法性校验。"""

from __future__ import annotations

import math
from typing import Annotated, List, Literal, Optional, Sequence, Union

from pydantic import BaseModel, Field, model_validator

MAX_PERIODS = 200
MAX_AUDIT_RECORDS = 2000
MIN_EFFICIENCY_EXCLUSIVE = 0.0
MAX_EFFICIENCY_INCLUSIVE = 1.0

# 价格 / 功率为 0.5h 数据：基础负荷、光伏预测单位 kW，电价单位 元/kWh。
Prices = Annotated[List[float], Field(min_length=1, max_length=MAX_PERIODS)]
Loads = Annotated[List[float], Field(min_length=1, max_length=MAX_PERIODS)]
Pvs = Annotated[List[float], Field(min_length=1, max_length=MAX_PERIODS)]

NonNeg = Field(ge=0.0)
Positive = Field(gt=0.0)


def _validate_telemetry_records(
    records: Sequence["TelemetryRecord"], capacity_kwh: float
) -> None:
    """逐条校验实测记录：有限、非负、SOC 不超容量。审计与结算共用。"""
    for i, rec in enumerate(records):
        for name, label in (
            ("base_load_kw", "基础负荷"),
            ("pv_kw", "光伏功率"),
            ("charge_ac_energy_kwh", "交流充电量"),
            ("discharge_ac_energy_kwh", "交流放电量"),
            ("grid_purchase_energy_kwh", "电网购电量"),
            ("soc_start_kwh", "首态 SOC"),
            ("soc_end_kwh", "末态 SOC"),
        ):
            v = getattr(rec, name)
            if not math.isfinite(v):
                raise ValueError(f"第 {i} 条记录的{label}不是有限数值：{v}")
            if v < 0:
                raise ValueError(f"第 {i} 条记录的{label}不允许为负值：{v}")
        if rec.soc_start_kwh > capacity_kwh:
            raise ValueError(
                f"第 {i} 条记录的首态 SOC {rec.soc_start_kwh}kWh "
                f"超过电池容量 {capacity_kwh}kWh"
            )
        if rec.soc_end_kwh > capacity_kwh:
            raise ValueError(
                f"第 {i} 条记录的末态 SOC {rec.soc_end_kwh}kWh "
                f"超过电池容量 {capacity_kwh}kWh"
            )


class DispatchRequest(BaseModel):
    prices: Prices = Field(description="各时段电价（元/kWh），长度需与负荷、光伏一致")
    base_load_kw: Loads = Field(description="各时段基础负荷（kW）")
    pv_kw: Pvs = Field(description="各时段光伏预测功率（kW）")

    capacity_kwh: float = Field(gt=0.0, le=100.0, description="电池容量（kWh），须为 0.1 的整数倍")
    max_charge_kw: float = Field(ge=0.0, le=100.0, description="最大充电功率（kW，交流侧）")
    max_discharge_kw: float = Field(ge=0.0, le=100.0, description="最大放电功率（kW，交流侧）")

    charge_efficiency: float = Field(gt=0.0, le=1.0, description="充电效率（0, 1]")
    discharge_efficiency: float = Field(gt=0.0, le=1.0, description="放电效率（0, 1]")

    initial_soc_kwh: float = Field(ge=0.0, description="初始 SOC（kWh），须为 0.1 的整数倍")
    min_terminal_soc_kwh: float = Field(ge=0.0, description="最低末端 SOC（kWh），须为 0.1 的整数倍")
    degradation_cost_per_kwh: float = Field(ge=0.0, description="电芯每吞吐 1kWh 的循环衰减成本（元/kWh）")

    @model_validator(mode="after")
    def _validate(self) -> "DispatchRequest":
        # 1. 等长
        n = len(self.prices)
        if len(self.base_load_kw) != n or len(self.pv_kw) != n:
            raise ValueError(
                f"电价、基础负荷、光伏预测必须等长：当前分别为 {n}、"
                f"{len(self.base_load_kw)}、{len(self.pv_kw)}"
            )

        # 2. 有限且非负（数组字段）
        for name, seq in (
            ("电价", self.prices),
            ("基础负荷", self.base_load_kw),
            ("光伏预测", self.pv_kw),
        ):
            for i, v in enumerate(seq):
                if not math.isfinite(v):
                    raise ValueError(f"{name}第 {i} 个时段不是有限数值：{v}")
                if v < 0:
                    raise ValueError(f"{name}不允许为负值，第 {i} 个时段为 {v}")

        # 3. 标量有限性
        for name in (
            "capacity_kwh",
            "max_charge_kw",
            "max_discharge_kw",
            "charge_efficiency",
            "discharge_efficiency",
            "initial_soc_kwh",
            "min_terminal_soc_kwh",
            "degradation_cost_per_kwh",
        ):
            v = getattr(self, name)
            if not math.isfinite(v):
                raise ValueError(f"{name} 必须是有限数值")

        # 4. 0.1kWh 网格
        from app.solver import SOC_STEP_KWH, _snap_steps

        for name, v in (
            ("电池容量", self.capacity_kwh),
            ("初始 SOC", self.initial_soc_kwh),
            ("最低末端 SOC", self.min_terminal_soc_kwh),
        ):
            _snap_steps(v, name)  # 非整数倍会抛中文 ValueError

        cap_steps = round(self.capacity_kwh / SOC_STEP_KWH)
        init_steps = round(self.initial_soc_kwh / SOC_STEP_KWH)
        end_steps = round(self.min_terminal_soc_kwh / SOC_STEP_KWH)
        if init_steps > cap_steps:
            raise ValueError("初始 SOC 不能超过电池容量")
        if end_steps > cap_steps:
            raise ValueError("最低末端 SOC 不能超过电池容量")

        return self


class PeriodPlan(BaseModel):
    index: int
    action: Literal["charge", "discharge", "idle"]
    action_energy_kwh: float
    charge_ac_energy_kwh: float
    discharge_ac_energy_kwh: float
    battery_power_kw: float
    grid_purchase_energy_kwh: float
    pv_curtailed_energy_kwh: float
    soc_start_kwh: float
    soc_end_kwh: float
    energy_cost_yuan: float
    degradation_cost_yuan: float


class DispatchSummary(BaseModel):
    num_periods: int
    period_hours: float
    soc_resolution_kwh: float
    total_energy_cost_yuan: float
    total_degradation_cost_yuan: float
    total_cost_yuan: float
    total_grid_purchase_kwh: float
    peak_grid_purchase_energy_kwh: float
    peak_grid_purchase_power_kw: float
    battery_throughput_kwh: float
    terminal_soc_kwh: float


class DispatchResponse(BaseModel):
    periods: List[PeriodPlan]
    summary: DispatchSummary


class TelemetryRecord(BaseModel):
    """一条半小时实测记录：功率单位 kW，电量与 SOC 单位 kWh。"""

    base_load_kw: float = Field(description="基础负荷功率（kW）")
    pv_kw: float = Field(description="光伏功率（kW）")
    charge_ac_energy_kwh: float = Field(description="交流侧充电量（kWh）")
    discharge_ac_energy_kwh: float = Field(description="交流侧放电量（kWh）")
    grid_purchase_energy_kwh: float = Field(description="电网购电量（kWh）")
    soc_start_kwh: float = Field(description="时段首 SOC（kWh）")
    soc_end_kwh: float = Field(description="时段末 SOC（kWh）")


class TelemetryAuditRequest(BaseModel):
    capacity_kwh: float = Field(gt=0.0, description="电池容量（kWh），须为正")
    charge_efficiency: float = Field(gt=0.0, le=1.0, description="充电效率（0, 1]")
    discharge_efficiency: float = Field(gt=0.0, le=1.0, description="放电效率（0, 1]")
    energy_tolerance_kwh: float = Field(ge=0.0, description="能量容差（kWh），非负")
    records: Annotated[
        List[TelemetryRecord], Field(min_length=1, max_length=MAX_AUDIT_RECORDS)
    ] = Field(description="按时间顺序排列的半小时实测记录")

    @model_validator(mode="after")
    def _validate(self) -> "TelemetryAuditRequest":
        # 1. 标量有限性（gt/ge 比较不拦截 inf）
        for name in (
            "capacity_kwh",
            "charge_efficiency",
            "discharge_efficiency",
            "energy_tolerance_kwh",
        ):
            v = getattr(self, name)
            if not math.isfinite(v):
                raise ValueError(f"{name} 必须是有限数值")

        # 2. 逐条记录：有限、非负、SOC 不超容量
        _validate_telemetry_records(self.records, self.capacity_kwh)

        return self


class TelemetryRecordAudit(BaseModel):
    index: int
    expected_soc_end_kwh: float = Field(description="按首态 SOC 与交流充放电量核算的期望末态 SOC")
    soc_residual_kwh: float = Field(description="实测末态 SOC − 期望末态 SOC")
    expected_grid_purchase_energy_kwh: float = Field(description="按功率平衡核算的期望购电量")
    grid_residual_kwh: float = Field(description="实测购电量 − 期望购电量")
    soc_continuity_gap_kwh: float = Field(description="首态 SOC − 上一条末态 SOC（首条为 0）")
    anomalous: bool = Field(description="任一分项残差绝对值超容差即为 true")
    reasons: List[str] = Field(description="触发异常的具体原因")


class TelemetryAuditResponse(BaseModel):
    num_records: int
    period_hours: float
    energy_tolerance_kwh: float
    anomalous: bool = Field(description="是否存在异常记录")
    anomalous_records: List[int] = Field(description="异常记录的下标")
    records: List[TelemetryRecordAudit]


class SettlementRequest(BaseModel):
    """计划执行结算请求：一次 /dispatch 的完整输入、其计划快照、容差与等长实测记录。"""

    dispatch: DispatchRequest = Field(description="生成该计划时使用的完整 /dispatch 输入")
    plan_snapshot: DispatchResponse = Field(
        description="当初 /dispatch 返回的计划快照（periods + summary），服务端会重算并核对"
    )
    settlement_tolerance_kwh: float = Field(
        ge=0.0, description="结算容差（kWh），同时用于遥测残差与 SOC 轨迹偏离判定"
    )
    records: Annotated[
        List[TelemetryRecord], Field(min_length=1, max_length=MAX_PERIODS)
    ] = Field(description="按时间顺序排列的半小时实测记录，长度须与计划时段数一致")

    @model_validator(mode="after")
    def _validate(self) -> "SettlementRequest":
        if not math.isfinite(self.settlement_tolerance_kwh):
            raise ValueError("settlement_tolerance_kwh 必须是有限数值")
        # 记录的有限/非负/SOC 容量约束与审计接口一致（容量取 dispatch 输入口径）
        _validate_telemetry_records(self.records, self.dispatch.capacity_kwh)
        n = len(self.records)
        planned_n = len(self.plan_snapshot.periods)
        if n != planned_n:
            raise ValueError(
                f"实测记录长度 {n} 与计划快照时段数 {planned_n} 不符，无法逐时段结算"
            )
        return self


class SettlementPeriodPlan(BaseModel):
    """结算明细中回显的该时段计划口径数据。"""

    action: Literal["charge", "discharge", "idle"]
    grid_purchase_energy_kwh: float
    battery_throughput_kwh: float
    energy_cost_yuan: float
    degradation_cost_yuan: float
    soc_start_kwh: float
    soc_end_kwh: float


class SettlementRecordAudit(TelemetryRecordAudit):
    """遥测审计结论外，附加结算专用的 SOC 断点判定。"""

    soc_breakpoint: bool = Field(description="首/末 SOC 偏离计划轨迹超容差")
    soc_breakpoint_reasons: List[str] = Field(description="SOC 断点的具体原因")


class SettlementPeriodResult(BaseModel):
    index: int
    status: Literal["settled", "pending"] = Field(
        description="settled=已结算；pending=首条异常/SOC 断点起冻结的待结算记录"
    )
    plan: SettlementPeriodPlan
    soc_gap_start_kwh: float = Field(description="实测首态 SOC − 计划首态 SOC")
    soc_gap_end_kwh: float = Field(description="实测末态 SOC − 计划末态 SOC")

    actual_grid_purchase_energy_kwh: Optional[float] = Field(description="实测购电量（待结算为 null）")
    actual_battery_throughput_kwh: Optional[float] = Field(
        description="实测电芯侧吞吐量 = 交流充电×ηc + 交流放电÷ηd（待结算为 null）"
    )
    actual_energy_cost_yuan: Optional[float]
    actual_degradation_cost_yuan: Optional[float]
    actual_total_cost_yuan: Optional[float]

    planned_energy_cost_yuan: float
    planned_degradation_cost_yuan: float
    planned_total_cost_yuan: float

    energy_cost_delta_yuan: Optional[float] = Field(description="实测能量费 − 计划能量费")
    degradation_cost_delta_yuan: Optional[float] = Field(description="实测衰减费 − 计划衰减费")
    total_cost_delta_yuan: Optional[float] = Field(description="实测总费用 − 计划总费用")
    grid_purchase_delta_kwh: Optional[float] = Field(description="实测购电量 − 计划购电量")
    throughput_delta_kwh: Optional[float] = Field(description="实测吞吐量 − 计划吞吐量")

    audit: SettlementRecordAudit
    reasons: List[str] = Field(description="待结算/断点原因（已结算记录为空）")


class SettlementSummary(BaseModel):
    settled_periods: int
    actual_grid_purchase_kwh: float
    planned_grid_purchase_kwh: float
    grid_purchase_delta_kwh: float
    actual_battery_throughput_kwh: float
    planned_battery_throughput_kwh: float
    throughput_delta_kwh: float
    actual_energy_cost_yuan: float
    planned_energy_cost_yuan: float
    energy_cost_delta_yuan: float
    actual_degradation_cost_yuan: float
    planned_degradation_cost_yuan: float
    degradation_cost_delta_yuan: float
    actual_total_cost_yuan: float
    planned_total_cost_yuan: float
    total_cost_delta_yuan: float


class SettlementAuditSummary(BaseModel):
    anomalous: bool
    anomalous_records: List[int]


class SettlementResponse(BaseModel):
    num_records: int
    period_hours: float
    settlement_tolerance_kwh: float
    status: Literal["settled", "partially_settled"] = Field(
        description="settled=全部记录连续有效已结算；partially_settled=存在异常/SOC 断点，"
        "仅前缀区间已结算，其余待结算"
    )
    settled_periods: int = Field(description="已结算的连续前缀时段数（自下标 0 起）")
    pending_periods: int = Field(description="待结算时段数")
    first_anomaly_index: Optional[int] = Field(
        description="首条异常或 SOC 断点的下标；全部有效时为 null"
    )
    anomalous_records: List[int] = Field(description="遥测审计不通过的记录下标")
    soc_breakpoint_records: List[int] = Field(description="SOC 偏离计划轨迹的记录下标")
    periods: List[SettlementPeriodResult]
    summary: SettlementSummary
    audit_summary: SettlementAuditSummary


class RescheduleRequest(BaseModel):
    """计划中途续排请求：原计划输入与快照、已执行实测前缀、审计容差与剩余时段新预测。"""

    dispatch: DispatchRequest = Field(description="生成原计划时使用的完整 /dispatch 输入")
    plan_snapshot: DispatchResponse = Field(
        description="当初 /dispatch 返回的计划快照（periods + summary），服务端会重算并核对"
    )
    audit_tolerance_kwh: float = Field(
        ge=0.0, description="前缀遥测审计容差（kWh），判定规则同 /telemetry/audit"
    )
    records: Annotated[
        List[TelemetryRecord], Field(min_length=1, max_length=MAX_PERIODS)
    ] = Field(description="已连续执行的实测前缀（至少一段，且须留有待执行时段）")
    remaining_prices: Prices = Field(description="剩余时段的新电价（元/kWh）")
    remaining_base_load_kw: Loads = Field(description="剩余时段的新基础负荷预测（kW）")
    remaining_pv_kw: Pvs = Field(description="剩余时段的新光伏预测功率（kW）")

    @model_validator(mode="after")
    def _validate(self) -> "RescheduleRequest":
        if not math.isfinite(self.audit_tolerance_kwh):
            raise ValueError("audit_tolerance_kwh 必须是有限数值")
        # 前缀记录的有限/非负/SOC 容量约束与审计、结算接口一致（容量取 dispatch 输入口径）
        _validate_telemetry_records(self.records, self.dispatch.capacity_kwh)

        n = len(self.plan_snapshot.periods)
        k = len(self.records)
        if k >= n:
            raise ValueError(
                f"已执行前缀长度 {k} 必须小于计划时段数 {n}：续排须留有待执行时段"
            )
        m = n - k
        for name, seq in (
            ("剩余时段电价", self.remaining_prices),
            ("剩余时段基础负荷", self.remaining_base_load_kw),
            ("剩余时段光伏预测", self.remaining_pv_kw),
        ):
            if len(seq) != m:
                raise ValueError(
                    f"{name}长度 {len(seq)} 与剩余时段数 {m} 不符"
                    f"（计划共 {n} 段，已执行前缀 {k} 段）"
                )
            for i, v in enumerate(seq):
                if not math.isfinite(v):
                    raise ValueError(f"{name}第 {i} 个时段不是有限数值：{v}")
                if v < 0:
                    raise ValueError(f"{name}不允许为负值，第 {i} 个时段为 {v}")
        return self


class RescheduleExecutedPeriod(BaseModel):
    """原时间轴上已执行的时段：回显实测记录，并按结算口径计实测费用。"""

    index: int
    status: Literal["executed"]
    base_load_kw: float
    pv_kw: float
    charge_ac_energy_kwh: float
    discharge_ac_energy_kwh: float
    grid_purchase_energy_kwh: float
    soc_start_kwh: float
    soc_end_kwh: float
    battery_throughput_kwh: float = Field(
        description="实测电芯侧吞吐量 = 交流充电×ηc + 交流放电÷ηd"
    )
    energy_cost_yuan: float = Field(description="实测能量费 = 原电价 × 实测购电量")
    degradation_cost_yuan: float = Field(description="实测衰减费 = 衰减单价 × 实测吞吐量")
    total_cost_yuan: float


class RescheduleScheduledPeriod(PeriodPlan):
    """原时间轴上待执行的时段：续排计划（index 为原时间轴下标）。"""

    status: Literal["scheduled"]


ReschedulePeriod = Annotated[
    Union[RescheduleExecutedPeriod, RescheduleScheduledPeriod],
    Field(discriminator="status"),
]


class ReschedulePrefixAudit(BaseModel):
    num_records: int
    energy_tolerance_kwh: float
    anomalous: bool = Field(description="是否存在异常记录（异常即整体拒绝，成功响应恒为 false）")
    anomalous_records: List[int]
    initial_soc_gap_kwh: float = Field(description="首条记录首态 SOC − 原初始 SOC（容差内才放行）")


class RescheduleSummary(BaseModel):
    executed_periods: int
    scheduled_periods: int
    prefix_end_soc_kwh: float = Field(description="前缀末态 SOC，即余段重排的初始 SOC")
    actual_prefix_grid_purchase_kwh: float
    actual_prefix_battery_throughput_kwh: float
    actual_prefix_energy_cost_yuan: float
    actual_prefix_degradation_cost_yuan: float
    actual_prefix_total_cost_yuan: float
    scheduled_grid_purchase_kwh: float
    scheduled_battery_throughput_kwh: float
    scheduled_energy_cost_yuan: float
    scheduled_degradation_cost_yuan: float
    scheduled_total_cost_yuan: float
    scheduled_terminal_soc_kwh: float
    projected_energy_cost_yuan: float = Field(description="全程能量费 = 实测前缀 + 余段预测")
    projected_degradation_cost_yuan: float = Field(description="全程衰减费 = 实测前缀 + 余段预测")
    projected_total_cost_yuan: float = Field(description="全程总费用 = 实测前缀 + 余段预测")
    original_plan_total_cost_yuan: float = Field(description="原计划总费用（重算口径）")
    projected_cost_delta_yuan: float = Field(description="全程预计总费用 − 原计划总费用")


class RescheduleResponse(BaseModel):
    num_periods: int = Field(description="原时间轴总时段数")
    executed_periods: int
    scheduled_periods: int
    period_hours: float
    audit_tolerance_kwh: float
    prefix_end_soc_kwh: float
    periods: List[ReschedulePeriod] = Field(
        description="原时间轴明细：已执行时段回显实测，待执行时段为续排计划"
    )
    summary: RescheduleSummary
    prefix_audit: ReschedulePrefixAudit
    remaining_dispatch: DispatchRequest = Field(
        description="余段调度输入：可直接作为 /dispatch 请求或 /dispatch/settlement 的 dispatch 字段"
    )
    remaining_plan_snapshot: DispatchResponse = Field(
        description="余段计划快照：与 remaining_dispatch 配对，可直接用于 /dispatch/settlement"
    )


class ErrorResponse(BaseModel):
    error: str
    reason: str
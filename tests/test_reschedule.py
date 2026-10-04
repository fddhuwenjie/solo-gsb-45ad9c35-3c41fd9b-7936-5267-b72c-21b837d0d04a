"""POST /dispatch/reschedule 行为测试：pytest tests/ -q"""

import copy
import json

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# 与结算测试相同的 4 时段场景：t0 谷充 0.2 电芯（交流 0.2/0.9≈0.222222），
# t1/t2 各放 0.09 交流（电芯 0.1），t3 静置；SOC 轨迹 0→0.2→0.1→0→0。
DISPATCH = dict(
    prices=[0.0, 1.0, 1.0, 0.0],
    base_load_kw=[0.2, 0.2, 0.2, 0.2],
    pv_kw=[0.0, 0.0, 0.0, 0.0],
    capacity_kwh=0.4,
    max_charge_kw=1.0,
    max_discharge_kw=1.0,
    charge_efficiency=0.9,
    discharge_efficiency=0.9,
    initial_soc_kwh=0.0,
    min_terminal_soc_kwh=0.0,
    degradation_cost_per_kwh=0.2,
)

RECORDS = [
    dict(
        base_load_kw=0.2, pv_kw=0.0,
        charge_ac_energy_kwh=round(0.2 / 0.9, 6), discharge_ac_energy_kwh=0.0,
        grid_purchase_energy_kwh=round(0.1 + 0.2 / 0.9, 6),
        soc_start_kwh=0.0, soc_end_kwh=0.2,
    ),
    dict(
        base_load_kw=0.2, pv_kw=0.0,
        charge_ac_energy_kwh=0.0, discharge_ac_energy_kwh=0.09,
        grid_purchase_energy_kwh=0.01,
        soc_start_kwh=0.2, soc_end_kwh=0.1,
    ),
    dict(
        base_load_kw=0.2, pv_kw=0.0,
        charge_ac_energy_kwh=0.0, discharge_ac_energy_kwh=0.09,
        grid_purchase_energy_kwh=0.01,
        soc_start_kwh=0.1, soc_end_kwh=0.0,
    ),
    dict(
        base_load_kw=0.2, pv_kw=0.0,
        charge_ac_energy_kwh=0.0, discharge_ac_energy_kwh=0.0,
        grid_purchase_energy_kwh=0.1,
        soc_start_kwh=0.0, soc_end_kwh=0.0,
    ),
]

TOL = 0.01

# 剩余两段（t2/t3）的新预测：t2 高价高负荷，t3 低价且光伏盈余
REMAINING = dict(
    remaining_prices=[2.0, 0.5],
    remaining_base_load_kw=[0.4, 0.1],
    remaining_pv_kw=[0.0, 0.2],
)


def _plan(dispatch=None):
    return client.post("/dispatch", json=dispatch or DISPATCH).json()


def _reschedule(records, remaining=None, tol=TOL, plan=None, dispatch=None):
    payload = {
        "dispatch": dispatch or DISPATCH,
        "plan_snapshot": plan or _plan(),
        "audit_tolerance_kwh": tol,
        "records": records,
        **(remaining or REMAINING),
    }
    return client.post("/dispatch/reschedule", json=payload)


def test_reschedule_from_matching_prefix():
    """前缀与计划一致：全时间轴明细 + 前缀实测费用 + 余段预测费用 + 余段输入/快照。"""
    r = _reschedule(copy.deepcopy(RECORDS[:2]))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["num_periods"] == 4
    assert data["executed_periods"] == 2
    assert data["scheduled_periods"] == 2
    assert data["prefix_end_soc_kwh"] == 0.1
    assert data["prefix_audit"]["anomalous"] is False
    assert data["prefix_audit"]["num_records"] == 2
    assert data["prefix_audit"]["initial_soc_gap_kwh"] == 0.0

    periods = data["periods"]
    assert [p["status"] for p in periods] == ["executed", "executed", "scheduled", "scheduled"]
    assert [p["index"] for p in periods] == [0, 1, 2, 3]

    # 已执行段：回显实测记录，按结算口径计费（原电价 × 实测购电、实测吞吐量 × 衰减单价）
    p0, p1 = periods[0], periods[1]
    assert p0["grid_purchase_energy_kwh"] == round(0.1 + 0.2 / 0.9, 6)
    assert p0["battery_throughput_kwh"] == 0.2
    assert p0["energy_cost_yuan"] == 0.0        # 谷时电价 0
    assert p0["degradation_cost_yuan"] == 0.04  # 0.2 电芯 × 0.2 元
    assert p1["energy_cost_yuan"] == 0.01       # 峰时电价 1 × 实测购电 0.01
    assert p1["degradation_cost_yuan"] == 0.02
    assert p1["total_cost_yuan"] == 0.03

    # 余段从实测末态 0.1 起排；新预测下 t2 高价放电、t3 光伏盈余静置
    assert periods[2]["soc_start_kwh"] == 0.1
    assert periods[2]["action"] == "discharge"
    assert periods[3]["action"] == "idle"

    # 汇总：实测前缀费用 + 余段预测费用
    summary = data["summary"]
    assert summary["actual_prefix_total_cost_yuan"] == 0.07
    assert summary["actual_prefix_grid_purchase_kwh"] == round(0.1 + 0.2 / 0.9 + 0.01, 6)
    assert summary["actual_prefix_battery_throughput_kwh"] == 0.3
    assert summary["scheduled_total_cost_yuan"] == 0.24  # 2.0×0.11 能量 + 0.02 衰减
    assert summary["scheduled_terminal_soc_kwh"] == 0.0
    assert summary["projected_total_cost_yuan"] == 0.31
    assert summary["original_plan_total_cost_yuan"] == 0.1
    assert summary["projected_cost_delta_yuan"] == 0.21

    # 余段调度输入：初值接前缀末态、电池参数与末端约束沿用原输入、预测用新值
    rem = data["remaining_dispatch"]
    assert rem["initial_soc_kwh"] == 0.1
    assert rem["min_terminal_soc_kwh"] == DISPATCH["min_terminal_soc_kwh"]
    assert rem["capacity_kwh"] == DISPATCH["capacity_kwh"]
    assert rem["max_charge_kw"] == DISPATCH["max_charge_kw"]
    assert rem["degradation_cost_per_kwh"] == DISPATCH["degradation_cost_per_kwh"]
    assert rem["prices"] == REMAINING["remaining_prices"]
    assert rem["base_load_kw"] == REMAINING["remaining_base_load_kw"]
    assert rem["pv_kw"] == REMAINING["remaining_pv_kw"]

    # 余段计划快照与直接调用 /dispatch 的结果一致
    direct = client.post("/dispatch", json=rem)
    assert direct.status_code == 200
    assert data["remaining_plan_snapshot"] == direct.json()


def test_deviated_but_consistent_prefix_reschedules_from_actual_state():
    """t1 实际未按计划放电（静置且自洽）：保留实测记录，从实测末态 0.2 续排。"""
    records = copy.deepcopy(RECORDS[:2])
    records[1] = dict(
        base_load_kw=0.2, pv_kw=0.0,
        charge_ac_energy_kwh=0.0, discharge_ac_energy_kwh=0.0,
        grid_purchase_energy_kwh=0.1,
        soc_start_kwh=0.2, soc_end_kwh=0.2,
    )
    r = _reschedule(records)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["prefix_end_soc_kwh"] == 0.2
    assert data["remaining_dispatch"]["initial_soc_kwh"] == 0.2

    periods = data["periods"]
    # 已执行段保留实测（偏离旧计划的 0.1 末态），不做旧计划轨迹核对
    assert periods[1]["status"] == "executed"
    assert periods[1]["soc_end_kwh"] == 0.2
    assert periods[1]["grid_purchase_energy_kwh"] == 0.1
    # 余段 SOC 与实测末态衔接
    assert periods[2]["status"] == "scheduled"
    assert periods[2]["soc_start_kwh"] == 0.2


def test_origin_gap_within_tolerance_accepted():
    """首条首态 SOC 与原初值差 0.005 ≤ 容差：放行，余段初值取网格化末态。"""
    records = [
        dict(
            base_load_kw=0.2, pv_kw=0.0,
            charge_ac_energy_kwh=round(0.195 / 0.9, 6), discharge_ac_energy_kwh=0.0,
            grid_purchase_energy_kwh=round(0.1 + 0.195 / 0.9, 6),
            soc_start_kwh=0.005, soc_end_kwh=0.2,
        ),
        dict(
            base_load_kw=0.2, pv_kw=0.0,
            charge_ac_energy_kwh=0.0, discharge_ac_energy_kwh=0.09,
            grid_purchase_energy_kwh=0.01,
            soc_start_kwh=0.2, soc_end_kwh=0.1,
        ),
    ]
    r = _reschedule(records)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["prefix_audit"]["initial_soc_gap_kwh"] == 0.005
    assert data["remaining_dispatch"]["initial_soc_kwh"] == 0.1


def test_prefix_audit_anomaly_rejected():
    """前缀记录购电量残差超容差 → prefix_audit_failed。"""
    records = copy.deepcopy(RECORDS[:2])
    records[1]["grid_purchase_energy_kwh"] = 0.11  # 期望 0.01，残差 0.1
    r = _reschedule(records)
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "prefix_audit_failed"
    assert "购电量残差" in body["reason"]


def test_prefix_continuity_break_rejected():
    """前缀内部 SOC 不衔接 → prefix_audit_failed。"""
    records = copy.deepcopy(RECORDS[:2])
    records[1]["soc_start_kwh"] = 0.15  # 与 t0 末态 0.2 差 0.05 超容差
    records[1]["soc_end_kwh"] = 0.05
    r = _reschedule(records)
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "prefix_audit_failed"
    assert "不衔接" in body["reason"]


def test_first_record_must_connect_initial_soc():
    """首条首态 SOC 偏离原初始 SOC 超容差（记录本身自洽）→ prefix_audit_failed。"""
    records = copy.deepcopy(RECORDS[:2])
    records[0] = dict(
        base_load_kw=0.2, pv_kw=0.0,
        charge_ac_energy_kwh=round(0.2 / 0.9, 6), discharge_ac_energy_kwh=0.0,
        grid_purchase_energy_kwh=round(0.1 + 0.2 / 0.9, 6),
        soc_start_kwh=0.1, soc_end_kwh=0.3,
    )
    records[1]["soc_start_kwh"] = 0.3
    records[1]["soc_end_kwh"] = 0.2
    r = _reschedule(records)
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "prefix_audit_failed"
    assert "原初始 SOC" in body["reason"]


def test_prefix_end_soc_off_grid_rejected():
    """前缀自洽但末态 SOC 0.15 不在 0.1kWh 网格 → prefix_end_soc_off_grid。"""
    records = [
        dict(
            base_load_kw=0.2, pv_kw=0.0,
            charge_ac_energy_kwh=round(0.15 / 0.9, 6), discharge_ac_energy_kwh=0.0,
            grid_purchase_energy_kwh=round(0.1 + 0.15 / 0.9, 6),
            soc_start_kwh=0.0, soc_end_kwh=0.15,
        )
    ]
    remaining = dict(
        remaining_prices=[1.0, 1.0, 1.0],
        remaining_base_load_kw=[0.2, 0.2, 0.2],
        remaining_pv_kw=[0.0, 0.0, 0.0],
    )
    r = _reschedule(records, remaining=remaining)
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "prefix_end_soc_off_grid"
    assert "0.1" in body["reason"]


def test_terminal_constraint_unsatisfiable_rejected():
    """实测末态过低，剩余时段充不到原末端 SOC → infeasible。"""
    dispatch = dict(
        prices=[1.0, 0.0],
        base_load_kw=[0.2, 0.2],
        pv_kw=[0.0, 0.0],
        capacity_kwh=0.4,
        max_charge_kw=0.4,  # 每时段最多充 0.2 电芯
        max_discharge_kw=1.0,
        charge_efficiency=1.0,
        discharge_efficiency=1.0,
        initial_soc_kwh=0.2,
        min_terminal_soc_kwh=0.4,
        degradation_cost_per_kwh=0.0,
    )
    plan = _plan(dispatch)
    # t0 实际放了 0.1 交流（自洽），末态 0.1；剩余 1 段最多充 0.2 → 最高 0.3 < 0.4
    records = [
        dict(
            base_load_kw=0.2, pv_kw=0.0,
            charge_ac_energy_kwh=0.0, discharge_ac_energy_kwh=0.1,
            grid_purchase_energy_kwh=0.0,
            soc_start_kwh=0.2, soc_end_kwh=0.1,
        )
    ]
    remaining = dict(
        remaining_prices=[0.0],
        remaining_base_load_kw=[0.2],
        remaining_pv_kw=[0.0],
    )
    r = _reschedule(records, remaining=remaining, plan=plan, dispatch=dispatch)
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "infeasible"
    assert "末端 SOC" in body["reason"]


def test_length_mismatch_rejected():
    """前缀覆盖全部时段、剩余预测长度不符 → invalid_input。"""
    # 前缀覆盖全部 4 段，无待执行时段
    r = _reschedule(
        copy.deepcopy(RECORDS),
        remaining=dict(
            remaining_prices=[1.0],
            remaining_base_load_kw=[0.2],
            remaining_pv_kw=[0.0],
        ),
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "invalid_input"
    assert "待执行时段" in body["reason"]

    # 剩余预测长度（1）与剩余时段数（2）不符
    r = _reschedule(
        copy.deepcopy(RECORDS[:2]),
        remaining=dict(
            remaining_prices=[1.0],
            remaining_base_load_kw=[0.2],
            remaining_pv_kw=[0.0],
        ),
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "invalid_input"
    assert "不符" in body["reason"]

    # 三个剩余数组长度不一致
    r = _reschedule(
        copy.deepcopy(RECORDS[:2]),
        remaining=dict(
            remaining_prices=[1.0, 0.5],
            remaining_base_load_kw=[0.2],
            remaining_pv_kw=[0.0, 0.0],
        ),
    )
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_input"


def test_tampered_snapshot_rejected():
    """篡改计划快照 → plan_snapshot_mismatch，不进入续排。"""
    plan = _plan()
    tampered = copy.deepcopy(plan)
    tampered["periods"][0]["grid_purchase_energy_kwh"] = 0.99
    r = _reschedule(copy.deepcopy(RECORDS[:2]), plan=tampered)
    assert r.status_code == 422
    assert r.json()["error"] == "plan_snapshot_mismatch"


def test_remaining_snapshot_directly_usable_for_settlement():
    """余段调度输入 + 计划快照可直接交给 /dispatch/settlement 完成后续结算。"""
    r = _reschedule(copy.deepcopy(RECORDS[:2]))
    assert r.status_code == 200, r.text
    data = r.json()
    rem = data["remaining_dispatch"]
    snap = data["remaining_plan_snapshot"]

    # 按余段计划构造自洽实测记录，直接交给现有结算接口
    records = [
        dict(
            base_load_kw=rem["base_load_kw"][j],
            pv_kw=rem["pv_kw"][j],
            charge_ac_energy_kwh=p["charge_ac_energy_kwh"],
            discharge_ac_energy_kwh=p["discharge_ac_energy_kwh"],
            grid_purchase_energy_kwh=p["grid_purchase_energy_kwh"],
            soc_start_kwh=p["soc_start_kwh"],
            soc_end_kwh=p["soc_end_kwh"],
        )
        for j, p in enumerate(snap["periods"])
    ]
    s = client.post(
        "/dispatch/settlement",
        json={
            "dispatch": rem,
            "plan_snapshot": snap,
            "settlement_tolerance_kwh": TOL,
            "records": records,
        },
    )
    assert s.status_code == 200, s.text
    settled = s.json()
    assert settled["status"] == "settled"
    assert settled["settled_periods"] == 2


def test_invalid_tolerance_and_measurements_rejected():
    """容差非法、测量值非法等沿用既有校验 → 422 invalid_input。"""
    assert _reschedule(copy.deepcopy(RECORDS[:2]), tol=-0.1).status_code == 422

    raw = json.dumps({
        "dispatch": DISPATCH,
        "plan_snapshot": _plan(),
        "audit_tolerance_kwh": float("nan"),
        "records": RECORDS[:2],
        **REMAINING,
    })
    r = client.post(
        "/dispatch/reschedule",
        content=raw,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_input"

    records = copy.deepcopy(RECORDS[:2])
    records[1]["grid_purchase_energy_kwh"] = -0.01
    assert _reschedule(records).status_code == 422

    # 剩余预测含负值
    r = _reschedule(
        copy.deepcopy(RECORDS[:2]),
        remaining=dict(
            remaining_prices=[-1.0, 0.5],
            remaining_base_load_kw=[0.4, 0.1],
            remaining_pv_kw=[0.0, 0.2],
        ),
    )
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_input"


def test_existing_endpoints_unchanged():
    """新增续排接口不影响 /dispatch、/telemetry/audit、/dispatch/settlement 与 /healthz。"""
    assert client.get("/healthz").json() == {"status": "ok"}

    r = client.post("/dispatch", json=DISPATCH)
    assert r.status_code == 200
    assert len(r.json()["periods"]) == 4

    r = client.post(
        "/telemetry/audit",
        json={
            "capacity_kwh": DISPATCH["capacity_kwh"],
            "charge_efficiency": 0.9,
            "discharge_efficiency": 0.9,
            "energy_tolerance_kwh": TOL,
            "records": RECORDS,
        },
    )
    assert r.status_code == 200
    assert r.json()["anomalous"] is False

    r = client.post(
        "/dispatch/settlement",
        json={
            "dispatch": DISPATCH,
            "plan_snapshot": _plan(),
            "settlement_tolerance_kwh": TOL,
            "records": copy.deepcopy(RECORDS),
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "settled"

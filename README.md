# 家庭储能时段调度 API

基于 **Python + FastAPI + Pydantic** 的家庭储能调度服务。输入等长的半小时电价、基础负荷、光伏预测序列以及电池参数，用**动态规划（SOC 离散到 0.1 kWh）**求出满足功率、SOC 和末端 SOC 约束的**最低总成本**充放电计划；另提供 `/telemetry/audit` 对半小时实测记录做能量守恒与购电量**审计**（只核对标记，不修正读数、不生成调度计划）；`/dispatch/settlement` 对一份已下发计划做**执行结算**：携带原始 `/dispatch` 输入与计划快照，服务重算校验快照未被篡改、复用遥测审计逐条核对，并只对首个异常 / SOC 断点之前的连续有效区间按实测购电量与吞吐量计费；`/dispatch/reschedule` 在计划执行到中途时**按实测状态续排**：核对快照与已执行前缀后，从实测末态出发按剩余时段的新电价 / 负荷 / 光伏预测重排，并给出可直接交给结算接口的余段调度输入与计划快照。

## 一条命令启动

需要 Docker 20.10+（带 Compose v2）：

```bash
docker compose up --build
```

服务启动后：

- 健康检查：<http://localhost:8000/healthz>
- 交互式文档（Swagger UI）：<http://localhost:8000/docs>
- OpenAPI Schema：<http://localhost:8000/openapi.json>

停止：`Ctrl+C`（后台运行用 `docker compose up -d --build`，停止用 `docker compose down`）。

本地裸机运行（Python 3.11+）：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 接口

### `GET /healthz`

```json
{"status": "ok"}
```

### `POST /dispatch`

请求体（每个时段固定 **0.5 小时**，三个数组必须等长，长度 1–200）：

| 字段 | 单位 | 说明 |
|---|---|---|
| `prices` | 元/kWh | 各时段购电电价（非负） |
| `base_load_kw` | kW | 各时段基础负荷 |
| `pv_kw` | kW | 各时段光伏预测功率 |
| `capacity_kwh` | kWh | 电池容量，必须为 0.1 的整数倍 |
| `max_charge_kw` / `max_discharge_kw` | kW | 最大充 / 放电**交流侧**功率 |
| `charge_efficiency` / `discharge_efficiency` | – | 充 / 放电效率，取值 (0, 1] |
| `initial_soc_kwh` | kWh | 初始 SOC（0.1 整数倍，≤ 容量） |
| `min_terminal_soc_kwh` | kWh | 最低末端 SOC（0.1 整数倍，≤ 容量） |
| `degradation_cost_per_kwh` | 元/kWh | 按电芯吞吐量计的循环衰减成本 |

示例：

```bash
curl -s -X POST http://localhost:8000/dispatch \
  -H 'Content-Type: application/json' \
  -d '{
    "prices": [1.0, 0.0, 1.0],
    "base_load_kw": [0.2, 0.2, 0.2],
    "pv_kw": [0.0, 0.0, 0.0],
    "capacity_kwh": 0.2,
    "max_charge_kw": 1.0,
    "max_discharge_kw": 1.0,
    "charge_efficiency": 1.0,
    "discharge_efficiency": 1.0,
    "initial_soc_kwh": 0.0,
    "min_terminal_soc_kwh": 0.0,
    "degradation_cost_per_kwh": 0.0
  }'
```

响应：`periods` 为逐时段计划，`summary` 为汇总。

```json
{
  "periods": [
    {
      "index": 0,
      "action": "idle",                 // charge / discharge / idle
      "action_energy_kwh": 0.0,         // 电芯侧能量变化：正=充，负=放，0.1 整数倍
      "charge_ac_energy_kwh": 0.0,      // 该时段电池吸收的交流电量
      "discharge_ac_energy_kwh": 0.0,   // 该时段电池放出的交流电量
      "battery_power_kw": 0.0,          // 正=充电功率，负=放电功率
      "grid_purchase_energy_kwh": 0.1,  // 购电量
      "pv_curtailed_energy_kwh": 0.0,   // 弃光量
      "soc_start_kwh": 0.0,
      "soc_end_kwh": 0.0,
      "energy_cost_yuan": 0.1,          // 该时段能量费
      "degradation_cost_yuan": 0.0      // 该时段衰减费
    }
  ],
  "summary": {
    "num_periods": 3,
    "period_hours": 0.5,
    "soc_resolution_kwh": 0.1,
    "total_energy_cost_yuan": 0.1,
    "total_degradation_cost_yuan": 0.0,
    "total_cost_yuan": 0.1,
    "total_grid_purchase_kwh": 0.2,
    "peak_grid_purchase_energy_kwh": 0.2,
    "peak_grid_purchase_power_kw": 0.4,
    "battery_throughput_kwh": 0.1,
    "terminal_soc_kwh": 0.0
  }
}
```

### `POST /telemetry/audit`

对按时间顺序排列的半小时**实测记录**逐条做台账核对：电芯侧能量守恒、购电量功率平衡、相邻记录 SOC 衔接。只审计并标记异常，**不修正读数、不生成调度计划**。

请求体：

| 字段 | 单位 | 说明 |
|---|---|---|
| `capacity_kwh` | kWh | 电池容量，须为正 |
| `charge_efficiency` / `discharge_efficiency` | – | 充 / 放电效率，取值 (0, 1] |
| `energy_tolerance_kwh` | kWh | 能量容差（非负）；任一分项残差绝对值**严格大于**它即判该条异常 |
| `records` | – | 按序排列的半小时实测记录，长度 1–2000 |

`records` 每条记录的字段（均须有限且非负，SOC 不得超过容量）：

| 字段 | 单位 | 说明 |
|---|---|---|
| `base_load_kw` | kW | 基础负荷功率 |
| `pv_kw` | kW | 光伏功率 |
| `charge_ac_energy_kwh` / `discharge_ac_energy_kwh` | kWh | 交流侧充 / 放电量 |
| `grid_purchase_energy_kwh` | kWh | 电网购电量 |
| `soc_start_kwh` / `soc_end_kwh` | kWh | 时段首 / 末 SOC |

逐条核算规则（与 `/dispatch` 的模型假设一致）：

```
期望末态 SOC = 首态 SOC + 交流充电量×充电效率 − 交流放电量÷放电效率
期望购电量   = max(0, (基础负荷 − 光伏)×0.5 + 交流充电量 − 交流放电量)
SOC 衔接差   = 首态 SOC − 上一条记录的末态 SOC（首条记 0）
```

响应中每条记录给出三个分项残差（实测 − 核算）：`soc_residual_kwh`、`grid_residual_kwh`、`soc_continuity_gap_kwh`，任一绝对值超容差则该条 `anomalous=true` 并在 `reasons` 列出原因；顶层 `anomalous_records` 汇总异常下标。审计结果正常返回 **HTTP 200**（异常是核对结论，不是请求错误）。

示例（第二条记录购电量实测 0.7，核算仅 0.55，残差 0.15 超容差）：

```bash
curl -s -X POST http://localhost:8000/telemetry/audit \
  -H 'Content-Type: application/json' \
  -d '{
    "capacity_kwh": 10.0,
    "charge_efficiency": 0.9,
    "discharge_efficiency": 0.9,
    "energy_tolerance_kwh": 0.05,
    "records": [
      {"base_load_kw": 1.0, "pv_kw": 0.4, "charge_ac_energy_kwh": 0.5,
       "discharge_ac_energy_kwh": 0.0, "grid_purchase_energy_kwh": 0.8,
       "soc_start_kwh": 2.0, "soc_end_kwh": 2.45},
      {"base_load_kw": 2.0, "pv_kw": 0.0, "charge_ac_energy_kwh": 0.0,
       "discharge_ac_energy_kwh": 0.45, "grid_purchase_energy_kwh": 0.7,
       "soc_start_kwh": 2.45, "soc_end_kwh": 1.95}
    ]
  }'
```

```json
{
  "num_records": 2,
  "period_hours": 0.5,
  "energy_tolerance_kwh": 0.05,
  "anomalous": true,
  "anomalous_records": [1],
  "records": [
    {
      "index": 0,
      "expected_soc_end_kwh": 2.45,
      "soc_residual_kwh": 0.0,
      "expected_grid_purchase_energy_kwh": 0.8,
      "grid_residual_kwh": 0.0,
      "soc_continuity_gap_kwh": 0.0,
      "anomalous": false,
      "reasons": []
    },
    {
      "index": 1,
      "expected_soc_end_kwh": 1.95,
      "soc_residual_kwh": 0.0,
      "expected_grid_purchase_energy_kwh": 0.55,
      "grid_residual_kwh": 0.15,
      "soc_continuity_gap_kwh": 0.0,
      "anomalous": true,
      "reasons": ["购电量残差 +0.150000kWh 超容差 0.05kWh（实测 0.7kWh，核算值 0.55kWh）"]
    }
  ]
}
```

### `POST /dispatch/settlement`

对已下发的调度计划做执行结算。请求携带四样东西：

| 字段 | 说明 |
|---|---|
| `dispatch` | 一次 `/dispatch` 的**完整输入**（电价/负荷/光伏序列 + 全部电池参数） |
| `plan_snapshot` | 当初 `/dispatch` 返回的计划快照（`periods` + `summary` 原样回传） |
| `settlement_tolerance_kwh` | 结算容差（kWh，非负）；遥测三类残差与 SOC 轨迹偏离均按「绝对值严格大于容差」判定 |
| `records` | 与计划**等长**的半小时实测记录（字段同 `/telemetry/audit`） |

服务端处理顺序：

1. **重算防篡改**：用现有求解器对 `dispatch` 重算，逐字段（动作、各能量、SOC、逐时段费用、summary）比对 `plan_snapshot`，任一处不一致整体拒绝（`plan_snapshot_mismatch`）；
2. **遥测审计**：复用 `/telemetry/audit` 的同一套核算，逐条检查电芯能量守恒、购电量平衡、相邻记录 SOC 衔接；
3. **SOC 断点**：额外比对每条记录的首 / 末 SOC 与计划 SOC 轨迹，偏差超容差即为断点（实际执行偏离了计划状态轨迹，后续时段的计划口径不再可比）；
4. **冻结待结算**：从**首条异常或 SOC 断点**（含该条）起，后续记录一律标为 `pending`（待结算），即使后面数据恢复自洽也不继续计费；
5. **有效区间计费**：对此前连续有效前缀（下标 0 起），逐时段按**实测购电量**算能量费、按**实测电芯吞吐量**（交流充电×ηc + 交流放电÷ηd）算衰减费，并给出与计划费用的差额及逐时段偏差。

`status=settled` 表示全部记录连续有效、整体结算；`status=partially_settled` 表示存在异常 / 断点，仅前缀结算。实测与预测的偏差（例如实际负荷高于预测导致多购电，但 SOC 轨迹不变）不属于异常，照常结算并计入费用差额。

整体拒绝（HTTP 422）的情形：

- `plan_snapshot_mismatch`：快照与重算结果不一致（被篡改或缺字段）；重算本身无解时为 `infeasible`；
- `invalid_input`：记录长度与计划时段数不符、容差为负 / 非有限、测量值为负 / 非有限、SOC 超容量等；
- `no_valid_interval`：**首条**记录即异常或 SOC 断点，不存在任何有效区间。

示例（全部记录有效；t1 购电表读数 0.015 比计划多 0.005kWh，在 0.01kWh 容差内，照常结算并计 0.005 元能量费差额）：

```bash
curl -s -X POST http://localhost:8000/dispatch/settlement \
  -H 'Content-Type: application/json' \
  -d '{
    "dispatch": {
      "prices": [0.0, 1.0, 1.0, 0.0],
      "base_load_kw": [0.2, 0.2, 0.2, 0.2],
      "pv_kw": [0.0, 0.0, 0.0, 0.0],
      "capacity_kwh": 0.4,
      "max_charge_kw": 1.0,
      "max_discharge_kw": 1.0,
      "charge_efficiency": 0.9,
      "discharge_efficiency": 0.9,
      "initial_soc_kwh": 0.0,
      "min_terminal_soc_kwh": 0.0,
      "degradation_cost_per_kwh": 0.2
    },
    "plan_snapshot": { "...": "原样回传 /dispatch 的完整响应（periods + summary）" },
    "settlement_tolerance_kwh": 0.01,
    "records": [
      {"base_load_kw": 0.2, "pv_kw": 0.0,
       "charge_ac_energy_kwh": 0.222222, "discharge_ac_energy_kwh": 0.0,
       "grid_purchase_energy_kwh": 0.322222, "soc_start_kwh": 0.0, "soc_end_kwh": 0.2},
      {"base_load_kw": 0.2, "pv_kw": 0.0,
       "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.09,
       "grid_purchase_energy_kwh": 0.015, "soc_start_kwh": 0.2, "soc_end_kwh": 0.1},
      {"base_load_kw": 0.2, "pv_kw": 0.0,
       "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.09,
       "grid_purchase_energy_kwh": 0.01, "soc_start_kwh": 0.1, "soc_end_kwh": 0.0},
      {"base_load_kw": 0.2, "pv_kw": 0.0,
       "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.0,
       "grid_purchase_energy_kwh": 0.1, "soc_start_kwh": 0.0, "soc_end_kwh": 0.0}
    ]
  }'
```

```json
{
  "num_records": 4,
  "period_hours": 0.5,
  "settlement_tolerance_kwh": 0.01,
  "status": "settled",
  "settled_periods": 4,
  "pending_periods": 0,
  "first_anomaly_index": null,
  "anomalous_records": [],
  "soc_breakpoint_records": [],
  "periods": [
    {
      "index": 1,
      "status": "settled",
      "plan": {
        "action": "discharge",
        "grid_purchase_energy_kwh": 0.01,
        "battery_throughput_kwh": 0.1,
        "energy_cost_yuan": 0.01,
        "degradation_cost_yuan": 0.02,
        "soc_start_kwh": 0.2,
        "soc_end_kwh": 0.1
      },
      "soc_gap_start_kwh": 0.0,
      "soc_gap_end_kwh": 0.0,
      "actual_grid_purchase_energy_kwh": 0.015,
      "actual_battery_throughput_kwh": 0.1,
      "actual_energy_cost_yuan": 0.015,
      "actual_degradation_cost_yuan": 0.02,
      "actual_total_cost_yuan": 0.035,
      "planned_energy_cost_yuan": 0.01,
      "planned_degradation_cost_yuan": 0.02,
      "planned_total_cost_yuan": 0.03,
      "energy_cost_delta_yuan": 0.005,
      "degradation_cost_delta_yuan": 0.0,
      "total_cost_delta_yuan": 0.005,
      "grid_purchase_delta_kwh": 0.005,
      "throughput_delta_kwh": 0.0,
      "audit": {
        "index": 1,
        "expected_soc_end_kwh": 0.1,
        "soc_residual_kwh": 0.0,
        "expected_grid_purchase_energy_kwh": 0.015,
        "grid_residual_kwh": 0.0,
        "soc_continuity_gap_kwh": 0.0,
        "anomalous": false,
        "reasons": [],
        "soc_breakpoint": false,
        "soc_breakpoint_reasons": []
      },
      "reasons": []
    }
  ],
  "summary": {
    "settled_periods": 4,
    "actual_grid_purchase_kwh": 0.447222,
    "planned_grid_purchase_kwh": 0.442222,
    "grid_purchase_delta_kwh": 0.005,
    "actual_battery_throughput_kwh": 0.4,
    "planned_battery_throughput_kwh": 0.4,
    "throughput_delta_kwh": 0.0,
    "actual_energy_cost_yuan": 0.025,
    "planned_energy_cost_yuan": 0.02,
    "energy_cost_delta_yuan": 0.005,
    "actual_degradation_cost_yuan": 0.08,
    "planned_degradation_cost_yuan": 0.08,
    "degradation_cost_delta_yuan": 0.0,
    "actual_total_cost_yuan": 0.105,
    "planned_total_cost_yuan": 0.1,
    "total_cost_delta_yuan": 0.005
  },
  "audit_summary": {"anomalous": false, "anomalous_records": []}
}
```

若 t2 实测购电量为 0.11（核算值 0.01，残差 0.1 超容差），则 t0/t1 正常结算、t2 起冻结：

```json
{
  "status": "partially_settled",
  "settled_periods": 2,
  "pending_periods": 2,
  "first_anomaly_index": 2,
  "anomalous_records": [2],
  "soc_breakpoint_records": [],
  "periods": [
    {"index": 0, "status": "settled", "...": "..."},
    {"index": 1, "status": "settled", "...": "..."},
    {
      "index": 2,
      "status": "pending",
      "actual_grid_purchase_energy_kwh": null,
      "actual_energy_cost_yuan": null,
      "total_cost_delta_yuan": null,
      "planned_total_cost_yuan": 0.03,
      "reasons": ["购电量残差 +0.100000kWh 超容差 0.01kWh（实测 0.11kWh，核算值 0.01kWh）"],
      "...": "审计分项残差与 SOC 偏差照常返回"
    },
    {
      "index": 3,
      "status": "pending",
      "actual_total_cost_yuan": null,
      "reasons": ["前序第 2 条记录已出现异常/SOC 断点，后续记录冻结为待结算"],
      "...": "..."
    }
  ],
  "summary": {
    "settled_periods": 2,
    "actual_total_cost_yuan": 0.07,
    "planned_total_cost_yuan": 0.07,
    "total_cost_delta_yuan": 0.0,
    "...": "汇总只覆盖已结算前缀；购电量、吞吐量、能量费、衰减费均给出实际/计划/差额三列"
  }
}
```

待结算记录的各 `actual_*` 与差额字段为 `null`，但计划口径（`planned_*`）、审计残差、SOC 偏差（`soc_gap_start_kwh` / `soc_gap_end_kwh`）与冻结原因照常返回，便于人工复核。

### `POST /dispatch/reschedule`

计划执行到中途时，依据实测状态续排剩余时段。请求携带六样东西：

| 字段 | 说明 |
|---|---|
| `dispatch` | 生成原计划时使用的完整 `/dispatch` 输入 |
| `plan_snapshot` | 当初 `/dispatch` 返回的计划快照（`periods` + `summary` 原样回传） |
| `audit_tolerance_kwh` | 前缀遥测审计容差（kWh，非负），判定规则同 `/telemetry/audit` |
| `records` | 已连续执行的实测前缀（字段同 `/telemetry/audit`）：**至少一段，且须留有待执行时段** |
| `remaining_prices` / `remaining_base_load_kw` / `remaining_pv_kw` | 剩余时段的新电价 / 基础负荷 / 光伏预测，长度须均等于「计划时段数 − 前缀段数」 |

服务端处理顺序：

1. **重算防篡改**：对 `dispatch` 重算并逐字段比对 `plan_snapshot`（与结算同一套核对），不一致整体拒绝；
2. **前缀审计**：复用 `/telemetry/audit` 的同一套核算核对已执行前缀，并要求**首条首态 SOC 在容差内衔接原初始 SOC**。已执行动作即使偏离旧计划，只要实测自洽即保留原记录——不做旧计划 SOC 轨迹核对；
3. **末态网格检查**：前缀末态 SOC 必须落在 0.1kWh 网格上，作为余段重排的初始状态；
4. **余段重排**：以实测末态为起点，沿用原电池参数与**原末端 SOC 约束**，按剩余时段的新预测重新求解；约束已无法满足时整体拒绝；
5. **费用汇总**：前缀按结算口径（实测购电量 × 原电价 + 实测吞吐量 × 衰减单价）计实测费用，余段按计划口径计预测费用，合计为全程预计总费用，并与原计划总费用对比。

响应要点：

- `periods`：**原时间轴**上的逐时段明细——已执行时段 `status=executed`，回显实测记录与实测费用；待执行时段 `status=scheduled`，为续排计划（`index` 为原时间轴下标，余段 SOC 与前缀末态衔接）；
- `summary`：`actual_prefix_*`（实测前缀费用）、`scheduled_*`（余段预测费用）、`projected_*`（两者合计）与 `projected_cost_delta_yuan`（相对原计划总费用的偏差）；
- `remaining_dispatch` + `remaining_plan_snapshot`：余段调度输入与计划快照，二者配对、与直接调用 `/dispatch` 的结果一致，**可直接交给 `/dispatch/settlement`** 在余段执行完毕后结算。

整体拒绝（HTTP 422）的情形：

- `plan_snapshot_mismatch`：快照与重算结果不一致；
- `invalid_input`：前缀为空或覆盖全部时段（须留有待执行时段）、剩余预测长度与剩余时段数不符、容差 / 测量值非法等；
- `prefix_audit_failed`：前缀遥测审计异常（电芯能量 / 购电量 / 衔接残差超容差），或首条首态 SOC 与原初始值脱节；
- `prefix_end_soc_off_grid`：前缀末态 SOC 不在 0.1kWh 网格上，无法作为余段初始状态；
- `infeasible`：从实测末态出发，原末端 SOC 约束已无法满足。

示例（原计划 4 段；t0 按计划谷充，t1 实际未放电、静置自洽，末态 0.2 偏离旧计划的 0.1；按新预测续排 t2/t3）：

```bash
curl -s -X POST http://localhost:8000/dispatch/reschedule \
  -H 'Content-Type: application/json' \
  -d '{
    "dispatch": {
      "prices": [0.0, 1.0, 1.0, 0.0],
      "base_load_kw": [0.2, 0.2, 0.2, 0.2],
      "pv_kw": [0.0, 0.0, 0.0, 0.0],
      "capacity_kwh": 0.4,
      "max_charge_kw": 1.0,
      "max_discharge_kw": 1.0,
      "charge_efficiency": 0.9,
      "discharge_efficiency": 0.9,
      "initial_soc_kwh": 0.0,
      "min_terminal_soc_kwh": 0.0,
      "degradation_cost_per_kwh": 0.2
    },
    "plan_snapshot": { "...": "原样回传 /dispatch 的完整响应（periods + summary）" },
    "audit_tolerance_kwh": 0.01,
    "records": [
      {"base_load_kw": 0.2, "pv_kw": 0.0,
       "charge_ac_energy_kwh": 0.222222, "discharge_ac_energy_kwh": 0.0,
       "grid_purchase_energy_kwh": 0.322222, "soc_start_kwh": 0.0, "soc_end_kwh": 0.2},
      {"base_load_kw": 0.2, "pv_kw": 0.0,
       "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.0,
       "grid_purchase_energy_kwh": 0.1, "soc_start_kwh": 0.2, "soc_end_kwh": 0.2}
    ],
    "remaining_prices": [2.0, 0.5],
    "remaining_base_load_kw": [0.4, 0.1],
    "remaining_pv_kw": [0.0, 0.2]
  }'
```

```json
{
  "num_periods": 4,
  "executed_periods": 2,
  "scheduled_periods": 2,
  "period_hours": 0.5,
  "audit_tolerance_kwh": 0.01,
  "prefix_end_soc_kwh": 0.2,
  "periods": [
    {
      "index": 1,
      "status": "executed",
      "grid_purchase_energy_kwh": 0.1,
      "soc_start_kwh": 0.2,
      "soc_end_kwh": 0.2,
      "battery_throughput_kwh": 0.0,
      "energy_cost_yuan": 0.1,
      "degradation_cost_yuan": 0.0,
      "total_cost_yuan": 0.1,
      "...": "已执行时段回显实测记录（base_load/pv/充放电量等）并按结算口径计费"
    },
    {
      "index": 2,
      "status": "scheduled",
      "action": "discharge",
      "action_energy_kwh": -0.2,
      "discharge_ac_energy_kwh": 0.18,
      "grid_purchase_energy_kwh": 0.02,
      "soc_start_kwh": 0.2,
      "soc_end_kwh": 0.0,
      "energy_cost_yuan": 0.04,
      "degradation_cost_yuan": 0.04,
      "...": "待执行时段为续排计划，SOC 与前缀末态 0.2 衔接"
    }
  ],
  "summary": {
    "actual_prefix_total_cost_yuan": 0.14,
    "scheduled_total_cost_yuan": 0.08,
    "projected_total_cost_yuan": 0.22,
    "original_plan_total_cost_yuan": 0.1,
    "projected_cost_delta_yuan": 0.12,
    "...": "购电量 / 吞吐量 / 能量费 / 衰减费均按实测前缀与余段预测分列"
  },
  "prefix_audit": {
    "num_records": 2,
    "energy_tolerance_kwh": 0.01,
    "anomalous": false,
    "anomalous_records": [],
    "initial_soc_gap_kwh": 0.0
  },
  "remaining_dispatch": {
    "prices": [2.0, 0.5],
    "base_load_kw": [0.4, 0.1],
    "pv_kw": [0.0, 0.2],
    "capacity_kwh": 0.4,
    "initial_soc_kwh": 0.2,
    "min_terminal_soc_kwh": 0.0,
    "...": "电池参数沿用原 dispatch 输入，可直接作为 /dispatch 请求体重排或复算"
  },
  "remaining_plan_snapshot": {
    "periods": [
      {"index": 0, "action": "discharge", "soc_start_kwh": 0.2, "soc_end_kwh": 0.0, "...": "..."},
      {"index": 1, "action": "idle", "soc_start_kwh": 0.0, "soc_end_kwh": 0.0, "...": "..."}
    ],
    "summary": {"num_periods": 2, "total_cost_yuan": 0.08, "terminal_soc_kwh": 0.0, "...": "..."}
  }
}
```

余段执行完毕后，以 `remaining_dispatch` 为 `dispatch`、`remaining_plan_snapshot` 为 `plan_snapshot`、余段实测记录为 `records` 调用 `/dispatch/settlement` 即可完成结算。

## 模型与假设

- **时段**：固定 0.5 h；功率（kW）× 0.5 即该时段能量（kWh）。
- **动作与 SOC 网格**：动作即一个时段内电芯能量变化，离散为 0.1 kWh 的整数倍：正数充电、负数放电、0 静置；SOC 全程位于 `[0, capacity]` 的 0.1 kWh 网格上。容量、初始 SOC、最低末端 SOC 必须精确落在该网格上，否则报参数错误（不做静默取整）。
- **功率平衡**（每时段）：

  ```
  购电量 = max(0, 基础负荷 − 光伏 + 充电交流电量 − 放电交流电量)
  ```

  光伏优先供负荷，剩余可给电池充电；**不允许向电网倒送电**，无法消纳的光伏盈余计为弃光（`pv_curtailed_energy_kwh`），放电量因此不会超过同时段净负荷。
- **效率**：充电交流电量 = 电芯电量 / ηc；放电交流电量 = 电芯电量 × ηd。功率上限按交流侧考核（充电：电芯电量 ≤ P充·Δt·ηc；放电：电芯电量 ≤ P放·Δt/ηd）。
- **费用**：
  - 能量费 = 电价 × 购电量；
  - 衰减费 = `degradation_cost_per_kwh` × 电芯侧吞吐量，吞吐量 = Σ|充入电芯能量| + Σ|电芯放出能量|（充、放各计一次）；
  - 总成本 = 能量费 + 衰减费。
- **末端约束**：最后一个时段结束时 SOC ≥ `min_terminal_soc_kwh`。

### 动态规划

状态 `(t, j)`：第 `t` 个时段结束、SOC 处于第 `j` 格（j × 0.1 kWh）时的最优前缀。每格最多转移到「静置 / 充电 1..k 格 / 放电 1..k 格」，转移范围同时受功率上限、SOC 上下界和"禁止倒送"约束，转移成本即时算出能量费与衰减费。时间复杂度 O(N · C · P)，其中 C = 容量/0.1，P = 单时段最多动作格数。

### 破平局（保证结果确定、可复现）

当不同计划在数值容差内无法区分时，依次按：

1. **总成本**低者优先（能量费 + 衰减费）；
2. 总成本相同 → **峰值购电量**（各时段购电量的最大值，kWh/半小时）小者优先，即偏好削峰；
3. 仍相同 → **动作序列字典序**小者优先，动作为整数（0.1 kWh 格数，正充负放），越靠前的时段差异权重越大——因此"先静置、后动作"会胜过"先动作、后静置"。

## 错误处理

非法输入或无解统一返回 **HTTP 422**，JSON 说明原因：

```json
{"error": "invalid_input", "reason": "电价、基础负荷、光伏预测必须等长：当前分别为 3、2、3"}
```

```json
{"error": "infeasible", "reason": "无满足末端 SOC 约束的计划：调度期末最高可达 SOC 约为 0.0kWh，低于最低末端 SOC 0.1kWh；原因是受时段数、最大充电功率或充电效率限制，无法在期末充到要求电量"}
```

`invalid_input` 覆盖：数组不等长、负值/非有限值、效率不在 (0,1]、容量/SOC 非 0.1 整数倍、初始或末端 SOC 超过容量、数组长度超出 1–200 等；`infeasible` 表示输入合法但约束可行域为空，并给出最高可达末端 SOC 等诊断信息。`/telemetry/audit` 的非法输入（容量非正、效率越界、测量值或容差为负/非有限、SOC 超容量、记录数超出 1–2000 等）同样以 `invalid_input` 返回。

`/dispatch/settlement` 除沿用上述非法输入拦截（记录数与计划时段数不符等，`invalid_input`）外，另有两类整体拒绝：

```json
{"error": "plan_snapshot_mismatch", "reason": "计划快照第 0 个时段的 grid_purchase_energy_kwh=0.99 与重算结果 0.322222 不一致，快照可能已被篡改"}
```

```json
{"error": "no_valid_interval", "reason": "首条实测记录（下标 0）即异常或 SOC 断点，不存在可结算的连续有效区间：电芯能量残差 -0.150000kWh 超容差 0.01kWh（实测末态 SOC 0.05kWh，核算值 0.2kWh）"}
```

- `plan_snapshot_mismatch`：计划快照与按 `dispatch` 重算的结果在任一字段上不一致（含动作、逐时段能量/SOC/费用、summary），或快照结构缺失；
- `no_valid_interval`：首条记录即未通过遥测审计或 SOC 轨迹核对，没有任何可结算前缀；
- 部分记录异常 / 断点**不是**错误：返回 HTTP 200 与 `status=partially_settled`，仅冻结异常点及后续记录。

`/dispatch/reschedule` 沿用快照核对与非法输入拦截，另有三类整体拒绝：

```json
{"error": "prefix_audit_failed", "reason": "已执行前缀未通过遥测审计，异常记录下标 [0]；第 0 条：购电量残差 +0.177778kWh 超容差 0.01kWh（实测 0.5kWh，核算值 0.322222kWh）"}
```

```json
{"error": "prefix_end_soc_off_grid", "reason": "前缀末态 SOC=0.15kWh 不是 0.1kWh 的整数倍，无法精确离散到 SOC 网格，无法作为余段重排的初始状态"}
```

```json
{"error": "infeasible", "reason": "自实测末态 SOC 0.1kWh 重排剩余 1 个时段无解，原末端 SOC 约束已无法满足：无满足末端 SOC 约束的计划：……"}
```

- `prefix_audit_failed`：前缀遥测审计异常（电芯能量 / 购电量 / SOC 衔接残差超容差），或首条首态 SOC 与原初始 SOC 脱节超容差；
- `prefix_end_soc_off_grid`：前缀末态 SOC 不是 0.1kWh 的整数倍，无法离散到 SOC 网格；
- `infeasible`：从实测末态出发重排余段时，原末端 SOC 约束已无法满足（重算原计划无解时同此错误码）。

## 测试

```bash
pip install pytest httpx
pytest tests/ -q
```

覆盖谷充峰放套利、效率损耗与衰减费核算、末端 SOC 约束、禁止倒送、光伏免费充电、无解诊断、各类非法输入，以及峰值与字典序两级破平局；遥测审计部分覆盖自洽记录通过、三类残差（电芯能量 / 购电量 / SOC 衔接）越限标记、容差边界（等于容差不判异常）、光伏盈余购电截断，以及容量 / 效率 / 容差 / 测量值的非法输入拦截。结算部分覆盖：全部有效全额结算（含容差内购电偏差计入费用差额、实测吞吐量衰减费核算、预测偏差场景）、审计异常与 SOC 断点起的前缀冻结、断点与衔接异常的区分、计划快照（periods / summary / 动作）篡改拒绝、dispatch 输入与快照不匹配拒绝、长度不符与无有效区间拒绝、重算无解传播。续排部分覆盖：前缀与计划一致及偏离旧计划但自洽时的中途重排（实测末态衔接、结算口径计费、余段快照与直接 `/dispatch` 一致）、首条 SOC 容差内放行与脱节拒绝、前缀审计异常拒绝、末态离网格拒绝、原末端约束不可满足拒绝、长度不符与快照篡改拒绝、余段输入 / 快照直接交给 `/dispatch/settlement` 完成结算，以及既有四个接口行为不变。

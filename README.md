# 基层环保监管资料服务

维护监管片区、企业风险档案、执法人员和帮扶记录，提供可追溯的监管基础数据；在此之上提供**风险优先级编排服务**，把逾期自查、未闭环隐患、治污设施异常、企业风险等级和近期帮扶记录汇成带版本的风险事实，在每日可用人力和片区时段约束下生成确定的现场检查、远程复核、线上帮扶与延后队列。

系统采用 Python 标准库和 SQLite，可在单个 Linux 进程中运行。现有能力包括操作者与角色登记、场所台账、领域资料记录、请求幂等校验、哈希串联审计以及轻量 HTTP/JSON 接口。领域资料类别为：district_profile、risk_profile、officer_roster、assistance_record、overdue_self_check、open_hazard、facility_anomaly、urgent_event。所有写操作都在事务中完成，同一请求编号携带相同内容时返回原结果，内容发生变化时返回业务冲突。

## 优先级编排规则

- **风险驱动而非固定遍历**：只有具备风险事实（逾期自查、未闭环隐患、治污设施异常、紧急事件）的企业才进入当日编排，连续打卡但无异常的企业不占名额。
- **同一事实不重复加权**：风险事实按 `(类别, external_key)` 去重，同键只取最新一条，重复同步不会叠加权重。总分 = 各事实权重之和 × 企业风险等级乘子。
- **带版本的事实与规则**：每次生成方案都固化 `rules_version`、规则内容摘要与 `facts_snapshot_hash`。调整权重、乘子、免访天数或远程容量只产生新规则版本，**只影响之后的新方案**，历史方案保持原结论与原分数。
- **确定性队列**：选择顺序固定为紧急优先、风险分降序、场所编号升序；同一份快照与容量在任何机器上结果一致。结论分 `onsite`（现场检查）、`remote`（远程复核）、`assist_online`（仅线上帮扶）、`deferred`（延后）。
- **人力与片区时段约束**：现场名额受当日总名额与各片区当日名额双重扣减，满员后降级远程复核，再满则延后。
- **无事不扰与紧急突破**：近期帮扶后的免访窗口内、且无紧急事件的企业只安排线上帮扶；`urgent_event` 凭 `trigger_basis`（触发依据）可突破免访窗口，依据随方案与解释永久留痕。
- **名额并发与版本核对**：锁定、释放、改派都必须携带当前方案 `revision`，每次操作推进版本并记录理由；领取由条件更新原子完成，两个调度员并发领取同一名额只有一个成功，绝不重复派单。被新版本取代的方案不能再派单。

### 领域资料字段约定

- `district_profile`：`district_id` 表示企业所属片区，用于片区名额扣减。
- `risk_profile`：`risk_level` 取 `high` / `medium` / `low`，缺省 `low`；多条取最新。
- `assistance_record`：`occurred_at`（帮扶时间）与可选 `exempt_until`（显式免访截止日期）；未给截止时间时按规则的免访天数推算。
- `overdue_self_check` / `open_hazard` / `facility_anomaly`：风险事实，按 `external_key` 去重。
- `urgent_event`：必须带 `trigger_basis` 说明紧急来源（如 12369 举报），用于突破免访窗口并留痕。

### 编排接口

- `GET /rules`：查看当前规则版本；`POST /rules/adjust` 登记新版本。
- `POST /plans/generate`：生成当日方案（`plan_date`、`onsite_capacity`、`district_capacity`、可选 `remote_capacity`）。
- `GET /plans?plan_date=` 与 `GET /plans/{plan_id}`：列出/查看方案与各队列。
- `GET /plans/{plan_id}/sites/{site_id}/explain`：解释一家企业为何入选、被延后或仅安排线上帮扶（含事实、权重、乘子、紧急依据）。
- `POST /slots/lock` / `/slots/release` / `/slots/reassign`：名额领取、释放、改派，均需 `expected_revision` 与 `reason`。

## 目录

- `src/regulatory_triage_core/`：领域模型、SQLite 存储、权限服务、审计链、确定性编排（`triage.py`）、编排服务（`orchestration.py`）、HTTP 路由和离线验收；
- `tests/`：核心规则、事务边界、接口路由、并发派单和端到端验收测试。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m regulatory_triage_core.acceptance
```

命令会在临时 SQLite 数据库中登记操作者、场所和领域资料，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m regulatory_triage_core.api --database regulatory_triage_core.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。业务写入接口通过 `X-Actor-Id` 标识操作者，支持操作者、场所和领域资料的登记、风险规则版本管理、检查方案生成与解释、名额锁定/释放/改派，以及审计事件查询。服务重启后，SQLite 中的业务状态、方案事实快照、派单结果和审计链继续保留。

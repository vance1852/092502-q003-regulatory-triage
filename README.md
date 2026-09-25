# 基层环保监管优先级编排服务

在监管片区、企业风险档案、执法人员和帮扶记录等基础数据之上，把**逾期自查、未闭环隐患、
治污设施异常、企业风险等级和近期帮扶记录**汇聚为带版本的风险事实，在每日可用人力与片区
时段约束下生成**确定的**现场检查与远程复核队列，并支持调度员锁定、释放、改派名额与
紧急事件穿透。系统采用 Python 标准库和 SQLite，可在单个 Linux 进程中运行。

## 核心语义

- **风险事实去重加权**：同一事实（按稳定业务标识，如隐患号、自查期、告警号）在一个方案中
  只出现一次、只计一次权重；隐患闭环等状态更新以新记录登记、归并为同一事实，不会重复加权。
- **带版本的风险事实**：事实新增、状态变更或闭环失效都会自增 `fact_version`；方案固化其
  `facts_hash`，事实材料（状态/来源）变化即产生新版本方案。
- **规则调整只影响新方案**：规则集按内容哈希版本化（`rule_id` + `version`），每个方案固化
  `rule_id/rule_version/rules_hash`；旧方案永不被改写，仍可按原规则解释。
- **确定性队列**：纯函数规则引擎，排序为（紧急优先、分数降序、风险等级、最早事实日期、
  场所编号），容量从最早时段开始扣减；相同输入必然得到相同队列。输入（事实/规则/时段/
  紧急事件）未变化时重复生成直接复用当前方案，不新增版本。
- **无事不扰与穿透**：近期帮扶后处于免访窗口的企业不进场，按分数安排远程复核或线上帮扶；
  高风险企业或重大（critical）异常可穿透窗口，且现场资格按帮扶缓解前的分数判定，避免
  "连续帮扶/打卡"掩盖高风险异常。
- **紧急事件**：可强制穿透免访窗口、排在队首、现场容量不足时回落远程；必须登记
  `trigger_type/trigger_reference/detail` 触发依据并留存 `trigger_hash`。
- **名额并发安全**：领取使用 `BEGIN IMMEDIATE` + 条件更新（`WHERE status='open'`），
  两个调度员并发领取只有一人成功；锁定/释放/改派必须携带 `expected_plan_version`，
  方案升版后旧名额（未领取者自动作 `superseded`）的陈旧操作一律拒绝，且理由必填、全程留痕。
- **重启后可解释**：方案条目、评分构成、免访窗口判定、紧急触发依据与事实列表均快照入库，
  重启后可通过 API 解释每家企业为何入选现场/远程、被延后或仅安排线上帮扶。

## 领域资料类别

`district_profile`（片区，含 `district_id`）、`risk_profile`（风险等级 high/medium/low）、
`officer_roster`、`assistance_record`（帮扶记录，含 `assisted_at`）、
`self_check_report`（自查，含 `period/submitted/due_date`）、
`hazard_record`（隐患，含稳定 `hazard_id/status/severity`）、
`facility_alert`（治污设施告警，含稳定 `alert_id/status/severity`）。
资料表按 `external_key` 只追加；同一风险对象的状态更新用新 `external_key` 登记，
但载荷中的 `hazard_id/period/alert_id` 必须保持不变，事实据此归并。

## HTTP 接口（写接口均需 `X-Actor-Id` 且支持 `request_id` 幂等）

- `POST /rules`：登记/激活规则版本（内容相同不升版）
- `POST /facts/refresh`：从领域资料重算带版本风险事实
- `POST /emergencies`：登记紧急事件（触发依据必填）
- `POST /plans`：生成/复用当日片区方案（`plan_date/district_id/slots`）
- `GET /plans?plan_date=&district_id=&plan_version=`：查看方案（默认最新版本）
- `GET /plans/explain?plan_date=&site_id=&district_id=`：解释单家企业的入选/延后/帮扶原因
- `GET /dispatches?plan_date=&district_id=&status=`：名额队列
- `POST /dispatches/claim | /dispatches/release | /dispatches/reassign`：
  锁定/释放/改派，携带 `expected_plan_version` 与 `reason`
- `GET /dispatch-history?dispatch_id=`：名额状态变更与理由留痕
- 基础接口：`/organizations`、`/actors`、`/sites`、`/domain-records`、`/audit-events`、`/health`

## 目录

- `src/regulatory_triage_core/`：领域模型、SQLite 存储、权限服务、审计链、
  纯函数规则引擎（`rules.py`）、优先级编排服务（`triage.py`）、HTTP 路由和离线验收；
- `tests/`：规则引擎、编排事务、并发派单、接口路由和端到端验收测试。

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

命令会在临时 SQLite 数据库中登记主体、场所与风险资料，生成方案、登记穿透免访窗口的
紧急事件、升版方案并领取名额，核对幂等、旧方案版本留存与审计链；成功时输出一行
`status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m regulatory_triage_core.api --database regulatory_triage_core.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。服务重启后，SQLite 中的规则版本、风险事实、方案快照、
名额状态、紧急事件与审计链继续保留。

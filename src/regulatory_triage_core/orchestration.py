"""风险优先级编排服务。

在基础资料服务之上，把逾期自查、未闭环隐患、治污设施异常、企业风险等级与近期
帮扶记录汇成带版本的风险事实快照，在每日可用人力和片区时段约束下生成确定的
现场检查、远程复核、线上帮扶与延后队列，并提供带版本核对的名额锁定、释放和
改派。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .domain import RISK_FACT_CATEGORIES
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import Actor, WriteReceipt
from .storage import Database
from .triage import (
    DEFAULT_FACTOR_WEIGHTS,
    DEFAULT_REMOTE_CAPACITY,
    DEFAULT_RISK_MULTIPLIERS,
)
from . import triage


DISPATCH_ROLES = ("admin", "operator")
READ_ROLES = ("admin", "operator", "reviewer", "auditor")


class TriageService:
    """协调规则版本、方案生成、名额并发与审计。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _today(self) -> str:
        return self.clock.now().date().isoformat()

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"],
                      row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create: Callable[[], tuple[str, str, dict[str, Any]]]) -> WriteReceipt:
        request_id = request_id.strip()
        if not request_id:
            raise ValidationError("request_id 不能为空")
        payload_hash = digest(payload)
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False)

    def _replay_if_seen(self, connection, *, request_id: str, action: str,
                        payload: dict[str, Any]) -> WriteReceipt | None:
        """在版本/状态等前置校验之前识别同一请求的重放。

        名额操作会推进方案 revision；若客户端带着原 request_id 重试，应原样返回
        首次结果，而不是因 revision 已变化而报冲突。
        """

        request_id = (request_id or "").strip()
        if not request_id:
            return None
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)

    # ---- 规则版本 ---------------------------------------------------------

    def _rules_from_row(self, row) -> triage.Rules:
        return triage.Rules(
            version=row["version"],
            factor_weights=json.loads(row["factor_weights_json"]),
            risk_multipliers=json.loads(row["risk_multipliers_json"]),
            no_visit_days=row["no_visit_days"],
            remote_capacity=row["remote_capacity"],
        )

    def current_rules(self) -> dict[str, Any]:
        """返回当前生效的规则版本，没有时按内置默认值给出（不落库）。"""

        with self.database.reading() as connection:
            row = connection.execute(
                "SELECT * FROM rule_versions ORDER BY version DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return {"version": None, "factor_weights": dict(DEFAULT_FACTOR_WEIGHTS),
                        "risk_multipliers": dict(DEFAULT_RISK_MULTIPLIERS),
                        "no_visit_days": triage.DEFAULT_NO_VISIT_DAYS,
                        "remote_capacity": DEFAULT_REMOTE_CAPACITY, "persisted": False}
            rules = self._rules_from_row(row)
        return {"version": rules.version, "factor_weights": rules.factor_weights,
                "risk_multipliers": rules.risk_multipliers, "no_visit_days": rules.no_visit_days,
                "remote_capacity": rules.remote_capacity, "persisted": True}

    def _ensure_rules(self, connection, actor_id: str) -> triage.Rules:
        row = connection.execute("SELECT * FROM rule_versions ORDER BY version DESC LIMIT 1").fetchone()
        if row is not None:
            return self._rules_from_row(row)
        rules = triage.Rules(
            version=0, factor_weights=dict(DEFAULT_FACTOR_WEIGHTS),
            risk_multipliers=dict(DEFAULT_RISK_MULTIPLIERS),
            no_visit_days=triage.DEFAULT_NO_VISIT_DAYS, remote_capacity=DEFAULT_REMOTE_CAPACITY,
        )
        return self._insert_rules(connection, rules, actor_id)

    def _insert_rules(self, connection, rules: triage.Rules, actor_id: str) -> triage.Rules:
        content_hash = rules.content_hash()
        existing = connection.execute(
            "SELECT * FROM rule_versions WHERE content_hash=?", (content_hash,)
        ).fetchone()
        if existing is not None:
            return self._rules_from_row(existing)
        version = connection.execute(
            "SELECT COALESCE(MAX(version),0)+1 AS next FROM rule_versions"
        ).fetchone()["next"]
        connection.execute(
            "INSERT INTO rule_versions(version,factor_weights_json,risk_multipliers_json,no_visit_days,"
            "remote_capacity,content_hash,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (version, canonical_json(rules.factor_weights), canonical_json(rules.risk_multipliers),
             rules.no_visit_days, rules.remote_capacity, content_hash, actor_id, self._now()),
        )
        return triage.Rules(version, rules.factor_weights, rules.risk_multipliers,
                            rules.no_visit_days, rules.remote_capacity)

    def adjust_rules(self, *, request_id: str, actor_id: str, factor_weights: dict[str, int] | None = None,
                     risk_multipliers: dict[str, float] | None = None, no_visit_days: int | None = None,
                     remote_capacity: int | None = None) -> WriteReceipt:
        """登记一个新规则版本；内容与当前一致时返回当前版本，不产生新版本。

        规则调整不会改写历史方案，历史方案固化了当时的规则版本号与内容摘要。
        """

        payload = {"actor_id": actor_id, "factor_weights": factor_weights,
                   "risk_multipliers": risk_multipliers, "no_visit_days": no_visit_days,
                   "remote_capacity": remote_capacity}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *DISPATCH_ROLES)

            merged_weights = dict(DEFAULT_FACTOR_WEIGHTS)
            merged_multipliers = dict(DEFAULT_RISK_MULTIPLIERS)
            current = connection.execute(
                "SELECT * FROM rule_versions ORDER BY version DESC LIMIT 1"
            ).fetchone()
            if current is not None:
                merged_weights = json.loads(current["factor_weights_json"])
                merged_multipliers = json.loads(current["risk_multipliers_json"])
                default_days = current["no_visit_days"]
                default_remote = current["remote_capacity"]
            else:
                default_days = triage.DEFAULT_NO_VISIT_DAYS
                default_remote = DEFAULT_REMOTE_CAPACITY

            if factor_weights is not None:
                self._validate_weights(factor_weights)
                merged_weights = {key: int(value) for key, value in factor_weights.items()}
            if risk_multipliers is not None:
                self._validate_multipliers(risk_multipliers)
                merged_multipliers = {key: float(value) for key, value in risk_multipliers.items()}
            days = default_days if no_visit_days is None else int(no_visit_days)
            remote = default_remote if remote_capacity is None else int(remote_capacity)
            if days < 0 or remote < 0:
                raise ValidationError("免访天数与远程容量不能为负")

            candidate = triage.Rules(0, merged_weights, merged_multipliers, days, remote)

            def create() -> tuple[str, str, dict[str, Any]]:
                rules = self._insert_rules(connection, candidate, actor_id)
                append_event(connection, actor_id=actor_id, action="rules.adjusted",
                             resource_type="rule_version", resource_id=str(rules.version),
                             detail={"factor_weights": rules.factor_weights,
                                     "risk_multipliers": rules.risk_multipliers,
                                     "no_visit_days": rules.no_visit_days,
                                     "remote_capacity": rules.remote_capacity,
                                     "content_hash": rules.content_hash()},
                             occurred_at=self._now())
                return "rule_version", str(rules.version), {"version": rules.version}

            return self._idempotent(connection, request_id=request_id, action="adjust_rules",
                                    payload=payload, create=create)

    def _validate_weights(self, weights: dict[str, int]) -> None:
        if not isinstance(weights, dict) or not weights:
            raise ValidationError("factor_weights 必须是非空对象")
        for key, value in weights.items():
            if key not in DEFAULT_FACTOR_WEIGHTS:
                raise ValidationError(f"未知风险事实类别：{key}")
            if not isinstance(value, int) or value < 0:
                raise ValidationError("事实权重必须是非负整数")

    def _validate_multipliers(self, multipliers: dict[str, float]) -> None:
        if not isinstance(multipliers, dict) or not multipliers:
            raise ValidationError("risk_multipliers 必须是非空对象")
        for key, value in multipliers.items():
            if key not in DEFAULT_RISK_MULTIPLIERS:
                raise ValidationError(f"未知风险等级：{key}")
            if not isinstance(value, (int, float)) or value <= 0:
                raise ValidationError("风险乘子必须是正数")

    # ---- 事实物化与方案生成 ----------------------------------------------

    def _load_site_records(self, connection, organization_id: str) -> dict[str, dict[str, Any]]:
        query = (
            "SELECT s.site_id, s.organization_id, dr.category, dr.external_key, dr.payload_json, "
            "dr.payload_hash, dr.created_at FROM sites s "
            "JOIN domain_records dr ON dr.site_id=s.site_id WHERE s.organization_id=? "
            "ORDER BY s.site_id, dr.created_at, dr.external_key"
        )
        sites: dict[str, dict[str, Any]] = {}
        for row in connection.execute(query, (organization_id,)):
            bucket = sites.setdefault(row["site_id"], {"records": []})
            bucket["records"].append({
                "category": row["category"], "external_key": row["external_key"],
                "payload": json.loads(row["payload_json"]), "payload_hash": row["payload_hash"],
                "created_at": row["created_at"],
            })
        return sites

    def generate_plan(self, *, request_id: str, actor_id: str, plan_date: str | None = None,
                      onsite_capacity: int, district_capacity: dict[str, int],
                      remote_capacity: int | None = None) -> WriteReceipt:
        """基于当前事实与当前规则版本生成当日确定队列。"""

        plan_date = plan_date or self._today()
        payload = {"actor_id": actor_id, "plan_date": plan_date, "onsite_capacity": onsite_capacity,
                   "district_capacity": district_capacity, "remote_capacity": remote_capacity}
        if not isinstance(onsite_capacity, int) or onsite_capacity < 0:
            raise ValidationError("onsite_capacity 必须是非负整数")
        if not isinstance(district_capacity, dict) or not district_capacity:
            raise ValidationError("district_capacity 必须是非空的片区名额对象")
        for district, value in district_capacity.items():
            if not str(district).strip() or not isinstance(value, int) or value < 0:
                raise ValidationError("片区名额必须映射到非负整数")

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *DISPATCH_ROLES)
            rules = self._ensure_rules(connection, actor_id)
            remote_total = rules.remote_capacity if remote_capacity is None else remote_capacity
            if not isinstance(remote_total, int) or remote_total < 0:
                raise ValidationError("remote_capacity 必须是非负整数")

            # 同一日期只保留一个活动方案；若已有人锁定名额则拒绝覆盖。
            prior = connection.execute(
                "SELECT * FROM plans WHERE plan_date=? AND status='active' ORDER BY created_at DESC LIMIT 1",
                (plan_date,),
            ).fetchone()
            if prior is not None:
                locked = connection.execute(
                    "SELECT COUNT(*) AS count FROM plan_sites WHERE plan_id=? AND claim_status='locked'",
                    (prior["plan_id"],),
                ).fetchone()["count"]
                if locked:
                    raise ConflictError("当日方案存在已锁定名额，不能重新生成")

            site_records = self._load_site_records(connection, actor.organization_id)
            records_by_site = {site_id: bucket["records"] for site_id, bucket in site_records.items()}
            facts_by_site = triage.build_facts(records_by_site, set(RISK_FACT_CATEGORIES))

            candidates: list[triage.CandidateInput] = []
            snapshot_material: list[dict[str, Any]] = []
            for site_id, records in records_by_site.items():
                facts = facts_by_site.get(site_id, [])
                if not facts:
                    continue  # 风险驱动：没有任何风险事实的企业不进入当日遍历。
                by_category: dict[str, list[dict[str, Any]]] = {}
                for record in records:
                    by_category.setdefault(record["category"], []).append(record)
                district_id = "default"
                district_records = by_category.get("district_profile", [])
                if district_records:
                    district_id = str(district_records[-1]["payload"].get("district_id", "default"))
                risk_level = triage.latest_risk_level(by_category.get("risk_profile", []))
                last_assistance, exempt_until = triage.latest_assistance(
                    by_category.get("assistance_record", []))
                candidates.append(triage.CandidateInput(
                    site_id=site_id, district_id=district_id, facts=tuple(facts),
                    risk_level=risk_level, last_assistance_at=last_assistance,
                    exempt_until=exempt_until, no_visit_days=rules.no_visit_days,
                ))
                for fact in facts:
                    snapshot_material.append({"site_id": site_id, "fact_type": fact.fact_type,
                                              "external_key": fact.external_key,
                                              "payload_hash": fact.payload_hash})

            snapshot_material.sort(key=lambda item: (item["site_id"], item["fact_type"],
                                                     item["external_key"]))
            facts_snapshot_hash = digest({"plan_date": plan_date, "facts": snapshot_material})
            allocated = triage.allocate(
                candidates, rules, plan_date, onsite_capacity, dict(district_capacity), remote_total)
            facts_by_site_id = {candidate.site_id: candidate.facts for candidate in candidates}

            def create() -> tuple[str, str, dict[str, Any]]:
                if prior is not None:
                    connection.execute("UPDATE plans SET status='superseded' WHERE plan_id=?",
                                       (prior["plan_id"],))
                plan_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO plans(plan_id,plan_date,rules_version,rules_content_hash,"
                    "facts_snapshot_hash,onsite_capacity,remote_capacity,district_capacity_json,"
                    "revision,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?, 'active',?,?)",
                    (plan_id, plan_date, rules.version, rules.content_hash(), facts_snapshot_hash,
                     onsite_capacity, remote_total, canonical_json(dict(district_capacity)),
                     0, actor_id, self._now()),
                )
                counts = {"onsite": 0, "remote": 0, "assist_online": 0, "deferred": 0}
                for item in allocated:
                    counts[item.decision] = counts.get(item.decision, 0) + 1
                    connection.execute(
                        "INSERT INTO plan_sites(plan_id,site_id,district_id,rank_order,decision,score,"
                        "risk_level,urgent,exempt,window_broken,facts_json,contributions_json,"
                        "reasons_json,assigned_slot,claim_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'open')",
                        (plan_id, item.site_id, item.district_id, item.rank, item.decision,
                         item.score, item.risk_level, int(item.urgent), int(item.exempt),
                         int(item.window_broken),
                         canonical_json([{"fact_type": fact.fact_type, "external_key": fact.external_key,
                                          "payload_hash": fact.payload_hash, "payload": fact.payload,
                                          "created_at": fact.created_at}
                                         for fact in facts_by_site_id[item.site_id]]),
                         canonical_json(item.fact_contributions), canonical_json(item.reasons),
                         item.assigned_slot),
                    )
                append_event(connection, actor_id=actor_id, action="plan.generated",
                             resource_type="plan", resource_id=plan_id,
                             detail={"plan_date": plan_date, "rules_version": rules.version,
                                     "rules_content_hash": rules.content_hash(),
                                     "facts_snapshot_hash": facts_snapshot_hash,
                                     "superseded": prior["plan_id"] if prior is not None else None,
                                     "counts": counts}, occurred_at=self._now())
                return "plan", plan_id, {"plan_id": plan_id, "plan_date": plan_date,
                                         "rules_version": rules.version, "counts": counts}

            return self._idempotent(connection, request_id=request_id, action="generate_plan",
                                    payload=payload, create=create)

    # ---- 名额锁定、释放、改派 ---------------------------------------------

    def _active_plan(self, connection, plan_id: str):
        row = connection.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError("方案不存在")
        return row

    def _plan_site(self, connection, plan_id: str, site_id: str):
        row = connection.execute("SELECT * FROM plan_sites WHERE plan_id=? AND site_id=?",
                                 (plan_id, site_id)).fetchone()
        if row is None:
            raise NotFoundError("该企业不在此方案中")
        return row

    def _check_revision(self, plan_row, expected_revision: int) -> None:
        if int(expected_revision) != plan_row["revision"]:
            raise ConflictError(
                f"方案版本不一致：期望 revision={expected_revision}，当前 revision={plan_row['revision']}")

    def _record_slot_action(self, connection, *, plan_id: str, site_id: str, action: str,
                            expected_revision: int, reason: str, actor_id: str,
                            from_officer_id: str | None, to_officer_id: str | None) -> None:
        connection.execute(
            "INSERT INTO slot_actions(action_id,plan_id,site_id,action,expected_revision,"
            "from_officer_id,to_officer_id,reason,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, plan_id, site_id, action, int(expected_revision),
             from_officer_id, to_officer_id, reason, actor_id, self._now()),
        )

    def lock_slot(self, *, request_id: str, actor_id: str, plan_id: str, site_id: str,
                  officer_id: str, expected_revision: int, reason: str) -> WriteReceipt:
        """调度员领取一个名额。两个调度员并发领取只有一个成功。"""

        payload = {"actor_id": actor_id, "plan_id": plan_id, "site_id": site_id,
                   "officer_id": officer_id, "expected_revision": expected_revision, "reason": reason}
        reason = (reason or "").strip()
        if not reason:
            raise ValidationError("锁定名额必须填写理由")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *DISPATCH_ROLES)
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="lock_slot", payload=payload)
            if replay is not None:
                return replay
            plan_row = self._active_plan(connection, plan_id)
            if plan_row["status"] != "active":
                raise ConflictError("方案已被新版本取代，不能在旧方案上派单")
            self._check_revision(plan_row, expected_revision)
            plan_site = self._plan_site(connection, plan_id, site_id)
            if plan_site["decision"] not in ("onsite", "remote"):
                raise ConflictError("仅现场检查或远程复核名额可被领取")
            if not self._officer_exists(connection, officer_id, actor.organization_id):
                raise NotFoundError("执法人员不在名册中")

            def create() -> tuple[str, str, dict[str, Any]]:
                result = connection.execute(
                    "UPDATE plan_sites SET claim_status='locked', claimed_by=?, claimed_at=? "
                    "WHERE plan_id=? AND site_id=? AND claim_status='open'",
                    (officer_id, self._now(), plan_id, site_id),
                )
                if result.rowcount != 1:
                    raise ConflictError("名额已被其他调度员领取")
                next_revision = plan_row["revision"] + 1
                connection.execute("UPDATE plans SET revision=? WHERE plan_id=? AND revision=?",
                                   (next_revision, plan_id, plan_row["revision"]))
                self._record_slot_action(connection, plan_id=plan_id, site_id=site_id, action="lock",
                                         expected_revision=expected_revision, reason=reason,
                                         actor_id=actor_id, from_officer_id=None,
                                         to_officer_id=officer_id)
                append_event(connection, actor_id=actor_id, action="slot.locked",
                             resource_type="plan_site", resource_id=f"{plan_id}:{site_id}",
                             detail={"plan_id": plan_id, "site_id": site_id, "officer_id": officer_id,
                                     "decision": plan_site["decision"], "reason": reason,
                                     "revision_before": plan_row["revision"],
                                     "revision_after": next_revision}, occurred_at=self._now())
                return "slot", f"{plan_id}:{site_id}", {"plan_id": plan_id, "site_id": site_id,
                                                        "revision": next_revision}

            return self._idempotent(connection, request_id=request_id, action="lock_slot",
                                    payload=payload, create=create)

    def release_slot(self, *, request_id: str, actor_id: str, plan_id: str, site_id: str,
                     expected_revision: int, reason: str) -> WriteReceipt:
        """释放已锁定名额，使其可被重新领取。"""

        payload = {"actor_id": actor_id, "plan_id": plan_id, "site_id": site_id,
                   "expected_revision": expected_revision, "reason": reason}
        reason = (reason or "").strip()
        if not reason:
            raise ValidationError("释放名额必须填写理由")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *DISPATCH_ROLES)
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="release_slot", payload=payload)
            if replay is not None:
                return replay
            plan_row = self._active_plan(connection, plan_id)
            self._check_revision(plan_row, expected_revision)
            plan_site = self._plan_site(connection, plan_id, site_id)
            if plan_site["claim_status"] != "locked":
                raise ConflictError("名额当前未锁定，无需释放")

            def create() -> tuple[str, str, dict[str, Any]]:
                previous_officer = plan_site["claimed_by"]
                connection.execute(
                    "UPDATE plan_sites SET claim_status='open', claimed_by=NULL, claimed_at=NULL "
                    "WHERE plan_id=? AND site_id=?",
                    (plan_id, site_id),
                )
                next_revision = plan_row["revision"] + 1
                connection.execute("UPDATE plans SET revision=? WHERE plan_id=? AND revision=?",
                                   (next_revision, plan_id, plan_row["revision"]))
                self._record_slot_action(connection, plan_id=plan_id, site_id=site_id, action="release",
                                         expected_revision=expected_revision, reason=reason,
                                         actor_id=actor_id, from_officer_id=previous_officer,
                                         to_officer_id=None)
                append_event(connection, actor_id=actor_id, action="slot.released",
                             resource_type="plan_site", resource_id=f"{plan_id}:{site_id}",
                             detail={"plan_id": plan_id, "site_id": site_id,
                                     "previous_officer_id": previous_officer, "reason": reason,
                                     "revision_before": plan_row["revision"],
                                     "revision_after": next_revision}, occurred_at=self._now())
                return "slot", f"{plan_id}:{site_id}", {"plan_id": plan_id, "site_id": site_id,
                                                        "revision": next_revision}

            return self._idempotent(connection, request_id=request_id, action="release_slot",
                                    payload=payload, create=create)

    def reassign_slot(self, *, request_id: str, actor_id: str, plan_id: str, site_id: str,
                      to_officer_id: str, expected_revision: int, reason: str) -> WriteReceipt:
        """把已锁定名额改派给另一名执法人员。"""

        payload = {"actor_id": actor_id, "plan_id": plan_id, "site_id": site_id,
                   "to_officer_id": to_officer_id, "expected_revision": expected_revision,
                   "reason": reason}
        reason = (reason or "").strip()
        if not reason:
            raise ValidationError("改派名额必须填写理由")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *DISPATCH_ROLES)
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="reassign_slot", payload=payload)
            if replay is not None:
                return replay
            plan_row = self._active_plan(connection, plan_id)
            if plan_row["status"] != "active":
                raise ConflictError("方案已被新版本取代，不能在旧方案上改派")
            self._check_revision(plan_row, expected_revision)
            plan_site = self._plan_site(connection, plan_id, site_id)
            if plan_site["claim_status"] != "locked":
                raise ConflictError("名额未锁定，不能改派")
            if not self._officer_exists(connection, to_officer_id, actor.organization_id):
                raise NotFoundError("目标执法人员不在名册中")

            def create() -> tuple[str, str, dict[str, Any]]:
                previous_officer = plan_site["claimed_by"]
                if to_officer_id == previous_officer:
                    raise ConflictError("目标执法人员与当前持有者相同")
                connection.execute(
                    "UPDATE plan_sites SET claimed_by=?, claimed_at=? WHERE plan_id=? AND site_id=?",
                    (to_officer_id, self._now(), plan_id, site_id),
                )
                next_revision = plan_row["revision"] + 1
                connection.execute("UPDATE plans SET revision=? WHERE plan_id=? AND revision=?",
                                   (next_revision, plan_id, plan_row["revision"]))
                self._record_slot_action(connection, plan_id=plan_id, site_id=site_id, action="reassign",
                                         expected_revision=expected_revision, reason=reason,
                                         actor_id=actor_id, from_officer_id=previous_officer,
                                         to_officer_id=to_officer_id)
                append_event(connection, actor_id=actor_id, action="slot.reassigned",
                             resource_type="plan_site", resource_id=f"{plan_id}:{site_id}",
                             detail={"plan_id": plan_id, "site_id": site_id,
                                     "from_officer_id": previous_officer,
                                     "to_officer_id": to_officer_id, "reason": reason,
                                     "revision_before": plan_row["revision"],
                                     "revision_after": next_revision}, occurred_at=self._now())
                return "slot", f"{plan_id}:{site_id}", {"plan_id": plan_id, "site_id": site_id,
                                                        "revision": next_revision}

            return self._idempotent(connection, request_id=request_id, action="reassign_slot",
                                    payload=payload, create=create)

    def _officer_exists(self, connection, officer_id: str, organization_id: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM actors WHERE actor_id=? AND organization_id=? AND active=1",
            (officer_id, organization_id),
        ).fetchone()
        return row is not None

    # ---- 查询与解释 -------------------------------------------------------

    def _site_view(self, row) -> dict[str, Any]:
        return {
            "site_id": row["site_id"], "district_id": row["district_id"], "rank": row["rank_order"],
            "decision": row["decision"], "decision_label": {
                "onsite": "现场检查", "remote": "远程复核",
                "assist_online": "仅线上帮扶", "deferred": "延后"}[row["decision"]],
            "score": row["score"], "risk_level": row["risk_level"],
            "urgent": bool(row["urgent"]), "exempt": bool(row["exempt"]),
            "window_broken": bool(row["window_broken"]),
            "facts": json.loads(row["facts_json"]),
            "contributions": json.loads(row["contributions_json"]),
            "reasons": json.loads(row["reasons_json"]),
            "assigned_slot": row["assigned_slot"], "claim_status": row["claim_status"],
            "claimed_by": row["claimed_by"], "claimed_at": row["claimed_at"],
        }

    def get_plan(self, *, actor_id: str, plan_id: str) -> dict[str, Any]:
        """返回一个方案的完整内容，供重启后解释。"""

        with self.database.reading() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *READ_ROLES)
            plan_row = self._active_plan(connection, plan_id)
            sites = [self._site_view(row) for row in connection.execute(
                "SELECT * FROM plan_sites WHERE plan_id=? ORDER BY rank_order, site_id", (plan_id,))]
        queues: dict[str, list[str]] = {key: [] for key in
                                        ("onsite", "remote", "assist_online", "deferred")}
        for site in sites:
            queues[site["decision"]].append(site["site_id"])
        return {
            "plan_id": plan_id, "plan_date": plan_row["plan_date"], "status": plan_row["status"],
            "revision": plan_row["revision"], "rules_version": plan_row["rules_version"],
            "rules_content_hash": plan_row["rules_content_hash"],
            "facts_snapshot_hash": plan_row["facts_snapshot_hash"],
            "onsite_capacity": plan_row["onsite_capacity"],
            "remote_capacity": plan_row["remote_capacity"],
            "district_capacity": json.loads(plan_row["district_capacity_json"]),
            "queues": queues, "sites": sites,
        }

    def explain_site(self, *, actor_id: str, plan_id: str, site_id: str) -> dict[str, Any]:
        """解释一家企业为何入选、被延后或仅安排线上帮扶。"""

        with self.database.reading() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *READ_ROLES)
            plan_row = self._active_plan(connection, plan_id)
            row = self._plan_site(connection, plan_id, site_id)
            view = self._site_view(row)
        urgent_facts = [fact for fact in view["facts"] if fact["fact_type"] == triage.URGENT]
        return {
            "plan_id": plan_id, "plan_date": plan_row["plan_date"], "revision": plan_row["revision"],
            "rules_version": plan_row["rules_version"],
            "rules_content_hash": plan_row["rules_content_hash"],
            "facts_snapshot_hash": plan_row["facts_snapshot_hash"],
            "site_id": site_id, "decision": view["decision"],
            "decision_label": view["decision_label"], "score": view["score"],
            "risk_level": view["risk_level"], "exempt": view["exempt"],
            "window_broken": view["window_broken"], "assigned_slot": view["assigned_slot"],
            "claim_status": view["claim_status"], "claimed_by": view["claimed_by"],
            "facts": view["facts"], "contributions": view["contributions"],
            "urgent_trigger_basis": [
                fact["payload"].get("trigger_basis") or fact["payload"].get("basis")
                for fact in urgent_facts],
            "reasons": view["reasons"],
        }

    def list_plans(self, *, actor_id: str, plan_date: str | None = None) -> list[dict[str, Any]]:
        """列出方案摘要，可按日期过滤。"""

        with self.database.reading() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *READ_ROLES)
            if plan_date:
                rows = connection.execute(
                    "SELECT * FROM plans WHERE plan_date=? ORDER BY created_at DESC", (plan_date,))
            else:
                rows = connection.execute(
                    "SELECT * FROM plans ORDER BY plan_date DESC, created_at DESC")
            result = []
            for row in rows:
                counts = {}
                for decision in ("onsite", "remote", "assist_online", "deferred"):
                    counts[decision] = connection.execute(
                        "SELECT COUNT(*) AS c FROM plan_sites WHERE plan_id=? AND decision=?",
                        (row["plan_id"], decision)).fetchone()["c"]
                result.append({"plan_id": row["plan_id"], "plan_date": row["plan_date"],
                               "status": row["status"], "revision": row["revision"],
                               "rules_version": row["rules_version"],
                               "facts_snapshot_hash": row["facts_snapshot_hash"], "counts": counts})
        return result

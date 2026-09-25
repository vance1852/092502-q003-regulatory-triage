"""优先级编排服务。

把逾期自查、未闭环隐患、治污设施异常、企业风险等级和近期帮扶记录
汇聚为带版本的风险事实，在每日可用人力与片区时段约束下生成确定的
现场检查与远程复核队列，并提供名额派单、紧急事件与重启后可复核的解释。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import (DispatchStateConflict, NotFoundError, PermissionDenied,
                     PlanVersionConflict, ValidationError)
from .models import DomainRecord
from .rules import (Fact, build_plan_items, derive_site_facts, facts_hash,
                    normalize_rules, normalize_slots, parse_day, score_site)
from .service import DomainService
from .storage import Database


class TriageService:
    """在基础资料服务之上编排风险事实、检查方案与派单名额。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        self.domains = DomainService(database, self.clock)

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _actor(self, connection, actor_id: str):
        return self.domains._actor(connection, actor_id)

    def _require(self, actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    # ---- 规则集版本 ----------------------------------------------------

    def register_rules(self, *, request_id: str, actor_id: str, rule_id: str,
                       rules: dict[str, Any], activate: bool = True) -> dict[str, Any]:
        """登记一个规则版本；编号相同且内容不同则版本号自增。

        规则调整不会改写既有方案：方案永久保存其规则编号与版本。
        """

        payload = {"rule_id": rule_id, "rules": rules, "activate": activate}
        normalized = normalize_rules(rules)
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "dispatcher")
            rule_id = self.domains._identifier(rule_id, "rule_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT version, payload_hash FROM rule_sets WHERE rule_id=? "
                    "ORDER BY version DESC LIMIT 1", (rule_id,)
                ).fetchone()
                payload_hash = digest(canonical_json(normalized))
                if row is not None:
                    if row["payload_hash"] == payload_hash:
                        version = row["version"]
                        created = False
                    else:
                        version = row["version"] + 1
                        created = True
                else:
                    version, created = 1, True
                if created:
                    connection.execute(
                        "INSERT INTO rule_sets(rule_id,version,payload_json,payload_hash,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?)",
                        (rule_id, version, canonical_json(normalized), payload_hash,
                         actor_id, self._now()),
                    )
                    append_event(connection, actor_id=actor_id, action="rules.registered",
                                 resource_type="rule_set", resource_id=rule_id,
                                 detail={"version": version, "rules_hash": payload_hash},
                                 occurred_at=self._now())
                activated = False
                if activate:
                    active = connection.execute(
                        "SELECT rule_id, version FROM active_rule_versions WHERE singleton=1"
                    ).fetchone()
                    if active is None or active["rule_id"] != rule_id or active["version"] != version:
                        connection.execute(
                            "INSERT INTO active_rule_versions(singleton,rule_id,version,activated_at) "
                            "VALUES(1,?,?,?) ON CONFLICT(singleton) DO UPDATE SET "
                            "rule_id=excluded.rule_id, version=excluded.version, activated_at=excluded.activated_at",
                            (rule_id, version, self._now()),
                        )
                        activated = True
                        append_event(connection, actor_id=actor_id, action="rules.activated",
                                     resource_type="rule_set", resource_id=rule_id,
                                     detail={"version": version}, occurred_at=self._now())
                response = {"rule_id": rule_id, "version": version,
                            "rules_hash": payload_hash, "activated": activated,
                            "created": created}
                return "rule_set", f"{rule_id}:v{version}", response

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="register_rules", payload=payload, create=create)
            response["replayed"] = receipt.replayed
            return response

    def _idempotent(self, connection, *, request_id, action, payload, create):
        """复用基础服务的幂等机制，同时取回完整业务响应。"""

        receipt = self.domains._idempotent(
            connection, request_id=request_id, action=action, payload=payload, create=create
        )
        row = connection.execute(
            "SELECT response_json FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        return receipt, json.loads(row["response_json"])

    def get_rules(self, rule_id: str, version: int) -> tuple[dict[str, Any], str, int, str]:
        row = self.database.connection.execute(
            "SELECT * FROM rule_sets WHERE rule_id=? AND version=?", (rule_id, version)
        ).fetchone()
        if row is None:
            raise NotFoundError("规则版本不存在")
        return (json.loads(row["payload_json"]), row["rule_id"], row["version"],
                row["payload_hash"])

    # ---- 风险事实快照 --------------------------------------------------

    def refresh_facts(self, *, request_id: str, actor_id: str, district_id: str | None = None) -> dict[str, Any]:
        """从领域资料重算事实快照（新增、状态变更、闭环失效均版本化）。"""

        payload = {"district_id": district_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "dispatcher", "operator", "reviewer")
            rules = normalize_rules(None)

            def create() -> tuple[str, str, dict[str, Any]]:
                site_rows = connection.execute(
                    "SELECT site_id FROM sites ORDER BY site_id"
                ).fetchall()
                site_ids = [row["site_id"] for row in site_rows]
                if district_id:
                    site_ids = [site_id for site_id in site_ids
                                if self._district_of(connection, site_id) == district_id]
                upserted = closed = 0
                as_of = self.clock.now().date()
                for site_id in site_ids:
                    records = self._load_records(connection, site_id)
                    derived, _ = derive_site_facts(site_id, records, rules, as_of)
                    for fact in derived:
                        upserted += self._upsert_fact(connection, fact)
                    closed += self._deactivate_missing(connection, site_id, derived)
                append_event(connection, actor_id=actor_id, action="facts.refreshed",
                             resource_type="fact_snapshot", resource_id=district_id or "*",
                             detail={"upserted": upserted, "closed": closed,
                                     "site_count": len(site_ids)},
                             occurred_at=self._now())
                response = {"upserted": upserted, "closed": closed,
                            "site_count": len(site_ids)}
                return "fact_refresh", district_id or "*", response

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="refresh_facts", payload=payload, create=create)
            response["replayed"] = receipt.replayed
            return response

    def _district_of(self, connection, site_id: str) -> str:
        records = connection.execute(
            "SELECT payload_json FROM domain_records WHERE site_id=? AND category='district_profile' "
            "ORDER BY created_at DESC, record_id DESC", (site_id,)
        ).fetchall()
        for row in records:
            data = json.loads(row["payload_json"])
            district = data.get("district_id")
            if district:
                return str(district)
        return "default"

    def _load_records(self, connection, site_id: str) -> list[DomainRecord]:
        rows = connection.execute(
            "SELECT * FROM domain_records WHERE site_id=? ORDER BY rowid",
            (site_id,),
        ).fetchall()
        return [DomainRecord(row["record_id"], row["site_id"], row["category"],
                             row["external_key"], json.loads(row["payload_json"]),
                             row["payload_hash"], row["created_by"], row["created_at"])
                for row in rows]

    def _upsert_fact(self, connection, fact: Fact) -> int:
        """插入新事实或在规则无关材料变化时升版；返回 1=新增/变更，0=无变化。"""

        row = connection.execute(
            "SELECT * FROM fact_snapshots WHERE fact_key=?", (fact.fact_key,)
        ).fetchone()
        material_hash = digest(canonical_json(fact.material()))
        if row is not None and row["source_hash"] == material_hash and bool(row["active"]) == fact.active:
            return 0
        if row is None:
            connection.execute(
                "INSERT INTO fact_snapshots(fact_key,fact_version,site_id,fact_type,status,"
                "severity,source_record_id,source_hash,evidence_json,observed_at,active,"
                "first_seen_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?)",
                (fact.fact_key, 1, fact.site_id, fact.fact_type, fact.status, fact.severity,
                 fact.source_record_id, material_hash, canonical_json(fact.evidence),
                 fact.observed_at, self._now(), self._now()),
            )
            return 1
        connection.execute(
            "UPDATE fact_snapshots SET fact_version=fact_version+1, status=?, severity=?, "
            "source_record_id=?, source_hash=?, evidence_json=?, observed_at=?, active=?, updated_at=?",
            (fact.status, fact.severity, fact.source_record_id, material_hash,
             canonical_json(fact.evidence), fact.observed_at, 1 if fact.active else 0, self._now()),
        )
        return 1

    def _deactivate_missing(self, connection, site_id: str,
                            derived: list[Fact]) -> int:
        """资料已闭环（最新状态不再产出该事实）时把旧事实置为失效并升版。"""

        active_keys = {fact.fact_key for fact in derived}
        closed = 0
        rows = connection.execute(
            "SELECT fact_key FROM fact_snapshots WHERE site_id=? AND active=1", (site_id,)
        ).fetchall()
        for row in rows:
            if row["fact_key"] not in active_keys:
                connection.execute(
                    "UPDATE fact_snapshots SET active=0, fact_version=fact_version+1, updated_at=? "
                    "WHERE fact_key=?",
                    (self._now(), row["fact_key"]),
                )
                closed += 1
        return closed

    # ---- 紧急事件 ------------------------------------------------------

    def declare_emergency(self, *, request_id: str, actor_id: str, site_id: str,
                          trigger_type: str, trigger_reference: str,
                          detail: dict[str, Any]) -> dict[str, Any]:
        """登记紧急事件。紧急事件可穿透免访窗口，但必须留下触发依据。"""

        if not isinstance(detail, dict) or not detail:
            raise ValidationError("detail 必须是非空对象，需说明触发依据")
        trigger_type = self.domains._text(trigger_type, "trigger_type", 60)
        trigger_reference = self.domains._text(trigger_reference, "trigger_reference", 120)
        payload = {"site_id": site_id, "trigger_type": trigger_type,
                   "trigger_reference": trigger_reference, "detail": detail}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "dispatcher", "operator")
            if connection.execute("SELECT 1 FROM sites WHERE site_id=?", (site_id,)).fetchone() is None:
                raise NotFoundError("场所不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                emergency_id = uuid.uuid4().hex
                trigger_hash = digest(canonical_json(payload))
                connection.execute(
                    "INSERT INTO emergencies(emergency_id,site_id,trigger_type,trigger_reference,"
                    "trigger_detail_json,trigger_hash,status,declared_by,declared_at) "
                    "VALUES(?,?,?,?,?,?,'active',?,?)",
                    (emergency_id, site_id, trigger_type, trigger_reference,
                     canonical_json(detail), trigger_hash, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="emergency.declared",
                             resource_type="emergency", resource_id=emergency_id,
                             detail={"site_id": site_id, "trigger_type": trigger_type,
                                     "trigger_reference": trigger_reference,
                                     "trigger_hash": trigger_hash},
                             occurred_at=self._now())
                response = {"emergency_id": emergency_id, "site_id": site_id,
                            "trigger_type": trigger_type,
                            "trigger_reference": trigger_reference,
                            "trigger_hash": trigger_hash, "status": "active"}
                return "emergency", emergency_id, response

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="declare_emergency", payload=payload, create=create)
            response["replayed"] = receipt.replayed
            return response

    # ---- 方案生成 ------------------------------------------------------

    def generate_plan(self, *, request_id: str, actor_id: str, plan_date: str,
                      district_id: str = "default", slots: list[dict[str, Any]] | None = None,
                      rule_id: str | None = None, rule_version: int | None = None,
                      force: bool = False) -> dict[str, Any]:
        """生成（或确定性复用）当日片区方案。

        事实或规则未变化时复用当前方案版本并保持 plan_version 不变；
        任一变化都会产生新的 plan_version，旧方案永不被改写。
        """

        day = parse_day(plan_date)
        payload = {"plan_date": day.isoformat(), "district_id": district_id,
                   "slots": slots, "rule_id": rule_id, "rule_version": rule_version,
                   "force": force}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "dispatcher")
            if rule_id is not None and rule_version is None:
                raise ValidationError("指定 rule_id 时必须同时指定 rule_version")
            if rule_id is not None:
                rules, resolved_id, resolved_version, rule_hash_value = self.get_rules(rule_id, int(rule_version))
            else:
                active = connection.execute(
                    "SELECT rule_id, version FROM active_rule_versions WHERE singleton=1"
                ).fetchone()
                if active is None:
                    rules = normalize_rules(None)
                    resolved_id, resolved_version, rule_hash_value = "default", 0, digest(canonical_json(rules))
                else:
                    rules, resolved_id, resolved_version, rule_hash_value = self.get_rules(
                        active["rule_id"], active["version"]
                    )
            normalized_slots = normalize_slots(slots, rules)

            def create() -> tuple[str, str, dict[str, Any]]:
                sites_payload = self._collect_sites(connection, district_id, day, rules)
                district_site_ids = {site["site_id"] for site in sites_payload["sites"]}
                emergencies = [emergency
                               for emergency in self._active_emergencies(connection, day)
                               if emergency["site_id"] in district_site_ids]
                state_hash = digest(canonical_json({
                    "facts": sites_payload["fact_material"],
                    "rules_hash": rule_hash_value,
                    "slots": [{key: slot[key] for key in ("slot_id", "start", "end",
                              "onsite_capacity", "remote_capacity")} for slot in normalized_slots],
                    "emergencies": emergencies,
                }))
                existing = connection.execute(
                    "SELECT * FROM plans WHERE plan_date=? AND district_id=? "
                    "ORDER BY plan_version DESC LIMIT 1",
                    (day.isoformat(), district_id),
                ).fetchone()
                if existing is not None and existing["inputs_hash"] == state_hash and not force:
                    return "plan", existing["plan_id"], {
                        "plan_id": existing["plan_id"], "plan_version": existing["plan_version"],
                        "reused": True, "changed": False}

                items = build_plan_items(sites=sites_payload["sites"],
                                         slots=normalized_slots, rules=rules,
                                         emergencies=emergencies)
                plan_version = (existing["plan_version"] + 1) if existing is not None else 1
                plan_id = uuid.uuid4().hex
                all_facts = sites_payload["facts"]
                fact_hash_value = facts_hash(all_facts)
                input_summary = {
                    "site_count": len(sites_payload["sites"]),
                    "fact_count": len(all_facts),
                    "slots": [{key: slot[key] for key in ("slot_id", "start", "end",
                               "onsite_capacity", "remote_capacity")} for slot in normalized_slots],
                    "emergency_count": len(emergencies),
                }
                connection.execute(
                    "INSERT INTO plans(plan_id,plan_date,district_id,rule_id,rule_version,"
                    "rules_hash,facts_hash,inputs_hash,input_summary_json,plan_version,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (plan_id, day.isoformat(), district_id, resolved_id, resolved_version,
                     rule_hash_value, fact_hash_value, state_hash,
                     canonical_json(input_summary), plan_version, actor_id, self._now()),
                )
                for item in items:
                    scored = item.pop("scored")
                    window = item.pop("window")
                    emergency_info = item.pop("emergency")
                    decision = {"reasons": item.pop("reasons"),
                                "deferred_reason": item.pop("deferred_reason"),
                                "weighted_sum": scored["weighted_sum"],
                                "unmitigated_score": scored["unmitigated_score"],
                                "contributions": scored["contributions"],
                                "risk_multiplier": scored["risk_multiplier"],
                                "assistance_mitigation_applied":
                                    scored["assistance_mitigation_applied"],
                                "earliest_observed_at": scored["earliest_observed_at"],
                                "window": window, "emergency": emergency_info}
                    item_facts = [fact for fact in sites_payload["facts_by_site"][item["site_id"]]]
                    connection.execute(
                        "INSERT INTO plan_items(plan_id,site_id,rank,action,score,risk_level,"
                        "slot_id,window_override,decision_json,facts_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (plan_id, item["site_id"], item["rank"], item["action"], item["score"],
                         item["risk_level"], item["slot_id"], 1 if item["window_override"] else 0,
                         canonical_json(decision), canonical_json(item_facts)),
                    )
                    for fact in item_facts:
                        connection.execute(
                            "INSERT INTO plan_facts(plan_id,fact_key,site_id,fact_type,weight) "
                            "VALUES(?,?,?,?,?)",
                            (plan_id, fact["fact_key"], item["site_id"], fact["fact_type"],
                             round(fact["weight"], 4)),
                        )
                self._create_dispatches(connection, plan_id=plan_id, plan_version=plan_version,
                                        plan_date=day.isoformat(), district_id=district_id,
                                        items=items, now=self._now())
                if existing is not None:
                    self._supersede_old_dispatches(connection, existing["plan_id"],
                                                   plan_id, actor_id, self._now())
                append_event(connection, actor_id=actor_id, action="plan.generated",
                             resource_type="plan", resource_id=plan_id,
                             detail={"plan_date": day.isoformat(), "district_id": district_id,
                                     "plan_version": plan_version, "rule_id": resolved_id,
                                     "rule_version": resolved_version, "facts_hash": fact_hash_value,
                                     "inputs_hash": state_hash, "reused": False},
                             occurred_at=self._now())
                response = {"plan_id": plan_id, "plan_version": plan_version, "reused": False,
                            "changed": existing is not None, **input_summary}
                return "plan", plan_id, response

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="generate_plan", payload=payload, create=create)
            response["replayed"] = receipt.replayed
            return response

    def _collect_sites(self, connection, district_id: str, day, rules) -> dict[str, Any]:
        site_rows = connection.execute("SELECT site_id FROM sites ORDER BY site_id").fetchall()
        sites: list[dict[str, Any]] = []
        all_facts: list[Fact] = []
        facts_by_site: dict[str, list[dict[str, Any]]] = {}
        fact_material: list[dict[str, Any]] = []
        for row in site_rows:
            site_id = row["site_id"]
            if self._district_of(connection, site_id) != district_id:
                continue
            records = self._load_records(connection, site_id)
            facts, context = derive_site_facts(site_id, records, rules, day)
            scored = score_site(facts, context, rules)
            risk_level = context["risk_level"]
            sites.append({"site_id": site_id, "facts": facts, "context": context,
                          "scored": scored, "risk_level": risk_level})
            fact_payloads = [{
                "fact_key": fact.fact_key, "fact_type": fact.fact_type, "status": fact.status,
                "severity": fact.severity, "weight": round(fact.weight, 4),
                "evidence": fact.evidence, "observed_at": fact.observed_at,
                "counts": fact.counts,
            } for fact in facts]
            facts_by_site[site_id] = fact_payloads
            all_facts.extend(facts)
            fact_material.extend(fact.material() for fact in facts)
        fact_material.sort(key=lambda item: item["fact_key"])
        return {"sites": sites, "facts": all_facts, "facts_by_site": facts_by_site,
                "fact_material": fact_material}

    def _active_emergencies(self, connection, day) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM emergencies WHERE status='active' "
            "AND substr(declared_at,1,10)<=? ORDER BY declared_at, emergency_id",
            (day.isoformat(),),
        ).fetchall()
        return [{
            "emergency_id": row["emergency_id"], "site_id": row["site_id"],
            "trigger_type": row["trigger_type"], "trigger_reference": row["trigger_reference"],
            "detail": json.loads(row["trigger_detail_json"]), "declared_at": row["declared_at"],
        } for row in rows]

    def _create_dispatches(self, connection, *, plan_id, plan_version, plan_date,
                           district_id, items, now) -> None:
        for item in items:
            if item["action"] not in ("onsite", "remote"):
                continue
            connection.execute(
                "INSERT INTO dispatches(dispatch_id,plan_id,plan_version,plan_date,district_id,"
                "site_id,slot_id,kind,status,created_at) VALUES(?,?,?,?,?,?,?,?,'open',?)",
                (uuid.uuid4().hex, plan_id, plan_version, plan_date, district_id,
                 item["site_id"], item["slot_id"], item["action"], now),
            )

    def _supersede_old_dispatches(self, connection, old_plan_id, new_plan_id,
                                  actor_id, now) -> int:
        """新方案生效后，旧方案仍开放的名额一律作废弃出并留痕。

        已领取（在途执行）的名额保持原状态，但版本核对会拒绝其后续
        锁定/释放/改派，直到按新版本处理。
        """

        rows = connection.execute(
            "SELECT dispatch_id FROM dispatches WHERE plan_id=? AND status='open'",
            (old_plan_id,),
        ).fetchall()
        for row in rows:
            dispatch_id = row["dispatch_id"]
            connection.execute(
                "UPDATE dispatches SET status='superseded', released_by=?, released_at=? "
                "WHERE dispatch_id=? AND status='open'",
                (actor_id, now, dispatch_id),
            )
            connection.execute(
                "INSERT INTO dispatch_events(dispatch_id,action,actor_id,from_status,"
                "to_status,expected_plan_version,reason,occurred_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (dispatch_id, "superseded", actor_id, "open", "superseded", None,
                 json.dumps({"reason": f"方案已更新，名额被新方案 {new_plan_id} 取代"},
                            ensure_ascii=False), now),
            )
        return len(rows)

    # ---- 名额领取/释放/改派 --------------------------------------------

    def claim_dispatch(self, *, request_id: str, actor_id: str, dispatch_id: str,
                       expected_plan_version: int, reason: str) -> dict[str, Any]:
        """领取名额。条件更新保证两个调度员并发领取不会重复派单。"""

        reason = self.domains._text(reason, "reason", 300)
        payload = {"dispatch_id": dispatch_id, "expected_plan_version": expected_plan_version,
                   "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "dispatcher")

            def create() -> tuple[str, str, dict[str, Any]]:
                row = self._dispatch_row(connection, dispatch_id)
                self._check_plan_version(connection, row, expected_plan_version)
                if row["status"] != "open":
                    raise DispatchStateConflict(f"名额当前状态为 {row['status']}，不能领取")
                result = connection.execute(
                    "UPDATE dispatches SET status='claimed', claimed_by=?, claimed_at=?, "
                    "released_by=NULL, released_at=NULL WHERE dispatch_id=? AND status='open'",
                    (actor_id, self._now(), dispatch_id),
                )
                if result.rowcount != 1:  # 并发下被他人抢先
                    raise DispatchStateConflict("名额已被其他调度员领取")
                self._event(connection, dispatch_id, "claimed", actor_id, "open", "claimed",
                            expected_plan_version, reason)
                append_event(connection, actor_id=actor_id, action="dispatch.claimed",
                             resource_type="dispatch", resource_id=dispatch_id,
                             detail={"plan_id": row["plan_id"], "site_id": row["site_id"],
                                     "plan_version": row["plan_version"], "reason": reason},
                             occurred_at=self._now())
                return self._dispatch_response(connection, dispatch_id, create_cache=row)

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="claim_dispatch", payload=payload, create=create)
            response["replayed"] = receipt.replayed
            return response

    def release_dispatch(self, *, request_id: str, actor_id: str, dispatch_id: str,
                         expected_plan_version: int, reason: str) -> dict[str, Any]:
        """释放已领取名额，回到可领取池，必须记录理由。"""

        reason = self.domains._text(reason, "reason", 300)
        payload = {"dispatch_id": dispatch_id, "expected_plan_version": expected_plan_version,
                   "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "dispatcher")

            def create() -> tuple[str, str, dict[str, Any]]:
                row = self._dispatch_row(connection, dispatch_id)
                self._check_plan_version(connection, row, expected_plan_version)
                if row["status"] != "claimed":
                    raise DispatchStateConflict(f"名额当前状态为 {row['status']}，不能释放")
                connection.execute(
                    "UPDATE dispatches SET status='open', claimed_by=NULL, claimed_at=NULL, "
                    "released_by=?, released_at=? WHERE dispatch_id=?",
                    (actor_id, self._now(), dispatch_id),
                )
                self._event(connection, dispatch_id, "released", actor_id, "claimed", "open",
                            expected_plan_version, reason)
                append_event(connection, actor_id=actor_id, action="dispatch.released",
                             resource_type="dispatch", resource_id=dispatch_id,
                             detail={"plan_id": row["plan_id"], "site_id": row["site_id"],
                                     "plan_version": row["plan_version"], "reason": reason},
                             occurred_at=self._now())
                return self._dispatch_response(connection, dispatch_id)

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="release_dispatch", payload=payload, create=create)
            response["replayed"] = receipt.replayed
            return response

    def reassign_dispatch(self, *, request_id: str, actor_id: str, dispatch_id: str,
                          expected_plan_version: int, to_actor_id: str,
                          reason: str) -> dict[str, Any]:
        """改派名额给另一名调度员/执法人员，必须核对方案版本并记录理由。"""

        reason = self.domains._text(reason, "reason", 300)
        to_actor_id = self.domains._identifier(to_actor_id, "to_actor_id")
        payload = {"dispatch_id": dispatch_id, "expected_plan_version": expected_plan_version,
                   "to_actor_id": to_actor_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "dispatcher")
            target = self._actor(connection, to_actor_id)
            if target.role not in ("admin", "dispatcher", "operator"):
                raise ValidationError("改派对象必须是调度员或执法人员")

            def create() -> tuple[str, str, dict[str, Any]]:
                row = self._dispatch_row(connection, dispatch_id)
                self._check_plan_version(connection, row, expected_plan_version)
                if row["status"] not in ("claimed", "open"):
                    raise DispatchStateConflict(f"名额当前状态为 {row['status']}，不能改派")
                connection.execute(
                    "UPDATE dispatches SET status='claimed', claimed_by=?, claimed_at=? "
                    "WHERE dispatch_id=?",
                    (to_actor_id, self._now(), dispatch_id),
                )
                self._event(connection, dispatch_id, "reassigned", actor_id, row["status"],
                            "claimed", expected_plan_version, reason, extra={"to": to_actor_id})
                append_event(connection, actor_id=actor_id, action="dispatch.reassigned",
                             resource_type="dispatch", resource_id=dispatch_id,
                             detail={"plan_id": row["plan_id"], "site_id": row["site_id"],
                                     "plan_version": row["plan_version"], "to_actor_id": to_actor_id,
                                     "reason": reason}, occurred_at=self._now())
                return self._dispatch_response(connection, dispatch_id)

            receipt, response = self._idempotent(connection, request_id=request_id,
                                                 action="reassign_dispatch", payload=payload, create=create)
            response["replayed"] = receipt.replayed
            return response

    def _dispatch_row(self, connection, dispatch_id: str):
        row = connection.execute(
            "SELECT * FROM dispatches WHERE dispatch_id=?", (dispatch_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("名额不存在")
        return row

    def _check_plan_version(self, connection, row, expected_plan_version: int) -> None:
        """名额操作必须对准当日片区的最新方案版本。

        名额行记录的是它所属方案的版本；若此后已生成更新版本，
        旧名额一律视为失效，携带陈旧版本的锁定/释放/改派都会被拒绝。
        """

        latest = connection.execute(
            "SELECT plan_version, plan_id FROM plans WHERE plan_date=? AND district_id=? "
            "ORDER BY plan_version DESC LIMIT 1",
            (row["plan_date"], row["district_id"]),
        ).fetchone()
        current = latest["plan_version"]
        try:
            expected = int(expected_plan_version)
        except (TypeError, ValueError) as exc:
            raise ValidationError("expected_plan_version 必须是整数") from exc
        if expected != current:
            raise PlanVersionConflict(
                f"方案版本已变化：当前为 {current}，请求携带 {expected}"
            )
        if row["plan_version"] != current:
            raise PlanVersionConflict(
                f"名额属于已失效的方案版本 {row['plan_version']}，当前为 {current}"
            )

    def _event(self, connection, dispatch_id, action, actor_id, from_status, to_status,
               expected_version, reason, extra=None) -> None:
        connection.execute(
            "INSERT INTO dispatch_events(dispatch_id,action,actor_id,from_status,to_status,"
            "expected_plan_version,reason,occurred_at) VALUES(?,?,?,?,?,?,?,?)",
            (dispatch_id, action, actor_id, from_status, to_status, expected_version,
             json.dumps({"reason": reason, **(extra or {})}, ensure_ascii=False), self._now()),
        )

    def _dispatch_response(self, connection, dispatch_id, create_cache=None) -> tuple[str, str, dict[str, Any]]:
        row = create_cache or self._dispatch_row(connection, dispatch_id)
        refreshed = connection.execute("SELECT * FROM dispatches WHERE dispatch_id=?",
                                       (dispatch_id,)).fetchone()
        response = {
            "dispatch_id": dispatch_id, "plan_id": refreshed["plan_id"],
            "plan_version": refreshed["plan_version"], "plan_date": refreshed["plan_date"],
            "district_id": refreshed["district_id"], "site_id": refreshed["site_id"],
            "slot_id": refreshed["slot_id"], "kind": refreshed["kind"],
            "status": refreshed["status"], "claimed_by": refreshed["claimed_by"],
        }
        return "dispatch", dispatch_id, response

    # ---- 查询与解释 ----------------------------------------------------

    def get_plan(self, plan_date: str, district_id: str = "default",
                 plan_version: int | None = None) -> dict[str, Any]:
        day = parse_day(plan_date)
        connection = self.database.connection
        if plan_version is None:
            plan = connection.execute(
                "SELECT * FROM plans WHERE plan_date=? AND district_id=? "
                "ORDER BY plan_version DESC LIMIT 1",
                (day.isoformat(), district_id),
            ).fetchone()
        else:
            plan = connection.execute(
                "SELECT * FROM plans WHERE plan_date=? AND district_id=? AND plan_version=?",
                (day.isoformat(), district_id, plan_version),
            ).fetchone()
        if plan is None:
            raise NotFoundError("方案不存在")
        items = []
        for row in connection.execute(
            "SELECT * FROM plan_items WHERE plan_id=? ORDER BY rank", (plan["plan_id"],)
        ):
            decision = json.loads(row["decision_json"])
            items.append({
                "site_id": row["site_id"], "rank": row["rank"], "action": row["action"],
                "score": row["score"], "risk_level": row["risk_level"],
                "slot_id": row["slot_id"], "window_override": bool(row["window_override"]),
                "facts": json.loads(row["facts_json"]),
                "decision": decision,
                "dispatch": self._dispatch_for(connection, plan["plan_id"], row["site_id"]),
            })
        return {
            "plan_id": plan["plan_id"], "plan_date": plan["plan_date"],
            "district_id": plan["district_id"], "plan_version": plan["plan_version"],
            "rule_id": plan["rule_id"], "rule_version": plan["rule_version"],
            "rules_hash": plan["rules_hash"], "facts_hash": plan["facts_hash"],
            "inputs_hash": plan["inputs_hash"],
            "input_summary": json.loads(plan["input_summary_json"]),
            "created_by": plan["created_by"], "created_at": plan["created_at"],
            "items": items,
        }

    def _dispatch_for(self, connection, plan_id: str, site_id: str) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT dispatch_id,kind,status,claimed_by,slot_id,claimed_at,reason "
            "FROM dispatches WHERE plan_id=? AND site_id=?", (plan_id, site_id)
        ).fetchone()
        if row is None:
            return None
        return {"dispatch_id": row["dispatch_id"], "kind": row["kind"],
                "status": row["status"], "claimed_by": row["claimed_by"],
                "slot_id": row["slot_id"], "claimed_at": row["claimed_at"]}

    def explain_site(self, *, plan_date: str, site_id: str, district_id: str = "default",
                     plan_version: int | None = None) -> dict[str, Any]:
        """重启后解释一家企业为何入选、被延后或仅安排线上帮扶。"""

        plan = self.get_plan(plan_date, district_id, plan_version)
        item = next((candidate for candidate in plan["items"]
                     if candidate["site_id"] == site_id), None)
        if item is None:
            raise NotFoundError("该场所不在方案覆盖范围内")
        decision = item["decision"]
        action_text = {
            "onsite": "入选现场检查",
            "remote": "入选远程复核",
            "assistance": "仅安排线上帮扶",
            "deferred": "被延后",
        }[item["action"]]
        return {
            "site_id": site_id, "plan_date": plan["plan_date"],
            "district_id": district_id, "plan_id": plan["plan_id"],
            "plan_version": plan["plan_version"], "rule_id": plan["rule_id"],
            "rule_version": plan["rule_version"], "rank": item["rank"],
            "action": item["action"], "conclusion": action_text,
            "score": item["score"], "risk_level": item["risk_level"],
            "reasons": decision["reasons"],
            "deferred_reason": decision["deferred_reason"],
            "window": decision["window"],
            "emergency": decision["emergency"],
            "score_detail": {
                "weighted_sum": decision["weighted_sum"],
                "unmitigated_score": decision["unmitigated_score"],
                "risk_multiplier": decision["risk_multiplier"],
                "assistance_mitigation_applied":
                    decision["assistance_mitigation_applied"],
                "contributions": decision["contributions"],
            },
            "facts": item["facts"],
        }

    def list_dispatches(self, *, plan_date: str, district_id: str = "default",
                        status: str | None = None) -> dict[str, Any]:
        day = parse_day(plan_date)
        connection = self.database.connection
        plan = connection.execute(
            "SELECT * FROM plans WHERE plan_date=? AND district_id=? ORDER BY plan_version DESC LIMIT 1",
            (day.isoformat(), district_id),
        ).fetchone()
        if plan is None:
            raise NotFoundError("方案不存在")
        query = "SELECT * FROM dispatches WHERE plan_id=?"
        parameters: list[Any] = [plan["plan_id"]]
        if status:
            query += " AND status=?"
            parameters.append(status)
        query += " ORDER BY slot_id, kind, site_id"
        items = [{
            "dispatch_id": row["dispatch_id"], "site_id": row["site_id"],
            "slot_id": row["slot_id"], "kind": row["kind"], "status": row["status"],
            "claimed_by": row["claimed_by"], "plan_version": row["plan_version"],
        } for row in connection.execute(query, parameters)]
        return {"plan_id": plan["plan_id"], "plan_version": plan["plan_version"],
                "plan_date": plan["plan_date"], "district_id": district_id, "items": items}

    def dispatch_history(self, dispatch_id: str) -> dict[str, Any]:
        row = self._dispatch_row(self.database.connection, dispatch_id)
        events = []
        for event_row in self.database.connection.execute(
            "SELECT * FROM dispatch_events WHERE dispatch_id=? ORDER BY event_seq",
            (dispatch_id,),
        ):
            detail = json.loads(event_row["reason"])
            events.append({"action": event_row["action"], "actor_id": event_row["actor_id"],
                           "from_status": event_row["from_status"],
                           "to_status": event_row["to_status"],
                           "expected_plan_version": event_row["expected_plan_version"],
                           "occurred_at": event_row["occurred_at"], **detail})
        return {"dispatch_id": dispatch_id, "plan_id": row["plan_id"],
                "plan_version": row["plan_version"], "site_id": row["site_id"],
                "status": row["status"], "events": events}

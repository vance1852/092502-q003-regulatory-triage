"""运行基础服务与优先级编排的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .service import DomainService
from .storage import Database
from .triage import TriageService


def run() -> dict[str, object]:
    """执行登记、风险汇聚、方案生成、紧急穿透与派单链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        service = DomainService(database, clock)
        triage = TriageService(database, clock)
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范监管局")
        service.register_actor(request_id="req-admin", actor_id="bootstrap",
                               new_actor_id="admin-001", display_name="系统管理员",
                               role="admin", organization_id="org-001")
        service.register_actor(request_id="req-dispatcher", actor_id="admin-001",
                               new_actor_id="disp-001", display_name="调度员",
                               role="dispatcher", organization_id="org-001")
        for site_id, name in (("site-001", "一号生产场所"), ("site-002", "二号生产场所"),
                              ("site-003", "三号生产场所")):
            service.register_site(request_id=f"req-{site_id}", actor_id="admin-001",
                                  site_id=site_id, organization_id="org-001", name=name,
                                  timezone_name="Asia/Shanghai")
            service.record_domain_data(request_id=f"req-dp-{site_id}", actor_id="admin-001",
                                       site_id=site_id, category="district_profile",
                                       external_key="district", data={"district_id": "D-001"})
        # site-001：逾期自查 + 高风险等级
        service.record_domain_data(request_id="req-risk-001", actor_id="admin-001",
                                   site_id="site-001", category="risk_profile",
                                   external_key="risk", data={"level": "high"})
        service.record_domain_data(request_id="req-sc-001", actor_id="admin-001",
                                   site_id="site-001", category="self_check_report",
                                   external_key="sc-2026q2",
                                   data={"period": "2026Q2", "submitted": False,
                                         "due_date": "2026-08-01"})
        # site-002：未闭环重大隐患，但近期刚帮扶（免访窗口内）
        service.record_domain_data(request_id="req-hz-002", actor_id="admin-001",
                                   site_id="site-002", category="hazard_record",
                                   external_key="hz-7-open",
                                   data={"hazard_id": "HZ-0007", "status": "open",
                                         "severity": "critical", "title": "治污设施旁路"})
        service.record_domain_data(request_id="req-as-002", actor_id="admin-001",
                                   site_id="site-002", category="assistance_record",
                                   external_key="assist-1",
                                   data={"assisted_at": "2026-09-10"})
        # site-003：治污设施异常，低风险，容量受限时的远程候选
        service.record_domain_data(request_id="req-fa-003", actor_id="admin-001",
                                   site_id="site-003", category="facility_alert",
                                   external_key="al-3",
                                   data={"alert_id": "AL-0003", "status": "active",
                                         "severity": "minor", "facility": "废气处理塔"})

        refresh = triage.refresh_facts(request_id="req-facts", actor_id="disp-001",
                                       district_id="D-001")
        plan = triage.generate_plan(
            request_id="req-plan", actor_id="disp-001", plan_date="2026-09-25",
            district_id="D-001",
            slots=[{"slot_id": "morning", "start": "08:00", "end": "12:00",
                    "onsite_capacity": 1, "remote_capacity": 2}])
        plan_view = triage.get_plan("2026-09-25", "D-001")
        actions = {item["site_id"]: item["action"] for item in plan_view["items"]}

        # site-003 突发紧急事件：穿透免访窗口并排在最前，挤走一个现场名额
        emergency = triage.declare_emergency(
            request_id="req-emergency", actor_id="disp-001", site_id="site-003",
            trigger_type="public_hotline", trigger_reference="12345-2026-0925",
            detail={"source": "12345 热线工单", "summary": "群众举报夜间偷排"})
        plan_v2 = triage.generate_plan(
            request_id="req-plan-v2", actor_id="disp-001", plan_date="2026-09-25",
            district_id="D-001",
            slots=[{"slot_id": "morning", "start": "08:00", "end": "12:00",
                    "onsite_capacity": 1, "remote_capacity": 2}])
        v2 = triage.get_plan("2026-09-25", "D-001")
        first_item = v2["items"][0]
        dispatch = triage.list_dispatches(plan_date="2026-09-25", district_id="D-001")
        emergency_dispatch = next(item for item in dispatch["items"]
                                  if item["site_id"] == "site-003")
        claim = triage.claim_dispatch(request_id="req-claim", actor_id="disp-001",
                                     dispatch_id=emergency_dispatch["dispatch_id"],
                                     expected_plan_version=2, reason="紧急举报即查")
        explanation = triage.explain_site(plan_date="2026-09-25", site_id="site-003",
                                          district_id="D-001")
        # 旧方案仍可解释：规则与事实版本固化在方案中
        old_explanation = triage.explain_site(plan_date="2026-09-25", site_id="site-002",
                                              district_id="D-001", plan_version=1)

        valid, event_count = service.verify_audit()
        result = {
            "status": "ok",
            "audit_events": event_count,
            "audit_valid": valid,
            "facts_upserted": refresh["upserted"],
            "plan_version": plan["plan_version"],
            "plan_v2_version": plan_v2["plan_version"],
            "plan_v1_actions": actions,
            "emergency_rank_first": first_item["site_id"] == "site-003",
            "emergency_action": first_item["action"],
            "emergency_override": first_item["window_override"],
            "emergency_trigger": emergency["trigger_reference"],
            "dispatch_status": claim["status"],
            "dispatch_owner": claim["claimed_by"],
            "explain_action": explanation["action"],
            "explain_has_trigger": explanation["emergency"] is not None,
            "old_plan_version_kept": old_explanation["plan_version"] == 1,
            "first_replayed": False,
            "second_replayed": triage.generate_plan(
                request_id="req-plan-v2", actor_id="disp-001", plan_date="2026-09-25",
                district_id="D-001",
                slots=[{"slot_id": "morning", "start": "08:00", "end": "12:00",
                        "onsite_capacity": 1, "remote_capacity": 2}])["replayed"],
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    required = ("audit_valid", "emergency_rank_first", "emergency_override",
                "explain_has_trigger", "old_plan_version_kept", "second_replayed")
    ok = result["status"] == "ok" and result["audit_valid"] and all(result[key] for key in required)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

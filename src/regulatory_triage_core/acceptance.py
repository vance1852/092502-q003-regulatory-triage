"""运行基础服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .orchestration import TriageService
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    """执行一条完整登记、编排、派单链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        service = DomainService(database, clock)
        triage = TriageService(database, clock)
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范企业")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="调度员", role="operator", organization_id="org-001")
        service.register_actor(request_id="req-inspector", actor_id="admin-001", new_actor_id="inspector-001",
                               display_name="执法员", role="operator", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号生产场所", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="district_profile", external_key="record-001",
                                           data={"district_id": "D1", "name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="district_profile", external_key="record-001",
                                            data={"district_id": "D1", "name": "基础资料", "enabled": True})

        # 风险事实：高风险、逾期自查、未闭环隐患，以及一起可突破免访窗口的紧急事件。
        service.record_domain_data(request_id="req-risk", actor_id="operator-001", site_id="site-001",
                                   category="risk_profile", external_key="risk-001",
                                   data={"risk_level": "high"})
        service.record_domain_data(request_id="req-overdue", actor_id="operator-001", site_id="site-001",
                                   category="overdue_self_check", external_key="overdue-001",
                                   data={"due_date": "2026-09-01"})
        service.record_domain_data(request_id="req-hazard", actor_id="operator-001", site_id="site-001",
                                   category="open_hazard", external_key="hazard-001",
                                   data={"severity": "major"})
        service.record_domain_data(request_id="req-assist", actor_id="operator-001", site_id="site-001",
                                   category="assistance_record", external_key="assist-001",
                                   data={"occurred_at": "2026-09-23T00:00:00Z"})
        service.record_domain_data(request_id="req-urgent", actor_id="operator-001", site_id="site-001",
                                   category="urgent_event", external_key="urgent-001",
                                   data={"trigger_basis": "12369 夜间举报偷排"})

        plan_receipt = triage.generate_plan(
            request_id="req-plan", actor_id="operator-001", plan_date="2026-09-25",
            onsite_capacity=2, district_capacity={"D1": 2})
        plan = triage.get_plan(actor_id="operator-001", plan_id=plan_receipt.resource_id)
        explanation = triage.explain_site(actor_id="operator-001",
                                          plan_id=plan_receipt.resource_id, site_id="site-001")
        triage.lock_slot(request_id="req-lock", actor_id="operator-001",
                         plan_id=plan_receipt.resource_id, site_id="site-001",
                         officer_id="inspector-001", expected_revision=0, reason="紧急举报当晚核查")

        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed,
                  "plan_rules_version": plan["rules_version"],
                  "plan_decision": explanation["decision"],
                  "window_broken": explanation["window_broken"],
                  "urgent_trigger_basis": explanation["urgent_trigger_basis"]}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

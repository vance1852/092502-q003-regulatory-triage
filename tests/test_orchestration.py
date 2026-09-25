import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from regulatory_triage_core.clock import FixedClock
from regulatory_triage_core.errors import ConflictError, NotFoundError, PermissionDenied
from regulatory_triage_core.orchestration import TriageService
from regulatory_triage_core.service import DomainService
from regulatory_triage_core.storage import Database


class OrchestrationTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = FixedClock(datetime(2026, 9, 25, tzinfo=timezone.utc))
        self.service = DomainService(self.database, self.clock)
        self.triage = TriageService(self.database, self.clock)
        self._bootstrap()

    def tearDown(self):
        self.database.close()

    def _bootstrap(self):
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="监管局")
        self.service.register_actor(request_id="adm", actor_id="bootstrap", new_actor_id="admin",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="op1", actor_id="admin", new_actor_id="disp1",
                                    display_name="调度员甲", role="operator", organization_id="o1")
        self.service.register_actor(request_id="op2", actor_id="admin", new_actor_id="disp2",
                                    display_name="调度员乙", role="operator", organization_id="o1")
        self.service.register_actor(request_id="insp", actor_id="admin", new_actor_id="insp1",
                                    display_name="执法员", role="operator", organization_id="o1")
        self.service.register_actor(request_id="aud", actor_id="admin", new_actor_id="aud1",
                                    display_name="审计员", role="auditor", organization_id="o1")

    def _site(self, sid, district):
        self.service.register_site(request_id="site-" + sid, actor_id="disp1", site_id=sid,
                                   organization_id="o1", name=sid, timezone_name="Asia/Shanghai")
        self.service.record_domain_data(
            request_id="dp-" + sid, actor_id="disp1", site_id=sid,
            category="district_profile", external_key="dpk-" + sid, data={"district_id": district})

    def _record(self, sid, category, key, data, rid, actor="disp1"):
        self.service.record_domain_data(request_id=rid, actor_id=actor, site_id=sid,
                                        category=category, external_key=key, data=data)

    def _scenario(self):
        # s1：高风险 + 逾期 + 隐患（同键重复不同步两次事实）
        self._site("s1", "D1")
        self._record("s1", "risk_profile", "rp1", {"risk_level": "high"}, "r-rp1")
        self._record("s1", "overdue_self_check", "osc1", {"due_date": "2026-09-01"}, "r-o1")
        self._record("s1", "open_hazard", "hz1", {"severity": "major"}, "r-h1")
        self._record("s1", "overdue_self_check", "osc1", {"due_date": "2026-09-01"}, "r-o1dup")
        # s2：免访窗口内、无紧急 -> 仅线上帮扶
        self._site("s2", "D1")
        self._record("s2", "facility_anomaly", "fa1", {"device": "fan"}, "r-fa1")
        self._record("s2", "assistance_record", "as1", {"occurred_at": "2026-09-20T00:00:00Z"}, "r-as1")
        # s3：免访窗口内但有紧急事件 -> 突破窗口
        self._site("s3", "D2")
        self._record("s3", "facility_anomaly", "fa2", {"device": "pump"}, "r-fa2")
        self._record("s3", "assistance_record", "as2", {"occurred_at": "2026-09-22T00:00:00Z"}, "r-as2")
        self._record("s3", "urgent_event", "ue1", {"trigger_basis": "12369 夜间举报"}, "r-ue1")

    def _generate(self, request_id="plan", date="2026-09-25", onsite=2,
                  districts=None, remote=None):
        districts = districts or {"D1": 1, "D2": 1}
        receipt = self.triage.generate_plan(
            request_id=request_id, actor_id="disp1", plan_date=date,
            onsite_capacity=onsite, district_capacity=districts, remote_capacity=remote)
        return receipt, self.triage.get_plan(actor_id="disp1", plan_id=receipt.resource_id)

    def test_plan_materializes_facts_and_queues(self):
        self._scenario()
        _, plan = self._generate()
        self.assertEqual(1, plan["rules_version"])
        self.assertIn("s3", plan["queues"]["onsite"])  # 紧急优先
        self.assertIn("s1", plan["queues"]["onsite"])
        self.assertEqual(["s2"], plan["queues"]["assist_online"])
        s1 = next(site for site in plan["sites"] if site["site_id"] == "s1")
        # 逾期同键只计一次：30+25 再乘高风险 2 = 110，而不是 140。
        self.assertEqual(110.0, s1["score"])
        s3 = next(site for site in plan["sites"] if site["site_id"] == "s3")
        self.assertTrue(s3["window_broken"])

    def test_auditor_can_read_but_not_generate(self):
        self._scenario()
        with self.assertRaises(PermissionDenied):
            self.triage.generate_plan(request_id="p", actor_id="aud1", plan_date="2026-09-25",
                                      onsite_capacity=2, district_capacity={"D1": 1, "D2": 1})

    def test_concurrent_lock_only_one_succeeds(self):
        self._scenario()
        receipt, plan = self._generate()
        pid = receipt.resource_id
        outcomes = []

        def lock(actor, officer, request_id):
            try:
                self.triage.lock_slot(request_id=request_id, actor_id=actor, plan_id=pid,
                                      site_id="s3", officer_id=officer, expected_revision=0,
                                      reason="并发领取测试")
                outcomes.append("ok")
            except ConflictError:
                outcomes.append("conflict")

        t1 = threading.Thread(target=lock, args=("disp1", "insp1", "lk-a"))
        t2 = threading.Thread(target=lock, args=("disp2", "disp2", "lk-b"))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(1, outcomes.count("ok"))
        self.assertEqual(1, outcomes.count("conflict"))
        refreshed = self.triage.get_plan(actor_id="disp1", plan_id=pid)
        locked = [s for s in refreshed["sites"] if s["site_id"] == "s3"][0]
        self.assertEqual("locked", locked["claim_status"])
        self.assertEqual(1, refreshed["revision"])

    def test_stale_revision_is_rejected(self):
        self._scenario()
        receipt, _ = self._generate()
        pid = receipt.resource_id
        self.triage.lock_slot(request_id="lk-a", actor_id="disp1", plan_id=pid, site_id="s3",
                              officer_id="insp1", expected_revision=0, reason="首次锁定")
        with self.assertRaises(ConflictError):
            self.triage.lock_slot(request_id="lk-b", actor_id="disp2", plan_id=pid, site_id="s1",
                                  officer_id="disp2", expected_revision=0, reason="旧版本")

    def test_release_and_reassign_require_reason_and_bump_revision(self):
        self._scenario()
        receipt, _ = self._generate()
        pid = receipt.resource_id
        self.triage.lock_slot(request_id="lk", actor_id="disp1", plan_id=pid, site_id="s3",
                              officer_id="insp1", expected_revision=0, reason="锁定")
        with self.assertRaises(ConflictError):
            self.triage.release_slot(request_id="rl-bad", actor_id="disp1", plan_id=pid,
                                     site_id="s3", expected_revision=2, reason="版本不符")
        self.triage.release_slot(request_id="rl", actor_id="disp1", plan_id=pid, site_id="s3",
                                 expected_revision=1, reason="执法员请假")
        plan = self.triage.get_plan(actor_id="disp1", plan_id=pid)
        self.assertEqual(2, plan["revision"])
        site = [s for s in plan["sites"] if s["site_id"] == "s3"][0]
        self.assertEqual("open", site["claim_status"])
        self.triage.lock_slot(request_id="lk2", actor_id="disp1", plan_id=pid, site_id="s3",
                              officer_id="insp1", expected_revision=2, reason="重新锁定")
        self.triage.reassign_slot(request_id="rs", actor_id="disp1", plan_id=pid, site_id="s3",
                                  to_officer_id="disp2", expected_revision=3, reason="属地回避")
        plan = self.triage.get_plan(actor_id="disp1", plan_id=pid)
        site = [s for s in plan["sites"] if s["site_id"] == "s3"][0]
        self.assertEqual("disp2", site["claimed_by"])
        self.assertEqual(4, plan["revision"])

    def test_rules_change_only_affects_new_plans(self):
        self._scenario()
        receipt, old_plan = self._generate(request_id="plan-old")
        self.triage.adjust_rules(
            request_id="rule2", actor_id="disp1",
            factor_weights={"overdue_self_check": 99, "open_hazard": 25, "facility_anomaly": 20})
        receipt2, new_plan = self._generate(request_id="plan-new", date="2026-09-26", onsite=5,
                                           districts={"D1": 5, "D2": 5})
        self.assertGreater(new_plan["rules_version"], old_plan["rules_version"])
        # 旧方案仍固化旧版本与旧分数。
        unchanged = self.triage.get_plan(actor_id="disp1", plan_id=receipt.resource_id)
        self.assertEqual(old_plan["rules_version"], unchanged["rules_version"])
        s1_old = [s for s in unchanged["sites"] if s["site_id"] == "s1"][0]
        s1_new = [s for s in new_plan["sites"] if s["site_id"] == "s1"][0]
        self.assertEqual(110.0, s1_old["score"])
        self.assertEqual((99 + 25) * 2, s1_new["score"])

    def test_regenerating_same_date_supersedes_but_refuses_when_locked(self):
        self._scenario()
        first, _ = self._generate(request_id="plan-1")
        second, plan2 = self._generate(request_id="plan-2")
        self.assertNotEqual(first.resource_id, second.resource_id)
        self.assertEqual("superseded",
                         self.triage.get_plan(actor_id="disp1", plan_id=first.resource_id)["status"])
        self.assertEqual("active", plan2["status"])
        # 在新方案锁定一个名额后，再次生成应被拒绝。
        self.triage.lock_slot(request_id="lk", actor_id="disp1", plan_id=second.resource_id,
                              site_id="s3", officer_id="insp1", expected_revision=0, reason="锁定")
        with self.assertRaises(ConflictError):
            self._generate(request_id="plan-3")

    def test_cannot_lock_superseded_plan(self):
        self._scenario()
        first, _ = self._generate(request_id="plan-1")
        self._generate(request_id="plan-2")
        with self.assertRaises(ConflictError):
            self.triage.lock_slot(request_id="lk", actor_id="disp1", plan_id=first.resource_id,
                                  site_id="s3", officer_id="insp1", expected_revision=0,
                                  reason="旧方案派单")

    def test_explain_survives_restart(self):
        self._scenario()
        receipt, _ = self._generate()
        pid = receipt.resource_id
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "restart.sqlite3")
            self.database.connection.backup(__import__("sqlite3").connect(path))
            database = Database(path)
            try:
                triage = TriageService(database, self.clock)
                explanation = triage.explain_site(actor_id="disp1", plan_id=pid, site_id="s2")
                self.assertEqual("assist_online", explanation["decision"])
                self.assertTrue(any("免访窗口" in reason for reason in explanation["reasons"]))
                urgent = triage.explain_site(actor_id="disp1", plan_id=pid, site_id="s3")
                self.assertEqual(["12369 夜间举报"], urgent["urgent_trigger_basis"])
                self.assertTrue(urgent["window_broken"])
            finally:
                database.close()

    def test_idempotent_lock_replay(self):
        self._scenario()
        receipt, _ = self._generate()
        pid = receipt.resource_id
        first = self.triage.lock_slot(request_id="same", actor_id="disp1", plan_id=pid, site_id="s3",
                                      officer_id="insp1", expected_revision=0, reason="锁定")
        second = self.triage.lock_slot(request_id="same", actor_id="disp1", plan_id=pid, site_id="s3",
                                       officer_id="insp1", expected_revision=0, reason="锁定")
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(first.resource_id, second.resource_id)

    def test_audit_chain_records_orchestration(self):
        self._scenario()
        self._generate(request_id="plan")
        valid, count = self.service.verify_audit()
        self.assertTrue(valid)
        actions = {event["action"] for event in self.service.audit_events()}
        self.assertIn("plan.generated", actions)


if __name__ == "__main__":
    unittest.main()

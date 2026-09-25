import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from regulatory_triage_core.clock import FixedClock
from regulatory_triage_core.errors import (ConflictError, DispatchStateConflict,
                                           NotFoundError, PermissionDenied,
                                           PlanVersionConflict, ValidationError)
from regulatory_triage_core.service import DomainService
from regulatory_triage_core.storage import Database
from regulatory_triage_core.triage import TriageService

SLOTS = [{"slot_id": "morning", "start": "08:00", "end": "12:00",
          "onsite_capacity": 2, "remote_capacity": 2}]


class TriageTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = FixedClock(datetime(2026, 9, 25, tzinfo=timezone.utc))
        self.service = DomainService(self.database, self.clock)
        self.triage = TriageService(self.database, self.clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="org", name="监管局")
        self.service.register_actor(request_id="actor-admin", actor_id="bootstrap",
                                    new_actor_id="adm", display_name="管理员",
                                    role="admin", organization_id="org")
        self.service.register_actor(request_id="actor-d1", actor_id="adm",
                                    new_actor_id="d1", display_name="调度员甲",
                                    role="dispatcher", organization_id="org")
        self.service.register_actor(request_id="actor-d2", actor_id="adm",
                                    new_actor_id="d2", display_name="调度员乙",
                                    role="dispatcher", organization_id="org")
        for sid in ("s1", "s2", "s3"):
            self.service.register_site(request_id="site-" + sid, actor_id="adm",
                                       site_id=sid, organization_id="org", name=sid,
                                       timezone_name="Asia/Shanghai")
            self.service.record_domain_data(
                request_id="dp-" + sid, actor_id="adm", site_id=sid,
                category="district_profile", external_key="dp",
                data={"district_id": "D1"})

    def tearDown(self):
        self.database.close()

    def _seed_risks(self):
        self.service.record_domain_data(
            request_id="risk1", actor_id="adm", site_id="s1",
            category="risk_profile", external_key="rp", data={"level": "high"})
        self.service.record_domain_data(
            request_id="sc1", actor_id="adm", site_id="s1",
            category="self_check_report", external_key="sc-1",
            data={"period": "2026Q2", "submitted": False, "due_date": "2026-08-01"})
        self.service.record_domain_data(
            request_id="hz2", actor_id="adm", site_id="s2",
            category="hazard_record", external_key="hz-1-open",
            data={"hazard_id": "HZ-1", "status": "open",
                  "severity": "critical", "title": "偷排"})
        self.service.record_domain_data(
            request_id="as2", actor_id="adm", site_id="s2",
            category="assistance_record", external_key="ar-1",
            data={"assisted_at": "2026-09-10"})

    def _plan(self, request_id="plan1"):
        return self.triage.generate_plan(request_id=request_id, actor_id="d1",
                                         plan_date="2026-09-25", district_id="D1",
                                         slots=SLOTS)

    def test_generates_deterministic_queues_and_explanation(self):
        self._seed_risks()
        result = self._plan()
        self.assertEqual(1, result["plan_version"])
        plan = self.triage.get_plan("2026-09-25", "D1")
        actions = {item["site_id"]: item["action"] for item in plan["items"]}
        # s1 逾期自查高风险 -> 现场；s2 critical 隐患穿透帮扶窗口 -> 现场；s3 无事不扰 -> 延后
        self.assertEqual("onsite", actions["s1"])
        self.assertEqual("onsite", actions["s2"])
        self.assertEqual("deferred", actions["s3"])
        s2 = self.triage.explain_site(plan_date="2026-09-25", site_id="s2",
                                      district_id="D1")
        self.assertTrue(s2["window"]["override"])
        self.assertEqual("high_risk_break", s2["window"]["override_reason"])
        self.assertGreater(s2["score_detail"]["unmitigated_score"], s2["score"])
        joined = " ".join(s2["reasons"])
        self.assertIn("免访窗口", joined)
        self.assertIn("突破", joined)
        s3 = self.triage.explain_site(plan_date="2026-09-25", site_id="s3",
                                      district_id="D1")
        self.assertEqual("deferred", s3["action"])
        self.assertEqual("no_active_risk_fact", s3["deferred_reason"])

    def test_unchanged_inputs_reuse_plan_version(self):
        self._seed_risks()
        first = self._plan()
        second = self.triage.generate_plan(request_id="plan-again", actor_id="d1",
                                           plan_date="2026-09-25", district_id="D1",
                                           slots=SLOTS)
        self.assertEqual(first["plan_id"], second["plan_id"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["plan_version"], second["plan_version"])

    def test_fact_closure_creates_new_plan_version_and_fact_version(self):
        self._seed_risks()
        self._plan()
        self.triage.refresh_facts(request_id="refresh1", actor_id="d1", district_id="D1")
        self.service.record_domain_data(
            request_id="hz2closed", actor_id="adm", site_id="s2",
            category="hazard_record", external_key="hz-1-closed",
            data={"hazard_id": "HZ-1", "status": "closed", "severity": "critical"})
        refresh = self.triage.refresh_facts(request_id="refresh2", actor_id="d1",
                                            district_id="D1")
        self.assertEqual(1, refresh["closed"])
        row = self.database.connection.execute(
            "SELECT fact_version, active FROM fact_snapshots WHERE fact_key=?",
            ("open_hazard:HZ-1",)).fetchone()
        self.assertEqual(2, row["fact_version"])
        self.assertEqual(0, row["active"])
        second = self.triage.generate_plan(request_id="plan2", actor_id="d1",
                                           plan_date="2026-09-25", district_id="D1",
                                           slots=SLOTS)
        self.assertEqual(2, second["plan_version"])
        # 旧方案仍可按原版本解释，未被改写
        old = self.triage.get_plan("2026-09-25", "D1", plan_version=1)
        old_s2 = next(item for item in old["items"] if item["site_id"] == "s2")
        self.assertEqual("onsite", old_s2["action"])

    def test_rule_change_only_affects_new_plan(self):
        self._seed_risks()
        self._plan()
        self.triage.register_rules(
            request_id="rules1", actor_id="d1", rule_id="r1",
            rules={"thresholds": {"onsite": 999.0, "remote": 999.0}})
        new_plan = self.triage.generate_plan(request_id="plan-new", actor_id="d1",
                                             plan_date="2026-09-25", district_id="D1",
                                             slots=SLOTS)
        self.assertEqual(2, new_plan["plan_version"])
        new = self.triage.get_plan("2026-09-25", "D1")
        self.assertEqual("r1", new["rule_id"])
        self.assertTrue(all(item["action"] in ("assistance", "deferred")
                            for item in new["items"]))
        old = self.triage.get_plan("2026-09-25", "D1", plan_version=1)
        self.assertEqual("default", old["rule_id"])
        self.assertEqual("onsite",
                         next(i for i in old["items"] if i["site_id"] == "s1")["action"])

    def test_same_rule_content_keeps_version(self):
        first = self.triage.register_rules(request_id="rules-a", actor_id="d1",
                                           rule_id="r1", rules={"no_visit_days": 14})
        second = self.triage.register_rules(request_id="rules-b", actor_id="d1",
                                            rule_id="r1", rules={"no_visit_days": 14})
        self.assertEqual(first["version"], second["version"])
        self.assertFalse(second["created"])

    def test_concurrent_claims_do_not_duplicate_dispatch(self):
        self._seed_risks()
        self._plan()
        listing = self.triage.list_dispatches(plan_date="2026-09-25",
                                              district_id="D1", status="open")
        dispatch_id = listing["items"][0]["dispatch_id"]
        barrier = threading.Barrier(2)
        outcomes = []

        def claim(actor, request_id):
            barrier.wait()
            try:
                result = self.triage.claim_dispatch(
                    request_id=request_id, actor_id=actor, dispatch_id=dispatch_id,
                    expected_plan_version=1, reason="并发领取测试")
                outcomes.append(("ok", result["claimed_by"]))
            except DispatchStateConflict:
                outcomes.append(("conflict", actor))

        t1 = threading.Thread(target=claim, args=("d1", "claim-1"))
        t2 = threading.Thread(target=claim, args=("d2", "claim-2"))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(2, len(outcomes))
        self.assertEqual(1, sum(1 for kind, _ in outcomes if kind == "ok"))
        self.assertEqual(1, sum(1 for kind, _ in outcomes if kind == "conflict"))
        row = self.database.connection.execute(
            "SELECT status, claimed_by FROM dispatches WHERE dispatch_id=?",
            (dispatch_id,)).fetchone()
        self.assertEqual("claimed", row["status"])
        self.assertIn(row["claimed_by"], ("d1", "d2"))

    def test_claim_replay_returns_same_owner(self):
        self._seed_risks()
        self._plan()
        dispatch_id = self.triage.list_dispatches(
            plan_date="2026-09-25", district_id="D1")["items"][0]["dispatch_id"]
        first = self.triage.claim_dispatch(
            request_id="claim-x", actor_id="d1", dispatch_id=dispatch_id,
            expected_plan_version=1, reason="首次领取")
        replay = self.triage.claim_dispatch(
            request_id="claim-x", actor_id="d1", dispatch_id=dispatch_id,
            expected_plan_version=1, reason="首次领取")
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["dispatch_id"], replay["dispatch_id"])
        self.assertEqual("d1", replay["claimed_by"])

    def test_stale_plan_version_is_rejected_on_release_and_reassign(self):
        self._seed_risks()
        self._plan()
        dispatch_id = self.triage.list_dispatches(
            plan_date="2026-09-25", district_id="D1")["items"][0]["dispatch_id"]
        self.triage.claim_dispatch(request_id="claim", actor_id="d1",
                                  dispatch_id=dispatch_id,
                                  expected_plan_version=1, reason="锁定")
        # 新事实导致方案升版
        self.service.record_domain_data(
            request_id="fa3", actor_id="adm", site_id="s3",
            category="facility_alert", external_key="al-1",
            data={"alert_id": "AL-1", "status": "active", "severity": "critical"})
        self.triage.generate_plan(request_id="plan2", actor_id="d1",
                                  plan_date="2026-09-25", district_id="D1", slots=SLOTS)
        with self.assertRaises(PlanVersionConflict):
            self.triage.release_dispatch(request_id="release", actor_id="d1",
                                         dispatch_id=dispatch_id,
                                         expected_plan_version=1, reason="陈旧释放")
        with self.assertRaises(PlanVersionConflict):
            self.triage.reassign_dispatch(request_id="reassign", actor_id="d1",
                                          dispatch_id=dispatch_id,
                                          expected_plan_version=1, to_actor_id="d2",
                                          reason="陈旧改派")
        history = self.triage.dispatch_history(dispatch_id)
        self.assertEqual("claimed", history["status"])

    def test_release_requires_reason_and_reopens_slot(self):
        self._seed_risks()
        self._plan()
        dispatch_id = self.triage.list_dispatches(
            plan_date="2026-09-25", district_id="D1")["items"][0]["dispatch_id"]
        self.triage.claim_dispatch(request_id="claim", actor_id="d1",
                                  dispatch_id=dispatch_id,
                                  expected_plan_version=1, reason="锁定")
        with self.assertRaises(ValidationError):
            self.triage.release_dispatch(request_id="bad-release", actor_id="d1",
                                         dispatch_id=dispatch_id,
                                         expected_plan_version=1, reason="   ")
        self.triage.release_dispatch(request_id="release", actor_id="d1",
                                     dispatch_id=dispatch_id,
                                     expected_plan_version=1, reason="企业申请改期")
        listing = self.triage.list_dispatches(plan_date="2026-09-25",
                                              district_id="D1", status="open")
        self.assertIn(dispatch_id, [item["dispatch_id"] for item in listing["items"]])
        history = self.triage.dispatch_history(dispatch_id)
        reasons = [event.get("reason") for event in history["events"]]
        self.assertIn("企业申请改期", reasons)

    def test_reassign_moves_ownership(self):
        self._seed_risks()
        self._plan()
        dispatch_id = self.triage.list_dispatches(
            plan_date="2026-09-25", district_id="D1")["items"][0]["dispatch_id"]
        result = self.triage.reassign_dispatch(
            request_id="reassign", actor_id="d1", dispatch_id=dispatch_id,
            expected_plan_version=1, to_actor_id="d2", reason="片区调整")
        self.assertEqual("d2", result["claimed_by"])
        self.assertEqual("claimed", result["status"])

    def test_emergency_breaks_window_and_keeps_trigger_evidence(self):
        # s3 处于帮扶窗口且无风险，原本不会被现场检查
        self.service.record_domain_data(
            request_id="as3", actor_id="adm", site_id="s3",
            category="assistance_record", external_key="ar-3",
            data={"assisted_at": "2026-09-20"})
        self._seed_risks()
        emergency = self.triage.declare_emergency(
            request_id="em1", actor_id="d1", site_id="s3",
            trigger_type="public_hotline", trigger_reference="12345-2026-0001",
            detail={"source": "12345 热线", "summary": "举报夜间偷排",
                    "received_at": "2026-09-25T06:00:00Z"})
        self._plan()
        explanation = self.triage.explain_site(plan_date="2026-09-25", site_id="s3",
                                               district_id="D1")
        self.assertEqual("onsite", explanation["action"])
        self.assertTrue(explanation["window"]["override"])
        self.assertEqual("emergency_trigger",
                         explanation["window"]["override_reason"])
        self.assertEqual("12345-2026-0001",
                         explanation["emergency"]["trigger_reference"])
        row = self.database.connection.execute(
            "SELECT trigger_hash FROM emergencies WHERE emergency_id=?",
            (emergency["emergency_id"],)).fetchone()
        self.assertTrue(row["trigger_hash"])
        with self.assertRaises(ValidationError):
            self.triage.declare_emergency(
                request_id="em2", actor_id="d1", site_id="s3",
                trigger_type="public_hotline", trigger_reference="12345-x", detail={})

    def test_new_plan_marks_unclaimed_old_dispatches_superseded(self):
        self._seed_risks()
        self._plan()
        first_listing = self.triage.list_dispatches(plan_date="2026-09-25",
                                                    district_id="D1")
        # 锁定一个名额，另一个保持开放
        claimed_id = first_listing["items"][0]["dispatch_id"]
        open_id = first_listing["items"][1]["dispatch_id"]
        self.triage.claim_dispatch(request_id="claim-one", actor_id="d1",
                                  dispatch_id=claimed_id, expected_plan_version=1,
                                  reason="先锁定一个")
        self.service.record_domain_data(
            request_id="fa3", actor_id="adm", site_id="s3",
            category="facility_alert", external_key="al-1",
            data={"alert_id": "AL-1", "status": "active", "severity": "critical"})
        self.triage.generate_plan(request_id="plan2", actor_id="d1",
                                  plan_date="2026-09-25", district_id="D1", slots=SLOTS)
        statuses = {row["dispatch_id"]: row["status"]
                    for row in self.database.connection.execute(
                        "SELECT dispatch_id, status FROM dispatches").fetchall()}
        self.assertEqual("superseded", statuses[open_id])
        self.assertEqual("claimed", statuses[claimed_id])
        history = self.triage.dispatch_history(open_id)
        self.assertEqual("superseded", history["events"][-1]["action"])

    def test_operator_cannot_generate_plan(self):
        self.service.register_actor(request_id="actor-op", actor_id="adm",
                                    new_actor_id="op1", display_name="执法员",
                                    role="operator", organization_id="org")
        with self.assertRaises(PermissionDenied):
            self.triage.generate_plan(request_id="plan-x", actor_id="op1",
                                      plan_date="2026-09-25", district_id="D1")

    def test_explanation_survives_restart(self):
        self._seed_risks()
        self._plan()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "restart.sqlite3"
            disk = Database(path)
            self.database.connection.backup(disk.connection)
            disk.close()
            restarted_db = Database(path)
            restarted = TriageService(restarted_db, self.clock)
            explanation = restarted.explain_site(plan_date="2026-09-25", site_id="s1",
                                                 district_id="D1")
            self.assertEqual("onsite", explanation["action"])
            self.assertEqual(1, explanation["plan_version"])
            self.assertTrue(explanation["facts"])
            valid, _ = DomainService(restarted_db).verify_audit()
            self.assertTrue(valid)
            restarted_db.close()


if __name__ == "__main__":
    unittest.main()

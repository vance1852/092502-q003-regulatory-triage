import unittest
from datetime import date

from regulatory_triage_core.models import DomainRecord
from regulatory_triage_core.rules import (FACT_FACILITY_ALERT, FACT_OPEN_HAZARD,
                                          FACT_OVERDUE_SELF_CHECK, build_plan_items,
                                          derive_site_facts, normalize_rules,
                                          normalize_slots, score_site)


def record(record_id, category, external_key, payload, created_at="2026-09-01T00:00:00Z"):
    return DomainRecord(record_id, "s1", category, external_key, payload,
                        "h" + record_id, "adm", created_at)


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = normalize_rules(None)
        self.day = date(2026, 9, 25)

    def site(self, records, site_id="s1"):
        facts, context = derive_site_facts(site_id, records, self.rules, self.day)
        scored = score_site(facts, context, self.rules)
        return {"site_id": site_id, "facts": facts, "context": context,
                "scored": scored, "risk_level": context["risk_level"]}

    def test_overdue_self_check_and_open_hazard_each_counted_once(self):
        records = [
            record("r1", "self_check_report", "sc-1",
                   {"period": "2026Q2", "submitted": False, "due_date": "2026-08-01"}),
            record("r2", "hazard_record", "hz-1",
                   {"hazard_id": "HZ-1", "status": "open", "severity": "major"}),
        ]
        site = self.site(records)
        keys = [f.fact_key for f in site["facts"] if f.counts]
        self.assertEqual(["open_hazard:HZ-1", "overdue_self_check:2026Q2"], sorted(keys))
        # 30+18(major) + 40+18(major) = 106，medium 倍率 1.0
        self.assertEqual(106.0, site["scored"]["score"])
        self.assertEqual(2, len(site["scored"]["contributions"]))

    def test_closed_hazard_uses_stable_business_key_and_does_not_count(self):
        records = [
            record("r1", "hazard_record", "hz-open",
                   {"hazard_id": "HZ-1", "status": "open", "severity": "critical"},
                   created_at="2026-09-01T00:00:00Z"),
            record("r2", "hazard_record", "hz-closed",
                   {"hazard_id": "HZ-1", "status": "closed", "severity": "critical"},
                   created_at="2026-09-10T00:00:00Z"),
        ]
        facts, _ = derive_site_facts("s1", records, self.rules, self.day)
        self.assertEqual([], [f for f in facts if f.fact_type == FACT_OPEN_HAZARD])

    def test_assistance_window_mitigates_but_high_risk_break_keeps_onsite_eligibility(self):
        # critical 隐患 40+30=70，高风险倍率 1.5 => 105；帮扶缓解 0.8 => 84
        records = [
            record("r1", "hazard_record", "hz-1",
                   {"hazard_id": "HZ-1", "status": "open", "severity": "critical"}),
            record("r2", "risk_profile", "rp", {"level": "high"}),
            record("r3", "assistance_record", "ar-1", {"assisted_at": "2026-09-10"}),
        ]
        site = self.site(records)
        self.assertTrue(site["context"]["assistance_in_window"])
        self.assertTrue(site["scored"]["assistance_mitigation_applied"])
        self.assertEqual(105.0, site["scored"]["unmitigated_score"])
        self.assertEqual(84.0, site["scored"]["score"])
        self.assertTrue(site["scored"]["high_risk_break"])

        slots = normalize_slots([{"slot_id": "am", "start": "08:00", "end": "12:00",
                                  "onsite_capacity": 1, "remote_capacity": 1}], self.rules)
        items = build_plan_items(sites=[site], slots=slots, rules=self.rules)
        self.assertEqual("onsite", items[0]["action"])
        self.assertTrue(items[0]["window_override"])

    def test_window_without_high_risk_becomes_remote_or_assistance(self):
        # minor 设施告警 25+8=33，low 倍率 0.7 => 23.1，低于现场阈值，窗口内
        records = [
            record("r1", "facility_alert", "a1",
                   {"alert_id": "AL-1", "status": "active", "severity": "minor"}),
            record("r2", "risk_profile", "rp", {"level": "low"}),
            record("r3", "assistance_record", "ar-1", {"assisted_at": "2026-09-20"}),
        ]
        site = self.site(records)
        slots = normalize_slots([{"slot_id": "am", "start": "08:00", "end": "12:00",
                                  "onsite_capacity": 1, "remote_capacity": 1}], self.rules)
        items = build_plan_items(sites=[site], slots=slots, rules=self.rules)
        self.assertEqual("assistance", items[0]["action"])
        self.assertFalse(items[0]["window_override"])

    def test_capacity_is_deterministic_and_ranked(self):
        high = self.site([
            record("r1", "hazard_record", "hz",
                   {"hazard_id": "HZ", "status": "open", "severity": "critical"}),
            record("r2", "risk_profile", "rp", {"level": "high"}),
        ], site_id="high")
        low = self.site([
            record("r3", "facility_alert", "a",
                   {"alert_id": "AL", "status": "active", "severity": "minor"}),
        ], site_id="low")
        slots = normalize_slots([{"slot_id": "am", "start": "08:00", "end": "12:00",
                                  "onsite_capacity": 1, "remote_capacity": 1}], self.rules)
        items = build_plan_items(sites=[low, high], slots=slots, rules=self.rules)
        actions = {item["site_id"]: item["action"] for item in items}
        self.assertEqual("onsite", actions["high"])
        self.assertEqual("remote", actions["low"])
        # 再次构建，同样输入得到同样结果
        slots2 = normalize_slots([{"slot_id": "am", "start": "08:00", "end": "12:00",
                                   "onsite_capacity": 1, "remote_capacity": 1}], self.rules)
        items2 = build_plan_items(sites=[low, high], slots=slots2, rules=self.rules)
        self.assertEqual([i["action"] for i in items], [i["action"] for i in items2])

    def test_capacity_exhausted_defers_with_reason(self):
        site = self.site([
            record("r1", "hazard_record", "hz",
                   {"hazard_id": "HZ", "status": "open", "severity": "critical"})])
        slots = normalize_slots([{"slot_id": "am", "start": "08:00", "end": "12:00",
                                  "onsite_capacity": 0, "remote_capacity": 0}], self.rules)
        items = build_plan_items(sites=[site], slots=slots, rules=self.rules)
        self.assertEqual("deferred", items[0]["action"])
        self.assertEqual("onsite_capacity_exhausted", items[0]["deferred_reason"])

    def test_emergency_forces_onsite_breaks_window_and_sorts_first(self):
        calm = self.site([record("r1", "risk_profile", "rp", {"level": "low"})], site_id="calm")
        emergency_site = self.site([
            record("r2", "assistance_record", "ar", {"assisted_at": "2026-09-20"}),
        ], site_id="urgent")
        slots = normalize_slots([{"slot_id": "am", "start": "08:00", "end": "12:00",
                                  "onsite_capacity": 1, "remote_capacity": 1}], self.rules)
        emergencies = [{"emergency_id": "e1", "site_id": "urgent",
                        "trigger_type": "hotline", "trigger_reference": "12345-1",
                        "detail": {"summary": "夜间偷排"}, "declared_at": "2026-09-25T01:00:00Z"}]
        items = build_plan_items(sites=[calm, emergency_site], slots=slots,
                                 rules=self.rules, emergencies=emergencies)
        self.assertEqual("urgent", items[0]["site_id"])
        self.assertEqual("onsite", items[0]["action"])
        self.assertTrue(items[0]["window_override"])
        self.assertEqual("emergency_trigger", items[0]["window"]["override_reason"])
        self.assertIsNotNone(items[0]["emergency"])
        self.assertEqual("12345-1", items[0]["emergency"]["trigger_reference"])

    def test_emergency_falls_back_to_remote_when_onsite_full(self):
        emergency_site = self.site([], site_id="urgent")
        slots = normalize_slots([{"slot_id": "am", "start": "08:00", "end": "12:00",
                                  "onsite_capacity": 0, "remote_capacity": 1}], self.rules)
        emergencies = [{"emergency_id": "e1", "site_id": "urgent",
                        "trigger_type": "hotline", "trigger_reference": "12345-1",
                        "detail": {}, "declared_at": "2026-09-25T01:00:00Z"}]
        items = build_plan_items(sites=[emergency_site], slots=slots,
                                 rules=self.rules, emergencies=emergencies)
        self.assertEqual("remote", items[0]["action"])

    def test_no_risk_fact_is_deferred_not_assisted(self):
        site = self.site([record("r1", "risk_profile", "rp", {"level": "low"})])
        slots = normalize_slots(None, self.rules)
        items = build_plan_items(sites=[site], slots=slots, rules=self.rules)
        self.assertEqual("deferred", items[0]["action"])
        self.assertEqual("no_active_risk_fact", items[0]["deferred_reason"])

    def test_default_slots_have_working_manpower_capacity(self):
        # 不传时段时使用规则默认人力，现场名额必须可正常扣减
        site = self.site([
            record("r1", "hazard_record", "hz",
                   {"hazard_id": "HZ", "status": "open", "severity": "critical"})])
        slots = normalize_slots(None, self.rules)
        items = build_plan_items(sites=[site], slots=slots, rules=self.rules)
        self.assertEqual("onsite", items[0]["action"])
        self.assertEqual("day", items[0]["slot_id"])


if __name__ == "__main__":
    unittest.main()

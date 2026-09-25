import unittest
from datetime import datetime, timezone

from regulatory_triage_core.clock import FixedClock
from regulatory_triage_core.triage import (
    ASSIST_ONLINE,
    DEFERRED,
    ONSITE,
    REMOTE,
    CandidateInput,
    Fact,
    Rules,
    allocate,
    build_facts,
    latest_assistance,
    latest_risk_level,
)


def rules():
    return Rules(version=1, factor_weights={"overdue_self_check": 30, "open_hazard": 25,
                                           "facility_anomaly": 20},
                 risk_multipliers={"high": 2.0, "medium": 1.2, "low": 1.0},
                 no_visit_days=15, remote_capacity=8)


class TriagePureLogicTest(unittest.TestCase):
    def test_same_business_key_counts_once(self):
        records = [
            {"category": "overdue_self_check", "external_key": "k1",
             "payload": {"v": 1}, "created_at": "2026-09-01T00:00:00Z", "payload_hash": "a"},
            {"category": "overdue_self_check", "external_key": "k1",
             "payload": {"v": 2}, "created_at": "2026-09-02T00:00:00Z", "payload_hash": "b"},
        ]
        facts = build_facts({"s1": records}, {"overdue_self_check"})["s1"]
        self.assertEqual(1, len(facts))
        # 同键只保留最新一条，因此只产生一个事实、只计一次权重。
        self.assertEqual("b", facts[0].payload_hash)

    def test_different_facts_accumulate_and_risk_multiplier_applies(self):
        candidates = [CandidateInput(
            site_id="s1", district_id="D1",
            facts=(Fact("overdue_self_check", "k1", "s1", {}, "", ""),
                   Fact("open_hazard", "k2", "s1", {}, "", "")),
            risk_level="high", last_assistance_at=None, exempt_until=None, no_visit_days=15)]
        result = allocate(candidates, rules(), "2026-09-25", 5, {"D1": 5})[0]
        # (30+25)*2 = 110，两个不同事实各计一次。
        self.assertEqual(110.0, result.score)
        self.assertEqual(ONSITE, result.decision)

    def test_exempt_without_urgent_is_assist_online_and_uses_no_onsite_slot(self):
        candidates = [CandidateInput(
            site_id="s1", district_id="D1",
            facts=(Fact("facility_anomaly", "k1", "s1", {}, "", ""),),
            risk_level="medium", last_assistance_at="2026-09-20T00:00:00Z",
            exempt_until=None, no_visit_days=15)]
        result = allocate(candidates, rules(), "2026-09-25", 0, {"D1": 0})[0]
        self.assertEqual(ASSIST_ONLINE, result.decision)
        self.assertTrue(result.exempt)
        self.assertFalse(result.window_broken)

    def test_urgent_event_breaks_exempt_window_and_is_onsite(self):
        candidates = [CandidateInput(
            site_id="s1", district_id="D1",
            facts=(Fact("facility_anomaly", "k1", "s1", {}, "", ""),
                   Fact("urgent_event", "u1", "s1", {"trigger_basis": "夜间举报"}, "", "")),
            risk_level="low", last_assistance_at="2026-09-22T00:00:00Z",
            exempt_until=None, no_visit_days=15)]
        result = allocate(candidates, rules(), "2026-09-25", 5, {"D1": 5})[0]
        self.assertTrue(result.window_broken)
        self.assertEqual(ONSITE, result.decision)

    def test_capacity_overflow_is_deterministic_by_score_then_id(self):
        candidates = []
        for sid, level in [("high1", "high"), ("low1", "low"), ("low2", "low")]:
            candidates.append(CandidateInput(
                site_id=sid, district_id="D1",
                facts=(Fact("overdue_self_check", "k-" + sid, sid, {}, "", ""),),
                risk_level=level, last_assistance_at=None, exempt_until=None, no_visit_days=15))
        result = allocate(candidates, rules(), "2026-09-25", 1, {"D1": 1}, remote_capacity=1)
        by_site = {item.site_id: item for item in result}
        self.assertEqual(ONSITE, by_site["high1"].decision)   # 60 分最高
        self.assertEqual(REMOTE, by_site["low1"].decision)   # 现场满，远程兜底（编号较小）
        self.assertEqual(DEFERRED, by_site["low2"].decision)  # 均满，延后

    def test_district_capacity_blocks_independently(self):
        candidates = [
            CandidateInput("a", "D1", (Fact("overdue_self_check", "ka", "a", {}, "", ""),),
                           "low", None, None, 15),
            CandidateInput("b", "D2", (Fact("overdue_self_check", "kb", "b", {}, "", ""),),
                           "low", None, None, 15),
        ]
        result = allocate(candidates, rules(), "2026-09-25", 5, {"D1": 0, "D2": 5}, remote_capacity=0)
        by_site = {item.site_id: item for item in result}
        self.assertEqual(DEFERRED, by_site["a"].decision)
        self.assertEqual(ONSITE, by_site["b"].decision)

    def test_allocation_is_order_independent(self):
        def make(sid):
            return CandidateInput(sid, "D1", (Fact("overdue_self_check", "k-" + sid, sid, {}, "", ""),),
                                  "low", None, None, 15)
        first = allocate([make("a"), make("b"), make("c")], rules(), "2026-09-25", 2, {"D1": 2})
        second = allocate([make("c"), make("a"), make("b")], rules(), "2026-09-25", 2, {"D1": 2})
        self.assertEqual([(x.site_id, x.decision) for x in first],
                         [(x.site_id, x.decision) for x in second])

    def test_latest_helpers(self):
        records = [{"external_key": "r1", "created_at": "2026-09-01T00:00:00Z",
                    "payload": {"risk_level": "low"}},
                   {"external_key": "r2", "created_at": "2026-09-03T00:00:00Z",
                    "payload": {"risk_level": "high"}}]
        self.assertEqual("high", latest_risk_level(records))
        occurred, until = latest_assistance(
            [{"external_key": "a", "created_at": "2026-09-01T00:00:00Z",
              "payload": {"occurred_at": "2026-09-01T00:00:00Z", "exempt_until": "2026-09-20"}}])
        self.assertEqual("2026-09-20", until)
        self.assertEqual("2026-09-01T00:00:00Z", occurred)


if __name__ == "__main__":
    unittest.main()

import unittest

from regulatory_triage_core.acceptance import run


class AcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["second_replayed"])
        self.assertEqual(1, result["plan_version"])
        self.assertEqual(2, result["plan_v2_version"])
        # 紧急事件穿透免访窗口并占据队首现场名额
        self.assertTrue(result["emergency_rank_first"])
        self.assertEqual("onsite", result["emergency_action"])
        self.assertTrue(result["emergency_override"])
        self.assertEqual("12345-2026-0925", result["emergency_trigger"])
        # 解释链保留触发依据；旧方案版本不被改写
        self.assertTrue(result["explain_has_trigger"])
        self.assertTrue(result["old_plan_version_kept"])
        self.assertEqual("claimed", result["dispatch_status"])
        self.assertEqual("disp-001", result["dispatch_owner"])
        # 首版方案：逾期高风险现场、窗口内重大隐患远程、低风险设施异常远程
        self.assertEqual("onsite", result["plan_v1_actions"]["site-001"])
        self.assertEqual("remote", result["plan_v1_actions"]["site-002"])
        self.assertEqual("remote", result["plan_v1_actions"]["site-003"])


if __name__ == "__main__":
    unittest.main()

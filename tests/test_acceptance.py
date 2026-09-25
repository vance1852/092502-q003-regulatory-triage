import unittest

from regulatory_triage_core.acceptance import run


class AcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertFalse(result["first_replayed"])
        self.assertTrue(result["second_replayed"])
        self.assertEqual(6, result["records"])
        self.assertEqual("onsite", result["plan_decision"])
        self.assertTrue(result["window_broken"])
        self.assertEqual(["12369 夜间举报偷排"], result["urgent_trigger_basis"])


if __name__ == "__main__":
    unittest.main()

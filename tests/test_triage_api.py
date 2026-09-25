import unittest

from regulatory_triage_core.api import route
from regulatory_triage_core.service import DomainService
from regulatory_triage_core.storage import Database


class TriageApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = DomainService(self.database)
        self._bootstrap()

    def tearDown(self):
        self.database.close()

    def _call(self, method, path, body=None, actor="disp1"):
        return route(self.service, method, path, body, {"X-Actor-Id": actor})

    def _bootstrap(self):
        route(self.service, "POST", "/organizations",
              {"request_id": "org", "organization_id": "o1", "name": "局"},
              {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "adm", "new_actor_id": "disp1", "display_name": "调度员",
               "role": "admin", "organization_id": "o1"}, {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "insp", "new_actor_id": "insp1", "display_name": "执法员",
               "role": "operator", "organization_id": "o1"}, {"X-Actor-Id": "disp1"})
        route(self.service, "POST", "/sites",
              {"request_id": "site", "site_id": "s1", "organization_id": "o1",
               "name": "车间", "timezone_name": "Asia/Shanghai"}, {"X-Actor-Id": "disp1"})
        for rid, category, key, data in [
                ("dp", "district_profile", "dpk", {"district_id": "D1"}),
                ("rp", "risk_profile", "rpk", {"risk_level": "high"}),
                ("osc", "overdue_self_check", "osck", {"due_date": "2026-09-01"})]:
            route(self.service, "POST", "/domain-records",
                  {"request_id": rid, "site_id": "s1", "category": category,
                   "external_key": key, "data": data}, {"X-Actor-Id": "disp1"})

    def test_rules_current_returns_defaults(self):
        status, payload = self._call("GET", "/rules")
        self.assertEqual(200, status)
        self.assertIn("overdue_self_check", payload["factor_weights"])

    def test_generate_get_explain_and_lock_flow(self):
        status, payload = self._call("POST", "/plans/generate", {
            "request_id": "plan1", "plan_date": "2026-09-25", "onsite_capacity": 1,
            "district_capacity": {"D1": 1}})
        self.assertEqual(201, status)
        plan_id = payload["resource_id"]

        status, plan = self._call("GET", f"/plans/{plan_id}")
        self.assertEqual(200, status)
        self.assertEqual(["s1"], plan["queues"]["onsite"])

        status, explanation = self._call("GET", f"/plans/{plan_id}/sites/s1/explain")
        self.assertEqual(200, status)
        self.assertEqual("onsite", explanation["decision"])
        self.assertEqual(1, explanation["rules_version"])

        status, locked = self._call("POST", "/slots/lock", {
            "request_id": "lk1", "plan_id": plan_id, "site_id": "s1",
            "officer_id": "insp1", "expected_revision": 0, "reason": "现场核查"})
        self.assertEqual(201, status)
        self.assertEqual(f"{plan_id}:s1", locked["resource_id"])

        # 并发重复领取：同一名额已是 locked，条件更新失败。
        status, again = self._call("POST", "/slots/lock", {
            "request_id": "lk2", "plan_id": plan_id, "site_id": "s1",
            "officer_id": "insp1", "expected_revision": 1, "reason": "重复"}, actor="disp1")
        self.assertEqual(409, status)
        self.assertEqual("conflict", again["error"])

    def test_stale_revision_conflict(self):
        _, payload = self._call("POST", "/plans/generate", {
            "request_id": "plan1", "plan_date": "2026-09-25", "onsite_capacity": 1,
            "district_capacity": {"D1": 1}})
        plan_id = payload["resource_id"]
        self._call("POST", "/slots/lock", {
            "request_id": "lk1", "plan_id": plan_id, "site_id": "s1",
            "officer_id": "insp1", "expected_revision": 0, "reason": "首次"})
        status, payload = self._call("POST", "/slots/release", {
            "request_id": "rl", "plan_id": plan_id, "site_id": "s1",
            "expected_revision": 0, "reason": "旧版本"})
        self.assertEqual(409, status)


if __name__ == "__main__":
    unittest.main()

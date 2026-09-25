import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

from regulatory_triage_core.api import Handler, route
from regulatory_triage_core.service import DomainService
from regulatory_triage_core.storage import Database
from regulatory_triage_core.triage import TriageService
from regulatory_triage_core.api import ThreadingHTTPServer


class TriageApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = DomainService(self.database)
        self.triage = TriageService(self.database)

    def tearDown(self):
        self.database.close()

    def _bootstrap(self):
        route(self.service, "POST", "/organizations",
              {"request_id": "org", "organization_id": "org", "name": "局"},
              {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "adm", "new_actor_id": "adm", "display_name": "管理员",
               "role": "admin", "organization_id": "org"},
              {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "d1", "new_actor_id": "d1", "display_name": "调度员甲",
               "role": "dispatcher", "organization_id": "org"},
              {"X-Actor-Id": "adm"})
        route(self.service, "POST", "/sites",
              {"request_id": "site1", "site_id": "s1", "organization_id": "org",
               "name": "一厂", "timezone_name": "Asia/Shanghai"},
              {"X-Actor-Id": "adm"})

    def test_rules_and_plan_endpoints(self):
        self._bootstrap()
        status, payload = route(self.service, "POST", "/rules",
                                {"request_id": "r1", "rule_id": "main",
                                 "rules": {"no_visit_days": 14}},
                                {"X-Actor-Id": "d1"})
        self.assertEqual(201, status)
        self.assertEqual(1, payload["version"])

        status, payload = route(self.service, "POST", "/plans",
                                {"request_id": "p1", "plan_date": "2026-09-25",
                                 "district_id": "default",
                                 "slots": [{"slot_id": "am", "start": "08:00",
                                            "end": "12:00", "onsite_capacity": 2,
                                            "remote_capacity": 2}]},
                                {"X-Actor-Id": "d1"})
        self.assertEqual(201, status)
        self.assertEqual(1, payload["plan_version"])

        status, payload = route(self.service, "GET",
                                "/plans?plan_date=2026-09-25&district_id=default",
                                None)
        self.assertEqual(200, status)
        self.assertEqual(1, payload["plan_version"])
        self.assertIn("s1", [item["site_id"] for item in payload["items"]])

    def test_explain_endpoint(self):
        self._bootstrap()
        route(self.service, "POST", "/plans",
              {"request_id": "p1", "plan_date": "2026-09-25"},
              {"X-Actor-Id": "d1"})
        status, payload = route(
            self.service, "GET",
            "/plans/explain?plan_date=2026-09-25&site_id=s1", None)
        self.assertEqual(200, status)
        self.assertEqual("deferred", payload["action"])
        self.assertIn("conclusion", payload)

    def test_missing_plan_date_is_400(self):
        status, payload = route(self.service, "GET", "/plans", None)
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_claim_flow_over_http_is_conflict_safe(self):
        self._bootstrap()
        route(self.service, "POST", "/domain-records",
              {"request_id": "hz1", "site_id": "s1", "category": "hazard_record",
               "external_key": "hz-open",
               "data": {"hazard_id": "HZ-1", "status": "open", "severity": "critical"}},
              {"X-Actor-Id": "adm"})
        route(self.service, "POST", "/plans",
              {"request_id": "p1", "plan_date": "2026-09-25",
               "slots": [{"slot_id": "am", "start": "08:00", "end": "12:00",
                          "onsite_capacity": 1, "remote_capacity": 1}]},
              {"X-Actor-Id": "d1"})
        status, listing = route(
            self.service, "GET", "/dispatches?plan_date=2026-09-25", None)
        self.assertEqual(200, status)
        dispatch_id = listing["items"][0]["dispatch_id"]

        status, claimed = route(self.service, "POST", "/dispatches/claim",
                                {"request_id": "c1", "dispatch_id": dispatch_id,
                                 "expected_plan_version": 1, "reason": "甲锁定"},
                                {"X-Actor-Id": "d1"})
        self.assertEqual(201, status)
        self.assertEqual("d1", claimed["claimed_by"])

        status, second = route(self.service, "POST", "/dispatches/claim",
                               {"request_id": "c2", "dispatch_id": dispatch_id,
                                "expected_plan_version": 1, "reason": "重复锁定"},
                               {"X-Actor-Id": "d1"})
        self.assertEqual(409, status)
        self.assertEqual("dispatch_state_conflict", second["error"])

        status, stale = route(self.service, "POST", "/dispatches/release",
                              {"request_id": "r1", "dispatch_id": dispatch_id,
                               "expected_plan_version": 99, "reason": "陈旧版本"},
                              {"X-Actor-Id": "d1"})
        self.assertEqual(409, status)
        self.assertEqual("plan_version_conflict", stale["error"])

    def test_emergency_requires_evidence(self):
        self._bootstrap()
        status, payload = route(self.service, "POST", "/emergencies",
                                {"request_id": "e1", "site_id": "s1",
                                 "trigger_type": "hotline",
                                 "trigger_reference": "x-1", "detail": {}},
                                {"X-Actor-Id": "d1"})
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_concurrent_http_claims_have_single_winner(self):
        self._bootstrap()
        route(self.service, "POST", "/domain-records",
              {"request_id": "hz1", "site_id": "s1", "category": "hazard_record",
               "external_key": "hz-open",
               "data": {"hazard_id": "HZ-1", "status": "open", "severity": "critical"}},
              {"X-Actor-Id": "adm"})
        route(self.service, "POST", "/plans",
              {"request_id": "p1", "plan_date": "2026-09-25",
               "slots": [{"slot_id": "am", "start": "08:00", "end": "12:00",
                          "onsite_capacity": 1, "remote_capacity": 1}]},
              {"X-Actor-Id": "d1"})
        _, listing = route(self.service, "GET", "/dispatches?plan_date=2026-09-25", None)
        dispatch_id = listing["items"][0]["dispatch_id"]

        Handler.service = self.service
        Handler.triage = self.triage
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            results = []

            def post_claim(request_id, actor):
                connection = HTTPConnection("127.0.0.1", port, timeout=5)
                body = json.dumps({"request_id": request_id,
                                   "dispatch_id": dispatch_id,
                                   "expected_plan_version": 1,
                                   "reason": "并发 HTTP 领取"}).encode()
                connection.request("POST", "/dispatches/claim", body,
                                   {"Content-Type": "application/json",
                                    "X-Actor-Id": actor})
                response = connection.getresponse()
                results.append((response.status, json.loads(response.read())))
                connection.close()

            threads = [threading.Thread(target=post_claim, args=(f"c{i}", f"d1"))
                       for i in range(2)]
            for worker in threads:
                worker.start()
            for worker in threads:
                worker.join()
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(2, len(results))
        self.assertEqual(1, sum(1 for status, _ in results if status == 201))
        self.assertEqual(1, sum(1 for status, _ in results if status == 409))


if __name__ == "__main__":
    unittest.main()

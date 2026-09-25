import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from festival_foundation.api import route
from festival_foundation.clock import FixedClock
from festival_foundation.relay import RelayService
from festival_foundation.service import DomainService
from festival_foundation.storage import Database


class RelayApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        self.service = DomainService(self.database, clock)
        self.relay = RelayService(self.database, clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="交通保障中心")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_site(request_id="site", actor_id="a1", site_id="s1",
                                   organization_id="o1", name="东高速入口",
                                   timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    def call(self, method, path, body=None, actor="a1"):
        return route(self.service, method, path, body or {},
                     {"X-Actor-Id": actor}, relay=self.relay)

    def test_risk_to_timeline_flow_over_http(self):
        status, body = self.call("POST", "/relay/risks", {
            "request_id": "rr1", "site_id": "s1", "road_code": "G15-k42",
            "message_id": "m1", "event_time": "2026-09-25T08:00:00Z",
            "title": "追尾占道", "priority": 1,
            "risks": [{"risk_id": "w1", "type": "road_block", "description": "两车追尾"}]})
        self.assertEqual(201, status)
        incident = body["incident_id"]

        status, body = self.call("GET", f"/relay/incidents/{incident}/timeline")
        self.assertEqual(200, status)
        self.assertEqual("open", body["road_status"])
        self.assertTrue(body["revision_chain_valid"])
        self.assertEqual(3, len(body["conditions"]))

    def test_idempotent_replay_returns_200(self):
        payload = {"request_id": "rr1", "site_id": "s1", "road_code": "G15-k42",
                   "message_id": "m1", "event_time": "2026-09-25T08:00:00Z",
                   "title": "追尾占道",
                   "risks": [{"risk_id": "w1", "type": "road_block", "description": "两车追尾"}]}
        first = self.call("POST", "/relay/risks", payload)
        second = self.call("POST", "/relay/risks", payload)
        self.assertEqual(201, first[0])
        self.assertEqual(200, second[0])
        self.assertTrue(second[1]["replayed"])

    def test_domain_error_mapped_to_status(self):
        status, body = self.call("POST", "/relay/risks", {
            "request_id": "bad", "site_id": "s1", "road_code": "G15-k42",
            "message_id": "m1", "event_time": "bad-time", "title": "x",
            "risks": [{"risk_id": "w1", "description": "d"}]})
        self.assertEqual(400, status)
        self.assertEqual("validation_error", body["error"])

    def test_unknown_relay_route_is_404(self):
        status, body = self.call("GET", "/relay/nope")
        self.assertEqual(404, status)

    def test_relay_routes_absent_when_not_enabled(self):
        status, _ = route(self.service, "GET", "/relay/sites/s1/resources", None)
        self.assertEqual(404, status)

    def test_resource_trace_route(self):
        self.call("POST", "/relay/risks", {
            "request_id": "rr1", "site_id": "s1", "road_code": "G15-k42",
            "message_id": "m1", "event_time": "2026-09-25T08:00:00Z",
            "title": "追尾占道",
            "risks": [{"risk_id": "w1", "type": "generic", "description": "d",
                       "needs_review": False}]})
        status, body = self.call("GET", "/relay/sites/s1/resources")
        self.assertEqual(200, status)
        self.assertEqual([], body["resources"])


class RelayPersistenceTest(unittest.TestCase):
    def test_unfinished_relay_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "relay.sqlite3"

            def fresh_service():
                database = Database(path)
                clock = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
                return database, DomainService(database, clock), RelayService(database, clock)

            db, service, relay = fresh_service()
            service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="交通保障中心")
            service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                   display_name="管理员", role="admin", organization_id="o1")
            service.register_site(request_id="site", actor_id="a1", site_id="s1",
                                  organization_id="o1", name="东高速入口",
                                  timezone_name="Asia/Shanghai")
            reported = relay.report_risk(
                request_id="rr1", actor_id="a1", site_id="s1", road_code="G15-k42",
                message_id="m1", event_time="2026-09-25T08:00:00Z", title="追尾占道",
                risks=[{"risk_id": "w1", "type": "road_block", "description": "两车追尾"}])
            incident = reported["response"]["incident_id"]
            relay.claim_lease(request_id="rl1", actor_id="a1",
                              incident_id=incident, ttl_seconds=3600)
            audit_before = service.verify_audit()
            db.close()

            # 模拟服务重启：重新打开同一个 SQLite 文件
            db2, service2, relay2 = fresh_service()
            timeline = relay2.incident_timeline(incident)
            self.assertEqual("open", timeline["road_status"])
            self.assertTrue(timeline["revision_chain_valid"])
            self.assertIsNotNone(timeline["active_lease"])
            self.assertEqual("a1", timeline["active_lease"]["holder_id"])
            self.assertIn("w1:clearance", timeline["explanation"]["blocking_conditions"])
            self.assertEqual(audit_before, service2.verify_audit())
            # 重启后可继续接力：补上封控并推进时间线
            relay2.report_action(request_id="ra1", actor_id="a1", incident_id=incident,
                                 kind="road_closed", message_id="m2",
                                 event_time="2026-09-25T08:05:00Z")
            self.assertEqual(2, relay2.incident_timeline(incident)["version"])
            db2.close()


if __name__ == "__main__":
    unittest.main()

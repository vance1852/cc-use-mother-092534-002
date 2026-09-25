import unittest
from datetime import datetime, timedelta, timezone

from festival_foundation.api import route
from festival_foundation.clock import FixedClock
from festival_foundation.service import DomainService
from festival_foundation.storage import Database
from festival_foundation.traffic import TrafficRelayService


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class TrafficApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 10, 1, 2, 0, tzinfo=timezone.utc))
        self.clock = clock
        self.service = DomainService(self.database, clock)
        self.traffic = TrafficRelayService(self.database, clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="交通保障局")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="领导", role="admin", organization_id="o1")
        self.service.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                                    display_name="值守", role="operator", organization_id="o1")
        self.service.register_actor(request_id="rv", actor_id="a1", new_actor_id="rv1",
                                    display_name="复核", role="reviewer", organization_id="o1")
        self.service.register_site(request_id="site", actor_id="op1", site_id="site1",
                                   organization_id="o1", name="G15", timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    def call(self, method, path, body=None, actor="op1"):
        return route(self.service, method, path, body or {},
                     {"X-Actor-Id": actor}, traffic=self.traffic)

    def test_traffic_routes_end_to_end(self):
        status, body = self.call("POST", "/traffic/incidents", {
            "request_id": "risk", "site_id": "site1", "scene_key": "K32",
            "title": "追尾", "event_time": iso(self.clock.now()), "message_id": "m1"})
        self.assertEqual(201, status)
        incident_id = body["resource_id"]

        status, body = self.call("POST", "/traffic/messages", {
            "request_id": "cm", "incident_id": incident_id, "message_id": "m2",
            "entry_type": "control_measure", "event_time": iso(self.clock.now()),
            "payload": {"action": "enforce", "kind": "lane_closure",
                        "conditions": [{"id": "clear", "label": "拖离"}]}})
        self.assertEqual(201, status)

        status, body = self.call("GET", f"/traffic/incidents/{incident_id}")
        self.assertEqual(200, status)
        self.assertEqual("controlling", body["road_status"]["status"])
        self.assertEqual("m1", body["evidence"]["risk_report"]["message_id"])

        status, body = self.call("GET", "/traffic/road-status?site_id=site1")
        self.assertEqual(200, status)
        self.assertEqual(incident_id, body["items"][0]["incident_id"])

    def test_release_requires_reviewer_over_http(self):
        _, body = self.call("POST", "/traffic/incidents", {
            "request_id": "risk", "site_id": "site1", "scene_key": "K33",
            "title": "抛锚", "event_time": iso(self.clock.now()), "message_id": "m1"})
        incident_id = body["resource_id"]
        self.call("POST", "/traffic/messages", {
            "request_id": "cm", "incident_id": incident_id, "message_id": "m2",
            "entry_type": "control_measure", "event_time": iso(self.clock.now()),
            "payload": {"action": "enforce", "kind": "lane_closure",
                        "conditions": [{"id": "clear", "label": "拖离"}]}})
        status, body = self.call("POST", "/traffic/messages", {
            "request_id": "rv-self", "incident_id": incident_id, "message_id": "m3",
            "entry_type": "review_result",
            "event_time": iso(self.clock.now() + timedelta(minutes=10)),
            "payload": {"condition_results": {"clear": "confirmed"},
                        "action": "request_release"}})
        self.assertEqual(201, status)
        # op1 是封控设置人，解除被阻止
        status, _ = self.call("GET", f"/traffic/incidents/{incident_id}")
        self.assertEqual(200, status)

        status, body = self.call("POST", "/traffic/messages", {
            "request_id": "rv-ok", "incident_id": incident_id, "message_id": "m4",
            "entry_type": "review_result",
            "event_time": iso(self.clock.now() + timedelta(minutes=11)),
            "payload": {"condition_results": {"clear": "confirmed"},
                        "action": "request_release"}}, actor="rv1")
        self.assertEqual(201, status)
        status, body = self.call("GET", f"/traffic/incidents/{incident_id}")
        self.assertEqual("closed", body["road_status"]["status"])

    def test_traffic_route_not_found_without_service(self):
        status, body = route(self.service, "GET", "/traffic/road-status", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", body["error"])

    def test_missing_actor_is_rejected(self):
        status, body = route(self.service, "POST", "/traffic/resources", {
            "request_id": "r1", "resource_id": "tow1", "site_id": "site1",
            "kind": "tow_truck", "label": "清障车"}, {}, traffic=self.traffic)
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", body["error"])


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from festival_foundation.clock import FixedClock
from festival_foundation.errors import ConflictError, PermissionDenied
from festival_foundation.service import DomainService
from festival_foundation.storage import Database
from festival_foundation.traffic import TrafficRelayService


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class RelayFixture:
    def __init__(self, database=None, clock=None):
        self.database = database or Database()
        self.clock = clock or FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        self.service = DomainService(self.database, self.clock)
        self.traffic = TrafficRelayService(self.database, self.clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="交通保障局")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        for rid, name in (("op1", "值守甲"), ("op2", "值守乙")):
            self.service.register_actor(request_id=f"actor-{rid}", actor_id="a1", new_actor_id=rid,
                                        display_name=name, role="operator", organization_id="o1")
        self.service.register_actor(request_id="actor-rv1", actor_id="a1", new_actor_id="rv1",
                                    display_name="复核员", role="reviewer", organization_id="o1")
        self.service.register_actor(request_id="actor-au1", actor_id="a1", new_actor_id="au1",
                                    display_name="审计员", role="auditor", organization_id="o1")
        self.service.register_site(request_id="site1", actor_id="op1", site_id="site1",
                                   organization_id="o1", name="G15 沈海干线",
                                   timezone_name="Asia/Shanghai")

    def response(self, request_id):
        row = self.database.connection.execute(
            "SELECT response_json FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        return json.loads(row["response_json"])

    def advance(self, minutes):
        self.clock._value += timedelta(minutes=minutes)


class TrafficRelayTest(unittest.TestCase):
    def setUp(self):
        self.fx = RelayFixture()
        self.t = self.fx.traffic
        self.t0 = self.fx.clock.now()
        self._incident_id = None

    def tearDown(self):
        self.fx.database.close()

    def report(self, request_id="risk1", message_id="m1", actor="op1", when=None):
        receipt = self.t.report_risk(request_id=request_id, actor_id=actor, site_id="site1",
                                     scene_key="K32+500", title="追尾事故占用快车道",
                                     event_time=iso(when or self.t0), message_id=message_id)
        self._incident_id = receipt.resource_id
        return receipt

    def enforce(self, request_id="cm1", message_id="m2", conditions=None, actor="op1", when=None):
        conditions = conditions or [{"id": "clear", "label": "事故车拖离"},
                                    {"id": "crowd", "label": "客流疏导完成"}]
        return self.t.append_message(request_id=request_id, actor_id=actor,
                                     incident_id=self.incident_id, message_id=message_id,
                                     entry_type="control_measure",
                                     event_time=iso(when or self.t0 + timedelta(minutes=5)),
                                     payload={"action": "enforce", "kind": "lane_closure",
                                              "conditions": conditions})

    @property
    def incident_id(self):
        return self._incident_id

    def test_full_relay_lifecycle_and_protected_terminal(self):
        receipt = self.report()
        self.assertFalse(receipt.replayed)
        self.enforce()
        explanation = self.t.incident_explanation(self.incident_id)
        self.assertEqual("controlling", explanation["road_status"]["status"])
        self.assertEqual(2, explanation["state_version"])
        self.assertEqual({"clear", "crowd"}, set(explanation["road_status"]["unmet_conditions"]))

        # 领取租约，转交携带未完成条件
        self.fx.advance(6)
        claim = self.t.claim_task(request_id="l1", actor_id="op1",
                                  incident_id=self.incident_id, ttl_seconds=600)
        lease1 = self.fx.response("l1")
        self.assertEqual(2, len(lease1["open_conditions"]))
        with self.assertRaises(ConflictError):
            self.t.claim_task(request_id="l2", actor_id="op2",
                              incident_id=self.incident_id, ttl_seconds=600)
        self.t.transfer_task(request_id="tr1", actor_id="op1",
                             lease_id=claim.resource_id, to_actor_id="op2")
        lease2 = self.fx.response("tr1")
        self.assertEqual("op2", lease2["holder_id"])
        self.assertEqual(lease1["lease_id"], lease2["predecessor_id"])
        self.assertEqual(2, len(lease2["open_conditions"]))
        with self.assertRaises(ConflictError):
            self.t.transfer_task(request_id="tr2", actor_id="op1",
                                 lease_id=lease1["lease_id"], to_actor_id="op2")

        # 封控人本人不能解除
        self.fx.advance(10)
        self.t.append_message(request_id="rv-self", actor_id="op2", incident_id=self.incident_id,
                              message_id="m4", entry_type="review_result",
                              event_time=iso(self.fx.clock.now()),
                              payload={"condition_results": {"clear": "confirmed",
                                                             "crowd": "confirmed"},
                                       "action": "request_release"})
        blocked = self.fx.response("rv-self")
        self.assertEqual("release_blocked", blocked["disposition"])
        self.assertTrue(any(b["code"] == "independent_reviewer_required"
                            for b in blocked["blockers"]))

        # 独立 reviewer 确认全部前置项后关闭
        review = self.t.append_message(request_id="rv-ok", actor_id="rv1",
                                       incident_id=self.incident_id, message_id="m5",
                                       entry_type="review_result",
                                       event_time=iso(self.fx.clock.now()),
                                       payload={"condition_results": {"clear": "confirmed",
                                                                      "crowd": "confirmed"},
                                                "action": "request_release"})
        closed = self.fx.response("rv-ok")
        self.assertEqual("closed", closed["disposition"])
        self.assertEqual(review.resource_id, f"{self.incident_id}:{closed['entry_sequence']}")

        # 迟到的封控消息不能重开终态
        late = self.t.append_message(request_id="late1", actor_id="op1",
                                     incident_id=self.incident_id, message_id="m6",
                                     entry_type="control_measure",
                                     event_time=iso(self.fx.clock.now()),
                                     payload={"action": "enforce", "kind": "road_closure",
                                              "conditions": [{"id": "x", "label": "迟到封控"}]})
        late_body = self.fx.response("late1")
        self.assertEqual("terminal_protected", late_body["disposition"])
        explanation = self.t.incident_explanation(self.incident_id)
        self.assertEqual("closed", explanation["road_status"]["status"])
        self.assertIsNone(explanation["active_lease"])
        self.assertTrue(explanation["entry_chain_valid"])
        self.assertEqual(1, len(explanation["evidence"]["ignored_after_close"]))
        self.assertIsNotNone(explanation["evidence"]["release"])

    def test_duplicate_message_replays_and_changed_content_conflicts(self):
        self.report()
        self.enforce()
        duplicate = self.t.append_message(request_id="cm-dup", actor_id="op1",
                                          incident_id=self.incident_id, message_id="m2",
                                          entry_type="control_measure",
                                          event_time=iso(self.t0 + timedelta(minutes=5)),
                                          payload={"action": "enforce", "kind": "lane_closure",
                                                   "conditions": [
                                                       {"id": "clear", "label": "事故车拖离"},
                                                       {"id": "crowd", "label": "客流疏导完成"}]})
        self.assertEqual("duplicate", self.fx.response("cm-dup")["disposition"])
        self.assertTrue(duplicate.replayed is False)
        with self.assertRaises(ConflictError):
            self.t.append_message(request_id="cm-tamper", actor_id="op1",
                                  incident_id=self.incident_id, message_id="m2",
                                  entry_type="control_measure",
                                  event_time=iso(self.t0 + timedelta(minutes=5)),
                                  payload={"action": "enforce", "kind": "road_closure",
                                           "conditions": []})

    def test_out_of_order_message_is_marked_late_but_kept(self):
        self.report()
        self.enforce(when=self.t0 + timedelta(minutes=10))
        late = self.t.append_message(request_id="late-msg", actor_id="op1",
                                     incident_id=self.incident_id, message_id="m-late",
                                     entry_type="resource_receipt",
                                     event_time=iso(self.t0 + timedelta(minutes=2)),
                                     payload={"action": "arrived", "note": "早到的到场回执"})
        body = self.fx.response("late-msg")
        self.assertTrue(body["late"])
        self.assertEqual("accepted_evidence_only", body["disposition"])
        explanation = self.t.incident_explanation(self.incident_id)
        self.assertTrue(all(entry["entry_hash"] for entry in explanation["timeline"]))
        self.assertTrue(explanation["entry_chain_valid"])

    def test_expired_lease_can_be_reclaimed_and_carries_open_conditions(self):
        self.report()
        self.enforce()
        self.t.claim_task(request_id="l1", actor_id="op1", incident_id=self.incident_id,
                          ttl_seconds=600)
        self.fx.advance(11)  # 租约过期
        self.t.claim_task(request_id="l2", actor_id="op2", incident_id=self.incident_id,
                          ttl_seconds=600)
        lease2 = self.fx.response("l2")
        self.assertTrue(lease2["active"])
        self.assertEqual("op2", lease2["holder_id"])
        self.assertEqual(2, len(lease2["open_conditions"]))
        chain = self.t.incident_explanation(self.incident_id)["leases"]
        self.assertEqual(["expired", None], [row["end_reason"] for row in chain])

    def test_reviewer_must_be_independent_for_each_condition(self):
        self.report()
        self.enforce(actor="op1")
        # op1 设置封控；op2 确认其中一条，reviewer 确认另一条 -> 仍不能解除
        self.t.append_message(request_id="rv-op2", actor_id="op2", incident_id=self.incident_id,
                              message_id="m3", entry_type="review_result",
                              event_time=iso(self.t0 + timedelta(minutes=6)),
                              payload={"condition_results": {"clear": "confirmed"}})
        blocked = self.t.append_message(request_id="rv-partial", actor_id="rv1",
                                        incident_id=self.incident_id, message_id="m4",
                                        entry_type="review_result",
                                        event_time=iso(self.t0 + timedelta(minutes=7)),
                                        payload={"condition_results": {"crowd": "confirmed"},
                                                 "action": "request_release"})
        body = self.fx.response("rv-partial")
        self.assertEqual("release_blocked", body["disposition"])
        codes = {(b.get("condition_id"), b["code"]) for b in body["blockers"]
                 if b["code"] == "independent_reviewer_required"}
        self.assertIn(("clear", "independent_reviewer_required"), codes)

    def test_revision_chain_cannot_overwrite_existing_condition(self):
        self.report()
        self.enforce()
        with self.assertRaises(ConflictError):
            self.t.append_message(request_id="cm2", actor_id="op1",
                                  incident_id=self.incident_id, message_id="m7",
                                  entry_type="control_measure",
                                  event_time=iso(self.t0 + timedelta(minutes=6)),
                                  payload={"action": "enforce", "kind": "lane_closure",
                                           "conditions": [{"id": "clear", "label": "重复条件"}]})

    def test_auditor_cannot_take_relay_actions(self):
        self.report()
        with self.assertRaises(PermissionDenied):
            self.t.claim_task(request_id="forbidden", actor_id="au1",
                              incident_id=self.incident_id)

    def test_active_incident_per_scene_is_unique(self):
        self.report()
        with self.assertRaises(ConflictError):
            self.report(request_id="risk2", message_id="m-other")

    def test_second_enforce_accumulates_control_kinds(self):
        self.report()
        self.enforce()
        self.t.append_message(request_id="cm2", actor_id="op1", incident_id=self.incident_id,
                              message_id="m7", entry_type="control_measure",
                              event_time=iso(self.t0 + timedelta(minutes=6)),
                              payload={"action": "enforce", "kind": "device_occupation",
                                       "conditions": [{"id": "device", "label": "设备撤除"}]})
        explanation = self.t.incident_explanation(self.incident_id)
        self.assertEqual({"device_occupation", "lane_closure"},
                         set(explanation["road_status"]["control_kind"].split(",")))
        self.assertEqual({"clear", "crowd", "device"},
                         set(explanation["road_status"]["unmet_conditions"]))


class AllocationTest(unittest.TestCase):
    def setUp(self):
        self.fx = RelayFixture()
        self.t = self.fx.traffic
        self.t0 = self.fx.clock.now()
        self._incident_id = None
        for rid, kind, label in (("tow1", "tow_truck", "清障车甲"), ("tow2", "tow_truck", "清障车乙"),
                                 ("amb1", "ambulance", "救护车甲")):
            self.t.register_resource(request_id=f"res-{rid}", actor_id="op1", resource_id=rid,
                                     site_id="site1", kind=kind, label=label)
        receipt = self.t.report_risk(request_id="risk1", actor_id="op1", site_id="site1",
                                     scene_key="K33", title="多车连撞",
                                     event_time=iso(self.t0), message_id="m1")
        self.incident_id = receipt.resource_id
        self.t.append_message(request_id="cm1", actor_id="op1", incident_id=self.incident_id,
                              message_id="m2", entry_type="control_measure",
                              event_time=iso(self.t0 + timedelta(minutes=2)),
                              payload={"action": "enforce", "kind": "road_closure",
                                       "conditions": [{"id": "clear", "label": "清障完毕"}]})

    def tearDown(self):
        self.fx.database.close()

    def test_infeasible_plan_carries_reasons_and_execute_leaves_nothing(self):
        self.t.propose_allocation(request_id="plan1", actor_id="op1",
                                  incident_id=self.incident_id,
                                  requests=[{"kind": "tow_truck"}, {"kind": "helicopter"}],
                                  reason="节日大流量需要空中支援")
        plan = self.fx.response("plan1")
        self.assertFalse(plan["feasible"])
        self.assertEqual(2, len(plan["items"]))
        shortage = next(item for item in plan["items"] if item["kind"] == "helicopter")
        self.assertEqual("escalate", shortage["action"])
        self.assertIn("helicopter", shortage["reason"])
        with self.assertRaises(ConflictError):
            self.t.decide_allocation(request_id="dec1", actor_id="op1", plan_id=plan["plan_id"],
                                     state_version=plan["state_version"], decision="execute")
        # 升级整体生效，且没有任何资源被占用
        self.t.decide_allocation(request_id="dec2", actor_id="op1", plan_id=plan["plan_id"],
                                 state_version=plan["state_version"], decision="escalate")
        self.assertEqual("escalated", self.fx.response("dec2")["status"])
        for rid in ("tow1", "tow2", "amb1"):
            self.assertEqual("available", self.t.resource_trace(rid)["status"])
        with self.assertRaises(ConflictError):
            self.t.decide_allocation(request_id="dec3", actor_id="op1", plan_id=plan["plan_id"],
                                     state_version=plan["state_version"], decision="escalate")

    def test_feasible_plan_executes_atomically_and_rejects_stale_version(self):
        proposed = self.t.propose_allocation(
            request_id="plan1", actor_id="op1", incident_id=self.incident_id,
            requests=[{"kind": "tow_truck"}, {"kind": "ambulance"}], reason="连撞处置")
        plan = self.fx.response("plan1")
        self.assertTrue(plan["feasible"])

        # 状态版本变化后，旧版本不能确认
        self.fx.advance(3)
        self.t.append_message(request_id="rv-progress", actor_id="rv1",
                              incident_id=self.incident_id, message_id="m3",
                              entry_type="review_result", event_time=iso(self.fx.clock.now()),
                              payload={"condition_results": {"clear": "rejected"}})
        with self.assertRaises(ConflictError):
            self.t.decide_allocation(request_id="dec-stale", actor_id="op1",
                                     plan_id=plan["plan_id"],
                                     state_version=plan["state_version"], decision="execute")
        # 重提方案后整体执行
        fresh = self.t.propose_allocation(
            request_id="plan2", actor_id="op1", incident_id=self.incident_id,
            requests=[{"kind": "tow_truck"}, {"kind": "ambulance"}], reason="连撞处置")
        plan2 = self.fx.response("plan2")
        self.t.decide_allocation(request_id="dec2", actor_id="op1", plan_id=plan2["plan_id"],
                                 state_version=plan2["state_version"], decision="execute")
        executed = self.fx.response("dec2")
        self.assertEqual("executed", executed["status"])
        occupied = {row["resource_id"]: row["status"]
                    for row in self.fx.database.connection.execute(
                        "SELECT resource_id,status FROM traffic_resources")}
        self.assertEqual("occupied", occupied["tow1"])
        self.assertEqual("occupied", occupied["amb1"])
        self.assertEqual("available", occupied["tow2"])
        # 幂等重放
        replay = self.t.decide_allocation(request_id="dec2", actor_id="op1",
                                          plan_id=plan2["plan_id"],
                                          state_version=plan2["state_version"], decision="execute")
        self.assertTrue(replay.replayed)
        self.assertEqual(proposed.resource_id, plan["plan_id"])

    def test_failed_execution_rolls_back_all_occupancy(self):
        self.t.propose_allocation(request_id="plan1", actor_id="op1",
                                  incident_id=self.incident_id,
                                  requests=[{"kind": "tow_truck"}, {"kind": "ambulance"}],
                                  reason="连撞处置")
        plan = self.fx.response("plan1")
        # 负责人确认前，救护车被同事件的一条占用回执先行占用
        self.fx.database.connection.execute(
            "UPDATE traffic_resources SET status='occupied',current_incident_id=?,holder_id='op1' "
            "WHERE resource_id='amb1'",
            (self.incident_id,),
        )
        with self.assertRaises(ConflictError):
            self.t.decide_allocation(request_id="dec1", actor_id="op1", plan_id=plan["plan_id"],
                                     state_version=plan["state_version"], decision="execute")
        tow = self.t.resource_trace("tow1")
        self.assertEqual("available", tow["status"])
        self.assertIsNone(tow["current_incident_id"])
        self.assertEqual([], tow["movements"])

    def test_resource_occupied_by_another_incident_cannot_be_double_taken(self):
        second = self.t.report_risk(request_id="risk2", actor_id="op1", site_id="site1",
                                    scene_key="K99", title="另一处抛锚",
                                    event_time=iso(self.t0 + timedelta(minutes=4)),
                                    message_id="m9")
        self.t.append_message(request_id="occ-tow1", actor_id="op1",
                              incident_id=self.incident_id, message_id="m10",
                              entry_type="resource_receipt",
                              event_time=iso(self.t0 + timedelta(minutes=5)),
                              payload={"action": "occupied", "resource_id": "tow1"})
        with self.assertRaises(ConflictError):
            self.t.append_message(request_id="occ-double", actor_id="op1",
                                  incident_id=second.resource_id, message_id="m11",
                                  entry_type="resource_receipt",
                                  event_time=iso(self.t0 + timedelta(minutes=6)),
                                  payload={"action": "occupied", "resource_id": "tow1"})
        self.assertEqual(self.incident_id,
                         self.t.resource_trace("tow1")["current_incident_id"])


class QueryAndPersistenceTest(unittest.TestCase):
    def test_explanation_documents_evidence_and_resource_trace(self):
        fx = RelayFixture()
        try:
            t0 = fx.clock.now()
            fx.traffic.register_resource(request_id="res1", actor_id="op1", resource_id="tow1",
                                         site_id="site1", kind="tow_truck", label="清障车甲")
            receipt = fx.traffic.report_risk(request_id="risk1", actor_id="op1", site_id="site1",
                                             scene_key="K34", title="故障车占道",
                                             event_time=iso(t0), message_id="m1")
            incident_id = receipt.resource_id
            fx.traffic.append_message(request_id="cm1", actor_id="op1", incident_id=incident_id,
                                      message_id="m2", entry_type="control_measure",
                                      event_time=iso(t0 + timedelta(minutes=2)),
                                      payload={"action": "enforce", "kind": "lane_closure",
                                               "conditions": [{"id": "clear", "label": "拖离"}]})
            fx.traffic.append_message(request_id="rr1", actor_id="op1", incident_id=incident_id,
                                      message_id="m3", entry_type="resource_receipt",
                                      event_time=iso(t0 + timedelta(minutes=3)),
                                      payload={"action": "occupied", "resource_id": "tow1"})
            explanation = fx.traffic.incident_explanation(incident_id)
            evidence = explanation["evidence"]
            self.assertEqual("m1", evidence["risk_report"]["message_id"])
            self.assertEqual("m2", evidence["control_enforced"]["message_id"])
            condition = evidence["conditions"][0]
            self.assertEqual("m2", condition["requested_in"]["message_id"])
            self.assertIsNone(condition["confirmed_in"])
            trace = fx.traffic.resource_trace("tow1")
            self.assertEqual(incident_id, trace["current_incident_id"])
            self.assertEqual("receipt_occupied", trace["movements"][-1]["action"])
            statuses = fx.traffic.road_status("site1")
            self.assertEqual(incident_id, statuses[0]["incident_id"])
        finally:
            fx.database.close()

    def test_unfinished_relay_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "relay.sqlite3"
            fx = RelayFixture(Database(path), FixedClock(datetime(2026, 9, 25, 8, 0,
                                                                  tzinfo=timezone.utc)))
            t0 = fx.clock.now()
            fx.traffic.register_resource(request_id="res1", actor_id="op1", resource_id="tow1",
                                         site_id="site1", kind="tow_truck", label="清障车甲")
            receipt = fx.traffic.report_risk(request_id="risk1", actor_id="op1", site_id="site1",
                                             scene_key="K35", title="抛锚占道",
                                             event_time=iso(t0), message_id="m1")
            incident_id = receipt.resource_id
            fx.traffic.append_message(request_id="cm1", actor_id="op1", incident_id=incident_id,
                                      message_id="m2", entry_type="control_measure",
                                      event_time=iso(t0 + timedelta(minutes=2)),
                                      payload={"action": "enforce", "kind": "lane_closure",
                                               "conditions": [{"id": "clear", "label": "拖离"}]})
            fx.traffic.claim_task(request_id="lease1", actor_id="op1",
                                  incident_id=incident_id, ttl_seconds=3600)
            fx.traffic.append_message(request_id="rr1", actor_id="op1", incident_id=incident_id,
                                      message_id="m3", entry_type="resource_receipt",
                                      event_time=iso(t0 + timedelta(minutes=3)),
                                      payload={"action": "occupied", "resource_id": "tow1"})
            valid_before, events_before = fx.service.verify_audit()
            fx.database.close()

            database = Database(path)
            service = DomainService(database, FixedClock(datetime(2026, 9, 25, 8, 5,
                                                                  tzinfo=timezone.utc)))
            traffic = TrafficRelayService(database, FixedClock(datetime(2026, 9, 25, 8, 5,
                                                                        tzinfo=timezone.utc)))
            try:
                explanation = traffic.incident_explanation(incident_id)
                self.assertEqual("controlling", explanation["road_status"]["status"])
                self.assertEqual(["clear"], explanation["road_status"]["unmet_conditions"])
                self.assertIsNotNone(explanation["active_lease"])
                self.assertEqual("op1", explanation["active_lease"]["holder_id"])
                self.assertEqual("occupied", traffic.resource_trace("tow1")["status"])
                self.assertTrue(explanation["entry_chain_valid"])
                valid_after, events_after = service.verify_audit()
                self.assertTrue(valid_after)
                self.assertEqual(events_before, events_after)
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()

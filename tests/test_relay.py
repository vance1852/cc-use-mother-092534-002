import unittest
from datetime import datetime, timedelta, timezone

from festival_foundation.errors import ConflictError, PermissionDenied, ValidationError
from festival_foundation.relay import RelayService
from festival_foundation.service import DomainService
from festival_foundation.storage import Database


class TickClock:
    def __init__(self, value):
        self.value = value

    def now(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


BASE = datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)


def at(minutes):
    moment = BASE + timedelta(minutes=minutes)
    return moment.isoformat().replace("+00:00", "Z")


class RelayTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = TickClock(BASE)
        self.service = DomainService(self.database, self.clock)
        self.relay = RelayService(self.database, self.clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="交通保障中心")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="op1", actor_id="admin1", new_actor_id="op1",
                                    display_name="值守甲", role="operator", organization_id="o1")
        self.service.register_actor(request_id="op2", actor_id="admin1", new_actor_id="op2",
                                    display_name="值守乙", role="operator", organization_id="o1")
        self.service.register_actor(request_id="rv1", actor_id="admin1", new_actor_id="rv1",
                                    display_name="复核丙", role="reviewer", organization_id="o1")
        self.service.register_actor(request_id="rv2", actor_id="admin1", new_actor_id="rv2",
                                    display_name="复核丁", role="reviewer", organization_id="o1")
        self.service.register_site(request_id="site", actor_id="admin1", site_id="s1",
                                   organization_id="o1", name="东高速入口", timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    # ------------------------------------------------------------ 工具

    def report(self, message_id="m1", road="G15-k42", risks=None, **kw):
        default_risks = [{"risk_id": "w1", "type": "road_block",
                          "description": "两车追尾", "needs_equipment": True}]
        params = dict(request_id=f"rr-{message_id}", actor_id="op1", site_id="s1",
                      road_code=road, message_id=message_id, event_time=at(0),
                      title="节假日追尾占道", priority=1,
                      risks=default_risks if risks is None else risks)
        params.update(kw)
        return self.relay.report_risk(**params)["response"]

    def action(self, kind, message_id, minutes, actor="op1", details=None, incident=None):
        return self.relay.report_action(
            request_id=f"ra-{message_id}", actor_id=actor,
            incident_id=incident or self.incident, kind=kind, message_id=message_id,
            event_time=at(minutes), details=details or {})["response"]

    def full_cycle(self):
        """走通：上报→封控→资源→清障→疏导→复核→解除→关闭。"""

        self.incident = self.report()["incident_id"]
        self.relay.claim_lease(request_id="rl1", actor_id="op1",
                               incident_id=self.incident, ttl_seconds=3600)
        self.action("road_closed", "m2", 2)
        plan = self.relay.plan_allocation(
            request_id="rp1", actor_id="op1", incident_id=self.incident,
            demands=[{"resource_key": "tow-07", "resource_type": "拖车"}],
            reason="清障需要拖车")["response"]
        self.relay.confirm_allocation(request_id="rc1", actor_id="op1",
                                      proposal_id=plan["proposal_id"],
                                      expected_version=plan["base_version"])
        alloc = self.relay.incident_timeline(self.incident)["allocations"][0]["allocation_id"]
        self.relay.acknowledge_resource(request_id="rak1", actor_id="op1", allocation_id=alloc,
                                        message_id="m3", event_time=at(5),
                                        details={"risk_id": "w1"})
        self.action("clearance_done", "m4", 8, details={"risk_id": "w1"})
        self.action("crowd_guided", "m5", 9, details={"risk_id": "w1"})
        self.action("review_recorded", "m6", 10, actor="rv1",
                    details={"result": "pass", "risk_id": "w1", "note": "现场已清空"})
        self.relay.lift_control(request_id="rlift", actor_id="rv1", incident_id=self.incident,
                                message_id="m7", event_time=at(12))
        self.relay.close_incident(request_id="rclose", actor_id="op1",
                                  incident_id=self.incident, message_id="m8", event_time=at(15))
        return self.incident

    # ------------------------------------------------------------ 上报与修订链

    def test_report_creates_conditions_and_revision(self):
        incident = self.report()["incident_id"]
        timeline = self.relay.incident_timeline(incident)
        self.assertEqual("open", timeline["road_status"])
        keys = {c["condition_key"] for c in timeline["conditions"]}
        self.assertEqual({"w1:control", "w1:clearance", "w1:resource", "w1:review"}, keys)
        self.assertTrue(timeline["revision_chain_valid"])

    def test_duplicate_message_rejected(self):
        self.report()
        with self.assertRaises(ConflictError):
            self.report(request_id="rr-dup")

    def test_same_request_id_replays(self):
        first = self.relay.report_risk(request_id="same", actor_id="op1", site_id="s1",
                                       road_code="G15-k42", message_id="m1", event_time=at(0),
                                       title="t", risks=[{"risk_id": "w1", "type": "road_block",
                                                          "description": "d"}])
        second = self.relay.report_risk(request_id="same", actor_id="op1", site_id="s1",
                                        road_code="G15-k42", message_id="m1", event_time=at(0),
                                        title="t", risks=[{"risk_id": "w1", "type": "road_block",
                                                           "description": "d"}])
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["resource_id"], second["resource_id"])

    def test_same_road_merges_into_open_incident(self):
        first = self.report(message_id="m1")
        second = self.report(message_id="m2", request_id="rr-m2",
                             risks=[{"risk_id": "w2", "type": "crowd_surge",
                                     "description": "客流激增", "needs_review": False}])
        self.assertEqual(first["incident_id"], second["incident_id"])
        self.assertFalse(second["new_incident"])
        keys = {c["condition_key"] for c in self.relay.incident_timeline(first["incident_id"])["conditions"]}
        self.assertIn("w2:guidance", keys)

    def test_out_of_order_message_marked_late_but_applied(self):
        self.incident = self.report()["incident_id"]
        self.action("road_closed", "m2", 5)
        late = self.action("note", "m3", 2, details={"text": "补报"})
        self.assertEqual("late", late["ordering"])
        self.assertTrue(late["applied"])

    def test_late_message_cannot_reopen_terminal_state(self):
        incident = self.full_cycle()
        late = self.relay.report_action(
            request_id="ra-late", actor_id="op1", incident_id=incident, kind="clearance_done",
            message_id="mLate", event_time=at(3), details={})["response"]
        self.assertFalse(late["applied"])
        self.assertEqual("closed", self.relay.incident_timeline(incident)["road_status"])

    def test_revision_chain_is_append_only_and_verifiable(self):
        incident = self.full_cycle()
        timeline = self.relay.incident_timeline(incident)
        self.assertTrue(timeline["revision_chain_valid"])
        seqs = [r["seq"] for r in timeline["timeline"]]
        self.assertEqual(sorted(seqs), seqs)
        hashes = [r["revision_hash"] for r in timeline["timeline"]]
        self.assertEqual(len(set(hashes)), len(hashes))
        # 篡改任何一条修订都会破坏链
        self.database.connection.execute(
            "UPDATE relay_revisions SET payload_json='{}' WHERE incident_id=? AND seq=1",
            (incident,))
        self.assertFalse(self.relay.incident_timeline(incident)["revision_chain_valid"])

    # ------------------------------------------------------------ 租约

    def test_lease_claim_conflict_and_expiry(self):
        self.incident = self.report()["incident_id"]
        self.relay.claim_lease(request_id="rl1", actor_id="op1",
                               incident_id=self.incident, ttl_seconds=120)
        with self.assertRaises(ConflictError):
            self.relay.claim_lease(request_id="rl2", actor_id="op2",
                                   incident_id=self.incident, ttl_seconds=120)
        self.clock.advance(minutes=3)
        lease = self.relay.claim_lease(request_id="rl3", actor_id="op2",
                                       incident_id=self.incident, ttl_seconds=120)["response"]
        self.assertEqual("op2", lease["holder_id"])

    def test_transfer_carries_pending_conditions(self):
        self.incident = self.report()["incident_id"]
        self.relay.claim_lease(request_id="rl1", actor_id="op1",
                               incident_id=self.incident, ttl_seconds=3600)
        moved = self.relay.transfer_lease(request_id="rt1", actor_id="op1",
                                          incident_id=self.incident, to_actor_id="op2",
                                          message_id="mt1")["response"]
        self.assertEqual("op2", moved["holder_id"])
        self.assertIn("w1:clearance", moved["pending_conditions"])
        with self.assertRaises(ConflictError):
            self.relay.claim_lease(request_id="rl2", actor_id="op1",
                                   incident_id=self.incident, ttl_seconds=60)

    def test_transfer_requires_open_conditions(self):
        incident = self.full_cycle()
        with self.assertRaises(ConflictError):
            self.relay.transfer_lease(request_id="rt9", actor_id="op1", incident_id=incident,
                                      to_actor_id="op2", message_id="mt9")

    def test_terminal_incident_has_no_active_lease(self):
        incident = self.full_cycle()
        timeline = self.relay.incident_timeline(incident)
        self.assertIsNone(timeline["active_lease"])

    # ------------------------------------------------------------ 解除封控

    def test_lift_blocked_by_open_conditions(self):
        self.incident = self.report()["incident_id"]
        self.action("road_closed", "m2", 2)
        with self.assertRaises(ConflictError) as ctx:
            self.relay.lift_control(request_id="rlift", actor_id="rv1",
                                    incident_id=self.incident, message_id="m3", event_time=at(3))
        self.assertIn("w1:clearance", str(ctx.exception))

    def test_lift_requires_independent_reviewer(self):
        self.incident = self.report()["incident_id"]
        self.action("road_closed", "m2", 2, actor="op1")
        self.action("clearance_done", "m3", 3, details={"risk_id": "w1"})
        self.action("resource_receipt", "m4", 4, details={"risk_id": "w1"})
        self.action("review_recorded", "m5", 5, actor="rv1",
                    details={"result": "pass", "risk_id": "w1"})
        # op1 执行了封控，又是 operator 而非 reviewer —— 双重拦截
        with self.assertRaises(PermissionDenied):
            self.relay.lift_control(request_id="rlift", actor_id="op1",
                                    incident_id=self.incident, message_id="m6", event_time=at(6))

    def test_operator_cannot_record_review(self):
        self.incident = self.report()["incident_id"]
        with self.assertRaises(PermissionDenied):
            self.action("review_recorded", "m2", 2, actor="op1",
                        details={"result": "pass", "risk_id": "w1"})

    def test_reviewer_cannot_perform_operations(self):
        self.incident = self.report()["incident_id"]
        with self.assertRaises(PermissionDenied):
            self.action("road_closed", "m2", 2, actor="rv1")

    def test_failed_review_reopens_conditions_and_late_message_cannot_close_them(self):
        self.incident = self.report()["incident_id"]
        self.action("road_closed", "m2", 2)
        self.action("clearance_done", "m3", 3, details={"risk_id": "w1"})
        self.action("resource_receipt", "m4", 4, details={"risk_id": "w1"})
        self.action("review_recorded", "m5", 5, actor="rv1",
                    details={"result": "pass", "risk_id": "w1"})
        # 复核丁复查失败，回流重开清障条件
        self.relay.reopen_after_review(request_id="rro", actor_id="rv2",
                                       incident_id=self.incident, message_id="m6",
                                       event_time=at(6), failed_conditions=["w1:clearance"])
        conditions = {c["condition_key"]: c["status"]
                      for c in self.relay.incident_timeline(self.incident)["conditions"]}
        self.assertEqual("open", conditions["w1:clearance"])
        # 一条事件时间早于回流时刻的迟到清障消息不得重新满足该条件
        self.action("clearance_done", "m7", 3, details={"risk_id": "w1"})
        conditions = {c["condition_key"]: c["status"]
                      for c in self.relay.incident_timeline(self.incident)["conditions"]}
        self.assertEqual("open", conditions["w1:clearance"])
        # 新的清障上报可以再次满足
        self.action("clearance_done", "m8", 7, details={"risk_id": "w1"})
        conditions = {c["condition_key"]: c["status"]
                      for c in self.relay.incident_timeline(self.incident)["conditions"]}
        self.assertEqual("satisfied", conditions["w1:clearance"])

    # ------------------------------------------------------------ 资源调配

    def test_plan_free_resource_and_confirm(self):
        self.incident = self.report()["incident_id"]
        plan = self.relay.plan_allocation(
            request_id="rp1", actor_id="op1", incident_id=self.incident,
            demands=[{"resource_key": "tow-07", "resource_type": "拖车"}], reason="清障")["response"]
        self.assertEqual("proposed", plan["status"])
        self.assertEqual("free", plan["items"][0]["source"])
        confirmed = self.relay.confirm_allocation(
            request_id="rc1", actor_id="op1", proposal_id=plan["proposal_id"],
            expected_version=plan["base_version"])["response"]
        self.assertEqual("confirmed", confirmed["status"])
        trace = self.relay.resource_trace("s1")
        self.assertEqual("reserved", trace["resources"][0]["destination"]["state"])

    def test_confirm_with_wrong_version_rejected(self):
        self.incident = self.report()["incident_id"]
        plan = self.relay.plan_allocation(
            request_id="rp1", actor_id="op1", incident_id=self.incident,
            demands=[{"resource_key": "tow-07", "resource_type": "拖车"}], reason="清障")["response"]
        with self.assertRaises(ConflictError):
            self.relay.confirm_allocation(request_id="rc1", actor_id="op1",
                                          proposal_id=plan["proposal_id"], expected_version=99)

    def test_shortage_plan_cannot_confirm_and_leaves_no_partial_occupation(self):
        first = self.report(message_id="m1", road="G15-k42")["incident_id"]
        plan_a = self.relay.plan_allocation(
            request_id="rpA", actor_id="op1", incident_id=first,
            demands=[{"resource_key": "tow-07", "resource_type": "拖车"}], reason="A 清障")["response"]
        self.relay.confirm_allocation(request_id="rcA", actor_id="op1",
                                      proposal_id=plan_a["proposal_id"],
                                      expected_version=plan_a["base_version"])
        second = self.report(message_id="m9", road="G15-k88",
                             risks=[{"risk_id": "w9", "type": "road_block",
                                     "description": "二次事故"}])["incident_id"]
        plan_b = self.relay.plan_allocation(
            request_id="rpB", actor_id="op1", incident_id=second,
            demands=[{"resource_key": "tow-07", "resource_type": "拖车"},
                     {"resource_key": "cone-100", "resource_type": "锥桶"}],
            reason="B 也缺拖车")["response"]
        sources = {item["resource_key"]: item["source"] for item in plan_b["items"]}
        self.assertEqual("shortage", sources["tow-07"])
        self.assertEqual("free", sources["cone-100"])
        self.assertEqual("infeasible", plan_b["status"])
        with self.assertRaises(ConflictError):
            self.relay.confirm_allocation(request_id="rcB", actor_id="op1",
                                          proposal_id=plan_b["proposal_id"],
                                          expected_version=plan_b["base_version"])
        # 失败不留任何部分占用
        trace = self.relay.resource_trace("s1")
        states = {r["resource_key"]: r["destination"]["state"] for r in trace["resources"]}
        self.assertNotIn("cone-100", states)

    def test_borrow_from_terminal_incident(self):
        donor = self.full_cycle()
        second = self.report(message_id="m9", road="G15-k88",
                             risks=[{"risk_id": "w9", "type": "road_block",
                                     "description": "二次事故"}])["incident_id"]
        plan = self.relay.plan_allocation(
            request_id="rpB", actor_id="op1", incident_id=second,
            demands=[{"resource_key": "tow-07", "resource_type": "拖车"}], reason="借用")["response"]
        self.assertEqual("borrow", plan["items"][0]["source"])
        self.assertEqual(donor, plan["items"][0]["source_incident_id"])
        self.relay.confirm_allocation(request_id="rcB", actor_id="op1",
                                      proposal_id=plan["proposal_id"],
                                      expected_version=plan["base_version"])
        trace = self.relay.resource_trace("s1")
        tow = next(r for r in trace["resources"] if r["resource_key"] == "tow-07")
        self.assertEqual(second, tow["destination"]["incident_id"])
        self.assertEqual("preempted", tow["history"][0]["status"])

    def test_acknowledge_flips_reserved_to_occupied(self):
        self.incident = self.report()["incident_id"]
        plan = self.relay.plan_allocation(
            request_id="rp1", actor_id="op1", incident_id=self.incident,
            demands=[{"resource_key": "tow-07", "resource_type": "拖车"}], reason="清障")["response"]
        self.relay.confirm_allocation(request_id="rc1", actor_id="op1",
                                      proposal_id=plan["proposal_id"],
                                      expected_version=plan["base_version"])
        alloc = self.relay.incident_timeline(self.incident)["allocations"][0]["allocation_id"]
        out = self.relay.acknowledge_resource(request_id="rak1", actor_id="op1",
                                              allocation_id=alloc, message_id="m3",
                                              event_time=at(5), details={"risk_id": "w1"})["response"]
        self.assertEqual("occupied", out["status"])
        conditions = {c["condition_key"]: c["status"]
                      for c in self.relay.incident_timeline(self.incident)["conditions"]}
        self.assertEqual("satisfied", conditions["w1:resource"])

    # ------------------------------------------------------------ 可解释查询

    def test_timeline_explains_state_and_evidence(self):
        incident = self.full_cycle()
        timeline = self.relay.incident_timeline(incident)
        self.assertEqual("closed", timeline["road_status"])
        self.assertFalse(timeline["explanation"]["can_lift_control"])
        decisions = {d["decision"] for d in timeline["decisions"]}
        self.assertIn("control_lifted", decisions)
        self.assertIn("independent_review_for_lift", decisions)
        review = next(d for d in timeline["decisions"] if d["decision"] == "independent_review_for_lift")
        self.assertTrue(review["evidence"]["independent"])
        for revision in timeline["timeline"]:
            self.assertIn("revision_hash", revision["evidence"])
            self.assertIn("message_id", revision["evidence"])

    def test_explanation_lists_blocking_conditions(self):
        self.incident = self.report()["incident_id"]
        explanation = self.relay.incident_timeline(self.incident)["explanation"]
        self.assertFalse(explanation["can_lift_control"])
        self.assertIn("w1:clearance", explanation["blocking_conditions"])

    def test_resource_trace_shows_free_after_release(self):
        incident = self.full_cycle()
        trace = self.relay.resource_trace("s1")
        tow = next(r for r in trace["resources"] if r["resource_key"] == "tow-07")
        self.assertEqual(incident, tow["destination"]["incident_id"])

    # ------------------------------------------------------------ 校验

    def test_invalid_event_time_rejected(self):
        with self.assertRaises(ValidationError):
            self.report(event_time="not-a-time")

    def test_invalid_risks_rejected(self):
        with self.assertRaises(ValidationError):
            self.report(risks=[])

    def test_unknown_incident(self):
        from festival_foundation.errors import NotFoundError
        with self.assertRaises(NotFoundError):
            self.relay.incident_timeline("missing")


if __name__ == "__main__":
    unittest.main()

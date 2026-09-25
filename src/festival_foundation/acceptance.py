"""运行基础服务与交通干线接力台账的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .relay import RelayService
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    """执行登记链与完整接力链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        service = DomainService(database, clock)
        relay = RelayService(database, clock)
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范交通保障机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="值守负责人", role="operator", organization_id="org-001")
        service.register_actor(request_id="req-operator-2", actor_id="admin-001", new_actor_id="operator-002",
                               display_name="接班值守", role="operator", organization_id="org-001")
        service.register_actor(request_id="req-reviewer", actor_id="admin-001", new_actor_id="reviewer-001",
                               display_name="独立复核", role="reviewer", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="高速东入口保障点", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="organization_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="organization_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})

        # ---- 接力台账：风险上报 → 领取/转交租约 → 封控 → 资源调配 → 清障疏导
        #      → 独立复核 → 解除封控 → 关闭；迟到消息留档但不改终态 ----
        t = lambda mm: f"2026-09-25T{8 + mm // 60:02d}:{mm % 60:02d}:00Z"
        reported = relay.report_risk(
            request_id="req-risk", actor_id="operator-001", site_id="site-001",
            road_code="G15-k42", message_id="msg-01", event_time=t(0),
            title="节日首日追尾占道", priority=1,
            risks=[{"risk_id": "r-01", "type": "road_block", "description": "两车追尾占用应急车道",
                    "needs_equipment": True}])
        incident = reported["response"]["incident_id"]
        relay.claim_lease(request_id="req-lease", actor_id="operator-001",
                          incident_id=incident, ttl_seconds=3600)
        relay.transfer_lease(request_id="req-transfer", actor_id="operator-001",
                             incident_id=incident, to_actor_id="operator-002", message_id="msg-02")
        relay.report_action(request_id="req-close-road", actor_id="operator-002",
                            incident_id=incident, kind="road_closed",
                            message_id="msg-03", event_time=t(2))
        plan = relay.plan_allocation(
            request_id="req-plan", actor_id="operator-002", incident_id=incident,
            demands=[{"resource_key": "tow-truck-07", "resource_type": "清障拖车"}],
            reason="事故清障需要拖车")["response"]
        relay.confirm_allocation(request_id="req-confirm", actor_id="operator-002",
                                 proposal_id=plan["proposal_id"],
                                 expected_version=plan["base_version"])
        alloc = relay.incident_timeline(incident)["allocations"][0]["allocation_id"]
        relay.acknowledge_resource(request_id="req-ack", actor_id="operator-002",
                                   allocation_id=alloc, message_id="msg-04",
                                   event_time=t(5), details={"risk_id": "r-01"})
        relay.report_action(request_id="req-clear", actor_id="operator-002",
                            incident_id=incident, kind="clearance_done",
                            message_id="msg-05", event_time=t(8), details={"risk_id": "r-01"})
        relay.report_action(request_id="req-guide", actor_id="operator-002",
                            incident_id=incident, kind="crowd_guided",
                            message_id="msg-06", event_time=t(9), details={"risk_id": "r-01"})
        relay.report_action(request_id="req-review", actor_id="reviewer-001",
                            incident_id=incident, kind="review_recorded",
                            message_id="msg-07", event_time=t(10),
                            details={"result": "pass", "risk_id": "r-01", "note": "现场已清空"})
        relay.lift_control(request_id="req-lift", actor_id="reviewer-001",
                           incident_id=incident, message_id="msg-08", event_time=t(12))
        late = relay.report_action(request_id="req-late", actor_id="operator-002",
                                   incident_id=incident, kind="clearance_done",
                                   message_id="msg-late", event_time=t(3))["response"]
        relay.close_incident(request_id="req-close", actor_id="operator-002",
                             incident_id=incident, message_id="msg-09", event_time=t(15))

        timeline = relay.incident_timeline(incident)
        trace = relay.resource_trace("site-001")
        valid, event_count = service.verify_audit()
        result = {"status": "ok", "records": len(service.list_domain_data("site-001")),
                  "audit_events": event_count, "audit_valid": valid,
                  "first_replayed": first.replayed, "second_replayed": replay.replayed,
                  "road_status": timeline["road_status"],
                  "revision_count": timeline["revision_count"],
                  "revision_chain_valid": timeline["revision_chain_valid"],
                  "late_message_applied": late["applied"],
                  "late_message_ordering": late["ordering"],
                  "active_lease_after_close": timeline["active_lease"] is not None,
                  "resource_state": trace["resources"][0]["destination"]["state"],
                  "decision_count": len(timeline["decisions"])}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

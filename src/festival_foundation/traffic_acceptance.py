"""运行交通干线节日保障接力台账的离线端到端验收。

场景：一处追尾风险从上报、封控、租约接力、资源不足候选调配、
独立复查解除，到迟到消息被终态保护拒绝重开的完整接力。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import FixedClock
from .service import DomainService
from .storage import Database
from .traffic import TrafficRelayService


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "traffic_acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 10, 1, 2, 0, tzinfo=timezone.utc))
        service = DomainService(database, clock)
        traffic = TrafficRelayService(database, clock)

        service.register_organization(request_id="org", actor_id="bootstrap",
                                      organization_id="org-holiday", name="节日交通保障中心")
        service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin-1",
                               display_name="值班领导", role="admin", organization_id="org-holiday")
        service.register_actor(request_id="op-a", actor_id="admin-1", new_actor_id="op-a",
                               display_name="巡检员甲", role="operator", organization_id="org-holiday")
        service.register_actor(request_id="op-b", actor_id="admin-1", new_actor_id="op-b",
                               display_name="清障员乙", role="operator", organization_id="org-holiday")
        service.register_actor(request_id="rv-a", actor_id="admin-1", new_actor_id="rv-a",
                               display_name="独立复核员", role="reviewer", organization_id="org-holiday")
        service.register_site(request_id="site", actor_id="admin-1", site_id="g15-k32",
                              organization_id="org-holiday", name="G15 K32 路段",
                              timezone_name="Asia/Shanghai")
        traffic.register_resource(request_id="tow", actor_id="op-a", resource_id="tow-01",
                                  site_id="g15-k32", kind="tow_truck", label="清障车一号")

        start = clock.now()
        risk = traffic.report_risk(request_id="risk", actor_id="op-a", site_id="g15-k32",
                                   scene_key="K32+500-east", title="追尾事故占用快车道",
                                   event_time=_iso(start), message_id="msg-risk")
        incident_id = risk.resource_id

        clock._value += timedelta(minutes=4)
        traffic.append_message(request_id="control", actor_id="op-a", incident_id=incident_id,
                               message_id="msg-control", entry_type="control_measure",
                               event_time=_iso(clock.now()),
                               payload={"action": "enforce", "kind": "lane_closure",
                                        "conditions": [{"id": "tow-away", "label": "事故车拖离"},
                                                       {"id": "crowd", "label": "客流疏导完成"}]})
        traffic.claim_task(request_id="claim-a", actor_id="op-a", incident_id=incident_id,
                           ttl_seconds=1800)
        traffic.transfer_task(request_id="handoff", actor_id="op-a",
                              lease_id=json.loads(database.connection.execute(
                                  "SELECT response_json FROM request_receipts WHERE request_id='claim-a'"
                              ).fetchone()["response_json"])["lease_id"], to_actor_id="op-b")

        clock._value += timedelta(minutes=6)
        traffic.append_message(request_id="receipt", actor_id="op-b", incident_id=incident_id,
                               message_id="msg-receipt", entry_type="resource_receipt",
                               event_time=_iso(clock.now()),
                               payload={"action": "occupied", "resource_id": "tow-01"})
        # 乱序迟到的到场回执
        traffic.append_message(request_id="receipt-late", actor_id="op-b", incident_id=incident_id,
                               message_id="msg-arrived-late", entry_type="resource_receipt",
                               event_time=_iso(start + timedelta(minutes=2)),
                               payload={"action": "arrived", "resource_id": "tow-01"})

        shortage = traffic.propose_allocation(
            request_id="plan", actor_id="op-b", incident_id=incident_id,
            requests=[{"kind": "tow_truck"}, {"kind": "rescue_crane"}], reason="大型客车需要起重")
        plan = json.loads(database.connection.execute(
            "SELECT response_json FROM request_receipts WHERE request_id='plan'"
        ).fetchone()["response_json"])
        traffic.decide_allocation(request_id="escalate", actor_id="op-b", plan_id=plan["plan_id"],
                                  state_version=plan["state_version"], decision="escalate")

        clock._value += timedelta(minutes=20)
        traffic.append_message(request_id="review", actor_id="rv-a", incident_id=incident_id,
                               message_id="msg-review", entry_type="review_result",
                               event_time=_iso(clock.now()),
                               payload={"condition_results": {"tow-away": "confirmed",
                                                              "crowd": "confirmed"},
                                        "action": "request_release"})
        # 关闭后迟到的重复封控不得重开
        protected = traffic.append_message(request_id="late-closure", actor_id="op-a",
                                           incident_id=incident_id, message_id="msg-late-closure",
                                           entry_type="control_measure",
                                           event_time=_iso(clock.now() + timedelta(minutes=3)),
                                           payload={"action": "enforce", "kind": "road_closure",
                                                    "conditions": [{"id": "late", "label": "迟到"}]})

        explanation = traffic.incident_explanation(incident_id)
        audit_valid, audit_events = service.verify_audit()
        result = {
            "status": "ok",
            "incident_id": incident_id,
            "final_road_status": explanation["road_status"]["status"],
            "entry_chain_valid": explanation["entry_chain_valid"],
            "late_entries": sum(1 for entry in explanation["timeline"] if entry["late"]),
            "terminal_protected": protected and json.loads(database.connection.execute(
                "SELECT response_json FROM request_receipts WHERE request_id='late-closure'"
            ).fetchone()["response_json"])["disposition"] == "terminal_protected",
            "plan_feasible": plan["feasible"],
            "plan_status": "escalated",
            "resource_final_status": traffic.resource_trace("tow-01")["status"],
            "lease_count": len(explanation["leases"]),
            "audit_valid": audit_valid,
            "audit_events": audit_events,
            "shortage_reason": next(item["reason"] for item in plan["items"]
                                    if item["kind"] == "rescue_crane"),
        }
        database.close()
        return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = (result["status"] == "ok" and result["final_road_status"] == "closed"
          and result["entry_chain_valid"] and result["terminal_protected"]
          and result["audit_valid"] and result["resource_final_status"] == "available")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

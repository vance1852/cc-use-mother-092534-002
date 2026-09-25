"""交通干线节日保障接力台账领域服务。

在基础层（组织、操作者、角色、场所、幂等、审计、事务）之上实现：

- 结构化风险上报、封控/清障/疏导等处置、资源回执、复查结果的事件时间线；
- 按事件时间判定乱序迟到消息与重复消息，同一现场的处置归并为只追加、
  哈希串联、不可覆盖的修订链；
- 会过期的责任租约，转交必须携带尚未完成的条件；
- 封控解除必须由独立复核者确认全部前置项；
- 受保护终态（封控解除/事件关闭）不会被迟到消息重新打开；
- 资源不足时形成带理由的候选调配，负责人按状态版本整体确认，
  失败不留任何部分占用。
"""

from __future__ import annotations

import uuid
from typing import Any

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .storage import Database


TERMINAL_INCIDENT_STATUSES = frozenset({"resolved", "closed"})

# 处置类型：封控类、过程类、终态类
REVISION_KINDS = frozenset({
    "risk_reported",
    "road_closed",
    "road_controlled",
    "lane_closed",
    "equipment_occupied",
    "clearance_done",
    "crowd_guided",
    "resource_receipt",
    "review_recorded",
    "control_lifted",
    "incident_closed",
    "note",
})
LEASE_SECONDS_DEFAULT = 90 * 60
LEASE_SECONDS_MAX = 12 * 3600


def _new_id() -> str:
    return uuid.uuid4().hex


class RelayService:
    """接力台账的写入与可解释查询。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 基础

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _require_actor(self, connection, actor_id: str, *roles: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        if roles and row["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")
        return dict(row)

    def _require_site(self, connection, site_id: str, actor: dict[str, Any]) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        if actor["role"] != "admin" and row["organization_id"] != actor["organization_id"]:
            raise PermissionDenied("不能操作其他组织的场所")
        return dict(row)

    def _incident(self, connection, incident_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM relay_incidents WHERE incident_id=?", (incident_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("接力事件不存在")
        return dict(row)

    def _conditions(self, connection, incident_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM relay_conditions WHERE incident_id=? ORDER BY condition_key", (incident_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def _open_conditions(self, connection, incident_id: str) -> list[dict[str, Any]]:
        return [c for c in self._conditions(connection, incident_id) if c["status"] == "open"]

    def _applied_revisions(self, connection, incident_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM relay_revisions WHERE incident_id=? AND applied=1 ORDER BY seq",
            (incident_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _revision(self, connection, revision_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM relay_revisions WHERE revision_id=?", (revision_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("修订不存在")
        return dict(row)

    def _incident_version(self, connection, incident_id: str) -> int:
        return connection.execute(
            "SELECT COUNT(*) AS c FROM relay_revisions WHERE incident_id=? AND applied=1",
            (incident_id,),
        ).fetchone()["c"]

    def _idempotent_impl(self, connection, *, request_id: str, action: str,
                         payload: dict[str, Any], create) -> dict[str, Any]:
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is not None:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return {"replayed": True, "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"],
                    "response": _json(row["response_json"])}
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return {"replayed": False, "resource_type": resource_type,
                "resource_id": resource_id, "response": response}

    def _append_revision(self, connection, *, incident_id: str, kind: str, message_id: str,
                         event_time: str, actor_id: str, payload: dict[str, Any],
                         ordering: str, applied: bool, not_applied_reason: str | None) -> dict[str, Any]:
        head = connection.execute(
            "SELECT revision_hash FROM relay_revisions WHERE incident_id=? "
            "ORDER BY seq DESC LIMIT 1", (incident_id,)
        ).fetchone()
        prev_hash = head["revision_hash"] if head else "0" * 64
        seq_row = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM relay_revisions WHERE incident_id=?",
            (incident_id,),
        ).fetchone()
        revision_id = _new_id()
        material = {
            "incident_id": incident_id,
            "kind": kind,
            "message_id": message_id,
            "event_time": event_time,
            "actor_id": actor_id,
            "payload": payload,
            "prev_hash": prev_hash,
        }
        revision_hash = digest(material)
        connection.execute(
            "INSERT INTO relay_revisions(revision_id,incident_id,seq,kind,message_id,event_time,"
            "received_at,actor_id,payload_json,payload_hash,ordering,applied,not_applied_reason,"
            "prev_hash,revision_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (revision_id, incident_id, seq_row["next_seq"], kind, message_id, event_time,
             self._now(), actor_id, canonical_json(payload), digest(payload), ordering,
             1 if applied else 0, not_applied_reason, prev_hash, revision_hash),
        )
        return {"revision_id": revision_id, "seq": seq_row["next_seq"], "revision_hash": revision_hash,
                "prev_hash": prev_hash, "event_time": event_time,
                "received_at": self._now(), "message_id": message_id, "kind": kind,
                "actor_id": actor_id}

    # ------------------------------------------------------------------ 上报

    def report_risk(self, *, request_id: str, actor_id: str, site_id: str, road_code: str,
                    message_id: str, event_time: str, title: str,
                    risks: list[dict[str, Any]], priority: int = 2) -> dict[str, Any]:
        """登记结构化风险上报，自动开启所需的处置前置条件。"""

        payload = {"actor_id": actor_id, "site_id": site_id, "road_code": road_code,
                   "message_id": message_id, "event_time": event_time, "title": title,
                   "risks": risks, "priority": priority}
        with self.database.transaction(immediate=True) as connection:
            actor = self._require_actor(connection, actor_id, "admin", "operator")
            self._require_site(connection, site_id, actor)
            road_code = self._identifier(road_code, "road_code")
            message_id = self._identifier(message_id, "message_id")
            title = self._text(title, "title")
            self._validate_event_time(event_time)
            self._validate_risks(risks)
            priority = int(priority)

            def create():
                duplicate = connection.execute(
                    "SELECT r.* FROM relay_revisions r JOIN relay_incidents i ON i.incident_id=r.incident_id "
                    "WHERE i.site_id=? AND i.road_code=? AND r.message_id=?",
                    (site_id, road_code, message_id),
                ).fetchone()
                if duplicate is not None:
                    raise ConflictError("重复消息：message_id 已经在该现场时间线上")
                existing = connection.execute(
                    "SELECT * FROM relay_incidents WHERE site_id=? AND road_code=? AND status='open'",
                    (site_id, road_code),
                ).fetchone()
                now = self._now()
                if existing is None:
                    incident_id = _new_id()
                    connection.execute(
                        "INSERT INTO relay_incidents(incident_id,site_id,road_code,title,priority,status,"
                        "version,created_by,created_at) VALUES(?,?,?,?,?, 'open', 0, ?,?)",
                        (incident_id, site_id, road_code, title, priority, actor_id, now),
                    )
                    created = True
                else:
                    incident_id = existing["incident_id"]
                    created = False
                rev = self._append_revision(
                    connection, incident_id=incident_id, kind="risk_reported",
                    message_id=message_id, event_time=event_time, actor_id=actor_id,
                    payload={"title": title, "priority": priority, "risks": risks},
                    ordering="in_order", applied=True, not_applied_reason=None,
                )
                for risk in risks:
                    self._open_conditions_for_risk(connection, incident_id, risk)
                connection.execute(
                    "UPDATE relay_incidents SET head_revision_id=?, version=? WHERE incident_id=?",
                    (rev["revision_id"], self._incident_version(connection, incident_id), incident_id),
                )
                append_event(connection, actor_id=actor_id,
                             action="relay.risk_reported", resource_type="relay_incident",
                             resource_id=incident_id,
                             detail={"site_id": site_id, "road_code": road_code,
                                     "message_id": message_id, "revision_id": rev["revision_id"],
                                     "new_incident": created},
                             occurred_at=now)
                return ("relay_incident", incident_id,
                        {"incident_id": incident_id, "revision_id": rev["revision_id"],
                         "revision_seq": rev["seq"], "new_incident": created})

            return self._idempotent_impl(connection, request_id=request_id, action="relay.report_risk",
                                         payload=payload, create=create)

    def _open_conditions_for_risk(self, connection, incident_id: str, risk: dict[str, Any]) -> None:
        risk_id = risk["risk_id"]
        conditions: list[tuple[str, str]] = []
        effects = set(risk.get("required_actions") or [])
        risk_type = risk.get("type", "")
        if risk_type == "road_block":
            effects.update({"road_closed", "clearance"})
        if risk_type in {"crowd_surge", "station_crowd"}:
            effects.add("crowd_guidance")
        if risk.get("needs_equipment"):
            effects.add("resource")
        if risk.get("needs_review", True):
            effects.add("review")
        if "road_closed" in effects:
            conditions.append((f"{risk_id}:control", f"风险 {risk_id} 的路段封控"))
        if "clearance" in effects:
            conditions.append((f"{risk_id}:clearance", f"风险 {risk_id} 的清障完成"))
        if "crowd_guidance" in effects:
            conditions.append((f"{risk_id}:guidance", f"风险 {risk_id} 的客流疏导"))
        if "resource" in effects:
            conditions.append((f"{risk_id}:resource", f"风险 {risk_id} 的设备/资源到位"))
        if "review" in effects:
            conditions.append((f"{risk_id}:review", f"风险 {risk_id} 的独立复查通过"))
        for key, label in conditions:
            connection.execute(
                "INSERT INTO relay_conditions(incident_id,condition_key,label,status) VALUES(?,?,?, 'open') "
                "ON CONFLICT(incident_id,condition_key) DO NOTHING",
                (incident_id, key, label),
            )

    # -------------------------------------------------------------- 处置上报

    def report_action(self, *, request_id: str, actor_id: str, incident_id: str, kind: str,
                      message_id: str, event_time: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
        """在事件时间线上追加一条处置/回执/复查类修订（含乱序与终态保护判断）。"""

        details = details or {}
        payload = {"actor_id": actor_id, "incident_id": incident_id, "kind": kind,
                   "message_id": message_id, "event_time": event_time, "details": details}
        with self.database.transaction(immediate=True) as connection:
            if kind == "review_recorded":
                actor = self._require_actor(connection, actor_id, "reviewer", "admin")
            else:
                actor = self._require_actor(connection, actor_id, "admin", "operator")
            if kind not in REVISION_KINDS or kind in {"risk_reported", "control_lifted", "incident_closed"}:
                raise ValidationError("不支持的处置类型")
            self._validate_event_time(event_time)
            message_id = self._identifier(message_id, "message_id")
            incident = self._incident(connection, incident_id)

            def create():
                duplicate = connection.execute(
                    "SELECT * FROM relay_revisions WHERE incident_id=? AND message_id=?",
                    (incident_id, message_id),
                ).fetchone()
                if duplicate is not None:
                    raise ConflictError("重复消息：message_id 已存在于该事件时间线")
                now = self._now()
                if incident["status"] in TERMINAL_INCIDENT_STATUSES:
                    # 受保护终态：消息只留档，不改变状态
                    applied = False
                    ordering = "late"
                    reason = f"事件已处于受保护终态 {incident['status']}，迟到消息不得重新打开"
                else:
                    applied = True
                    reason = None
                    ordering = "in_order" if self._is_in_order(connection, incident_id, event_time) \
                        else "late"
                rev = self._append_revision(
                    connection, incident_id=incident_id, kind=kind, message_id=message_id,
                    event_time=event_time, actor_id=actor_id, payload=details,
                    ordering=ordering, applied=applied, not_applied_reason=reason,
                )
                if applied:
                    self._apply_side_effects(connection, incident=incident, kind=kind,
                                             actor=actor, details=details, revision=rev, now=now)
                    connection.execute(
                        "UPDATE relay_incidents SET head_revision_id=?, version=? WHERE incident_id=?",
                        (rev["revision_id"], self._incident_version(connection, incident_id), incident_id),
                    )
                append_event(connection, actor_id=actor_id, action=f"relay.{kind}",
                             resource_type="relay_revision", resource_id=rev["revision_id"],
                             detail={"incident_id": incident_id, "message_id": message_id,
                                     "applied": applied, "ordering": ordering,
                                     "not_applied_reason": reason},
                             occurred_at=now)
                return ("relay_revision", rev["revision_id"],
                        {"incident_id": incident_id, "revision_id": rev["revision_id"],
                         "revision_seq": rev["seq"], "applied": applied, "ordering": ordering,
                         "not_applied_reason": reason})

            return self._idempotent_impl(connection, request_id=request_id,
                                         action=f"relay.action.{kind}", payload=payload, create=create)

    def _is_in_order(self, connection, incident_id: str, event_time: str) -> bool:
        head_time = connection.execute(
            "SELECT event_time FROM relay_revisions WHERE incident_id=? AND applied=1 "
            "ORDER BY seq DESC LIMIT 1", (incident_id,),
        ).fetchone()
        return head_time is None or event_time >= head_time["event_time"]

    def _apply_side_effects(self, connection, *, incident: dict[str, Any], kind: str,
                            actor: dict[str, Any], details: dict[str, Any],
                            revision: dict[str, Any], now: str) -> None:
        incident_id = incident["incident_id"]
        if kind in {"road_closed", "road_controlled", "lane_closed"}:
            self._satisfy(connection, incident_id, suffix="control", revision=revision, now=now,
                          risk_id=details.get("risk_id"))
        elif kind == "equipment_occupied":
            self._satisfy(connection, incident_id, suffix="control", revision=revision, now=now,
                          risk_id=details.get("risk_id"))
        elif kind == "clearance_done":
            self._satisfy(connection, incident_id, suffix="clearance", revision=revision, now=now,
                          risk_id=details.get("risk_id"))
        elif kind == "crowd_guided":
            self._satisfy(connection, incident_id, suffix="guidance", revision=revision, now=now,
                          risk_id=details.get("risk_id"))
        elif kind == "resource_receipt":
            self._satisfy(connection, incident_id, suffix="resource", revision=revision, now=now,
                          risk_id=details.get("risk_id"))
        elif kind == "review_recorded":
            result = details.get("result")
            if result not in {"pass", "fail"}:
                raise ValidationError("复查 result 必须是 pass 或 fail")
            review_id = _new_id()
            note = str(details.get("note") or "").strip()
            if len(note) > 500:
                raise ValidationError("note 不能超过 500 个字符")
            connection.execute(
                "INSERT INTO relay_reviews(review_id,incident_id,revision_id,reviewer_id,result,note,"
                "created_at) VALUES(?,?,?,?,?,?,?)",
                (review_id, incident_id, revision["revision_id"], actor["actor_id"], result,
                 note, now),
            )
            if result == "pass":
                self._satisfy(connection, incident_id, suffix="review", revision=revision, now=now,
                              risk_id=details.get("risk_id"))

    def _satisfy(self, connection, incident_id: str, *, suffix: str,
                 revision: dict[str, Any], now: str, risk_id: str | None) -> None:
        if risk_id:
            keys = [f"{risk_id}:{suffix}"]
        else:
            rows = connection.execute(
                "SELECT condition_key FROM relay_conditions WHERE incident_id=? AND status='open' "
                "AND condition_key LIKE ?", (incident_id, f"%:{suffix}"),
            ).fetchall()
            keys = [row["condition_key"] for row in rows]
        for key in keys:
            # 复查失败回流重开的条件，不允许乱序迟到消息把它重新满足
            connection.execute(
                "UPDATE relay_conditions SET status='satisfied', satisfied_revision_id=?, satisfied_at=?, "
                "reopened_at=NULL WHERE incident_id=? AND condition_key=? AND status='open' "
                "AND (reopened_at IS NULL OR reopened_at <= ?)",
                (revision["revision_id"], now, incident_id, key, revision["event_time"]),
            )

    # -------------------------------------------------------------- 解除封控

    def lift_control(self, *, request_id: str, actor_id: str, incident_id: str, message_id: str,
                     event_time: str, note: str = "") -> dict[str, Any]:
        """独立复核者确认全部前置项后解除封控；迟到消息不能复活受保护终态。"""

        payload = {"actor_id": actor_id, "incident_id": incident_id, "message_id": message_id,
                   "event_time": event_time, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._require_actor(connection, actor_id, "reviewer", "admin")
            self._validate_event_time(event_time)
            message_id = self._identifier(message_id, "message_id")
            incident = self._incident(connection, incident_id)

            def create():
                duplicate = connection.execute(
                    "SELECT * FROM relay_revisions WHERE incident_id=? AND message_id=?",
                    (incident_id, message_id),
                ).fetchone()
                if duplicate is not None:
                    raise ConflictError("重复消息：message_id 已存在于该事件时间线")
                now = self._now()
                if incident["status"] in TERMINAL_INCIDENT_STATUSES:
                    return self._reject_late(connection, incident=incident, kind="control_lifted",
                                             message_id=message_id, event_time=event_time,
                                             actor_id=actor_id, details={"note": note}, now=now,
                                             reason=f"事件已处于受保护终态 {incident['status']}")
                if not self._is_in_order(connection, incident_id, event_time):
                    raise ConflictError("迟到的封控解除消息不被采纳，请在当前时间线重新发起")
                open_conditions = self._open_conditions(connection, incident_id)
                if open_conditions:
                    raise ConflictError(
                        "仍有前置条件未满足：" + ", ".join(c["condition_key"] for c in open_conditions)
                    )
                last_control_actor = self._last_control_actor(connection, incident_id)
                if last_control_actor is not None and actor["role"] != "admin" \
                        and actor["actor_id"] == last_control_actor:
                    raise PermissionDenied("封控解除必须由未执行该封控的独立复核者确认")
                rev = self._append_revision(
                    connection, incident_id=incident_id, kind="control_lifted",
                    message_id=message_id, event_time=event_time, actor_id=actor_id,
                    payload={"note": note, "open_conditions": []},
                    ordering="in_order", applied=True, not_applied_reason=None,
                )
                connection.execute(
                    "UPDATE relay_incidents SET status='resolved', head_revision_id=?, version=?,"
                    "closed_at=? WHERE incident_id=?",
                    (rev["revision_id"], self._incident_version(connection, incident_id), now, incident_id),
                )
                self._complete_leases(connection, incident_id, now)
                append_event(connection, actor_id=actor_id, action="relay.control_lifted",
                             resource_type="relay_incident", resource_id=incident_id,
                             detail={"message_id": message_id, "revision_id": rev["revision_id"],
                                     "independent_of": last_control_actor},
                             occurred_at=now)
                return ("relay_incident", incident_id,
                        {"incident_id": incident_id, "revision_id": rev["revision_id"],
                         "revision_seq": rev["seq"], "applied": True, "status": "resolved"})

            return self._idempotent_impl(connection, request_id=request_id, action="relay.lift_control",
                                         payload=payload, create=create)

    def close_incident(self, *, request_id: str, actor_id: str, incident_id: str, message_id: str,
                       event_time: str, note: str = "") -> dict[str, Any]:
        """负责人关闭已解除封控的事件，关闭后任何迟到消息都不能重开。"""

        payload = {"actor_id": actor_id, "incident_id": incident_id, "message_id": message_id,
                   "event_time": event_time, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._require_actor(connection, actor_id, "admin", "operator")
            self._validate_event_time(event_time)
            message_id = self._identifier(message_id, "message_id")
            incident = self._incident(connection, incident_id)

            def create():
                duplicate = connection.execute(
                    "SELECT * FROM relay_revisions WHERE incident_id=? AND message_id=?",
                    (incident_id, message_id),
                ).fetchone()
                if duplicate is not None:
                    raise ConflictError("重复消息：message_id 已存在于该事件时间线")
                now = self._now()
                if incident["status"] == "closed":
                    return self._reject_late(connection, incident=incident, kind="incident_closed",
                                             message_id=message_id, event_time=event_time,
                                             actor_id=actor_id, details={"note": note}, now=now,
                                             reason="事件已关闭")
                if incident["status"] != "resolved":
                    raise ConflictError("只有封控已解除的事件才能关闭")
                if not self._is_in_order(connection, incident_id, event_time):
                    raise ConflictError("迟到的关闭消息不被采纳")
                rev = self._append_revision(
                    connection, incident_id=incident_id, kind="incident_closed",
                    message_id=message_id, event_time=event_time, actor_id=actor_id,
                    payload={"note": note}, ordering="in_order", applied=True,
                    not_applied_reason=None,
                )
                connection.execute(
                    "UPDATE relay_incidents SET status='closed', head_revision_id=?, version=?,"
                    "closed_at=? WHERE incident_id=?",
                    (rev["revision_id"], self._incident_version(connection, incident_id), now, incident_id),
                )
                self._complete_leases(connection, incident_id, now)
                append_event(connection, actor_id=actor_id, action="relay.incident_closed",
                             resource_type="relay_incident", resource_id=incident_id,
                             detail={"message_id": message_id, "revision_id": rev["revision_id"]},
                             occurred_at=now)
                return ("relay_incident", incident_id,
                        {"incident_id": incident_id, "revision_id": rev["revision_id"],
                         "revision_seq": rev["seq"], "applied": True, "status": "closed"})

            return self._idempotent_impl(connection, request_id=request_id, action="relay.close_incident",
                                         payload=payload, create=create)

    def _reject_late(self, connection, *, incident: dict[str, Any], kind: str, message_id: str,
                     event_time: str, actor_id: str, details: dict[str, Any], now: str,
                     reason: str) -> tuple[str, str, dict[str, Any]]:
        rev = self._append_revision(
            connection, incident_id=incident["incident_id"], kind=kind, message_id=message_id,
            event_time=event_time, actor_id=actor_id, payload=details, ordering="late",
            applied=False, not_applied_reason=reason,
        )
        append_event(connection, actor_id=actor_id, action=f"relay.{kind}_rejected",
                     resource_type="relay_revision", resource_id=rev["revision_id"],
                     detail={"incident_id": incident["incident_id"], "reason": reason},
                     occurred_at=now)
        return ("relay_revision", rev["revision_id"],
                {"incident_id": incident["incident_id"], "revision_id": rev["revision_id"],
                 "revision_seq": rev["seq"], "applied": False, "ordering": "late",
                 "not_applied_reason": reason})

    def _last_control_actor(self, connection, incident_id: str) -> str | None:
        row = connection.execute(
            "SELECT actor_id FROM relay_revisions WHERE incident_id=? AND applied=1 "
            "AND kind IN ('road_closed','road_controlled','lane_closed') ORDER BY seq DESC LIMIT 1",
            (incident_id,),
        ).fetchone()
        return row["actor_id"] if row else None

    # ------------------------------------------------------------------ 租约

    def claim_lease(self, *, request_id: str, actor_id: str, incident_id: str,
                    ttl_seconds: int = LEASE_SECONDS_DEFAULT, note: str = "") -> dict[str, Any]:
        """值守人员领取会过期的责任租约。"""

        payload = {"actor_id": actor_id, "incident_id": incident_id,
                   "ttl_seconds": ttl_seconds, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._require_actor(connection, actor_id, "admin", "operator")
            ttl_seconds = int(ttl_seconds)
            if not 60 <= ttl_seconds <= LEASE_SECONDS_MAX:
                raise ValidationError("租约时长必须在 60 秒到 12 小时之间")
            incident = self._incident(connection, incident_id)
            if incident["status"] in TERMINAL_INCIDENT_STATUSES:
                raise ConflictError("事件已终结，不能再领取租约")

            def create():
                now = self._now()
                active = self._active_lease(connection, incident_id, now)
                if active is not None:
                    raise ConflictError(f"事件已有生效租约，持有者 {active['holder_id']}")
                lease_id = _new_id()
                expires_at = self._expires_at(ttl_seconds)
                connection.execute(
                    "INSERT INTO relay_leases(lease_id,incident_id,holder_id,status,claimed_at,"
                    "expires_at,note) VALUES(?,?,?, 'active', ?,?,?)",
                    (lease_id, incident_id, actor_id, now, expires_at, note),
                )
                append_event(connection, actor_id=actor_id, action="relay.lease_claimed",
                             resource_type="relay_lease", resource_id=lease_id,
                             detail={"incident_id": incident_id, "expires_at": expires_at},
                             occurred_at=now)
                return ("relay_lease", lease_id,
                        {"lease_id": lease_id, "incident_id": incident_id,
                         "holder_id": actor_id, "status": "active",
                         "claimed_at": now, "expires_at": expires_at,
                         "pending_conditions": [c["condition_key"] for c in
                                                self._open_conditions(connection, incident_id)]})

            return self._idempotent_impl(connection, request_id=request_id, action="relay.claim_lease",
                                         payload=payload, create=create)

    def transfer_lease(self, *, request_id: str, actor_id: str, incident_id: str,
                       to_actor_id: str, message_id: str, note: str = "") -> dict[str, Any]:
        """转交责任租约，必须附带尚未完成的条件；旧租约失效，新租约重新计时。"""

        payload = {"actor_id": actor_id, "incident_id": incident_id, "to_actor_id": to_actor_id,
                   "message_id": message_id, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._require_actor(connection, actor_id, "admin", "operator")
            recipient = self._require_actor(connection, to_actor_id, "admin", "operator")
            message_id = self._identifier(message_id, "message_id")
            incident = self._incident(connection, incident_id)
            if incident["status"] in TERMINAL_INCIDENT_STATUSES:
                raise ConflictError("事件已终结，不能转交租约")

            def create():
                now = self._now()
                active = self._active_lease(connection, incident_id, now)
                if active is None:
                    raise ConflictError("没有生效的责任租约可转交")
                if active["holder_id"] != actor_id and actor["role"] != "admin":
                    raise PermissionDenied("只有当前持有者可以转交租约")
                pending = self._open_conditions(connection, incident_id)
                if not pending:
                    raise ConflictError("没有尚未完成的条件，无需转交（请走解除封控流程）")
                new_lease_id = _new_id()
                expires_at = self._expires_at(LEASE_SECONDS_DEFAULT)
                connection.execute(
                    "UPDATE relay_leases SET status='transferred' WHERE lease_id=?",
                    (active["lease_id"],),
                )
                connection.execute(
                    "INSERT INTO relay_leases(lease_id,incident_id,holder_id,status,claimed_at,"
                    "expires_at,transferred_from_lease_id,note) VALUES(?,?,?, 'active', ?,?,?,?)",
                    (new_lease_id, incident_id, to_actor_id, now, expires_at,
                     active["lease_id"], note),
                )
                rev = self._append_revision(
                    connection, incident_id=incident_id, kind="note",
                    message_id=message_id, event_time=now, actor_id=actor_id,
                    payload={"transfer": {"from_lease_id": active["lease_id"],
                                          "from_holder": actor_id, "to_holder": to_actor_id},
                             "pending_conditions": [c["condition_key"] for c in pending],
                             "note": note},
                    ordering="in_order", applied=True, not_applied_reason=None,
                )
                connection.execute(
                    "UPDATE relay_incidents SET head_revision_id=?, version=? WHERE incident_id=?",
                    (rev["revision_id"], self._incident_version(connection, incident_id), incident_id),
                )
                append_event(connection, actor_id=actor_id, action="relay.lease_transferred",
                             resource_type="relay_lease", resource_id=new_lease_id,
                             detail={"incident_id": incident_id, "from_lease_id": active["lease_id"],
                                     "to_actor_id": to_actor_id,
                                     "pending_conditions": [c["condition_key"] for c in pending],
                                     "revision_id": rev["revision_id"]},
                             occurred_at=now)
                return ("relay_lease", new_lease_id,
                        {"lease_id": new_lease_id, "incident_id": incident_id,
                         "holder_id": to_actor_id, "status": "active",
                         "claimed_at": now, "expires_at": expires_at,
                         "transferred_from_lease_id": active["lease_id"],
                         "pending_conditions": [c["condition_key"] for c in pending]})

            return self._idempotent_impl(connection, request_id=request_id,
                                         action="relay.transfer_lease", payload=payload, create=create)

    def _active_lease(self, connection, incident_id: str, now: str) -> dict[str, Any] | None:
        self._expire_leases(connection, incident_id, now)
        row = connection.execute(
            "SELECT * FROM relay_leases WHERE incident_id=? AND status='active'", (incident_id,)
        ).fetchone()
        return dict(row) if row else None

    def _expire_leases(self, connection, incident_id: str, now: str) -> int:
        rows = connection.execute(
            "SELECT * FROM relay_leases WHERE incident_id=? AND status='active' AND expires_at<=?",
            (incident_id, now),
        ).fetchall()
        for row in rows:
            connection.execute("UPDATE relay_leases SET status='expired' WHERE lease_id=?", (row["lease_id"],))
            append_event(connection, actor_id="system", action="relay.lease_expired",
                         resource_type="relay_lease", resource_id=row["lease_id"],
                         detail={"incident_id": incident_id, "holder_id": row["holder_id"]},
                         occurred_at=now)
        return len(rows)

    def _complete_leases(self, connection, incident_id: str, now: str) -> int:
        """事件到达受保护终态时终止生效租约。"""

        rows = connection.execute(
            "SELECT * FROM relay_leases WHERE incident_id=? AND status='active'", (incident_id,)
        ).fetchall()
        for row in rows:
            connection.execute("UPDATE relay_leases SET status='completed' WHERE lease_id=?",
                               (row["lease_id"],))
            append_event(connection, actor_id="system", action="relay.lease_completed",
                         resource_type="relay_lease", resource_id=row["lease_id"],
                         detail={"incident_id": incident_id, "holder_id": row["holder_id"]},
                         occurred_at=now)
        return len(rows)

    def _expires_at(self, ttl_seconds: int) -> str:
        from datetime import timedelta
        return (self.clock.now() + timedelta(seconds=ttl_seconds)).isoformat().replace("+00:00", "Z")

    # ------------------------------------------------------------------ 资源

    def plan_allocation(self, *, request_id: str, actor_id: str, incident_id: str,
                        demands: list[dict[str, Any]], reason: str) -> dict[str, Any]:
        """资源不足时形成带理由的候选调配方案（不产生任何占用）。"""

        payload = {"actor_id": actor_id, "incident_id": incident_id,
                   "demands": demands, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._require_actor(connection, actor_id, "admin", "operator")
            incident = self._incident(connection, incident_id)
            if incident["status"] in TERMINAL_INCIDENT_STATUSES:
                raise ConflictError("事件已终结，不能再调配资源")
            demands = self._validate_demands(demands)
            reason = self._text(reason, "reason", 500)

            def create():
                now = self._now()
                items = self._build_candidates(connection, incident=incident, demands=demands)
                proposal_id = _new_id()
                base_version = self._incident_version(connection, incident_id)
                feasible = all(item["source"] != "shortage" for item in items)
                connection.execute(
                    "INSERT INTO relay_proposals(proposal_id,incident_id,site_id,demands_json,reason,"
                    "status,base_version,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (proposal_id, incident_id, incident["site_id"], canonical_json(demands), reason,
                     "proposed" if feasible else "infeasible", base_version, actor_id, now),
                )
                for index, item in enumerate(items):
                    connection.execute(
                        "INSERT INTO relay_proposal_items(proposal_id,item_index,resource_key,"
                        "resource_type,source,source_incident_id,reason) VALUES(?,?,?,?,?,?,?)",
                        (proposal_id, index, item["resource_key"], item["resource_type"],
                         item["source"], item.get("source_incident_id"), item["reason"]),
                    )
                append_event(connection, actor_id=actor_id, action="relay.allocation_planned",
                             resource_type="relay_proposal", resource_id=proposal_id,
                             detail={"incident_id": incident_id, "feasible": feasible,
                                     "item_count": len(items), "base_version": base_version},
                             occurred_at=now)
                return ("relay_proposal", proposal_id,
                        {"proposal_id": proposal_id, "incident_id": incident_id,
                         "status": "proposed" if feasible else "infeasible",
                         "base_version": base_version, "reason": reason,
                         "items": [{"resource_key": item["resource_key"],
                                    "resource_type": item["resource_type"],
                                    "source": item["source"],
                                    "source_incident_id": item.get("source_incident_id"),
                                    "reason": item["reason"]} for item in items]})

            return self._idempotent_impl(connection, request_id=request_id,
                                         action="relay.plan_allocation", payload=payload, create=create)

    def _build_candidates(self, connection, *, incident: dict[str, Any],
                          demands: list[dict[str, Any]]) -> list[dict[str, Any]]:
        site_id = incident["site_id"]
        incident_id = incident["incident_id"]
        items: list[dict[str, Any]] = []
        for demand in demands:
            key = demand["resource_key"]
            rtype = demand["resource_type"]
            quantity = demand.get("quantity", 1)
            active = connection.execute(
                "SELECT * FROM relay_allocations WHERE site_id=? AND resource_key=? "
                "AND status IN ('reserved','occupied')", (site_id, key),
            ).fetchall()
            held_here = any(row["incident_id"] == incident_id for row in active)
            free_needed = quantity
            if held_here:
                items.append({"resource_key": key, "resource_type": rtype, "source": "free",
                              "reason": "本事件已持有该资源，无需重复占用"})
                continue
            if not active:
                items.append({"resource_key": key, "resource_type": rtype, "source": "free",
                              "reason": f"场所内 {key} 当前空闲，可直接预留"})
                continue
            # 资源被占用：寻找持有者是否已解除封控（可优先借用），否则标记缺口
            donor = None
            for row in active:
                holder = connection.execute(
                    "SELECT status FROM relay_incidents WHERE incident_id=?", (row["incident_id"],)
                ).fetchone()
                if holder is not None and holder["status"] in TERMINAL_INCIDENT_STATUSES:
                    donor = dict(row)
                    break
            if donor is not None:
                items.append({"resource_key": key, "resource_type": rtype, "source": "borrow",
                              "source_incident_id": donor["incident_id"],
                              "reason": f"{key} 被 {donor['incident_id']} 占用但该现场已终结，可回收调配"})
            else:
                holders = ", ".join(sorted({row["incident_id"] for row in active}))
                items.append({"resource_key": key, "resource_type": rtype, "source": "shortage",
                              "reason": f"{key} 被未结事件占用（{holders}），场内无空闲资源，需外部增援"})
        return items

    def confirm_allocation(self, *, request_id: str, actor_id: str, proposal_id: str,
                           expected_version: int) -> dict[str, Any]:
        """负责人按状态版本整体确认候选方案；任一资源无法锁定则全部不占用。"""

        payload = {"actor_id": actor_id, "proposal_id": proposal_id,
                   "expected_version": expected_version}
        with self.database.transaction(immediate=True) as connection:
            actor = self._require_actor(connection, actor_id, "admin", "operator")
            proposal_row = connection.execute(
                "SELECT * FROM relay_proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if proposal_row is None:
                raise NotFoundError("调配方案不存在")
            proposal = dict(proposal_row)
            item_rows = connection.execute(
                "SELECT * FROM relay_proposal_items WHERE proposal_id=? ORDER BY item_index",
                (proposal_id,),
            ).fetchall()

            def create():
                now = self._now()
                incident_id = proposal["incident_id"]
                incident = self._incident(connection, incident_id)
                if proposal["status"] != "proposed":
                    raise ConflictError(f"方案状态为 {proposal['status']}，不能确认")
                current_version = self._incident_version(connection, incident_id)
                if current_version != int(expected_version):
                    raise ConflictError(
                        f"状态版本冲突：方案基于版本 {proposal['base_version']}，期望 {expected_version}，"
                        f"当前为 {current_version}"
                    )
                # 原子地整体锁定：唯一部分索引保证不会出现双重占用；
                # 任何一行失败，整笔事务回滚，不留部分占用。
                allocation_ids: list[str] = []
                for row in item_rows:
                    item = dict(row)
                    if item["source"] == "shortage":
                        raise ConflictError(f"资源 {item['resource_key']} 仍无来源，不能整体确认")
                    allocation_id = _new_id()
                    if item["source"] == "borrow" and item["source_incident_id"]:
                        donor = connection.execute(
                            "SELECT status FROM relay_incidents WHERE incident_id=?",
                            (item["source_incident_id"],),
                        ).fetchone()
                        if donor is None or donor["status"] not in TERMINAL_INCIDENT_STATUSES:
                            raise ConflictError(
                                f"借用来源 {item['source_incident_id']} 已不在终态，方案需重新规划")
                        connection.execute(
                            "UPDATE relay_allocations SET status='preempted', released_at=?, "
                            "release_reason='回收调配给更高优先级现场' WHERE site_id=? AND resource_key=? "
                            "AND status IN ('reserved','occupied') AND incident_id=?",
                            (now, proposal["site_id"], item["resource_key"], item["source_incident_id"]),
                        )
                    try:
                        connection.execute(
                            "INSERT INTO relay_allocations(allocation_id,site_id,resource_key,incident_id,"
                            "status,proposal_id,created_by,created_at) "
                            "VALUES(?,?,?,?,'reserved',?,?,?)",
                            (allocation_id, proposal["site_id"], item["resource_key"], incident_id,
                             proposal_id, actor_id, now),
                        )
                    except Exception as exc:  # 唯一部分索引冲突
                        raise ConflictError(
                            f"资源 {item['resource_key']} 已被其他事务锁定，本次确认整体作废"
                        ) from exc
                    allocation_ids.append(allocation_id)
                connection.execute(
                    "UPDATE relay_proposals SET status='confirmed' WHERE proposal_id=?", (proposal_id,)
                )
                append_event(connection, actor_id=actor_id, action="relay.allocation_confirmed",
                             resource_type="relay_proposal", resource_id=proposal_id,
                             detail={"incident_id": incident_id, "allocations": allocation_ids,
                                     "version": current_version},
                             occurred_at=now)
                return ("relay_proposal", proposal_id,
                        {"proposal_id": proposal_id, "status": "confirmed",
                         "incident_id": incident_id, "version": current_version,
                         "allocations": allocation_ids})

            return self._idempotent_impl(connection, request_id=request_id,
                                         action="relay.confirm_allocation", payload=payload, create=create)

    def acknowledge_resource(self, *, request_id: str, actor_id: str, allocation_id: str,
                             message_id: str, event_time: str,
                             details: dict[str, Any] | None = None) -> dict[str, Any]:
        """现场资源到位回执：预留转为实际占用，并满足对应资源条件。"""

        details = details or {}
        payload = {"actor_id": actor_id, "allocation_id": allocation_id,
                   "message_id": message_id, "event_time": event_time, "details": details}
        with self.database.transaction(immediate=True) as connection:
            actor = self._require_actor(connection, actor_id, "admin", "operator")
            message_id = self._identifier(message_id, "message_id")
            self._validate_event_time(event_time)
            row = connection.execute(
                "SELECT * FROM relay_allocations WHERE allocation_id=?", (allocation_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("资源占用记录不存在")
            allocation = dict(row)
            incident = self._incident(connection, allocation["incident_id"])

            def create():
                now = self._now()
                if allocation["status"] != "reserved":
                    raise ConflictError(f"占用记录状态为 {allocation['status']}，不能回执")
                connection.execute(
                    "UPDATE relay_allocations SET status='occupied' WHERE allocation_id=?",
                    (allocation_id,),
                )
                terminal = incident["status"] in TERMINAL_INCIDENT_STATUSES
                rev = self._append_revision(
                    connection, incident_id=incident["incident_id"], kind="resource_receipt",
                    message_id=message_id, event_time=event_time, actor_id=actor_id,
                    payload={"allocation_id": allocation_id,
                             "resource_key": allocation["resource_key"], **details},
                    ordering="late" if terminal else (
                        "in_order" if self._is_in_order(
                            connection, incident["incident_id"], event_time) else "late"),
                    applied=not terminal,
                    not_applied_reason="事件已终结，迟到回执仅留档" if terminal else None,
                )
                if not terminal:
                    self._satisfy(connection, incident["incident_id"], suffix="resource",
                                  revision=rev, now=now, risk_id=details.get("risk_id"))
                    connection.execute(
                        "UPDATE relay_incidents SET head_revision_id=?, version=? WHERE incident_id=?",
                        (rev["revision_id"],
                         self._incident_version(connection, incident["incident_id"]),
                         incident["incident_id"]),
                    )
                append_event(connection, actor_id=actor_id, action="relay.resource_acknowledged",
                             resource_type="relay_allocation", resource_id=allocation_id,
                             detail={"incident_id": incident["incident_id"],
                                     "resource_key": allocation["resource_key"],
                                     "revision_id": rev["revision_id"]},
                             occurred_at=now)
                return ("relay_allocation", allocation_id,
                        {"allocation_id": allocation_id, "status": "occupied",
                         "incident_id": incident["incident_id"],
                         "resource_key": allocation["resource_key"],
                         "revision_id": rev["revision_id"]})

            return self._idempotent_impl(connection, request_id=request_id,
                                         action="relay.acknowledge_resource", payload=payload,
                                         create=create)

    # -------------------------------------------------------------- 可解释查询

    def incident_timeline(self, incident_id: str) -> dict[str, Any]:
        """返回道路状态、条件、租约、资源以及每个决定所依据的修订证据。"""

        connection = self.database.connection
        incident = self._incident(connection, incident_id)
        revisions = []
        for row in connection.execute(
            "SELECT * FROM relay_revisions WHERE incident_id=? ORDER BY seq", (incident_id,)
        ):
            item = dict(row)
            item["payload"] = _json(item.pop("payload_json"))
            item.pop("payload_hash", None)
            evidence = self._evidence_for(connection, item)
            item["evidence"] = evidence
            revisions.append(item)
        conditions = []
        for condition in self._conditions(connection, incident_id):
            evidence = None
            if condition["satisfied_revision_id"]:
                rev = self._revision(connection, condition["satisfied_revision_id"])
                evidence = {"revision_id": rev["revision_id"], "seq": rev["seq"],
                            "kind": rev["kind"], "message_id": rev["message_id"],
                            "event_time": rev["event_time"], "actor_id": rev["actor_id"]}
            conditions.append({**condition, "evidence": evidence})
        now = self._now()
        active_lease = self._active_lease(connection, incident_id, now)
        lease_info = None
        if active_lease is not None:
            lease_info = {k: active_lease[k] for k in
                          ("lease_id", "holder_id", "status", "claimed_at", "expires_at")}
            lease_info["pending_conditions"] = [c["condition_key"] for c in conditions
                                                if c["status"] == "open"]
        allocations = []
        for row in connection.execute(
            "SELECT * FROM relay_allocations WHERE incident_id=? ORDER BY created_at", (incident_id,)
        ):
            alloc = dict(row)
            alloc.pop("proposal_id", None)
            alloc["evidence"] = {"proposal_id": row["proposal_id"]}
            allocations.append(alloc)
        decision_log = self._decision_log(connection, incident_id)
        chain_ok, chain_count = self._verify_revision_chain(connection, incident_id)
        return {
            "incident_id": incident_id,
            "site_id": incident["site_id"],
            "road_code": incident["road_code"],
            "title": incident["title"],
            "priority": incident["priority"],
            "road_status": incident["status"],
            "version": incident["version"],
            "created_at": incident["created_at"],
            "closed_at": incident["closed_at"],
            "revision_chain_valid": chain_ok,
            "revision_count": chain_count,
            "conditions": conditions,
            "active_lease": lease_info,
            "allocations": allocations,
            "timeline": revisions,
            "decisions": decision_log,
            "explanation": self._explain(incident, conditions, lease_info, allocations, revisions),
        }

    def _evidence_for(self, connection, revision: dict[str, Any]) -> dict[str, Any]:
        evidence: dict[str, Any] = {
            "revision_id": revision["revision_id"],
            "seq": revision["seq"],
            "prev_hash": revision["prev_hash"],
            "revision_hash": revision["revision_hash"],
            "message_id": revision["message_id"],
            "event_time": revision["event_time"],
            "received_at": revision["received_at"],
            "actor_id": revision["actor_id"],
        }
        if revision["kind"] == "review_recorded":
            row = connection.execute(
                "SELECT review_id,result,note FROM relay_reviews WHERE revision_id=?",
                (revision["revision_id"],),
            ).fetchone()
            if row:
                evidence["review"] = dict(row)
        if revision["kind"] == "resource_receipt":
            payload = revision["payload"]
            alloc_id = payload.get("allocation_id")
            if alloc_id:
                row = connection.execute(
                    "SELECT allocation_id,resource_key,status,proposal_id FROM relay_allocations "
                    "WHERE allocation_id=?", (alloc_id,),
                ).fetchone()
                if row:
                    evidence["allocation"] = dict(row)
        if revision["kind"] == "note" and isinstance(revision["payload"], dict) \
                and revision["payload"].get("transfer"):
            evidence["transfer"] = revision["payload"]["transfer"]
        return evidence

    def _decision_log(self, connection, incident_id: str) -> list[dict[str, Any]]:
        """汇总影响状态的关键决定及其证据。"""

        decisions: list[dict[str, Any]] = []
        for row in connection.execute(
            "SELECT * FROM relay_revisions WHERE incident_id=? AND applied=1 "
            "AND kind IN ('risk_reported','road_closed','road_controlled','lane_closed',"
            "'control_lifted','incident_closed') ORDER BY seq", (incident_id,),
        ):
            rev = dict(row)
            decisions.append({
                "decision": rev["kind"],
                "evidence": {"revision_id": rev["revision_id"], "seq": rev["seq"],
                             "message_id": rev["message_id"], "event_time": rev["event_time"],
                             "actor_id": rev["actor_id"], "revision_hash": rev["revision_hash"]},
            })
        # 封控解除的独立性证据
        lifted = connection.execute(
            "SELECT * FROM relay_revisions WHERE incident_id=? AND kind='control_lifted' AND applied=1",
            (incident_id,),
        ).fetchall()
        if lifted:
            control_actor = self._last_control_actor(connection, incident_id)
            for row in lifted:
                decisions.append({
                    "decision": "independent_review_for_lift",
                    "evidence": {"lift_revision_id": row["revision_id"],
                                 "reviewer_id": row["actor_id"],
                                 "control_actor_id": control_actor,
                                 "independent": control_actor != row["actor_id"]},
                })
        # 被拒绝的迟到/终态消息也要可解释
        for row in connection.execute(
            "SELECT * FROM relay_revisions WHERE incident_id=? AND applied=0 ORDER BY seq",
            (incident_id,),
        ):
            rev = dict(row)
            decisions.append({
                "decision": "message_rejected",
                "evidence": {"revision_id": rev["revision_id"], "message_id": rev["message_id"],
                             "kind": rev["kind"], "event_time": rev["event_time"],
                             "reason": rev["not_applied_reason"]},
            })
        return decisions

    def _explain(self, incident, conditions, lease_info, allocations, revisions) -> dict[str, Any]:
        open_conditions = [c["condition_key"] for c in conditions if c["status"] == "open"]
        applied = [r for r in revisions if r["applied"]]
        rejected = [r for r in revisions if not r["applied"]]
        latest = applied[-1] if applied else None
        return {
            "road_state": incident["status"],
            "road_state_reason": self._road_state_reason(incident["status"], latest),
            "can_lift_control": incident["status"] == "open" and not open_conditions,
            "blocking_conditions": open_conditions,
            "resource_summary": [
                {"resource_key": a["resource_key"], "status": a["status"],
                 "allocation_id": a["allocation_id"]} for a in allocations
                if a["status"] in ("reserved", "occupied")],
            "responsibility": lease_info,
            "late_messages_ignored": [
                {"message_id": r["message_id"], "kind": r["kind"], "reason": r["not_applied_reason"]}
                for r in rejected],
        }

    @staticmethod
    def _road_state_reason(status: str, latest) -> str:
        if latest is None:
            return "尚无已采纳修订"
        if status == "open":
            return f"现场仍在处置，最近采纳修订：{latest['kind']}（{latest['message_id']}）"
        if status == "resolved":
            return "独立复核者确认全部前置条件后已解除封控"
        if status == "closed":
            return "事件已关闭，受保护终态不再被迟到消息改变"
        return status

    def resource_trace(self, site_id: str) -> dict[str, Any]:
        """解释场所内每项资源的去向。"""

        connection = self.database.connection
        items = []
        rows = connection.execute(
            "SELECT resource_key, MIN(created_at) AS first_seen FROM relay_allocations "
            "WHERE site_id=? GROUP BY resource_key ORDER BY resource_key", (site_id,),
        ).fetchall()
        now = self._now()
        for row in rows:
            key = row["resource_key"]
            current = connection.execute(
                "SELECT * FROM relay_allocations WHERE site_id=? AND resource_key=? "
                "AND status IN ('reserved','occupied')", (site_id, key),
            ).fetchall()
            history = []
            for hrow in connection.execute(
                "SELECT allocation_id,incident_id,status,proposal_id,created_at,released_at,"
                "release_reason FROM relay_allocations WHERE site_id=? AND resource_key=? ORDER BY created_at",
                (site_id, key),
            ):
                history.append(dict(hrow))
            if current:
                holder = current[0]
                destination = {"state": holder["status"], "incident_id": holder["incident_id"],
                               "allocation_id": holder["allocation_id"],
                               "proposal_id": holder["proposal_id"]}
            else:
                destination = {"state": "free"}
            items.append({"resource_key": key, "destination": destination,
                          "history": history, "as_of": now})
        return {"site_id": site_id, "as_of": now, "resources": items}

    def _verify_revision_chain(self, connection, incident_id: str) -> tuple[bool, int]:
        previous = "0" * 64
        count = 0
        for row in connection.execute(
            "SELECT * FROM relay_revisions WHERE incident_id=? ORDER BY seq", (incident_id,)
        ):
            material = {
                "incident_id": row["incident_id"], "kind": row["kind"],
                "message_id": row["message_id"], "event_time": row["event_time"],
                "actor_id": row["actor_id"], "payload": _json(row["payload_json"]),
                "prev_hash": row["prev_hash"],
            }
            if row["prev_hash"] != previous or digest(material) != row["revision_hash"]:
                return False, count
            previous = row["revision_hash"]
            count += 1
        return True, count

    def reopen_after_review(self, *, request_id: str, actor_id: str, incident_id: str,
                            message_id: str, event_time: str, failed_conditions: list[str],
                            note: str = "") -> dict[str, Any]:
        """复查失败后的显式回流（重开条件），区别于被拒绝的迟到消息。"""

        payload = {"actor_id": actor_id, "incident_id": incident_id, "message_id": message_id,
                   "event_time": event_time, "failed_conditions": failed_conditions, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._require_actor(connection, actor_id, "reviewer", "admin")
            message_id = self._identifier(message_id, "message_id")
            self._validate_event_time(event_time)
            incident = self._incident(connection, incident_id)

            def create():
                now = self._now()
                if incident["status"] == "closed":
                    raise ConflictError("已关闭事件不能回流，须新建风险上报")
                reopened: list[str] = []
                for key in failed_conditions:
                    cursor = connection.execute(
                        "UPDATE relay_conditions SET status='open', satisfied_revision_id=NULL, "
                        "satisfied_at=NULL, reopened_at=? WHERE incident_id=? AND condition_key=? "
                        "AND status='satisfied'",
                        (event_time, incident_id, key),
                    )
                    if cursor.rowcount:
                        reopened.append(key)
                if not reopened:
                    raise ConflictError("没有可回流的已满足条件")
                rev = self._append_revision(
                    connection, incident_id=incident_id, kind="review_recorded",
                    message_id=message_id, event_time=event_time, actor_id=actor_id,
                    payload={"result": "fail", "reopened_conditions": reopened, "note": note},
                    ordering="in_order", applied=True, not_applied_reason=None,
                )
                connection.execute(
                    "INSERT INTO relay_reviews(review_id,incident_id,revision_id,reviewer_id,result,note,"
                    "created_at) VALUES(?,?,?,?,?,?,?)",
                    (_new_id(), incident_id, rev["revision_id"], actor_id, "fail",
                     str(note or "")[:500], now),
                )
                if incident["status"] == "resolved":
                    connection.execute(
                        "UPDATE relay_incidents SET status='open' WHERE incident_id=?", (incident_id,)
                    )
                connection.execute(
                    "UPDATE relay_incidents SET head_revision_id=?, version=? WHERE incident_id=?",
                    (rev["revision_id"], self._incident_version(connection, incident_id), incident_id),
                )
                append_event(connection, actor_id=actor_id, action="relay.review_reopened",
                             resource_type="relay_incident", resource_id=incident_id,
                             detail={"revision_id": rev["revision_id"], "reopened": reopened},
                             occurred_at=now)
                return ("relay_revision", rev["revision_id"],
                        {"incident_id": incident_id, "revision_id": rev["revision_id"],
                         "revision_seq": rev["seq"], "applied": True,
                         "reopened_conditions": reopened, "status": "open"})

            return self._idempotent_impl(connection, request_id=request_id,
                                         action="relay.reopen_after_review", payload=payload,
                                         create=create)

    # -------------------------------------------------------------- 校验工具

    def _identifier(self, value: str, field: str) -> str:
        import re
        value = str(value).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{1,127}", value):
            raise ValidationError(f"{field} 格式无效")
        return value

    @staticmethod
    def _text(value: str, field: str, limit: int = 200) -> str:
        value = str(value or "").strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    @staticmethod
    def _validate_event_time(value: str) -> str:
        from datetime import datetime
        value = str(value).strip()
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("event_time 必须是 ISO 8601 时间") from exc
        return value

    def _validate_risks(self, risks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(risks, list) or not risks:
            raise ValidationError("risks 必须是非空数组")
        seen: set[str] = set()
        normalized = []
        for risk in risks:
            if not isinstance(risk, dict):
                raise ValidationError("风险项必须是对象")
            risk_id = self._identifier(str(risk.get("risk_id", "")), "risk_id")
            if risk_id in seen:
                raise ValidationError(f"风险编号重复：{risk_id}")
            seen.add(risk_id)
            description = self._text(str(risk.get("description", "")), "description", 500)
            rtype = str(risk.get("type", "generic")).strip()
            normalized.append({
                "risk_id": risk_id,
                "type": rtype,
                "description": description,
                "required_actions": list(risk.get("required_actions") or []),
                "needs_equipment": bool(risk.get("needs_equipment", False)),
                "needs_review": bool(risk.get("needs_review", True)),
                "location": str(risk.get("location", "")).strip(),
            })
        return normalized

    def _validate_demands(self, demands: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(demands, list) or not demands:
            raise ValidationError("demands 必须是非空数组")
        normalized = []
        seen: set[str] = set()
        for demand in demands:
            if not isinstance(demand, dict):
                raise ValidationError("资源需求必须是对象")
            key = self._identifier(str(demand.get("resource_key", "")), "resource_key")
            if key in seen:
                raise ValidationError(f"资源需求重复：{key}")
            seen.add(key)
            rtype = self._text(str(demand.get("resource_type", "")), "resource_type", 80)
            quantity = int(demand.get("quantity", 1))
            if quantity < 1:
                raise ValidationError("quantity 必须为正整数")
            normalized.append({"resource_key": key, "resource_type": rtype, "quantity": quantity})
        return normalized


def _json(value: str) -> Any:
    import json
    return json.loads(value)

"""交通干线节日保障接力台账。

在基础层的机构、场所、角色、SQLite 事务与哈希审计之上，实现：

- 结构化风险上报、控制措施、资源回执与复查结果的时间线归并；
- 乱序/迟到/重复消息判定，以及不可覆盖、只可追加的修订链；
- 会过期的责任租约，转交时携带尚未完成的条件；
- 独立复核确认全部前置项后才能解除封控，受保护终态不被迟到消息重开；
- 资源不足时带理由的候选调配，负责人按状态版本整体确认，失败不留部分占用；
- 可解释的道路状态、资源去向与决策证据查询。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import WriteReceipt

ENTRY_TYPES = frozenset({"risk_report", "control_measure", "resource_receipt", "review_result"})
CONTROL_KINDS = frozenset({"road_closure", "lane_closure", "ramp_closure", "device_occupation"})
RECEIPT_ACTIONS = frozenset({"arrived", "occupied", "released", "cleared"})
REVIEW_RESULTS = frozenset({"confirmed", "rejected"})

INCIDENT_OPEN = "open"
INCIDENT_CONTROLLING = "controlling"
INCIDENT_CLOSED = "closed"
PROTECTED_TERMINAL_STATES = frozenset({INCIDENT_CLOSED})

DEFAULT_LEASE_TTL_SECONDS = 1800
MAX_LEASE_TTL_SECONDS = 4 * 3600


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_event_time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("event_time 必须是 ISO 8601 时间字符串")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("event_time 时间格式无效") from exc
    if parsed.tzinfo is None:
        raise ValidationError("event_time 必须包含时区")
    return parsed.astimezone(timezone.utc)


class TrafficRelayService:
    """交通干线接力台账的领域服务。"""

    def __init__(self, database, clock=None) -> None:
        self.database = database
        if clock is None:
            from .clock import SystemClock

            clock = SystemClock()
        self.clock = clock

    def _now(self) -> datetime:
        return self.clock.now().astimezone(timezone.utc)

    def _now_iso(self) -> str:
        return _iso(self._now())

    # ----- 基础校验 -----------------------------------------------------

    def _load_actor(self, connection, actor_id: str) -> dict[str, Any]:
        if not actor_id:
            raise PermissionDenied("缺少 X-Actor-Id")
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = dict(row)
        if not actor["active"]:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require(self, actor: dict[str, Any], *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _load_site(self, connection, site_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return dict(row)

    def _check_site_scope(self, actor: dict[str, Any], site: dict[str, Any]) -> None:
        if actor["role"] != "admin" and actor["organization_id"] != site["organization_id"]:
            raise PermissionDenied("不能操作其他组织场所下的接力台账")

    def _load_incident(self, connection, incident_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM traffic_incidents WHERE incident_id=?", (incident_id,)).fetchone()
        if row is None:
            raise NotFoundError("交通事件不存在")
        return dict(row)

    def _idempotent(self, connection, *, request_id: str, action: str, payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]) -> WriteReceipt:
        request_id = str(request_id).strip()
        if not request_id:
            raise ValidationError("request_id 不能为空")
        payload_hash = digest(payload)
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now_iso()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False)

    # ----- 修订链 -------------------------------------------------------

    def _watermark(self, connection, incident_id: str) -> datetime | None:
        row = connection.execute(
            "SELECT MAX(event_time) AS latest FROM traffic_entries WHERE incident_id=?", (incident_id,)
        ).fetchone()
        if row is None or not row["latest"]:
            return None
        return datetime.fromisoformat(row["latest"].replace("Z", "+00:00"))

    def _append_entry(self, connection, incident: dict[str, Any], *, message_id: str, entry_type: str,
                      event_time: datetime, actor_id: str, payload: dict[str, Any],
                      disposition: str, late: bool, note: str = "") -> dict[str, Any]:
        row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS last FROM traffic_entries WHERE incident_id=?",
            (incident["incident_id"],),
        ).fetchone()
        sequence = row["last"] + 1
        head = connection.execute(
            "SELECT entry_hash FROM traffic_entries WHERE incident_id=? ORDER BY sequence DESC LIMIT 1",
            (incident["incident_id"],),
        ).fetchone()
        previous_hash = head["entry_hash"] if head else "0" * 64
        payload_hash = digest(payload)
        material = {
            "incident_id": incident["incident_id"],
            "sequence": sequence,
            "message_id": message_id,
            "entry_type": entry_type,
            "event_time": _iso(event_time),
            "actor_id": actor_id,
            "payload_hash": payload_hash,
            "previous_hash": previous_hash,
        }
        entry_hash = digest(material)
        connection.execute(
            "INSERT INTO traffic_entries(incident_id,sequence,message_id,entry_type,event_time,received_at,"
            "actor_id,payload_json,payload_hash,disposition,late,note,previous_hash,entry_hash) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (incident["incident_id"], sequence, message_id, entry_type, _iso(event_time),
             self._now_iso(), actor_id, canonical_json(payload), payload_hash, disposition,
             1 if late else 0, note, previous_hash, entry_hash),
        )
        connection.execute(
            "UPDATE traffic_incidents SET head_hash=?, updated_at=? WHERE incident_id=?",
            (entry_hash, self._now_iso(), incident["incident_id"]),
        )
        incident["head_hash"] = entry_hash
        return {"sequence": sequence, "message_id": message_id, "entry_hash": entry_hash,
                "payload_hash": payload_hash, "disposition": disposition, "late": late}

    def _bump_version(self, connection, incident: dict[str, Any]) -> int:
        incident["state_version"] += 1
        connection.execute(
            "UPDATE traffic_incidents SET state_version=?, updated_at=? WHERE incident_id=?",
            (incident["state_version"], self._now_iso(), incident["incident_id"]),
        )
        return incident["state_version"]

    # ----- 风险上报 -----------------------------------------------------

    def report_risk(self, *, request_id: str, actor_id: str, site_id: str, scene_key: str,
                    title: str, event_time: str, message_id: str, incident_id: str | None = None,
                    details: dict[str, Any] | None = None) -> WriteReceipt:
        details = details or {}
        if not isinstance(details, dict):
            raise ValidationError("details 必须是对象")
        payload = {"actor_id": actor_id, "site_id": site_id, "scene_key": scene_key, "title": title,
                   "event_time": event_time, "message_id": message_id, "incident_id": incident_id,
                   "details": details}
        event_dt = _parse_event_time(event_time)
        scene_key = str(scene_key).strip()
        if not scene_key:
            raise ValidationError("scene_key 不能为空")
        message_id = str(message_id).strip()
        if not message_id:
            raise ValidationError("message_id 不能为空")
        title = str(title).strip()
        if not title or len(title) > 200:
            raise ValidationError("title 不能为空且不能超过 200 个字符")
        with self.database.transaction(immediate=True) as connection:
            actor = self._load_actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            site = self._load_site(connection, site_id)
            self._check_site_scope(actor, site)
            incident_id = incident_id or f"inc-{uuid.uuid4().hex[:12]}"

            def create() -> tuple[str, str, dict[str, Any]]:
                active = connection.execute(
                    "SELECT incident_id FROM traffic_incidents WHERE site_id=? AND scene_key=? "
                    "AND status!=? ORDER BY created_at",
                    (site_id, scene_key, INCIDENT_CLOSED),
                ).fetchall()
                if active:
                    raise ConflictError(
                        f"同一现场已有进行中的接力事件 {active[0]['incident_id']}，请向该事件追加消息"
                    )
                now = self._now_iso()
                try:
                    connection.execute(
                        "INSERT INTO traffic_incidents(incident_id,site_id,scene_key,title,status,state_version,"
                        "head_hash,created_by,created_at,updated_at) VALUES(?,?,?,?,?,1,?,?,?,?)",
                        (incident_id, site_id, scene_key, title, INCIDENT_OPEN, "0" * 64, actor_id, now, now),
                    )
                except Exception as exc:
                    raise ConflictError("事件编号已经存在") from exc
                incident = self._load_incident(connection, incident_id)
                entry = self._append_entry(connection, incident, message_id=message_id,
                                          entry_type="risk_report", event_time=event_dt,
                                          actor_id=actor_id, payload={"title": title, "details": details},
                                          disposition="accepted", late=False)
                connection.execute(
                    "UPDATE traffic_incidents SET head_hash=? WHERE incident_id=?",
                    (entry["entry_hash"], incident_id),
                )
                append_event(connection, actor_id=actor_id, action="traffic.risk_reported",
                             resource_type="traffic_incident", resource_id=incident_id,
                             detail={"site_id": site_id, "scene_key": scene_key, "title": title,
                                     "message_id": message_id, "entry_sequence": entry["sequence"],
                                     "event_time": _iso(event_dt), "entry_hash": entry["entry_hash"]},
                             occurred_at=now)
                response = {"incident_id": incident_id, "entry_sequence": entry["sequence"],
                            "state_version": 1, "disposition": "accepted"}
                return "traffic_incident", incident_id, response

            return self._idempotent(connection, request_id=request_id, action="traffic_report_risk",
                                    payload=payload, create=create)

    # ----- 时间线消息 ---------------------------------------------------

    def append_message(self, *, request_id: str, actor_id: str, incident_id: str, message_id: str,
                       entry_type: str, event_time: str, payload: dict[str, Any]) -> WriteReceipt:
        if entry_type not in ENTRY_TYPES - {"risk_report"}:
            raise ValidationError("entry_type 不受支持")
        if not isinstance(payload, dict):
            raise ValidationError("payload 必须是对象")
        message_id = str(message_id).strip()
        if not message_id:
            raise ValidationError("message_id 不能为空")
        event_dt = _parse_event_time(event_time)
        envelope = {"actor_id": actor_id, "incident_id": incident_id, "message_id": message_id,
                    "entry_type": entry_type, "event_time": event_time, "payload": payload}
        with self.database.transaction(immediate=True) as connection:
            actor = self._load_actor(connection, actor_id)
            incident = self._load_incident(connection, incident_id)
            site = self._load_site(connection, incident["site_id"])
            self._check_site_scope(actor, site)
            duplicate = connection.execute(
                "SELECT * FROM traffic_entries WHERE incident_id=? AND message_id=?",
                (incident_id, message_id),
            ).fetchone()
            if duplicate is not None:
                if duplicate["payload_hash"] != digest(payload) or duplicate["entry_type"] != entry_type:
                    raise ConflictError("同一 message_id 已用于不同内容")
                response = {"incident_id": incident_id, "entry_sequence": duplicate["sequence"],
                            "state_version": incident["state_version"], "disposition": "duplicate",
                            "late": bool(duplicate["late"])}
                return self._replay_or_conflict(connection, request_id=request_id,
                                                action="traffic_append_message", payload=envelope,
                                                resource_id=f"{incident_id}:{duplicate['sequence']}",
                                                response=response)

            def create() -> tuple[str, str, dict[str, Any]]:
                watermark = self._watermark(connection, incident_id)
                late = watermark is not None and event_dt < watermark
                terminal = incident["status"] in PROTECTED_TERMINAL_STATES
                note = ""
                blockers: list[dict[str, Any]] = []
                if terminal:
                    disposition = "terminal_protected"
                    note = "事件已处于受保护终态，迟到消息仅作为证据留存，不改变道路状态"
                    self._validate_payload_shape(entry_type, payload)
                elif entry_type == "control_measure":
                    disposition = self._apply_control(connection, actor, incident, payload, late)
                elif entry_type == "resource_receipt":
                    disposition = self._apply_resource_receipt(connection, actor, incident, payload)
                elif entry_type == "review_result":
                    disposition, blockers = self._apply_review(connection, actor, incident, payload)
                else:  # pragma: no cover - 入口已经过滤
                    raise ValidationError("entry_type 不受支持")
                entry = self._append_entry(connection, incident, message_id=message_id,
                                          entry_type=entry_type, event_time=event_dt, actor_id=actor_id,
                                          payload=payload, disposition=disposition, late=late, note=note)
                if blockers:
                    connection.execute(
                        "UPDATE traffic_entries SET note=? WHERE incident_id=? AND sequence=?",
                        ("; ".join(b["reason"] for b in blockers), incident_id, entry["sequence"]),
                    )
                append_event(connection, actor_id=actor_id,
                             action=f"traffic.{entry_type}_recorded",
                             resource_type="traffic_incident", resource_id=incident_id,
                             detail={"message_id": message_id, "entry_sequence": entry["sequence"],
                                     "entry_type": entry_type, "disposition": disposition,
                                     "late": late, "state_version": incident["state_version"],
                                     "blockers": blockers, "entry_hash": entry["entry_hash"],
                                     "event_time": _iso(event_dt)},
                             occurred_at=self._now_iso())
                response = {"incident_id": incident_id, "entry_sequence": entry["sequence"],
                            "state_version": incident["state_version"], "disposition": disposition,
                            "late": late, "head_hash": entry["entry_hash"], "blockers": blockers}
                return "traffic_entry", f"{incident_id}:{entry['sequence']}", response

            return self._idempotent(connection, request_id=request_id, action="traffic_append_message",
                                    payload=envelope, create=create)

    def _replay_or_conflict(self, connection, *, request_id: str, action: str,
                            payload: dict[str, Any], resource_id: str,
                            response: dict[str, Any]) -> WriteReceipt:
        payload_hash = digest(payload)
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, "traffic_entry", resource_id,
             canonical_json(response), self._now_iso()),
        )
        return WriteReceipt(request_id, "traffic_entry", resource_id, False)

    def _validate_payload_shape(self, entry_type: str, payload: dict[str, Any]) -> None:
        if entry_type == "control_measure":
            self._control_shape(payload, apply=False)
        elif entry_type == "resource_receipt":
            self._receipt_shape(payload)
        elif entry_type == "review_result":
            self._review_shape(payload)

    # ----- 控制措施与前置条件 -------------------------------------------

    def _control_shape(self, payload: dict[str, Any], *, apply: bool) -> dict[str, Any]:
        action = str(payload.get("action", "")).strip()
        if action not in {"enforce", "release_request"}:
            raise ValidationError("控制措施 action 只能是 enforce 或 release_request")
        kind = str(payload.get("kind", "")).strip()
        if action == "enforce" and kind not in CONTROL_KINDS:
            raise ValidationError("封控 kind 不受支持")
        conditions = payload.get("conditions", [])
        if not isinstance(conditions, list):
            raise ValidationError("conditions 必须是数组")
        normalized = []
        for index, item in enumerate(conditions):
            if not isinstance(item, dict):
                raise ValidationError("conditions 每一项必须是对象")
            label = str(item.get("label", "")).strip()
            if not label:
                raise ValidationError(f"第 {index + 1} 项条件缺少 label")
            condition_id = str(item.get("id", f"c{index + 1}")).strip()
            normalized.append({"id": condition_id, "label": label})
        if apply and action == "enforce" and not kind:
            raise ValidationError("enforce 必须提供 kind")
        return {"action": action, "kind": kind, "conditions": normalized}

    def _apply_control(self, connection, actor: dict[str, Any], incident: dict[str, Any],
                       payload: dict[str, Any], late: bool) -> str:
        self._require(actor, "admin", "operator")
        shaped = self._control_shape(payload, apply=True)
        if shaped["action"] == "release_request":
            append_event(connection, actor_id=actor["actor_id"], action="traffic.release_requested",
                         resource_type="traffic_incident", resource_id=incident["incident_id"],
                         detail={"late": late}, occurred_at=self._now_iso())
            return "release_requested"
        existing = {row["condition_id"] for row in connection.execute(
            "SELECT condition_id FROM traffic_conditions WHERE incident_id=?", (incident["incident_id"],)
        )}
        for condition in shaped["conditions"]:
            if condition["id"] in existing:
                raise ConflictError(f"前置条件 {condition['id']} 已存在，修订链不允许覆盖")
        kinds = {part for part in (incident["control_kind"] or "").split(",") if part}
        kinds.add(shaped["kind"])
        control_kinds = ",".join(sorted(kinds))
        self._bump_version(connection, incident)
        connection.execute(
            "UPDATE traffic_incidents SET status=?, control_kind=?, control_event_time=? "
            "WHERE incident_id=?",
            (INCIDENT_CONTROLLING, control_kinds, self._now_iso(), incident["incident_id"]),
        )
        incident["status"] = INCIDENT_CONTROLLING
        incident["control_kind"] = control_kinds
        incident.setdefault("control_by", None)
        if not incident.get("control_by"):
            connection.execute(
                "UPDATE traffic_incidents SET control_by=? WHERE incident_id=?",
                (actor["actor_id"], incident["incident_id"]),
            )
            incident["control_by"] = actor["actor_id"]
        sequence_row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM traffic_entries WHERE incident_id=?",
            (incident["incident_id"],),
        ).fetchone()
        request_sequence = sequence_row["sequence"] + 1
        for condition in shaped["conditions"]:
            connection.execute(
                "INSERT INTO traffic_conditions(incident_id,condition_id,label,status,requested_sequence) "
                "VALUES(?,?,?,?,?)",
                (incident["incident_id"], condition["id"], condition["label"], "pending", request_sequence),
            )
        append_event(connection, actor_id=actor["actor_id"], action="traffic.control_enforced",
                     resource_type="traffic_incident", resource_id=incident["incident_id"],
                     detail={"kind": shaped["kind"], "conditions": shaped["conditions"],
                             "state_version": incident["state_version"]},
                     occurred_at=self._now_iso())
        return "accepted"

    # ----- 资源回执 -----------------------------------------------------

    def _receipt_shape(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action", "")).strip()
        if action not in RECEIPT_ACTIONS:
            raise ValidationError("资源回执 action 不受支持")
        resource_id = payload.get("resource_id")
        if resource_id is not None:
            resource_id = str(resource_id).strip()
        note = str(payload.get("note", "")).strip()
        return {"action": action, "resource_id": resource_id, "note": note}

    def _apply_resource_receipt(self, connection, actor: dict[str, Any], incident: dict[str, Any],
                                payload: dict[str, Any]) -> str:
        self._require(actor, "admin", "operator", "reviewer")
        shaped = self._receipt_shape(payload)
        resource_id = shaped["resource_id"]
        if not resource_id:
            return "accepted_evidence_only"
        row = connection.execute("SELECT * FROM traffic_resources WHERE resource_id=?", (resource_id,)).fetchone()
        if row is None:
            return "accepted_evidence_only"
        resource = dict(row)
        if resource["site_id"] != incident["site_id"] and actor["role"] != "admin":
            raise PermissionDenied("资源与事件不属于同一场所")
        entry_sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS next FROM traffic_entries WHERE incident_id=?",
            (incident["incident_id"],),
        ).fetchone()["next"]
        if shaped["action"] == "occupied":
            if resource["status"] == "available":
                connection.execute(
                    "UPDATE traffic_resources SET status='occupied', current_incident_id=?, holder_id=?, "
                    "occupied_at=?, version=version+1 WHERE resource_id=?",
                    (incident["incident_id"], actor["actor_id"], self._now_iso(), resource_id),
                )
            elif resource["current_incident_id"] != incident["incident_id"]:
                raise ConflictError(
                    f"资源 {resource_id} 已被事件 {resource['current_incident_id']} 占用，"
                    "请先取得释放回执或发起调配"
                )
        elif shaped["action"] in {"released", "cleared"} and resource["current_incident_id"] == incident["incident_id"]:
            connection.execute(
                "UPDATE traffic_resources SET status='available', current_incident_id=NULL, holder_id=NULL, "
                "occupied_at=NULL, version=version+1 WHERE resource_id=?",
                (resource_id,),
            )
        connection.execute(
            "INSERT INTO traffic_resource_movements(resource_id,incident_id,action,actor_id,"
            "entry_sequence,occurred_at) VALUES(?,?,?,?,?,?)",
            (resource_id, incident["incident_id"], f"receipt_{shaped['action']}", actor["actor_id"],
             entry_sequence, self._now_iso()),
        )
        return "accepted"

    # ----- 复查与封控解除 -----------------------------------------------

    def _review_shape(self, payload: dict[str, Any]) -> dict[str, Any]:
        results = payload.get("condition_results", {})
        if not isinstance(results, dict):
            raise ValidationError("condition_results 必须是对象")
        normalized = {}
        for condition_id, result in results.items():
            condition_id = str(condition_id).strip()
            result = str(result).strip()
            if not condition_id:
                raise ValidationError("条件编号不能为空")
            if result not in REVIEW_RESULTS:
                raise ValidationError(f"条件 {condition_id} 的复查结论只能是 confirmed/rejected")
            normalized[condition_id] = result
        action = str(payload.get("action", "")).strip()
        if action and action != "request_release":
            raise ValidationError("复查 action 只能为空或 request_release")
        return {"condition_results": normalized, "request_release": action == "request_release"}

    def _apply_review(self, connection, actor: dict[str, Any], incident: dict[str, Any],
                      payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        self._require(actor, "admin", "operator", "reviewer")
        shaped = self._review_shape(payload)
        sequence_row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM traffic_entries WHERE incident_id=?",
            (incident["incident_id"],),
        ).fetchone()
        entry_sequence = sequence_row["sequence"] + 1
        if shaped["condition_results"]:
            self._bump_version(connection, incident)
        for condition_id, result in shaped["condition_results"].items():
            row = connection.execute(
                "SELECT * FROM traffic_conditions WHERE incident_id=? AND condition_id=?",
                (incident["incident_id"], condition_id),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"前置条件 {condition_id} 不存在")
            if result == "confirmed":
                connection.execute(
                    "UPDATE traffic_conditions SET status='confirmed', confirmed_by=?, "
                    "confirmed_sequence=?, confirmed_at=? WHERE incident_id=? AND condition_id=?",
                    (actor["actor_id"], entry_sequence, self._now_iso(),
                     incident["incident_id"], condition_id),
                )
            else:
                connection.execute(
                    "UPDATE traffic_conditions SET status='rejected' WHERE incident_id=? AND condition_id=?",
                    (incident["incident_id"], condition_id),
                )
        if shaped["condition_results"]:
            append_event(connection, actor_id=actor["actor_id"], action="traffic.conditions_reviewed",
                         resource_type="traffic_incident", resource_id=incident["incident_id"],
                         detail={"results": shaped["condition_results"], "entry_sequence": entry_sequence,
                                 "state_version": incident["state_version"]},
                         occurred_at=self._now_iso())
        if not shaped["request_release"]:
            return "accepted", []
        return self._try_release(connection, actor, incident, entry_sequence)

    def _release_blockers(self, connection, incident: dict[str, Any], actor: dict[str, Any]
                          ) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        if incident["status"] != INCIDENT_CONTROLLING:
            blockers.append({"code": "not_controlling", "reason": "当前未处于封控状态，无需解除"})
            return blockers
        pending = connection.execute(
            "SELECT c.condition_id, c.label, c.status, c.confirmed_by, a.role AS confirmer_role "
            "FROM traffic_conditions c LEFT JOIN actors a ON a.actor_id=c.confirmed_by "
            "WHERE c.incident_id=?",
            (incident["incident_id"],),
        ).fetchall()
        for row in pending:
            if row["status"] != "confirmed":
                blockers.append({"code": "condition_unconfirmed", "condition_id": row["condition_id"],
                                 "label": row["label"], "status": row["status"],
                                 "reason": f"前置条件未确认：{row['label']}"})
                continue
            if row["confirmer_role"] != "reviewer":
                blockers.append({"code": "independent_reviewer_required",
                                 "condition_id": row["condition_id"],
                                 "reason": f"前置条件 {row['label']} 由 {row['confirmed_by']} 确认，"
                                           "必须由 reviewer 角色复核"})
            elif incident["control_by"] and row["confirmed_by"] == incident["control_by"]:
                blockers.append({"code": "independent_reviewer_required",
                                 "condition_id": row["condition_id"],
                                 "reason": f"前置条件 {row['label']} 由封控设置人本人确认，必须独立复核"})
        if actor["role"] != "reviewer":
            blockers.append({"code": "independent_reviewer_required",
                             "reason": "解除封控必须由 reviewer 角色的独立复核者执行"})
        elif incident["control_by"] and actor["actor_id"] == incident["control_by"]:
            blockers.append({"code": "independent_reviewer_required",
                             "reason": "复核者不能是设置封控的同一人，必须独立复核"})
        return blockers

    def _try_release(self, connection, actor: dict[str, Any], incident: dict[str, Any],
                     entry_sequence: int) -> tuple[str, list[dict[str, Any]]]:
        blockers = self._release_blockers(connection, incident, actor)
        if blockers:
            append_event(connection, actor_id=actor["actor_id"], action="traffic.release_blocked",
                         resource_type="traffic_incident", resource_id=incident["incident_id"],
                         detail={"blockers": blockers, "entry_sequence": entry_sequence},
                         occurred_at=self._now_iso())
            return "release_blocked", blockers
        self._bump_version(connection, incident)
        now = self._now_iso()
        connection.execute(
            "UPDATE traffic_incidents SET status=?, closed_by=?, closed_sequence=?, closed_at=?, "
            "control_kind=NULL, updated_at=? WHERE incident_id=?",
            (INCIDENT_CLOSED, actor["actor_id"], entry_sequence, now, now, incident["incident_id"]),
        )
        incident["status"] = INCIDENT_CLOSED
        occupied = connection.execute(
            "SELECT resource_id FROM traffic_resources WHERE current_incident_id=?",
            (incident["incident_id"],),
        ).fetchall()
        for row in occupied:
            connection.execute(
                "UPDATE traffic_resources SET status='available', current_incident_id=NULL, holder_id=NULL, "
                "occupied_at=NULL, version=version+1 WHERE resource_id=?",
                (row["resource_id"],),
            )
            connection.execute(
                "INSERT INTO traffic_resource_movements(resource_id,incident_id,action,actor_id,occurred_at) "
                "VALUES(?,?,?,?,?)",
                (row["resource_id"], incident["incident_id"], "released_incident_closed",
                 actor["actor_id"], now),
            )
        connection.execute(
            "UPDATE traffic_leases SET ended_at=?, end_reason='incident_closed' WHERE incident_id=? "
            "AND ended_at IS NULL",
            (now, incident["incident_id"]),
        )
        append_event(connection, actor_id=actor["actor_id"], action="traffic.control_released",
                     resource_type="traffic_incident", resource_id=incident["incident_id"],
                     detail={"entry_sequence": entry_sequence, "released_resources": len(occupied),
                             "state_version": incident["state_version"]},
                     occurred_at=now)
        return "closed", []

    # ----- 责任租约 -----------------------------------------------------

    def _expire_stale_leases(self, connection, incident_id: str, now: datetime) -> None:
        connection.execute(
            "UPDATE traffic_leases SET ended_at=expires_at, end_reason='expired' WHERE incident_id=? "
            "AND ended_at IS NULL AND expires_at<=?",
            (incident_id, _iso(now)),
        )

    def _open_conditions(self, connection, incident_id: str) -> list[dict[str, str]]:
        rows = connection.execute(
            "SELECT condition_id, label FROM traffic_conditions WHERE incident_id=? "
            "AND status!='confirmed' ORDER BY condition_id",
            (incident_id,),
        ).fetchall()
        return [{"condition_id": row["condition_id"], "label": row["label"]} for row in rows]

    def claim_task(self, *, request_id: str, actor_id: str, incident_id: str,
                   ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS) -> WriteReceipt:
        payload = {"actor_id": actor_id, "incident_id": incident_id, "ttl_seconds": ttl_seconds}
        if not isinstance(ttl_seconds, int) or not (60 <= ttl_seconds <= MAX_LEASE_TTL_SECONDS):
            raise ValidationError("ttl_seconds 必须在 60 到 14400 之间")
        with self.database.transaction(immediate=True) as connection:
            actor = self._load_actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            incident = self._load_incident(connection, incident_id)
            site = self._load_site(connection, incident["site_id"])
            self._check_site_scope(actor, site)
            if incident["status"] in PROTECTED_TERMINAL_STATES:
                raise ConflictError("事件已终态关闭，不能再领取接力任务")
            now = self._now()
            self._expire_stale_leases(connection, incident_id, now)

            def create() -> tuple[str, str, dict[str, Any]]:
                active = connection.execute(
                    "SELECT * FROM traffic_leases WHERE incident_id=? AND ended_at IS NULL",
                    (incident_id,),
                ).fetchone()
                if active is not None:
                    if active["holder_id"] == actor_id:
                        response = self._lease_dict(connection, active)
                        return "traffic_lease", active["lease_id"], response
                    raise ConflictError(f"责任租约仍在 {active['holder_id']} 手中，未到期或未转交")
                lease_id = uuid.uuid4().hex
                expires = now + timedelta(seconds=ttl_seconds)
                open_conditions = self._open_conditions(connection, incident_id)
                connection.execute(
                    "INSERT INTO traffic_leases(lease_id,incident_id,holder_id,granted_by,granted_at,"
                    "expires_at,open_conditions_json) VALUES(?,?,?,?,?,?,?)",
                    (lease_id, incident_id, actor_id, actor_id, _iso(now), _iso(expires),
                     canonical_json(open_conditions)),
                )
                append_event(connection, actor_id=actor_id, action="traffic.lease_granted",
                             resource_type="traffic_lease", resource_id=lease_id,
                             detail={"incident_id": incident_id, "expires_at": _iso(expires),
                                     "open_conditions": open_conditions,
                                     "state_version": incident["state_version"]},
                             occurred_at=_iso(now))
                response = self._lease_dict(connection,
                                            connection.execute("SELECT * FROM traffic_leases WHERE lease_id=?",
                                                               (lease_id,)).fetchone())
                return "traffic_lease", lease_id, response

            return self._idempotent(connection, request_id=request_id, action="traffic_claim_task",
                                    payload=payload, create=create)

    def transfer_task(self, *, request_id: str, actor_id: str, lease_id: str,
                      to_actor_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "lease_id": lease_id, "to_actor_id": to_actor_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._load_actor(connection, actor_id)
            to_actor = self._load_actor(connection, to_actor_id)
            self._require(to_actor, "admin", "operator")
            row = connection.execute("SELECT * FROM traffic_leases WHERE lease_id=?", (lease_id,)).fetchone()
            if row is None:
                raise NotFoundError("责任租约不存在")
            lease = dict(row)
            incident = self._load_incident(connection, lease["incident_id"])
            site = self._load_site(connection, incident["site_id"])
            self._check_site_scope(actor, site)
            if to_actor["role"] != "admin" and to_actor["organization_id"] != site["organization_id"]:
                raise PermissionDenied("不能转交给其他组织的人员")
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                self._expire_stale_leases(connection, lease["incident_id"], now)
                current = connection.execute("SELECT * FROM traffic_leases WHERE lease_id=?", (lease_id,)).fetchone()
                if current is None or current["ended_at"] is not None:
                    raise ConflictError("责任租约已结束，不能转交")
                if current["holder_id"] != actor_id:
                    raise PermissionDenied("只有当前持有人可以转交租约")
                open_conditions = self._open_conditions(connection, lease["incident_id"])
                new_lease_id = uuid.uuid4().hex
                connection.execute(
                    "UPDATE traffic_leases SET ended_at=?, end_reason='transferred' WHERE lease_id=?",
                    (_iso(now), lease_id),
                )
                connection.execute(
                    "INSERT INTO traffic_leases(lease_id,incident_id,holder_id,granted_by,granted_at,"
                    "expires_at,predecessor_id,open_conditions_json) VALUES(?,?,?,?,?,?,?,?)",
                    (new_lease_id, lease["incident_id"], to_actor_id, actor_id, _iso(now),
                     current["expires_at"], lease_id, canonical_json(open_conditions)),
                )
                append_event(connection, actor_id=actor_id, action="traffic.lease_transferred",
                             resource_type="traffic_lease", resource_id=new_lease_id,
                             detail={"incident_id": lease["incident_id"], "predecessor_id": lease_id,
                                     "from_actor": actor_id, "to_actor": to_actor_id,
                                     "open_conditions": open_conditions,
                                     "state_version": incident["state_version"]},
                             occurred_at=_iso(now))
                response = self._lease_dict(connection,
                                            connection.execute("SELECT * FROM traffic_leases WHERE lease_id=?",
                                                               (new_lease_id,)).fetchone())
                return "traffic_lease", new_lease_id, response

            return self._idempotent(connection, request_id=request_id, action="traffic_transfer_task",
                                    payload=payload, create=create)

    def _lease_dict(self, connection, row) -> dict[str, Any]:
        lease = dict(row)
        now = _iso(self._now())
        expired = bool(lease["ended_at"]) or lease["expires_at"] <= now
        incident = self._load_incident(connection, lease["incident_id"])
        return {"lease_id": lease["lease_id"], "incident_id": lease["incident_id"],
                "holder_id": lease["holder_id"], "granted_by": lease["granted_by"],
                "granted_at": lease["granted_at"], "expires_at": lease["expires_at"],
                "ended_at": lease["ended_at"], "end_reason": lease["end_reason"],
                "predecessor_id": lease["predecessor_id"],
                "open_conditions": json.loads(lease["open_conditions_json"]),
                "active": not expired, "incident_state_version": incident["state_version"]}

    # ----- 资源登记与候选调配 -------------------------------------------

    def register_resource(self, *, request_id: str, actor_id: str, resource_id: str, site_id: str,
                          kind: str, label: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "resource_id": resource_id, "site_id": site_id,
                   "kind": kind, "label": label}
        resource_id = str(resource_id).strip()
        kind = str(kind).strip()
        label = str(label).strip()
        if not resource_id or not kind or not label:
            raise ValidationError("resource_id/kind/label 不能为空")
        with self.database.transaction(immediate=True) as connection:
            actor = self._load_actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._load_site(connection, site_id)
            self._check_site_scope(actor, site)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO traffic_resources(resource_id,site_id,kind,label,status,version) "
                        "VALUES(?,?,?,?,?,1)",
                        (resource_id, site_id, kind, label, "available"),
                    )
                except Exception as exc:
                    raise ConflictError("资源编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="traffic.resource_registered",
                             resource_type="traffic_resource", resource_id=resource_id,
                             detail={"site_id": site_id, "kind": kind, "label": label},
                             occurred_at=self._now_iso())
                return "traffic_resource", resource_id, {"resource_id": resource_id}

            return self._idempotent(connection, request_id=request_id, action="traffic_register_resource",
                                    payload=payload, create=create)

    def propose_allocation(self, *, request_id: str, actor_id: str, incident_id: str,
                           requests: list[dict[str, str]], reason: str) -> WriteReceipt:
        if not isinstance(requests, list) or not requests:
            raise ValidationError("requests 必须是非空数组")
        normalized: list[dict[str, str]] = []
        for index, item in enumerate(requests):
            if not isinstance(item, dict) or not str(item.get("kind", "")).strip():
                raise ValidationError(f"第 {index + 1} 项调配请求缺少 kind")
            normalized.append({"kind": str(item["kind"]).strip(),
                               "note": str(item.get("note", "")).strip()})
        reason = str(reason or "").strip()
        payload = {"actor_id": actor_id, "incident_id": incident_id, "requests": normalized, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._load_actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            incident = self._load_incident(connection, incident_id)
            self._check_site_scope(actor, self._load_site(connection, incident["site_id"]))
            if incident["status"] in PROTECTED_TERMINAL_STATES:
                raise ConflictError("事件已关闭，不能再提出调配")

            def create() -> tuple[str, str, dict[str, Any]]:
                plan_id = uuid.uuid4().hex
                items: list[dict[str, Any]] = []
                feasible = True
                reasons: list[str] = []
                for position, request in enumerate(normalized):
                    pick = connection.execute(
                        "SELECT * FROM traffic_resources WHERE kind=? AND status='available' "
                        "ORDER BY (site_id!=?) ASC, resource_id ASC LIMIT 1",
                        (request["kind"], incident["site_id"]),
                    ).fetchone()
                    if pick is not None:
                        items.append({"position": position, "resource_id": pick["resource_id"],
                                      "kind": request["kind"], "site_id": pick["site_id"],
                                      "action": "allocate", "satisfied": 1,
                                      "reason": f"同型资源空闲，位于场所 {pick['site_id']}"})
                        continue
                    feasible = False
                    alternatives = connection.execute(
                        "SELECT * FROM traffic_resources WHERE kind=? ORDER BY "
                        "(site_id=?) DESC, (status='available') DESC, resource_id LIMIT 5",
                        (request["kind"], incident["site_id"]),
                    ).fetchall()
                    candidate_detail = []
                    for candidate in alternatives:
                        if candidate["status"] != "available":
                            why = f"资源 {candidate['resource_id']} 正被事件 {candidate['current_incident_id']} 占用，需先协调归还"
                        else:
                            why = f"资源 {candidate['resource_id']} 位于其他场所 {candidate['site_id']}，需跨场所调拨"
                        candidate_detail.append({"resource_id": candidate["resource_id"],
                                                 "site_id": candidate["site_id"],
                                                 "status": candidate["status"],
                                                 "current_incident_id": candidate["current_incident_id"],
                                                 "reason": why})
                    line_reason = (f"本场所缺少空闲的 {request['kind']}"
                                   if not candidate_detail
                                   else f"本场所缺少空闲的 {request['kind']}，仅存在受限候选")
                    reasons.append(line_reason)
                    items.append({"position": position, "resource_id": None, "kind": request["kind"],
                                  "site_id": None, "action": "escalate", "satisfied": 0,
                                  "reason": line_reason, "candidates": candidate_detail})
                reason_summary = reason if feasible else "；".join(reasons) or "资源不足"
                connection.execute(
                    "INSERT INTO traffic_plans(plan_id,incident_id,state_version,status,feasible,"
                    "reason_summary,requested_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (plan_id, incident_id, incident["state_version"], "proposed", 1 if feasible else 0,
                     reason_summary, canonical_json(normalized), actor_id, self._now_iso()),
                )
                for item in items:
                    connection.execute(
                        "INSERT INTO traffic_plan_items(plan_id,position,resource_id,kind,site_id,action,"
                        "reason,satisfied) VALUES(?,?,?,?,?,?,?,?)",
                        (plan_id, item["position"], item["resource_id"], item["kind"], item["site_id"],
                         item["action"], item["reason"], item["satisfied"]),
                    )
                append_event(connection, actor_id=actor_id, action="traffic.allocation_proposed",
                             resource_type="traffic_plan", resource_id=plan_id,
                             detail={"incident_id": incident_id, "feasible": feasible,
                                     "state_version": incident["state_version"],
                                     "reason_summary": reason_summary, "items": items},
                             occurred_at=self._now_iso())
                response = {"plan_id": plan_id, "incident_id": incident_id,
                            "state_version": incident["state_version"], "status": "proposed",
                            "feasible": feasible, "reason_summary": reason_summary, "items": items}
                return "traffic_plan", plan_id, response

            return self._idempotent(connection, request_id=request_id, action="traffic_propose_allocation",
                                    payload=payload, create=create)

    def decide_allocation(self, *, request_id: str, actor_id: str, plan_id: str,
                          state_version: int, decision: str) -> WriteReceipt:
        if decision not in {"execute", "escalate"}:
            raise ValidationError("decision 只能是 execute 或 escalate")
        if not isinstance(state_version, int):
            raise ValidationError("state_version 必须是整数")
        payload = {"actor_id": actor_id, "plan_id": plan_id, "state_version": state_version,
                   "decision": decision}
        with self.database.transaction(immediate=True) as connection:
            actor = self._load_actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            plan_row = connection.execute("SELECT * FROM traffic_plans WHERE plan_id=?", (plan_id,)).fetchone()
            if plan_row is None:
                raise NotFoundError("调配方案不存在")
            plan = dict(plan_row)
            incident = self._load_incident(connection, plan["incident_id"])
            self._check_site_scope(actor, self._load_site(connection, incident["site_id"]))

            def create() -> tuple[str, str, dict[str, Any]]:
                if plan["status"] != "proposed":
                    raise ConflictError("方案已经被处理，不能重复确认")
                if incident["state_version"] != state_version or plan["state_version"] != state_version:
                    raise ConflictError(
                        f"状态版本已变化：方案基于版本 {plan['state_version']}，"
                        f"当前为 {incident['state_version']}"
                    )
                items = [dict(row) for row in connection.execute(
                    "SELECT * FROM traffic_plan_items WHERE plan_id=? ORDER BY position", (plan_id,))]
                now = self._now_iso()
                if decision == "execute":
                    if not plan["feasible"]:
                        raise ConflictError(f"方案不可执行：{plan['reason_summary']}")
                    for item in items:
                        current = connection.execute(
                            "SELECT * FROM traffic_resources WHERE resource_id=? AND status='available'",
                            (item["resource_id"],),
                        ).fetchone()
                        if current is None:
                            raise ConflictError(
                                f"资源 {item['resource_id']} 已不再空闲，方案版本过期，整体确认失败（无任何占用）"
                            )
                    for item in items:
                        connection.execute(
                            "UPDATE traffic_resources SET status='occupied', current_incident_id=?, "
                            "holder_id=?, occupied_at=?, version=version+1 WHERE resource_id=?",
                            (incident["incident_id"], actor["actor_id"], now, item["resource_id"]),
                        )
                        connection.execute(
                            "INSERT INTO traffic_resource_movements(resource_id,incident_id,action,plan_id,"
                            "actor_id,occurred_at) VALUES(?,?,?,?,?,?)",
                            (item["resource_id"], incident["incident_id"], "allocated", plan_id,
                             actor["actor_id"], now),
                        )
                    new_status = "executed"
                else:
                    if plan["feasible"]:
                        raise ConflictError("方案可执行，不能按短缺上报升级")
                    new_status = "escalated"
                connection.execute(
                    "UPDATE traffic_plans SET status=?, confirmed_by=?, confirmed_at=? WHERE plan_id=?",
                    (new_status, actor_id, now, plan_id),
                )
                self._bump_version(connection, incident)
                append_event(connection, actor_id=actor_id, action=f"traffic.allocation_{new_status}",
                             resource_type="traffic_plan", resource_id=plan_id,
                             detail={"incident_id": incident["incident_id"], "decision": decision,
                                     "item_count": len(items), "state_version": incident["state_version"]},
                             occurred_at=now)
                response = {"plan_id": plan_id, "incident_id": incident["incident_id"],
                            "status": new_status, "state_version": incident["state_version"]}
                return "traffic_plan", plan_id, response

            return self._idempotent(connection, request_id=request_id, action="traffic_decide_allocation",
                                    payload=payload, create=create)

    # ----- 查询与证据解释 -----------------------------------------------

    def _entries(self, connection, incident_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM traffic_entries WHERE incident_id=? ORDER BY sequence", (incident_id,)
        ).fetchall()
        result = []
        for row in rows:
            result.append({"sequence": row["sequence"], "message_id": row["message_id"],
                           "entry_type": row["entry_type"], "event_time": row["event_time"],
                           "received_at": row["received_at"], "actor_id": row["actor_id"],
                           "payload": json.loads(row["payload_json"]),
                           "payload_hash": row["payload_hash"], "disposition": row["disposition"],
                           "late": bool(row["late"]), "note": row["note"],
                           "previous_hash": row["previous_hash"], "entry_hash": row["entry_hash"]})
        return result

    def incident_explanation(self, incident_id: str) -> dict[str, Any]:
        """返回当前道路状态、资源去向与每个决定所依赖证据的完整解释。"""

        connection = self.database.connection
        incident = self._load_incident(connection, incident_id)
        entries = self._entries(connection, incident_id)
        conditions = []
        for row in connection.execute(
            "SELECT c.*, e.message_id AS confirmed_message FROM traffic_conditions c "
            "LEFT JOIN traffic_entries e ON e.incident_id=c.incident_id "
            "AND e.sequence=c.confirmed_sequence WHERE c.incident_id=? ORDER BY c.condition_id",
            (incident_id,),
        ):
            conditions.append({"condition_id": row["condition_id"], "label": row["label"],
                               "status": row["status"], "requested_sequence": row["requested_sequence"],
                               "confirmed_by": row["confirmed_by"],
                               "confirmed_sequence": row["confirmed_sequence"],
                               "confirmed_message_id": row["confirmed_message"],
                               "confirmed_at": row["confirmed_at"]})
        unmet = [c["condition_id"] for c in conditions if c["status"] != "confirmed"]
        now = self._now_iso()
        lease_rows = connection.execute(
            "SELECT * FROM traffic_leases WHERE incident_id=? ORDER BY granted_at", (incident_id,)
        ).fetchall()
        leases = []
        for row in lease_rows:
            lease = dict(row)
            leases.append({"lease_id": lease["lease_id"], "holder_id": lease["holder_id"],
                           "granted_by": lease["granted_by"], "granted_at": lease["granted_at"],
                           "expires_at": lease["expires_at"], "ended_at": lease["ended_at"],
                           "end_reason": lease["end_reason"], "predecessor_id": lease["predecessor_id"],
                           "open_conditions": json.loads(lease["open_conditions_json"]),
                           "active": lease["ended_at"] is None and lease["expires_at"] > now})
        resources = []
        for row in connection.execute(
            "SELECT r.* FROM traffic_resources r WHERE r.current_incident_id=? OR r.resource_id IN "
            "(SELECT m.resource_id FROM traffic_resource_movements m WHERE m.incident_id=?) "
            "ORDER BY r.resource_id",
            (incident_id, incident_id),
        ):
            resource = dict(row)
            movements = [dict(m) for m in connection.execute(
                "SELECT movement_id,action,actor_id,plan_id,occurred_at FROM traffic_resource_movements "
                "WHERE resource_id=? AND incident_id=? ORDER BY movement_id",
                (resource["resource_id"], incident_id))]
            resources.append({"resource_id": resource["resource_id"], "kind": resource["kind"],
                              "label": resource["label"], "site_id": resource["site_id"],
                              "status": resource["status"], "version": resource["version"],
                              "holder_id": resource["holder_id"], "occupied_at": resource["occupied_at"],
                              "movements": movements})
        plans = []
        for row in connection.execute("SELECT * FROM traffic_plans WHERE incident_id=? ORDER BY created_at",
                                      (incident_id,)):
            plan = dict(row)
            items = [dict(item) for item in connection.execute(
                "SELECT position,resource_id,kind,site_id,action,reason,satisfied FROM traffic_plan_items "
                "WHERE plan_id=? ORDER BY position", (plan["plan_id"],))]
            plans.append({"plan_id": plan["plan_id"], "state_version": plan["state_version"],
                          "status": plan["status"], "feasible": bool(plan["feasible"]),
                          "reason_summary": plan["reason_summary"],
                          "requested": json.loads(plan["requested_json"]),
                          "created_by": plan["created_by"], "created_at": plan["created_at"],
                          "confirmed_by": plan["confirmed_by"], "confirmed_at": plan["confirmed_at"],
                          "items": items})
        chain_ok = self._verify_entry_chain(connection, incident_id)
        road_status = {
            "incident_id": incident_id,
            "status": incident["status"],
            "control_kind": incident["control_kind"],
            "control_by": incident["control_by"],
            "control_event_time": incident["control_event_time"],
            "closed_by": incident["closed_by"],
            "closed_at": incident["closed_at"],
            "releasable": incident["status"] == INCIDENT_CONTROLLING and not unmet,
            "unmet_conditions": unmet,
            "independence_rule": "解除封控必须由非封控设置人的 reviewer 确认全部前置条件",
        }
        evidence = self._decision_evidence(incident, entries, conditions, leases, plans)
        return {"incident_id": incident_id, "site_id": incident["site_id"],
                "scene_key": incident["scene_key"], "title": incident["title"],
                "state_version": incident["state_version"], "head_hash": incident["head_hash"],
                "road_status": road_status, "conditions": conditions, "active_lease":
                next((lease for lease in leases if lease["active"]), None),
                "leases": leases, "resources": resources, "plans": plans,
                "timeline": entries, "entry_chain_valid": chain_ok, "evidence": evidence}

    def _decision_evidence(self, incident, entries, conditions, leases, plans) -> dict[str, Any]:
        by_sequence = {entry["sequence"]: entry for entry in entries}

        def ref(sequence: int | None) -> dict[str, Any] | None:
            if sequence is None:
                return None
            entry = by_sequence.get(sequence)
            if entry is None:
                return None
            return {"entry_sequence": sequence, "message_id": entry["message_id"],
                    "actor_id": entry["actor_id"], "event_time": entry["event_time"],
                    "payload_hash": entry["payload_hash"], "entry_hash": entry["entry_hash"]}

        condition_evidence = []
        for condition in conditions:
            condition_evidence.append({
                "condition_id": condition["condition_id"],
                "label": condition["label"],
                "status": condition["status"],
                "requested_in": ref(condition["requested_sequence"]),
                "confirmed_in": ref(condition["confirmed_sequence"]),
            })
        return {
            "risk_report": ref(1),
            "control_enforced": ref(self._find_entry(entries, "control_measure",
                                                     lambda p: p.get("action") == "enforce")),
            "release": ref(incident["closed_sequence"]),
            "release_attempts": [ref(seq) for seq in self._entry_sequences(
                entries, lambda e: e["disposition"] == "release_blocked")],
            "ignored_after_close": [ref(seq) for seq in self._entry_sequences(
                entries, lambda e: e["disposition"] == "terminal_protected")],
            "conditions": condition_evidence,
            "lease_chain": [{"lease_id": lease["lease_id"], "holder_id": lease["holder_id"],
                             "predecessor_id": lease["predecessor_id"],
                             "granted_in_conditions": lease["open_conditions"]} for lease in leases],
            "plans": [{"plan_id": plan["plan_id"], "status": plan["status"],
                       "based_on_state_version": plan["state_version"],
                       "decided_by": plan["confirmed_by"]} for plan in plans],
        }

    @staticmethod
    def _find_entry(entries, entry_type: str, predicate) -> int | None:
        for entry in entries:
            if entry["entry_type"] == entry_type and predicate(entry["payload"]):
                return entry["sequence"]
        return None

    @staticmethod
    def _entry_sequences(entries, predicate) -> list[int]:
        return [entry["sequence"] for entry in entries if predicate(entry)]

    def _verify_entry_chain(self, connection, incident_id: str) -> bool:
        previous = "0" * 64
        for row in connection.execute(
            "SELECT * FROM traffic_entries WHERE incident_id=? ORDER BY sequence", (incident_id,)
        ):
            material = {"incident_id": row["incident_id"], "sequence": row["sequence"],
                        "message_id": row["message_id"], "entry_type": row["entry_type"],
                        "event_time": row["event_time"], "actor_id": row["actor_id"],
                        "payload_hash": row["payload_hash"], "previous_hash": row["previous_hash"]}
            if row["previous_hash"] != previous or digest(material) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def road_status(self, site_id: str | None = None) -> list[dict[str, Any]]:
        connection = self.database.connection
        query = ("SELECT i.*, s.name AS site_name FROM traffic_incidents i "
                 "JOIN sites s ON s.site_id=i.site_id")
        parameters: list[Any] = []
        if site_id:
            query += " WHERE i.site_id=?"
            parameters.append(site_id)
        query += " ORDER BY i.created_at"
        result = []
        for row in connection.execute(query, parameters):
            result.append({"incident_id": row["incident_id"], "site_id": row["site_id"],
                           "site_name": row["site_name"], "scene_key": row["scene_key"],
                           "title": row["title"], "status": row["status"],
                           "control_kind": row["control_kind"], "control_by": row["control_by"],
                           "control_event_time": row["control_event_time"],
                           "state_version": row["state_version"], "head_hash": row["head_hash"]})
        return result

    def resource_trace(self, resource_id: str) -> dict[str, Any]:
        connection = self.database.connection
        row = connection.execute("SELECT * FROM traffic_resources WHERE resource_id=?", (resource_id,)).fetchone()
        if row is None:
            raise NotFoundError("资源不存在")
        resource = dict(row)
        movements = [dict(m) for m in connection.execute(
            "SELECT movement_id,incident_id,action,plan_id,actor_id,entry_sequence,occurred_at "
            "FROM traffic_resource_movements WHERE resource_id=? ORDER BY movement_id", (resource_id,))]
        return {"resource_id": resource_id, "site_id": resource["site_id"], "kind": resource["kind"],
                "label": resource["label"], "status": resource["status"], "version": resource["version"],
                "current_incident_id": resource["current_incident_id"], "holder_id": resource["holder_id"],
                "occupied_at": resource["occupied_at"], "movements": movements}

"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, text
from .repository import Repository
from .rules import DESIGN_BURIAL_DEPTH_M, DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    @staticmethod
    def _enrich(record: Dict[str, Any]) -> Dict[str, Any]:
        payload = record.get("payload", {})
        record["design_burial_depth_m"] = payload.get("design_burial_depth_m", DESIGN_BURIAL_DEPTH_M)
        record["blocking"] = Service._blocking(record["state"], payload)
        return record

    @staticmethod
    def _blocking(state: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if state != "rectification_pending":
            return None
        return {
            "reason": "burial_depth_below_design",
            "design_depth_m": payload.get("design_burial_depth_m", DESIGN_BURIAL_DEPTH_M),
            "shallowest_point": {"distance_km": payload.get("burial_min_distance_km"), "burial_depth_m": payload.get("burial_min_depth_m")},
            "gap_m": payload.get("burial_gap_m"),
            "measured_at": payload.get("burial_measured_at"),
            "round": payload.get("burial_round_count"),
            "rectified": bool(payload.get("rectified")),
            "rectified_at": payload.get("rectified_at"),
            "vessel_name": payload.get("vessel_name"),
            "can_release": True,
        }

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self._enrich(self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id))

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        records = self.repository.list_records(state=state, limit=limit)
        return [self._enrich(record) for record in records]

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self._enrich(self.repository.get(record_id))

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary, burial = self.rules.apply_action(record, action, data or {})
        points = burial.get("points") if burial else None
        details = {"summary": summary, "input": data or {}, "from": record["state"], "to": new_state}
        if burial:
            details["burial"] = {key: value for key, value in burial.items() if key != "points"}
        saved = self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details=details,
            burial_points=points,
        )
        return self._enrich(saved)

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def burial_history(self, actor: Actor, record_id: int, round_no: Optional[int] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.burial_history(record_id, round_no=round_no)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

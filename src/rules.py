"""跨海光缆故障与抢修协调领域规则与状态转换。"""
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, optional_text, text, text_list


INITIAL_STATE = "detected"
DESIGN_BURIAL_DEPTH_M = 1.5
CREATE_ROLES = {'noc_operator'}
ACTION_ROLES = {'approve': {'repair_manager'}, 'mobilize': {'vessel_master'}, 'survey': {'cable_engineer'}, 'splice': {'cable_engineer'}, 'burial_survey': {'cable_engineer'}, 'rectify': {'cable_engineer'}, 'resurvey': {'cable_engineer'}, 'change_vessel': {'vessel_master'}, 'test': {'noc_operator'}, 'restore': {'noc_operator', 'repair_manager'}, 'cancel': {'repair_manager'}}
# resurvey/rectify/change_vessel为待整改态自环，实际放行目标(buried)由apply_action按复测结果决定。
TRANSITIONS = {'approve': {'detected': 'approved'}, 'mobilize': {'approved': 'mobilized'}, 'survey': {'mobilized': 'surveyed'}, 'splice': {'surveyed': 'spliced'}, 'burial_survey': {'spliced': 'rectification_pending'}, 'rectify': {'rectification_pending': 'rectification_pending'}, 'resurvey': {'rectification_pending': 'rectification_pending'}, 'change_vessel': {'rectification_pending': 'rectification_pending'}, 'test': {'buried': 'tested'}, 'restore': {'tested': 'restored'}, 'cancel': {'detected': 'cancelled', 'approved': 'cancelled', 'mobilized': 'cancelled', 'rectification_pending': 'cancelled'}}


def _parse_iso(value: Any, key: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s不能为空" % key)
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("%s必须是ISO8601时间" % key) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _validate_burial_points(data: Dict[str, Any], payload: Dict[str, Any]) -> List[Dict[str, float]]:
    raw = data.get("burial_points")
    if not isinstance(raw, list) or not raw:
        raise ValidationError("burial_points必须是非空测点列表")
    start = float(payload["start_km"])
    end = float(payload["end_km"])
    points: List[Dict[str, float]] = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValidationError("测点必须是对象")
        distance = round(number(item, "distance_km", 0), 3)
        depth = round(number(item, "burial_depth_m", 0), 3)
        if distance < start or distance > end:
            raise ValidationError("测点里程超出抢修区段")
        if distance in seen:
            raise ValidationError("测点里程不能重复")
        seen.add(distance)
        points.append({"distance_km": distance, "burial_depth_m": depth})
    points.sort(key=lambda point: point["distance_km"])
    if points[0]["distance_km"] > start or points[-1]["distance_km"] < end:
        raise ValidationError("测点必须覆盖全段，起止里程均需有测点")
    return points


class DomainRules:
    INITIAL_STATE = INITIAL_STATE
    DESIGN_BURIAL_DEPTH_M = DESIGN_BURIAL_DEPTH_M

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "cable")
        text(p, "segment")
        start = number(p, "start_km", 0)
        end = number(p, "end_km", 0)
        number(p, "depth_m", 1)
        integer(p, "sea_state", 0, 9)
        boolean(p, "vessel_available")
        number(p, "spare_length_km", 0)
        boolean(p, "permit_valid")
        integer(p, "capacity_gbps", 1)
        if end <= start:
            raise ValidationError("结束里程必须大于开始里程")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        distance = float(p["end_km"]) - float(p["start_km"])
        p["repair_distance_km"] = round(distance, 2)
        p["required_spare_km"] = round(distance * 1.05, 2)
        p["estimated_repair_hours"] = round(distance / 2.0 + float(p["depth_m"]) / 100.0 + int(p["sea_state"]) * 2.0, 2)
        p["repair_feasible"] = bool(p["vessel_available"] and p["permit_valid"] and p["spare_length_km"] >= p["required_spare_km"] and int(p["sea_state"]) <= 5)
        p["design_burial_depth_m"] = DESIGN_BURIAL_DEPTH_M
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] in {"restored", "cancelled"} or item["payload"].get("cable") != payload.get("cable") or item["payload"].get("segment") != payload.get("segment"):
                continue
            if float(payload["start_km"]) < float(item["payload"].get("end_km", 0)) and float(payload["end_km"]) > float(item["payload"].get("start_km", 0)):
                raise Conflict("同一光缆区段已有未结束抢修")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    @staticmethod
    def _append_burial_round(payload: Dict[str, Any], points: List[Dict[str, float]], kind: str, measured_at: datetime, vessel_name: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        shallowest = min(points, key=lambda point: point["burial_depth_m"])
        min_depth = shallowest["burial_depth_m"]
        passed = min_depth >= DESIGN_BURIAL_DEPTH_M
        rounds = list(payload.get("burial_rounds", []))
        summary = {
            "round": len(rounds) + 1,
            "kind": kind,
            "measured_at": _iso(measured_at),
            "point_count": len(points),
            "full_coverage": True,
            "min_depth_m": min_depth,
            "min_distance_km": shallowest["distance_km"],
            "gap_m": round(DESIGN_BURIAL_DEPTH_M - min_depth, 3) if min_depth < DESIGN_BURIAL_DEPTH_M else 0.0,
            "passed": passed,
            "vessel_name": vessel_name,
        }
        rounds.append(summary)
        return rounds, summary

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str, Optional[Dict[str, Any]]]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        burial: Optional[Dict[str, Any]] = None
        if action == "approve":
            if not bool(p["permit_valid"]) or not bool(p["vessel_available"]):
                raise ValidationError("许可或船舶条件不满足")
            changes["repair_manager"] = text(data, "repair_manager")
            summary = "抢修方案已批准"
        elif action == "mobilize":
            if float(data.get("weather_window_hours", 0)) < float(p["estimated_repair_hours"]):
                raise ValidationError("海况窗口不足以完成抢修")
            if float(data.get("available_spare_km", 0)) < float(p["required_spare_km"]):
                raise ValidationError("船上备缆不足")
            changes["weather_window_hours"] = float(data["weather_window_hours"])
            changes["vessel_name"] = text(data, "vessel_name")
            summary = "抢修船已动员"
        elif action == "survey":
            if not boolean(data, "survey_complete"):
                raise ValidationError("勘察尚未完成")
            fault_km = number(data, "fault_location_km", 0)
            if not (float(p["start_km"]) <= fault_km <= float(p["end_km"])):
                raise ValidationError("故障点不在申报区段")
            changes["fault_location_km"] = fault_km
            summary = "故障点勘察完成"
        elif action == "splice":
            loss = number(data, "splice_loss_db", 0)
            if loss > 0.2:
                raise ValidationError("接续损耗超过阈值")
            if float(data.get("spare_used_km", 0)) < float(p["repair_distance_km"]):
                raise ValidationError("备缆使用长度不足")
            changes["splice_loss_db"] = loss
            changes["spare_used_km"] = float(data["spare_used_km"])
            summary = "光缆接续完成，待埋深复测"
        elif action in {"burial_survey", "resurvey"}:
            if action == "resurvey":
                if not bool(p.get("rectified")):
                    raise ValidationError("现场整改尚未完成，不能提交复测")
                measured_at = _parse_iso(data.get("measured_at"), "measured_at")
                if measured_at < _parse_iso(p.get("rectified_at"), "rectified_at"):
                    raise ValidationError("复测时间早于整改完成时间，不能放行")
            else:
                measured_at = _parse_iso(data.get("measured_at"), "measured_at") if data.get("measured_at") else datetime.now(timezone.utc)
            points = _validate_burial_points(data, p)
            kind = "resurvey" if action == "resurvey" else "initial"
            rounds, round_summary = self._append_burial_round(p, points, kind, measured_at, str(p.get("vessel_name", "")))
            passed = bool(round_summary["passed"])
            changes["burial_rounds"] = rounds
            changes["burial_round_count"] = round_summary["round"]
            changes["burial_status"] = "passed" if passed else "pending"
            changes["burial_passed"] = passed
            changes["burial_min_depth_m"] = round_summary["min_depth_m"]
            changes["burial_min_distance_km"] = round_summary["min_distance_km"]
            changes["burial_gap_m"] = round_summary["gap_m"]
            changes["burial_measured_at"] = round_summary["measured_at"]
            new_state = "buried" if passed else "rectification_pending"
            if action == "resurvey":
                summary = "全段复测达标，埋深合格放行" if passed else "全段复测未达标，仍待整改"
            else:
                summary = "埋深复测合格" if passed else "埋深低于设计值%s米，停在待整改" % DESIGN_BURIAL_DEPTH_M
            burial = dict(round_summary)
            burial["points"] = points
        elif action == "rectify":
            rectified_at = _parse_iso(data.get("rectified_at"), "rectified_at")
            if p.get("rectified_at") and rectified_at < _parse_iso(p.get("rectified_at"), "rectified_at"):
                raise ValidationError("整改完成时间不能早于上次整改完成时间")
            changes["rectified"] = True
            changes["rectified_at"] = _iso(rectified_at)
            changes["rectification_note"] = optional_text(data, "rectification_note", "")
            summary = "现场整改完成，等待全段复测"
        elif action == "change_vessel":
            new_vessel = text(data, "new_vessel_name")
            current_vessel = str(p.get("vessel_name", ""))
            if not current_vessel:
                raise ValidationError("当前没有在役船舶")
            if new_vessel == current_vessel:
                raise ValidationError("新船不能与原船相同")
            released = list(p.get("released_vessels", []))
            released.append({"vessel_name": current_vessel, "released_at": _iso(datetime.now(timezone.utc)), "released_for_round": int(p.get("burial_round_count", 0))})
            changes["released_vessels"] = released
            changes["vessel_name"] = new_vessel
            summary = "整改期间换船，原船%s已释放，%s接手" % (current_vessel, new_vessel)
        elif action == "test":
            end_loss = number(data, "end_to_end_loss_db", 0)
            if end_loss > 0.5:
                raise ValidationError("端到端损耗不合格")
            changes["end_to_end_loss_db"] = end_loss
            changes["test_passed"] = True
            summary = "系统测试通过"
        elif action == "restore":
            if not boolean(data, "traffic_restored"):
                raise ValidationError("业务流量尚未恢复")
            changes["traffic_restored"] = True
            changes["restore_capacity_gbps"] = integer(data, "restore_capacity_gbps", 1)
            summary = "通信恢复"
        elif action == "cancel":
            changes["cancel_reason"] = text(data, "cancel_reason")
            summary = "抢修取消"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action), burial

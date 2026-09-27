"""跨海光缆故障与抢修协调领域规则与状态转换。"""
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, optional_text, text, text_list


INITIAL_STATE = "detected"
DEPTH_PENDING_STATE = "depth_pending"
DEPTH_VERIFIED_STATE = "depth_verified"
DESIGN_BURIAL_DEPTH_M = 1.5

CREATE_ROLES = {'noc_operator'}
ACTION_ROLES = {'approve': {'repair_manager'}, 'mobilize': {'vessel_master'}, 'survey': {'cable_engineer'}, 'splice': {'cable_engineer'}, 'depth_check': {'cable_engineer'}, 'rectify': {'vessel_master'}, 'resurvey': {'cable_engineer'}, 'test': {'noc_operator'}, 'restore': {'noc_operator', 'repair_manager'}, 'cancel': {'repair_manager'}}

_DYNAMIC_TARGET = object()
TRANSITIONS = {'approve': {'detected': 'approved'}, 'mobilize': {'approved': 'mobilized'}, 'survey': {'mobilized': 'surveyed'}, 'splice': {'surveyed': 'spliced'}, 'depth_check': {'spliced': _DYNAMIC_TARGET}, 'rectify': {'depth_pending': 'depth_pending'}, 'resurvey': {'depth_pending': _DYNAMIC_TARGET}, 'test': {'depth_verified': 'tested'}, 'restore': {'tested': 'restored'}, 'cancel': {'detected': 'cancelled', 'approved': 'cancelled', 'mobilized': 'cancelled'}}


def _parse_timestamp(data: Dict[str, Any], key: str) -> Tuple[datetime, str]:
    raw = text(data, key)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("%s必须是带时区的ISO时间" % key) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError("%s必须包含时区" % key)
    return parsed, parsed.isoformat()


def _parse_points(data: Dict[str, Any], payload: Dict[str, Any], key: str = "points") -> List[Dict[str, Any]]:
    raw = data.get(key)
    if not isinstance(raw, list) or not raw:
        raise ValidationError("复测测点至少需要一个")
    start_km = float(payload["start_km"])
    end_km = float(payload["end_km"])
    points: List[Dict[str, Any]] = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValidationError("测点必须是对象")
        distance = round(number(item, "distance_km", 0), 3)
        depth = round(number(item, "depth_m", 0), 3)
        if not (start_km <= distance <= end_km):
            raise ValidationError("测点距离超出抢修区段")
        if distance in seen:
            raise ValidationError("测点距离不能重复")
        seen.add(distance)
        points.append({"distance_km": distance, "depth_m": depth})
    points.sort(key=lambda point: point["distance_km"])
    return points


class DomainRules:
    INITIAL_STATE = INITIAL_STATE
    DEPTH_PENDING_STATE = DEPTH_PENDING_STATE
    DEPTH_VERIFIED_STATE = DEPTH_VERIFIED_STATE
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

    def require_transition(self, record: Dict[str, Any], action: str) -> Any:
        target = TRANSITIONS.get(action, {}).get(record["state"])
        if target is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return target

    @staticmethod
    def _depth_evaluation(points: List[Dict[str, Any]], measured_at: datetime, rectified_at: Optional[datetime]) -> Tuple[bool, Dict[str, Any], float, List[str]]:
        shallowest = min(points, key=lambda point: point["depth_m"])
        reasons: List[str] = []
        below = float(shallowest["depth_m"]) < DESIGN_BURIAL_DEPTH_M
        if below:
            reasons.append("below_design_depth")
        if rectified_at is not None and measured_at < rectified_at:
            reasons.append("measured_before_rectified")
        gap_m = round(DESIGN_BURIAL_DEPTH_M - float(shallowest["depth_m"]), 3) if below else 0.0
        return not reasons, shallowest, gap_m, reasons

    @staticmethod
    def _burial_status(previous: Dict[str, Any], round_no: int, kind: str, points: List[Dict[str, Any]], measured_at: str, passed: bool, shallowest: Dict[str, Any], gap_m: float, reasons: List[str]) -> Dict[str, Any]:
        status = dict(previous or {})
        status.update({
            "design_depth_m": DESIGN_BURIAL_DEPTH_M,
            "latest_round": round_no,
            "latest_kind": kind,
            "passed": bool(passed),
            "shallowest_distance_km": shallowest["distance_km"],
            "shallowest_depth_m": shallowest["depth_m"],
            "gap_m": gap_m,
            "measured_at": measured_at,
            "block_reasons": list(reasons),
        })
        if not status.get("baseline_distances_km"):
            status["baseline_distances_km"] = [point["distance_km"] for point in points]
        return status

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str, Optional[Dict[str, Any]]]:
        target_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        depth_round: Optional[Dict[str, Any]] = None
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
            summary = "光缆接续完成，等待埋深复测"
        elif action in ("depth_check", "resurvey"):
            previous_status = p.get("burial") or {}
            rectified_iso = previous_status.get("rectified_at")
            rectified_at = None
            if action == "resurvey":
                if not rectified_iso:
                    raise ValidationError("现场整改尚未完成，不能提交复测")
                rectified_at = datetime.fromisoformat(rectified_iso)
            measured_at, measured_iso = _parse_timestamp(data, "measured_at")
            points = _parse_points(data, p)
            if action == "resurvey":
                baseline = set(previous_status.get("baseline_distances_km", []))
                distances = {point["distance_km"] for point in points}
                if not baseline.issubset(distances):
                    raise ValidationError("复测必须覆盖全部历史测点，完成全段复测")
            passed, shallowest, gap_m, reasons = self._depth_evaluation(points, measured_at, rectified_at)
            round_no = int(previous_status.get("latest_round", 0)) + 1
            kind = "initial" if action == "depth_check" else "resurvey"
            status = self._burial_status(previous_status, round_no, kind, points, measured_iso, passed, shallowest, gap_m, reasons)
            changes["burial"] = status
            depth_round = {
                "round_no": round_no,
                "kind": kind,
                "points": points,
                "measured_at": measured_iso,
                "passed": bool(passed),
                "shallowest_distance_km": shallowest["distance_km"],
                "shallowest_depth_m": shallowest["depth_m"],
                "gap_m": gap_m,
                "reasons": reasons,
            }
            if passed:
                target_state = DEPTH_VERIFIED_STATE
                summary = "第%s轮埋深复测达标（共%s个测点），允许进入系统测试" % (round_no, len(points))
            else:
                target_state = DEPTH_PENDING_STATE
                detail = "最浅点%s公里处%s米，缺口%s米" % (shallowest["distance_km"], shallowest["depth_m"], gap_m)
                if "measured_before_rectified" in reasons:
                    summary = "第%s轮复测时间早于整改完成时间，不能放行；%s" % (round_no, detail) if "below_design_depth" in reasons else "第%s轮复测时间早于整改完成时间，不能放行" % round_no
                else:
                    summary = "第%s轮埋深复测低于设计值%s米，停在待整改：%s" % (round_no, DESIGN_BURIAL_DEPTH_M, detail)
        elif action == "rectify":
            rectified_at, rectified_iso = _parse_timestamp(data, "rectified_at")
            note = optional_text(data, "rectification_note", "")
            status = dict(p.get("burial") or {})
            old_vessel = optional_text(p, "vessel_name", "")
            new_vessel = optional_text(data, "new_vessel_name", "")
            vessel_changes = list(p.get("vessel_changes", []))
            if new_vessel and new_vessel != old_vessel:
                changes["vessel_name"] = new_vessel
                changes["released_vessel_name"] = old_vessel
                changes["vessel_released"] = True
                vessel_changes.append({"released_vessel_name": old_vessel, "vessel_name": new_vessel, "rectified_at": rectified_iso})
                status["vessel_released"] = True
                status["released_vessel_name"] = old_vessel
            changes["vessel_changes"] = vessel_changes
            changes["rectified_at"] = rectified_iso
            status["rectified_at"] = rectified_iso
            status["rectification_note"] = note
            status["passed"] = False
            status["block_reasons"] = ["awaiting_resurvey"]
            changes["burial"] = status
            summary = "现场整改完成，等待全段复测"
            if changes.get("vessel_released"):
                summary = "现场整改完成，原船%s已释放、%s进场，等待全段复测" % (old_vessel, new_vessel)
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
        if target_state is _DYNAMIC_TARGET:
            raise Conflict("内部错误：埋深复测未给出目标状态")
        return target_state, p, summary or ("已执行%s" % action), depth_round

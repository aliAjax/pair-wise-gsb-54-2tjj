import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {'cable': 'SEA-1', 'segment': 'S3', 'start_km': 120.0, 'end_km': 135.0, 'depth_m': 1800.0, 'sea_state': 3, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 400}
ENGINEER = Actor("ce-1", "cable_engineer")
MASTER = Actor("vm-1", "vessel_master")
NOC = Actor("noc-1", "noc_operator")

SHALLOW_POINTS = [{'distance_km': 122.0, 'depth_m': 1.8}, {'distance_km': 128.0, 'depth_m': 0.9}, {'distance_km': 134.0, 'depth_m': 1.6}]
FULL_POINTS = [{'distance_km': 122.0, 'depth_m': 1.8}, {'distance_km': 128.0, 'depth_m': 2.1}, {'distance_km': 134.0, 'depth_m': 1.5}]


def advance_to_spliced(service, reference="CABLE-40001"):
    record = service.create(Actor("creator", "noc_operator"), reference, CREATE_DATA)
    steps = [
        ('approve', Actor("rm", "repair_manager"), {'repair_manager': 'RM-2'}),
        ('mobilize', MASTER, {'weather_window_hours': 40, 'available_spare_km': 18, 'vessel_name': 'CS-1'}),
        ('survey', ENGINEER, {'survey_complete': True, 'fault_location_km': 128}),
        ('splice', ENGINEER, {'splice_loss_db': 0.12, 'spare_used_km': 16}),
    ]
    for action, actor, data in steps:
        record = service.act(actor, record["id"], record["version"], action, data)
    return record


class DepthGateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_initial_check_below_design_halts_at_pending_with_details(self):
        record = advance_to_spliced(self.service)
        record = self.service.act(ENGINEER, record["id"], record["version"], 'depth_check', {'measured_at': '2026-09-26T08:00:00+00:00', 'points': SHALLOW_POINTS})
        self.assertEqual(record["state"], "depth_pending")
        burial = record["payload"]["burial"]
        self.assertFalse(burial["passed"])
        self.assertEqual(burial["shallowest_distance_km"], 128.0)
        self.assertEqual(burial["shallowest_depth_m"], 0.9)
        self.assertEqual(burial["gap_m"], 0.6)
        self.assertEqual(burial["latest_kind"], "initial")
        self.assertEqual(burial["baseline_distances_km"], [122.0, 128.0, 134.0])
        # 列表里也能直接看到谁卡在埋深
        pending = self.service.list_records(NOC, state="depth_pending")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["payload"]["burial"]["gap_m"], 0.6)
        detail = self.service.get_record(NOC, record["id"])
        self.assertEqual(detail["updated_by"], "ce-1")
        with self.assertRaises(Conflict):
            self.service.act(NOC, record["id"], record["version"], 'test', {'end_to_end_loss_db': 0.3})

    def test_initial_check_at_design_boundary_passes(self):
        record = advance_to_spliced(self.service, "CABLE-40002")
        record = self.service.act(ENGINEER, record["id"], record["version"], 'depth_check', {'measured_at': '2026-09-26T08:00:00+00:00', 'points': FULL_POINTS})
        self.assertEqual(record["state"], "depth_verified")
        record = self.service.act(NOC, record["id"], record["version"], 'test', {'end_to_end_loss_db': 0.3})
        self.assertEqual(record["state"], "tested")

    def test_resurvey_requires_rectification_and_full_coverage(self):
        record = advance_to_spliced(self.service, "CABLE-40003")
        record = self.service.act(ENGINEER, record["id"], record["version"], 'depth_check', {'measured_at': '2026-09-26T08:00:00+00:00', 'points': SHALLOW_POINTS})
        with self.assertRaises(ValidationError):
            self.service.act(ENGINEER, record["id"], record["version"], 'resurvey', {'measured_at': '2026-09-26T09:00:00+00:00', 'points': FULL_POINTS})
        record = self.service.act(MASTER, record["id"], record["version"], 'rectify', {'rectified_at': '2026-09-26T12:00:00+00:00'})
        with self.assertRaises(ValidationError):
            self.service.act(ENGINEER, record["id"], record["version"], 'resurvey', {'measured_at': '2026-09-26T14:00:00+00:00', 'points': FULL_POINTS[:2]})

    def test_resurvey_earlier_than_rectification_is_blocked_but_kept(self):
        record = advance_to_spliced(self.service, "CABLE-40004")
        record = self.service.act(ENGINEER, record["id"], record["version"], 'depth_check', {'measured_at': '2026-09-26T08:00:00+00:00', 'points': SHALLOW_POINTS})
        record = self.service.act(MASTER, record["id"], record["version"], 'rectify', {'rectified_at': '2026-09-26T12:00:00+00:00'})
        record = self.service.act(ENGINEER, record["id"], record["version"], 'resurvey', {'measured_at': '2026-09-26T11:00:00+00:00', 'points': FULL_POINTS})
        self.assertEqual(record["state"], "depth_pending")
        burial = record["payload"]["burial"]
        self.assertIn("measured_before_rectified", burial["block_reasons"])
        self.assertEqual(burial["latest_round"], 2)
        rounds = self.service.depth_history(NOC, record["id"])
        self.assertEqual([round_item["passed"] for round_item in rounds], [False, False])

    def test_rectify_resurvey_pass_releases_and_restores_flow(self):
        record = advance_to_spliced(self.service, "CABLE-40005")
        record = self.service.act(ENGINEER, record["id"], record["version"], 'depth_check', {'measured_at': '2026-09-26T08:00:00+00:00', 'points': SHALLOW_POINTS})
        record = self.service.act(MASTER, record["id"], record["version"], 'rectify', {'rectified_at': '2026-09-26T12:00:00+00:00', 'new_vessel_name': 'CS-2', 'rectification_note': '冲埋加深'})
        self.assertEqual(record["state"], "depth_pending")
        self.assertEqual(record["payload"]["vessel_name"], "CS-2")
        self.assertEqual(record["payload"]["released_vessel_name"], "CS-1")
        self.assertTrue(record["payload"]["vessel_released"])
        self.assertEqual(record["payload"]["vessel_changes"][0]["released_vessel_name"], "CS-1")
        points = FULL_POINTS + [{'distance_km': 130.0, 'depth_m': 1.9}]
        record = self.service.act(ENGINEER, record["id"], record["version"], 'resurvey', {'measured_at': '2026-09-26T14:00:00+00:00', 'points': points})
        self.assertEqual(record["state"], "depth_verified")
        burial = record["payload"]["burial"]
        self.assertTrue(burial["passed"])
        self.assertEqual(burial["latest_round"], 2)
        rounds = self.service.depth_history(NOC, record["id"])
        self.assertEqual(len(rounds), 2)
        self.assertEqual([round_item["kind"] for round_item in rounds], ["initial", "resurvey"])
        self.assertEqual([round_item["round_no"] for round_item in rounds], [1, 2])
        self.assertEqual(len(rounds[0]["points"]), 3)
        record = self.service.act(NOC, record["id"], record["version"], 'test', {'end_to_end_loss_db': 0.3})
        record = self.service.act(NOC, record["id"], record["version"], 'restore', {'traffic_restored': True, 'restore_capacity_gbps': 400})
        self.assertEqual(record["state"], "restored")

    def test_rectify_without_vessel_swap_keeps_vessel(self):
        record = advance_to_spliced(self.service, "CABLE-40006")
        record = self.service.act(ENGINEER, record["id"], record["version"], 'depth_check', {'measured_at': '2026-09-26T08:00:00+00:00', 'points': SHALLOW_POINTS})
        record = self.service.act(MASTER, record["id"], record["version"], 'rectify', {'rectified_at': '2026-09-26T12:00:00+00:00'})
        self.assertEqual(record["payload"]["vessel_name"], "CS-1")
        self.assertNotIn("released_vessel_name", record["payload"])

    def test_invalid_points_and_timestamp(self):
        record = advance_to_spliced(self.service, "CABLE-40007")
        with self.assertRaises(ValidationError):
            self.service.act(ENGINEER, record["id"], record["version"], 'depth_check', {'measured_at': '2026-09-26T08:00:00+00:00', 'points': [{'distance_km': 150.0, 'depth_m': 2.0}]})
        with self.assertRaises(ValidationError):
            self.service.act(ENGINEER, record["id"], record["version"], 'depth_check', {'measured_at': '2026-09-26T08:00:00+00:00', 'points': [{'distance_km': 128.0, 'depth_m': 2.0}, {'distance_km': 128.0, 'depth_m': 1.0}]})
        with self.assertRaises(ValidationError):
            self.service.act(ENGINEER, record["id"], record["version"], 'depth_check', {'measured_at': '2026-09-26 08:00:00', 'points': FULL_POINTS})
        rounds = self.service.depth_history(NOC, record["id"])
        self.assertEqual(rounds, [])

    def test_depth_check_permission(self):
        record = advance_to_spliced(self.service, "CABLE-40008")
        with self.assertRaises(PermissionDenied):
            self.service.act(NOC, record["id"], record["version"], 'depth_check', {'measured_at': '2026-09-26T08:00:00+00:00', 'points': FULL_POINTS})
        with self.assertRaises(PermissionDenied):
            self.service.act(ENGINEER, record["id"], record["version"], 'rectify', {'rectified_at': '2026-09-26T12:00:00+00:00'})

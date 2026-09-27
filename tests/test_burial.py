import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {'cable': 'SEA-1', 'segment': 'S9', 'start_km': 120.0, 'end_km': 135.0, 'depth_m': 1800.0, 'sea_state': 3, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 400}
PASS_POINTS = [{'distance_km': 120.0, 'burial_depth_m': 1.8}, {'distance_km': 127.5, 'burial_depth_m': 2.0}, {'distance_km': 135.0, 'burial_depth_m': 1.6}]
SHALLOW_POINTS = [{'distance_km': 120.0, 'burial_depth_m': 1.8}, {'distance_km': 127.5, 'burial_depth_m': 1.2}, {'distance_km': 135.0, 'burial_depth_m': 1.6}]

ENGINEER = Actor("eng", "cable_engineer")
MASTER = Actor("master", "vessel_master")
NOC = Actor("noc", "noc_operator")


class BurialFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _to_spliced(self, reference="CABLE-90001"):
        record = self.service.create(Actor("creator", "noc_operator"), reference, CREATE_DATA)
        steps = [
            ("repair_manager", 'approve', {'repair_manager': 'RM-2'}),
            ("vessel_master", 'mobilize', {'weather_window_hours': 40, 'available_spare_km': 18, 'vessel_name': 'CS-1'}),
            ("cable_engineer", 'survey', {'survey_complete': True, 'fault_location_km': 128}),
            ("cable_engineer", 'splice', {'splice_loss_db': 0.12, 'spare_used_km': 16}),
        ]
        for role, action, data in steps:
            record = self.service.act(Actor("op", role), record["id"], record["version"], action, data)
        return record

    def test_pass_on_first_survey_goes_to_buried(self):
        record = self._to_spliced()
        record = self.service.act(ENGINEER, record["id"], record["version"], 'burial_survey', {'measured_at': '2026-09-26T10:00:00+00:00', 'burial_points': PASS_POINTS})
        self.assertEqual(record["state"], "buried")
        self.assertTrue(record["payload"]["burial_passed"])
        self.assertIsNone(record["blocking"])

    def test_shallow_survey_blocks_with_shallowest_and_gap(self):
        record = self._to_spliced()
        record = self.service.act(ENGINEER, record["id"], record["version"], 'burial_survey', {'measured_at': '2026-09-26T10:00:00+00:00', 'burial_points': SHALLOW_POINTS})
        self.assertEqual(record["state"], "rectification_pending")
        blocking = record["blocking"]
        self.assertEqual(blocking["reason"], "burial_depth_below_design")
        self.assertEqual(blocking["shallowest_point"], {"distance_km": 127.5, "burial_depth_m": 1.2})
        self.assertEqual(blocking["gap_m"], 0.3)
        self.assertEqual(blocking["vessel_name"], "CS-1")
        detail = self.service.get_record(ENGINEER, record["id"])
        self.assertEqual(detail["blocking"]["shallowest_point"]["burial_depth_m"], 1.2)
        items = self.service.list_records(ENGINEER, state="rectification_pending")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], record["id"])
        self.assertEqual(items[0]["blocking"]["gap_m"], 0.3)

    def test_testing_blocked_until_buried(self):
        record = self._to_spliced()
        with self.assertRaises(Conflict):
            self.service.act(NOC, record["id"], record["version"], 'test', {'end_to_end_loss_db': 0.3})
        record = self.service.act(ENGINEER, record["id"], record["version"], 'burial_survey', {'measured_at': '2026-09-26T10:00:00+00:00', 'burial_points': SHALLOW_POINTS})
        with self.assertRaises(Conflict):
            self.service.act(NOC, record["id"], record["version"], 'test', {'end_to_end_loss_db': 0.3})

    def test_resurvey_requires_rectification(self):
        record = self._to_spliced()
        record = self.service.act(ENGINEER, record["id"], record["version"], 'burial_survey', {'measured_at': '2026-09-26T10:00:00+00:00', 'burial_points': SHALLOW_POINTS})
        with self.assertRaises(ValidationError):
            self.service.act(ENGINEER, record["id"], record["version"], 'resurvey', {'measured_at': '2026-09-26T11:00:00+00:00', 'burial_points': PASS_POINTS})

    def test_resurvey_before_rectification_completion_rejected(self):
        record = self._to_spliced()
        record = self.service.act(ENGINEER, record["id"], record["version"], 'burial_survey', {'measured_at': '2026-09-26T10:00:00+00:00', 'burial_points': SHALLOW_POINTS})
        record = self.service.act(ENGINEER, record["id"], record["version"], 'rectify', {'rectified_at': '2026-09-26T12:00:00+00:00', 'rectification_note': '冲埋加深'})
        with self.assertRaises(ValidationError):
            self.service.act(ENGINEER, record["id"], record["version"], 'resurvey', {'measured_at': '2026-09-26T11:59:00+00:00', 'burial_points': PASS_POINTS})

    def test_resurvey_still_shallow_keeps_blocking(self):
        record = self._to_spliced()
        record = self.service.act(ENGINEER, record["id"], record["version"], 'burial_survey', {'measured_at': '2026-09-26T10:00:00+00:00', 'burial_points': SHALLOW_POINTS})
        record = self.service.act(ENGINEER, record["id"], record["version"], 'rectify', {'rectified_at': '2026-09-26T12:00:00+00:00'})
        record = self.service.act(ENGINEER, record["id"], record["version"], 'resurvey', {'measured_at': '2026-09-26T13:00:00+00:00', 'burial_points': SHALLOW_POINTS})
        self.assertEqual(record["state"], "rectification_pending")
        self.assertEqual(record["payload"]["burial_round_count"], 2)
        self.assertTrue(record["blocking"])

    def test_change_vessel_releases_original_but_keeps_history(self):
        record = self._to_spliced()
        record = self.service.act(ENGINEER, record["id"], record["version"], 'burial_survey', {'measured_at': '2026-09-26T10:00:00+00:00', 'burial_points': SHALLOW_POINTS})
        record = self.service.act(ENGINEER, record["id"], record["version"], 'rectify', {'rectified_at': '2026-09-26T12:00:00+00:00'})
        record = self.service.act(MASTER, record["id"], record["version"], 'change_vessel', {'new_vessel_name': 'CS-2'})
        self.assertEqual(record["state"], "rectification_pending")
        self.assertEqual(record["payload"]["vessel_name"], "CS-2")
        self.assertEqual(record["payload"]["released_vessels"][0]["vessel_name"], "CS-1")
        self.assertEqual(record["blocking"]["vessel_name"], "CS-2")

        history = self.service.burial_history(NOC, record["id"])
        first_round = [point for point in history if point["round"] == 1]
        self.assertEqual(len(first_round), 3)
        self.assertTrue(all(point["vessel_name"] == "CS-1" for point in first_round))

        with self.assertRaises(ValidationError):
            self.service.act(MASTER, record["id"], record["version"], 'change_vessel', {'new_vessel_name': 'CS-2'})

        record = self.service.act(ENGINEER, record["id"], record["version"], 'resurvey', {'measured_at': '2026-09-26T13:00:00+00:00', 'burial_points': PASS_POINTS})
        self.assertEqual(record["state"], "buried")
        history = self.service.burial_history(NOC, record["id"])
        self.assertEqual({point["round"] for point in history}, {1, 2})
        self.assertTrue(all(point["vessel_name"] == "CS-2" for point in history if point["round"] == 2))

        record = self.service.act(NOC, record["id"], record["version"], 'test', {'end_to_end_loss_db': 0.3})
        record = self.service.act(Actor("rm", "repair_manager"), record["id"], record["version"], 'restore', {'traffic_restored': True, 'restore_capacity_gbps': 400})
        self.assertEqual(record["state"], "restored")

    def test_points_must_cover_full_segment(self):
        record = self._to_spliced()
        gaps = [{'distance_km': 120.0, 'burial_depth_m': 2.0}, {'distance_km': 130.0, 'burial_depth_m': 2.0}]
        with self.assertRaises(ValidationError):
            self.service.act(ENGINEER, record["id"], record["version"], 'burial_survey', {'burial_points': gaps})
        outside = PASS_POINTS + [{'distance_km': 140.0, 'burial_depth_m': 2.0}]
        with self.assertRaises(ValidationError):
            self.service.act(ENGINEER, record["id"], record["version"], 'burial_survey', {'burial_points': outside})

    def test_change_vessel_requires_vessel_master(self):
        record = self._to_spliced()
        record = self.service.act(ENGINEER, record["id"], record["version"], 'burial_survey', {'measured_at': '2026-09-26T10:00:00+00:00', 'burial_points': SHALLOW_POINTS})
        with self.assertRaises(PermissionDenied):
            self.service.act(ENGINEER, record["id"], record["version"], 'change_vessel', {'new_vessel_name': 'CS-2'})

"""Synthetic coverage for destination labels and playback relationships."""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from homed_extract import _media_targets
from generate_homekit_reports import action_rule, summarize, build_device_room_lookup
from generate_inspector import row_to_rule


class ActionDestinationTests(unittest.TestCase):
    def test_configured_service_label_preserves_accessory_identity(self):
        action = {
            "actionType": "characteristicWrite", "accessoryName": "Factory Strip",
            "serviceConfiguredName": "Desk Light", "serviceName": "Desk Light",
            "room": "Example Room", "characteristic": "Power State",
            "targetValueRaw": "hex:08",
        }
        row = summarize({"actionSets": [{"actions": [action]}]}, 1)
        rule = row_to_rule(row, {"Factory Strip": "Example Room"})
        self.assertEqual(rule["actions"], ["Factory Strip"])
        self.assertEqual(rule["actionLabels"], ["Desk Light (Example Room)"])
        self.assertEqual(rule["then"], ["Turn off: Desk Light (Example Room)"])
        # An unconfigured generic service must not replace the accessory label.
        action["serviceConfiguredName"] = ""
        action["serviceName"] = "Lightbulb"
        self.assertIn("Factory Strip (Example Room)", action_rule(action))
        action["serviceName"] = "Reading Strip"
        self.assertIn("Reading Strip (Example Room)", action_rule(action))
        action["serviceProvidedName"] = "Reading Strip"
        self.assertIn("Factory Strip (Example Room)", action_rule(action))

    def test_versioned_playback_join_filters_home_and_keeps_unnamed_targets(self):
        with sqlite3.connect(":memory:") as db:
            db.executescript("""
                CREATE TABLE ZMKFACCESSORY (Z_PK INTEGER);
                INSERT INTO ZMKFACCESSORY VALUES (1), (2), (3);
                CREATE TABLE Z_77ACTIONMEDIAPLAYBACKS_ (
                    Z_77ACCESSORIES_ INTEGER, Z_88ACTIONMEDIAPLAYBACKS_ INTEGER
                );
                INSERT INTO Z_77ACTIONMEDIAPLAYBACKS_ VALUES
                    (1, 10), (2, 10), (3, 10), (1, 20);
            """)
            checks = []
            targets = _media_targets(db.cursor(), 10, {
                1: {"name": "Speaker", "room": "Example Room"},
                2: {"name": "", "providedName": "", "room": "Other Room"},
            }, checks)
        self.assertEqual([t["accessoryId"] for t in targets], [1, 2])
        self.assertEqual([c["status"] for c in checks], ["present", "present", "outside-selected-home"])
        action = {"actionType": "mediaPlayback", "state": 2, "targets": targets}
        row = summarize({"actionSets": [{"actions": [action]}]}, 1)
        rooms = build_device_room_lookup({}, [row])
        rule = row_to_rule(row, rooms)
        self.assertEqual(rule["rawCounts"]["actions"], 1)
        self.assertEqual(set(rule["rooms"]), {"Example Room", "Other Room"})
        self.assertIn("Speaker (Example Room)", rule["then"][0])
        self.assertIn("Unnamed audio accessory (Other Room)", rule["then"][0])

    def test_repeated_speaker_names_retain_all_rooms(self):
        action = {"actionType": "mediaPlayback", "state": 2, "targets": [
            {"accessoryName": "Speaker", "room": "First Room"},
            {"accessoryName": "Speaker", "room": "Second Room"},
        ]}
        row = summarize({"actionSets": [{"actions": [action]}]}, 1)
        rule = row_to_rule(row, build_device_room_lookup({}, [row]))
        self.assertEqual(set(rule["rooms"]), {"First Room", "Second Room"})
        self.assertEqual(len(rule["actionLabels"]), 2)
        ambiguous = row_to_rule(row, {"Speaker": "Unrelated Room"})
        self.assertEqual(set(ambiguous["rooms"]), {"First Room", "Second Room"})

    def test_absent_playback_join_preserves_legacy_summary(self):
        with sqlite3.connect(":memory:") as db:
            self.assertEqual(_media_targets(db.cursor(), 1, {}), [])
        self.assertEqual(action_rule({"actionType": "mediaPlayback", "state": 2}),
                         "pausar/detener reproduccion multimedia")


if __name__ == "__main__":
    unittest.main()

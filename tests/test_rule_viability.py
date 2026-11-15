"""Synthetic evidence for orphan classification and start/end separation."""
import plistlib
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from rule_analysis import analyze_rule, condition_reference_issues
from generate_homekit_reports import summarize, recompute_filtered_when_rules
from generate_inspector import row_to_rule
from homed_extract import _extract_events, _resolve_characteristic, _service_reference_checks, decode_calendar_identifier


def rule(events, actions=None, **extra):
    return row_to_rule(summarize({"triggerType": "event", "enabled": True, "events": events,
        "actionSets": [{"actions": actions or []}], **extra}, 1), {})


def broken_start():
    return {"eventRole": "start", "eventType": "charValue", "serviceReference": 99,
            "referenceChecks": [{"kind": "service", "id": 99, "status": "missing"}]}


class RuleViabilityTests(unittest.TestCase):
    def test_proven_missing_trigger_target_is_orphaned_with_visible_evidence(self):
        result = rule([broken_start()], eventGraphComplete=True)
        self.assertTrue(result["isOrphaned"])
        self.assertTrue(result["needsReview"])
        self.assertEqual(result["structureStatus"], "Orphaned rule")
        self.assertEqual(result["referenceIssues"][0]["id"], 99)
        self.assertEqual(len(result["orphanEvidence"]), 1)

    def test_alternative_start_can_keep_rule_viable(self):
        solar = {"eventRole": "start", "eventType": "significantTime", "significantEvent": "sunset"}
        result = rule([broken_start(), solar], eventGraphComplete=True)
        self.assertFalse(result["isOrphaned"])
        self.assertEqual(result["triggerStatus"], "partial")
        self.assertTrue(result["needsReview"])

    def test_all_alternatives_must_be_proven_impossible(self):
        self.assertTrue(rule([broken_start(), broken_start()], eventGraphComplete=True)["isOrphaned"])
        unknown = {"eventRole": "start", "eventType": "unsupported-event"}
        self.assertFalse(rule([broken_start(), unknown], eventGraphComplete=True)["isOrphaned"])
        legacy = broken_start()
        legacy.pop("eventRole")
        self.assertFalse(rule([legacy], eventGraphComplete=True)["isOrphaned"])
        outside = broken_start()
        outside["referenceChecks"][0]["status"] = "outside-selected-home"
        self.assertFalse(rule([outside], eventGraphComplete=True)["isOrphaned"])

    def test_names_enabled_state_and_absence_of_start_are_not_orphan_proofs(self):
        for events in [[], [{"eventRole": "end", "eventType": "duration", "durationSeconds": 600}]]:
            result = rule(events, [{"actionType": "mediaPlayback", "state": 2}],
                          name="Old Remote Button 1", enabled=False)
            self.assertFalse(result["isOrphaned"])
            self.assertEqual(result["triggerStatus"], "missing")
        self.assertFalse(rule([])["isOrphaned"])

    def test_verified_complete_graph_without_start_events_is_orphaned(self):
        for events in [[], [{"eventRole": "end", "eventType": "duration", "durationSeconds": 600}]]:
            result = rule(events, [{"actionType": "mediaPlayback", "state": 2}], eventGraphComplete=True)
            self.assertTrue(result["isOrphaned"])
            self.assertTrue(result["missingStartEvent"])
            self.assertIsNone(result["orphanEvidence"][0]["startEvent"])
        empty = rule([], eventGraphComplete=True)
        self.assertTrue(empty["isOrphaned"])
        self.assertTrue(empty["isEmptyRule"])
        self.assertEqual(empty["structureStatus"], "Orphaned rule · empty")

    def test_unknown_graph_or_other_trigger_type_cannot_prove_absence(self):
        for complete in [False, None]:
            self.assertFalse(rule([], eventGraphComplete=complete)["isOrphaned"])
            self.assertFalse(rule([broken_start()], eventGraphComplete=complete)["isOrphaned"])
        self.assertFalse(rule([], triggerType="timer", eventGraphComplete=True)["isOrphaned"])

    def test_broken_action_does_not_prevent_start(self):
        event = {"eventRole": "start", "eventType": "calendar", "dateComponents": {"hour": 6, "minute": 0}}
        action = {"actionType": "characteristicWrite", "accessoryId": 8,
                  "referenceChecks": [{"kind": "accessory", "id": 8, "status": "missing"}]}
        result = rule([event], [action])
        self.assertFalse(result["isOrphaned"])
        self.assertTrue(result["needsReview"])
        self.assertEqual(result["referenceIssues"][0]["part"], "THEN")

    def test_unresolved_condition_reference_is_reviewed_without_false_orphan_claim(self):
        issue = condition_reference_issues({"kind": "characteristicRef", "value": ["example-uuid", 1, 2], "ref": None})
        row = summarize({"triggerType": "event", "events": [{"eventRole": "start", "eventType": "presence"}]}, 1)
        row["conditionReferenceIssues"] = issue
        result = row_to_rule(row, {})
        self.assertFalse(result["isOrphaned"])
        self.assertTrue(result["needsReview"])
        self.assertEqual(result["referenceIssues"][0]["part"], "IF")

    def test_calendar_impossibility_requires_known_calendar_and_handles_leap_days(self):
        event = {"eventRole": "start", "eventType": "calendar", "calendarIdentifier": "gregorian",
                 "dateComponents": {"month": 2, "day": 30}}
        self.assertTrue(rule([event], eventGraphComplete=True)["isOrphaned"])
        self.assertFalse(rule([{**event, "calendarIdentifier": "unknown"}], eventGraphComplete=True)["isOrphaned"])
        self.assertFalse(rule([{**event, "dateComponents": {"month": 2, "day": 29}}], eventGraphComplete=True)["isOrphaned"])
        self.assertTrue(rule([{**event, "dateComponents": {"year": 2025, "month": 2, "day": 29}}], eventGraphComplete=True)["isOrphaned"])
        self.assertFalse(rule([{**event, "dateComponents": {"year": 2024, "month": 2, "day": 29}}], eventGraphComplete=True)["isOrphaned"])

    def test_solar_start_and_duration_end_are_separate(self):
        events = [
            {"eventRole": "end", "eventType": "duration", "durationSeconds": 10800},
            {"eventRole": "start", "eventType": "significantTime", "significantEvent": "sunset", "offsetSeconds": 7200},
        ]
        row = summarize({"triggerType": "event", "events": events, "recurrenceDaysMask": 127}, 1)
        recompute_filtered_when_rules(row)
        result = row_to_rule(row, {})
        self.assertEqual(result["when"], ["Daily · 2 hours after sunset"])
        self.assertEqual(result["end"], ["Restore previous state after 3 hours"])
        self.assertEqual(result["startEventCount"], 1)
        self.assertEqual(result["endEventCount"], 1)
        self.assertEqual(result["rawCounts"]["events"], 2)
        self.assertFalse(result["hasCompoundTrigger"])
        self.assertFalse(result["needsReview"])

    def test_legacy_duration_is_not_silently_reclassified_as_an_end(self):
        result = rule([{"eventType": "duration"}])
        self.assertEqual(result["triggerStatus"], "unresolved")
        self.assertEqual(result["end"], [])
        self.assertFalse(result["isOrphaned"])

    def test_execute_once_does_not_display_daily_recurrence(self):
        result = rule([{"eventRole": "start", "eventType": "calendar", "dateComponents": {"hour": 6, "minute": 0}}],
                      recurrenceDaysMask=127, executeOnce=True)
        self.assertEqual(result["when"], ["Once · At 06:00"])

    def test_unknown_end_duration_needs_review_without_invalidating_start(self):
        result = rule([
            {"eventRole": "start", "eventType": "calendar", "dateComponents": {"hour": 6, "minute": 0}},
            {"eventRole": "end", "eventType": "duration"},
        ])
        self.assertTrue(result["needsReview"])
        self.assertFalse(result["isOrphaned"])
        self.assertEqual(result["triggerStatus"], "non-device")


class ReferenceExtractionTests(unittest.TestCase):
    def test_missing_reference_is_verified_against_database_not_display_names(self):
        with sqlite3.connect(":memory:") as db:
            db.executescript("""
                CREATE TABLE ZMKFSERVICE (Z_PK INTEGER, ZACCESSORY INTEGER);
                CREATE TABLE ZMKFACCESSORY (Z_PK INTEGER);
                INSERT INTO ZMKFSERVICE VALUES (2,5), (3,6);
                INSERT INTO ZMKFACCESSORY VALUES (6);
            """)
            self.assertEqual(_service_reference_checks(db.cursor(), 1, {})[0]["status"], "missing")
            checks = _service_reference_checks(db.cursor(), 2, {})
            self.assertEqual(checks[1], {"kind": "accessory", "id": 5, "status": "missing"})
            self.assertEqual(_service_reference_checks(db.cursor(), 3, {})[0]["status"], "outside-selected-home")

    def test_characteristic_instance_does_not_fall_back_to_an_unrelated_primary_key(self):
        with sqlite3.connect(":memory:") as db:
            db.execute("CREATE TABLE ZMKFCHARACTERISTIC (Z_PK INTEGER, ZSERVICE INTEGER, ZINSTANCEID INTEGER)")
            db.execute("INSERT INTO ZMKFCHARACTERISTIC VALUES (7,22,1)")
            info, check = _resolve_characteristic(db.cursor(), 7, 33, {7: {"serviceId": 22}}, {})
            self.assertEqual(info, {})
            self.assertEqual(check["status"], "missing")

    def test_end_flag_duration_and_dangling_start_are_preserved(self):
        with sqlite3.connect(":memory:") as db:
            db.executescript("""
                CREATE TABLE ZMKFEVENT (Z_PK INTEGER, Z_ENT INTEGER, ZSIGNIFICANTEVENT TEXT,
                    ZPRESENCETYPE INTEGER, ZOFFSETSECONDS INTEGER, ZDURATION REAL,
                    ZEVENTVALUE BLOB, ZCHARACTERISTICID INTEGER, ZSERVICE INTEGER,
                    ZFIREDATECOMPONENTS BLOB, ZTRIGGER INTEGER, ZENDEVENT INTEGER);
                CREATE TABLE ZMKFSERVICE (Z_PK INTEGER, ZACCESSORY INTEGER);
                CREATE TABLE ZMKFCHARACTERISTIC (Z_PK INTEGER, ZSERVICE INTEGER, ZINSTANCEID INTEGER);
                INSERT INTO ZMKFEVENT VALUES (1,91,NULL,NULL,NULL,NULL,X'01',7,99,NULL,1,0);
                INSERT INTO ZMKFEVENT VALUES (2,92,NULL,NULL,NULL,600,NULL,NULL,NULL,NULL,1,1);
            """)
            events = _extract_events(db.cursor(), 1, {}, {}, {}, {91: "charValue", 92: "duration"})
        self.assertEqual(events[1]["eventRole"], "end")
        self.assertEqual(events[1]["durationSeconds"], 600)
        result = rule(events, eventGraphComplete=True)
        self.assertTrue(result["isOrphaned"])
        self.assertEqual(result["startEventCount"], 1)

    def test_calendar_identifier_is_explicit_not_assumed(self):
        archive = {"$objects": ["$null", {"$class": plistlib.UID(2), "NS.calendar": plistlib.UID(3)},
                                {"$classname": "NSDateComponents"},
                                {"$class": plistlib.UID(4), "NS.identifier": plistlib.UID(5)},
                                {"$classname": "NSCalendar"}, "gregorian"]}
        self.assertEqual(decode_calendar_identifier(plistlib.dumps(archive, fmt=plistlib.FMT_BINARY)), "gregorian")
        archive["$objects"][1].pop("NS.calendar")
        self.assertIsNone(decode_calendar_identifier(plistlib.dumps(archive, fmt=plistlib.FMT_BINARY)))
        self.assertIsNone(decode_calendar_identifier(b"unsupported"))


if __name__ == "__main__":
    unittest.main()

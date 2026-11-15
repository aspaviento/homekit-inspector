"""Portable scalar decoding and button assignment regressions."""
import sqlite3
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from generate_homekit_reports import decode_homekit_scalar, event_rule, summarize, action_rule
from generate_inspector import row_to_rule, attach_hue_button_assignments, build_payload, write_html
from homed_extract import _extract_events


class ScalarTests(unittest.TestCase):
    def test_opack_scalars_preserve_types_and_compact_integer_offset(self):
        for raw, expected in [("01", True), ("02", False), ("07", -1), ("08", 0),
                              ("09", 1), ("0a", 2), ("12", 10), ("1c", 20),
                              ("26", 30), ("2f", 39), ("3064", 100), ("310001", 256)]:
            with self.subTest(raw=raw):
                result = decode_homekit_scalar("hex:" + raw, "uint8")
                self.assertEqual(result, expected)
                self.assertIs(type(result), type(expected))
        self.assertEqual(decode_homekit_scalar(2, "uint8"), 2)
        self.assertEqual(decode_homekit_scalar("hex:35" + struct.pack("<f", 50).hex()), 50)
        self.assertEqual(decode_homekit_scalar("hex:36" + struct.pack("<d", 60.5).hex()), 60.5)

    def test_unsupported_or_truncated_opack_remains_visible(self):
        for raw in ["hex:", "hex:00", "hex:03", "hex:30", "hex:300001", "hex:3500", "hex:d001", "hex:zz"]:
            self.assertEqual(decode_homekit_scalar(raw), raw)
        for number in [float("nan"), float("inf"), float("-inf")]:
            raw = "hex:36" + struct.pack("<d", number).hex()
            self.assertEqual(decode_homekit_scalar(raw), raw)

    def test_context_distinguishes_off_closed_absent_and_button_press(self):
        event = {"accessory": "Example Accessory", "service": "Switch", "room": "Example Room",
                 "eventValueRaw": "hex:02", "format": "uint8"}
        for characteristic, suffix in [
            ("Active", "turns off"), ("Power State", "turns off"),
            ("Contact Sensor State", "se cierra"), ("Occupancy Detected", "stops detecting occupancy"),
            ("Motion Detected", "stops detecting motion"),
        ]:
            self.assertEqual(event_rule({**event, "characteristic": characteristic}),
                             "Example Accessory (Example Room) " + suffix)
        self.assertIn("se abre", event_rule({**event, "characteristic": "Contact Sensor State", "eventValueRaw": "hex:01"}))
        event.update(characteristic="Programmable Switch Event", serviceProvidedName="Hue dimmer switch button 3")
        for raw, label in [("08", "Single Press"), ("09", "Double Press"), ("0a", "Long Press")]:
            text = event_rule({**event, "eventValueRaw": "hex:" + raw})
            self.assertIn("Example Accessory (Example Room) / Button 3", text)
            self.assertTrue(text.endswith(label))

    def test_power_actions_use_serialization_not_vendor_or_name(self):
        for raw, label in [("01", "on"), ("09", "on"), ("02", "off"), ("08", "off")]:
            action = {"actionType": "characteristicWrite", "accessoryName": "Example Light",
                      "characteristic": "Power State", "format": "bool", "targetValueRaw": "hex:" + raw}
            self.assertEqual(action_rule(action), f"Turn {label}: Example Light")

    def test_numeric_encodings_keep_enum_and_percentage_meanings(self):
        for characteristic, raw, meaning in [
            ("Brightness", "hex:12", "10"), ("Target Position", "hex:26", "30"),
            ("Security System Target State", "hex:08", "Home"),
            ("Security System Target State", "hex:09", "Away"),
            ("Security System Target State", "hex:0a", "Night"),
            ("Security System Target State", "hex:0b", "Disarmed"),
        ]:
            action = {"actionType": "characteristicWrite", "accessoryName": "Example Accessory",
                      "characteristic": characteristic, "format": "uint8", "targetValueRaw": raw}
            self.assertTrue(action_rule(action).endswith(" = " + meaning))


class RuleStructureTests(unittest.TestCase):
    def test_calendar_and_presence_do_not_need_a_trigger_device(self):
        for event in [{"eventType": "calendar", "dateComponents": {"hour": 1, "minute": 0}},
                      {"eventType": "presence", "presenceType": 1}]:
            row = summarize({"events": [event]}, 1)
            rule = row_to_rule(row, {})
            self.assertNotIn("review", rule["confidence"])
            self.assertEqual(rule["triggerStatus"], "non-device")

    def test_missing_unresolved_and_empty_rules_are_distinct(self):
        action = {"actionType": "mediaPlayback", "state": 2}
        missing = row_to_rule(summarize({"actionSets": [{"actions": [action]}]}, 1), {})
        self.assertEqual(missing["structureStatus"], "Missing start event")
        unresolved = row_to_rule(summarize({"events": [{"eventType": "duration"}]}, 2), {})
        self.assertEqual(unresolved["structureStatus"], "Trigger not resolved")
        empty = row_to_rule(summarize({}, 3), {})
        self.assertEqual(empty["structureStatus"], "Empty rule · possibly residual")
        self.assertNotIn("Hue", empty["structureStatus"])

    def test_raw_unknown_event_value_is_flagged_and_preserved(self):
        event = {"eventType": "charValue", "accessory": "Example", "characteristic": "Active",
                 "eventValueRaw": "hex:ff"}
        rule = row_to_rule(summarize({"events": [event]}, 1), {})
        self.assertTrue(rule["hasUnresolvedValues"])
        self.assertEqual(rule["rawDetails"]["events"][0]["eventValueRaw"], "hex:ff")

    def test_filtered_event_references_stay_out_of_display_and_usage(self):
        row = summarize({"events": [{"eventType": "charValue", "accessory": "Example Sensor",
                                   "accessoryId": 9, "room": "Example Room"}]}, 1)
        row["eventDevices"] = []
        rule = row_to_rule(row, {"Example Sensor": "Example Room"})
        self.assertEqual(rule["eventLabels"], [])
        self.assertEqual(rule["eventAccessoryIds"], [])
        self.assertEqual(rule["rooms"], [])
        self.assertEqual(len(rule["rawDetails"]["events"]), 1)

    @unittest.skipUnless(shutil.which("node"), "Node is required for generated JavaScript validation")
    def test_generated_ui_renders_buttons_raw_details_and_id_based_usage(self):
        row = summarize({"name": "Example Rule", "enabled": True, "events": [{
            "eventType": "charValue", "characteristic": "Programmable Switch Event",
            "accessory": "Duplicate Name", "accessoryId": 1, "serviceId": 3,
            "serviceProvidedName": "Hue dimmer switch button 1", "eventValueRaw": "hex:08",
        }], "actionSets": [{"actions": [{
            "actionType": "characteristicWrite", "characteristic": "Power State",
            "accessoryName": "Duplicate Name", "accessoryId": 2,
            "serviceConfiguredName": "<unsafe>", "targetValueRaw": "hex:02",
        }]}]}, 1)
        payload = build_payload({}, [row])
        payload["layout"] = {"roomsWithoutZone": [{"name": "Example Room", "accessories": [{
            "id": 1, "name": "Duplicate Name", "manufacturer": "Signify Netherlands B.V.",
            "model": "RWL021", "services": [
                {"id": i, "serviceType": "00000089-0000-1000-8000-0026BB765291",
                 "providedName": f"Hue dimmer switch button {n}"} for i, n in [(3, 1), (4, 2)]
            ],
        }, {"id": 2, "name": "Duplicate Name", "services": []}]}]}
        attach_hue_button_assignments(payload["layout"], [row])

        class ScriptParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.active = False
                self.blocks = []

            def handle_starttag(self, tag, attrs):
                if tag == "script":
                    self.active = True
                    self.blocks.append([dict(attrs).get("type", ""), ""])

            def handle_endtag(self, tag):
                if tag == "script":
                    self.active = False

            def handle_data(self, data):
                if self.active:
                    self.blocks[-1][1] += data

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.html"
            write_html(payload, path)
            parser = ScriptParser()
            parser.feed(path.read_text())
            for index, (kind, script) in enumerate(parser.blocks):
                if kind == "application/json":
                    json.loads(script)
                    continue
                script_path = Path(tmp) / f"script-{index}.js"
                script_path.write_text(script)
                subprocess.run(["node", "--check", str(script_path)], check=True, capture_output=True)
                # Exercise rendering functions without replacing the browser's DOM.
                script = script.replace("\ninit();\n", "\n")
                harness = '''
const vm = require('node:vm');
const fs = require('node:fs');
const script = fs.readFileSync(process.argv[2], 'utf8');
const payload = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const checks = `
  const accessory = data.layout.roomsWithoutZone[0].accessories[0];
  const block = hueButtonBlock(accessory);
  if (!block.includes('HomeKit assignment') || !block.includes('Behavior configured in Hue')) throw Error('Missing button behaviors');
  if (!roomCard(data.layout.roomsWithoutZone[0]).includes('Hue buttons')) throw Error('Buttons missing from Home Layout');
  const detail = automationDetailHtml(data.rules[0]);
  if (!detail.includes('&lt;unsafe&gt;') || !detail.includes('hex:02')) throw Error('Missing escaped labels or raw values');
  const emptyOrphanDetail = automationDetailHtml({...data.rules[0], orphanEvidence: [{startEvent: null, reason: 'Complete event graph has no associated start events'}]});
  if (!emptyOrphanDetail.includes('Complete event graph') || emptyOrphanDetail.includes('Start event null')) throw Error('Missing graph evidence rendered as an event index');
  const usage = buildAutomationUsage();
  if (usage.get('1')?.[0]?.role !== 'WHEN' || usage.get('2')?.[0]?.role !== 'THEN') throw Error('Duplicate names lost identifier relationships');
  data.rules.push({...data.rules[0], id: 2, name: 'Orphan Example', isOrphaned: true, needsReview: true, confidence: 'review'});
  data.rules.push({...data.rules[0], id: 3, name: 'Action Issue Example', isOrphaned: false, needsReview: true, confidence: 'review'});
  els.confidence.value = 'orphaned';
  if (filteredRules().length !== 1 || filteredRules()[0].id !== 2) throw Error('Orphan filter includes other review cases');
  els.confidence.value = 'review';
  if (filteredRules().length !== 2) throw Error('Needs review lost orphan or action issues');
`;
vm.runInNewContext(script + checks, {
  document: {getElementById: id => id === 'payload' ? {textContent: JSON.stringify(payload)} : {value: ''}, querySelector: () => ({}), querySelectorAll: () => []},
  window: {addEventListener() {}}, localStorage: {getItem() {return null;}}, structuredClone,
});
'''
                script_path.write_text(script)
                harness_path = Path(tmp) / "harness.js"
                harness_path.write_text(harness)
                payload_path = Path(tmp) / "payload.json"
                payload_path.write_text(json.dumps(payload))
                subprocess.run(["node", str(harness_path), str(script_path), str(payload_path)],
                               check=True, capture_output=True, text=True)


class HueButtonTests(unittest.TestCase):
    def layout(self):
        return {"roomsWithoutZone": [{"name": "Example Room", "accessories": [{
            "id": 1, "name": "Renamed Remote", "manufacturer": "Signify Netherlands B.V.", "model": "RWL021",
            "services": [{"id": i, "serviceType": "00000089-0000-1000-8000-0026BB765291",
                          "providedName": f"Hue dimmer switch button {i}", "originalName": "Same Name"}
                         for i in range(1, 5)],
        }]}]}

    def test_assignments_match_service_ids_not_rule_names(self):
        layout = self.layout()
        row = summarize({"name": "Unrelated Rule Name", "enabled": False, "events": [{
            "characteristic": "Programmable Switch Event", "serviceId": 2,
            "accessoryId": 1, "eventValueRaw": "hex:08", "accessory": "Renamed Remote",
        }]}, 1)
        orphan = summarize({"name": "Hue dimmer switch button 1 Example Single Press"}, 2)
        attach_hue_button_assignments(layout, [row, orphan])
        buttons = layout["roomsWithoutZone"][0]["accessories"][0]["hueButtons"]
        self.assertEqual(len(buttons), 4)
        self.assertEqual(buttons[0]["behavior"], "Behavior configured in Hue")
        self.assertEqual(buttons[1]["behavior"], "HomeKit assignment")
        self.assertFalse(buttons[1]["assignments"][0]["enabled"])
        self.assertEqual(buttons[1]["assignments"][0]["name"], "Unrelated Rule Name")
        self.assertEqual(orphan["eventDevices"], [])

    def test_unknown_assignment_ids_do_not_imply_hue_fallback(self):
        layout = self.layout()
        row = summarize({"events": [{"characteristic": "Programmable Switch Event", "accessory": "Example"}]}, 1)
        attach_hue_button_assignments(layout, [row])
        self.assertIn("unavailable", layout["roomsWithoutZone"][0]["accessories"][0]["hueButtons"][0]["behavior"])
        other = self.layout()
        other["roomsWithoutZone"][0]["accessories"][0]["manufacturer"] = "Other Manufacturer"
        attach_hue_button_assignments(other, [])
        self.assertNotIn("hueButtons", other["roomsWithoutZone"][0]["accessories"][0])

    def test_extracted_events_retain_relationship_ids_and_rooms(self):
        with sqlite3.connect(":memory:") as db:
            db.executescript("""
                CREATE TABLE ZMKFEVENT (Z_ENT INTEGER, ZSIGNIFICANTEVENT TEXT,
                    ZPRESENCETYPE INTEGER, ZOFFSETSECONDS INTEGER, ZDURATION INTEGER,
                    ZEVENTVALUE BLOB, ZCHARACTERISTICID INTEGER, ZSERVICE INTEGER,
                    ZFIREDATECOMPONENTS BLOB, ZTRIGGER INTEGER);
                INSERT INTO ZMKFEVENT VALUES (91,NULL,NULL,NULL,NULL,X'08',1,22,NULL,7);
            """)
            characteristic = {"description": "Programmable Switch Event", "format": "uint8",
                              "serviceId": 22, "accessoryId": 8, "room": "Example Room",
                              "accessory": "Example Remote", "service": "Example Remote"}
            events = _extract_events(db.cursor(), 7, {}, {(22, 1): characteristic}, {
                22: {"accessoryId": 8, "room": "Example Room", "providedName": "Hue dimmer switch button 2"},
            }, {91: "charValue"})
        self.assertEqual(events[0]["serviceId"], 22)
        self.assertEqual(events[0]["accessoryId"], 8)
        self.assertEqual(events[0]["room"], "Example Room")


if __name__ == "__main__":
    unittest.main()

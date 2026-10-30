import subprocess
import json
import base64
import struct
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from generate_inspector import load_manufacturer_logos, write_html  # noqa: E402


class FilterMarkupParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.filter_tabs = set()
        self.select_ids = set()

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if attributes.get("data-filter-tab"):
            self.filter_tabs.add(attributes["data-filter-tab"])
        if tag == "select" and attributes.get("id"):
            self.select_ids.add(attributes["id"])


class GenerateInspectorTests(unittest.TestCase):
    def test_manufacturer_logos_are_embedded_with_conservative_matching(self):
        logos = load_manufacturer_logos({"Apple Inc.", "  sHeLlY  ", "Ring", "Eufy", "Signify Netherlands B.V.", "Philips", "", "Unknown"})
        self.assertEqual(set(logos), {"Apple Inc.", "  sHeLlY  ", "Ring", "Eufy", "Signify Netherlands B.V."})
        for value in logos.values():
            self.assertTrue(value.startswith("data:image/png;base64,"))
            image = base64.b64decode(value.split(",", 1)[1])
            self.assertEqual(image[:8], b'\x89PNG\r\n\x1a\n')
            width, height = struct.unpack('>II', image[16:24])
            self.assertGreater(width, 1)
            self.assertGreater(height, 1)
            self.assertEqual(image[25], 6)

    def test_logo_assets_are_complete_and_have_no_external_resources(self):
        directory = ROOT / "assets" / "manufacturers"
        catalog = json.loads((directory / "catalog.json").read_text())
        for slug in catalog:
            source = directory / f"{slug}.svg"
            if source.is_file():
                svg = ET.parse(source)
                for element in svg.iter():
                    self.assertNotIn(element.tag.rsplit("}", 1)[-1], {"script", "image", "use", "foreignObject"})
                    self.assertFalse(any(key.endswith("href") for key in element.attrib))
            image = (directory / f"{slug}.png").read_bytes()
            self.assertEqual(image[:8], b'\x89PNG\r\n\x1a\n')
            self.assertEqual(image[25], 6)

    def test_catalog_aliases_are_unambiguous_and_do_not_guess_unknown_brands(self):
        catalog = json.loads((ROOT / 'assets/manufacturers/catalog.json').read_text())
        aliases = {}
        for slug, values in catalog.items():
            for value in values:
                normalized = ' '.join(value.casefold().split())
                self.assertIn(aliases.get(normalized, slug), {slug})
                aliases[normalized] = slug
        names = {'Eve Systems', 'Aqara', 'Netatmo', 'Broadlink', 'haier', 'homebridge.io', 'Unknown Device', 'Default-Manufacturer'}
        self.assertEqual(set(load_manufacturer_logos(names)), names - {'Unknown Device', 'Default-Manufacturer'})

    def test_logos_cover_bridges_and_hubs_without_database_access(self):
        payload = {
            "layout": {"zones": [{"rooms": ["Example Room"]}]},
            "infrastructure": {
                "homeHubs": [{"manufacturer": "Apple"}],
                "bridges": [{"manufacturer": "Homebridge", "accessories": [{"manufacturer": "Ring"}]}],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            write_html(payload, Path(tmp) / "report.html")
        self.assertEqual(set(payload["manufacturerLogos"]), {"Apple", "Homebridge", "Ring"})

    def test_generated_html_contains_contextual_filter_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "generate_inspector.py"),
                    str(ROOT / "examples" / "sample_output.json"),
                    "--no-db",
                    "--output-dir",
                    tmp,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            html = (Path(tmp) / "homekit_inspector.html").read_text(encoding="utf-8")
            data = json.loads((Path(tmp) / "homekit_inspector_data.json").read_text())
            version_source = (ROOT / "scripts" / "inspector_version.py").read_text()
            self.assertIn(f'VERSION = "{data["metadata"]["inspectorVersion"]}"', version_source)
            self.assertIn('id="inspectorVersion"', html)

        parser = FilterMarkupParser()
        parser.feed(html)
        self.assertEqual(
            parser.filter_tabs,
            {"layout", "bridges", "manufacturers", "capabilities", "automations", "scenes", "config"},
        )
        self.assertTrue(
            {
                "layoutZone",
                "layoutRoom",
                "layoutCapability",
                "bridgeName",
                "manufacturerName",
                "capabilityName",
                "status",
                "sceneRoom",
                "configAssignment",
            }.issubset(parser.select_ids)
        )


if __name__ == "__main__":
    unittest.main()

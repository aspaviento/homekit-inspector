import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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

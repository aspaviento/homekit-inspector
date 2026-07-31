import tempfile
import unittest
from pathlib import Path

from scripts.serve_inspector import DEFAULT_INDEX, safe_resolve


class ServeInspectorTests(unittest.TestCase):
    def test_root_path_resolves_to_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            self.assertEqual(safe_resolve(root, "/"), root / DEFAULT_INDEX)

    def test_file_path_stays_inside_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            self.assertEqual(safe_resolve(root, "/report.html"), root / "report.html")

    def test_parent_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            self.assertIsNone(safe_resolve(root, "/../secret.txt"))


if __name__ == "__main__":
    unittest.main()

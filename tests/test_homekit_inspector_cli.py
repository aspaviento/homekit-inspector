import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.homekit_inspector_cli import (
    ConfigError,
    DATA_FILENAME,
    EXPORT_FILENAME,
    REPORT_FILENAME,
    atomic_write_json,
    load_config,
    publish_report,
    sha256_file,
    validate_generated_output,
)


ROOT = Path(__file__).resolve().parents[1]


class HomeKitInspectorCliTests(unittest.TestCase):
    def write_config(self, directory: Path, payload: dict) -> Path:
        path = directory / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_loads_local_config_and_resolves_relative_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config = load_config(
                self.write_config(
                    root,
                    {
                        "version": 1,
                        "database": "core.sqlite",
                        "workingDirectory": "work",
                        "publish": {"type": "local", "path": "served/report.html"},
                    },
                )
            )
            self.assertEqual(config.database, root / "core.sqlite")
            self.assertEqual(config.working_directory, root / "work")
            self.assertEqual(config.publish.path, root / "served/report.html")

    def test_rejects_unsafe_remote_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            path = self.write_config(
                root,
                {
                    "version": 1,
                    "publish": {
                        "type": "ssh",
                        "host": "inspector.local",
                        "path": "/srv/report;touch-bad",
                    },
                },
            )
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_rejects_ssh_host_that_looks_like_an_option(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            path = self.write_config(
                root,
                {
                    "version": 1,
                    "publish": {
                        "type": "ssh",
                        "host": "-oProxyCommand",
                        "path": "/srv/homekit_inspector.html",
                    },
                },
            )
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_local_publish_is_hash_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "source.html"
            destination = root / "served" / "report.html"
            source.write_text("<html>report</html>", encoding="utf-8")
            config = load_config(
                self.write_config(
                    root,
                    {
                        "version": 1,
                        "publish": {"type": "local", "path": str(destination)},
                    },
                )
            )
            result = publish_report(config.publish, source)
            self.assertEqual(result, sha256_file(source))
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o644)

    def test_ssh_publish_uses_rsync_and_verifies_remote_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "source.html"
            source.write_text("<html>report</html>", encoding="utf-8")
            config = load_config(
                self.write_config(
                    root,
                    {
                        "version": 1,
                        "publish": {
                            "type": "ssh",
                            "host": "inspector.local",
                            "path": "/srv/inspector/homekit_inspector.html",
                        },
                    },
                )
            )
            digest = sha256_file(source)
            responses = [
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 0, digest + "\n", ""),
            ]
            with patch(
                "scripts.homekit_inspector_cli.run_command", side_effect=responses
            ) as runner:
                result = publish_report(config.publish, source)
            self.assertEqual(result, digest)
            self.assertEqual(runner.call_count, 2)
            self.assertEqual(runner.call_args_list[0].args[0][0], "rsync")
            self.assertEqual(runner.call_args_list[1].args[0][0], "ssh")

    def test_generated_output_validation_requires_payload_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / EXPORT_FILENAME).write_text("{}", encoding="utf-8")
            (root / DATA_FILENAME).write_text('{"rules": []}', encoding="utf-8")
            (root / REPORT_FILENAME).write_text(
                "<title>HomeKit Inspector</title><script>const data = {};</script>",
                encoding="utf-8",
            )
            hashes = validate_generated_output(root)
            self.assertEqual(set(hashes), {EXPORT_FILENAME, DATA_FILENAME, REPORT_FILENAME})

    def test_status_json_write_is_atomic_and_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status.json"
            atomic_write_json(path, {"state": "success"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["state"], "success")

    def test_command_wrapper_exposes_cli_help(self):
        completed = subprocess.run(
            [str(ROOT / "bin/homekit-inspector"), "--help"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("refresh", completed.stdout)
        self.assertIn("validate-config", completed.stdout)

    def test_named_status_wrapper_uses_config_argument(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config_path = self.write_config(
                root,
                {
                    "version": 1,
                    "workingDirectory": "work",
                    "publish": {"type": "local", "path": "served/report.html"},
                },
            )
            completed = subprocess.run(
                [
                    str(ROOT / "bin/homekit-inspector-status"),
                    "--config",
                    str(config_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        self.assertEqual(json.loads(completed.stdout)["state"], "never")

    def test_wrapper_accepts_configurable_python_executable(self):
        environment = os.environ.copy()
        environment["HOMEKIT_INSPECTOR_PYTHON"] = sys.executable
        completed = subprocess.run(
            [str(ROOT / "bin/homekit-inspector"), "--help"],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertIn("HomeKit Inspector", completed.stdout)


if __name__ == "__main__":
    unittest.main()

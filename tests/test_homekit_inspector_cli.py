import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.homekit_inspector_cli import (
    ConfigError,
    DATA_FILENAME,
    EXPORT_FILENAME,
    NoRedirectHandler,
    REPORT_FILENAME,
    atomic_write_json,
    load_config,
    publish_report,
    sha256_file,
    validate_config,
    validate_generated_output,
)


ROOT = Path(__file__).resolve().parents[1]


class HomeKitInspectorCliTests(unittest.TestCase):
    def test_server_publication_does_not_follow_redirects(self):
        handler = NoRedirectHandler()
        self.assertIsNone(
            handler.redirect_request(None, None, 307, "redirect", {}, "https://other")
        )

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

    def test_defaults_to_local_publication_when_publish_is_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config = load_config(
                self.write_config(root, {"version": 1})
            )
            self.assertEqual(config.publish.kind, "local")
            self.assertEqual(
                config.publish.path,
                Path.home()
                / "Library/Application Support/HomeKit Inspector/published/homekit_inspector.html",
            )

    def test_loads_server_config_and_resolves_private_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config = load_config(
                self.write_config(
                    root,
                    {
                        "version": 1,
                        "publish": {
                            "type": "server",
                            "url": "https://inspector.example/api/v1/report",
                            "secretFile": "secrets/publish-secret",
                            "caFile": "secrets/server-ca.pem",
                        },
                    },
                )
            )
            self.assertEqual(config.publish.kind, "server")
            self.assertEqual(
                config.publish.url, "https://inspector.example/api/v1/report"
            )
            self.assertEqual(
                config.publish.secret_file, root / "secrets/publish-secret"
            )
            self.assertEqual(config.publish.ca_file, root / "secrets/server-ca.pem")
            self.assertIsNone(config.publish.path)

    def test_rejects_remote_server_url_without_https(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            path = self.write_config(
                root,
                {
                    "version": 1,
                    "publish": {
                        "type": "server",
                        "url": "http://inspector.example/api/v1/report",
                        "secretFile": "publish-secret",
                    },
                },
            )
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_rejects_server_url_with_embedded_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            path = self.write_config(
                root,
                {
                    "version": 1,
                    "publish": {
                        "type": "server",
                        "url": "https://user:secret@inspector.example/api/v1/report",
                        "secretFile": "publish-secret",
                    },
                },
            )
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_rejects_unexpected_server_api_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            path = self.write_config(
                root,
                {
                    "version": 1,
                    "publish": {
                        "type": "server",
                        "url": "https://inspector.example/api/v1/other",
                        "secretFile": "publish-secret",
                    },
                },
            )
            with self.assertRaisesRegex(ConfigError, "/api/v1/report"):
                load_config(path)

    def test_server_validation_rejects_non_private_secret_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            secret = root / "publish-secret"
            secret.write_text("a" * 64, encoding="ascii")
            secret.chmod(0o644)
            config = load_config(
                self.write_config(
                    root,
                    {
                        "version": 1,
                        "publish": {
                            "type": "server",
                            "url": "https://inspector.example/api/v1/report",
                            "secretFile": "publish-secret",
                        },
                    },
                )
            )
            with self.assertRaisesRegex(ConfigError, "owner-only permissions"):
                validate_config(config, require_database=False)

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
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)

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
        self.assertIn("show-config", completed.stdout)

    def test_show_config_prints_effective_path_and_resolved_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config_path = self.write_config(
                root,
                {
                    "version": 1,
                    "database": "core.sqlite",
                    "workingDirectory": "output",
                    "inputs": {"themeConfig": "config/themes.json"},
                    "publish": {"type": "local", "path": "served/report.html"},
                },
            )
            completed = subprocess.run(
                [
                    str(ROOT / "bin/homekit-inspector"),
                    "show-config",
                    "--config",
                    str(config_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["configPath"], str(config_path))
            self.assertEqual(payload["database"], str(root / "core.sqlite"))
            self.assertEqual(
                payload["inputs"]["themeConfig"], str(root / "config/themes.json")
            )
            self.assertEqual(
                payload["publish"]["path"], str(root / "served/report.html")
            )

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

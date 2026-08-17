#!/usr/bin/env python3
"""Install a refresh configuration and its private inputs into user storage."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

try:
    from homekit_inspector_cli import (
        ConfigError,
        RefreshConfig,
        atomic_copy,
        atomic_write_json,
        load_config,
    )
except ModuleNotFoundError:  # Imported as scripts.install_refresh_config in tests.
    from scripts.homekit_inspector_cli import (
        ConfigError,
        RefreshConfig,
        atomic_copy,
        atomic_write_json,
        load_config,
    )


INPUT_FILENAMES = {
    "themeConfig": ("theme_config", "theme-config.json"),
    "privateOverrides": ("private_overrides", "private-overrides.json"),
    "homebridgeConfig": ("homebridge_config", "homebridge-config.json"),
}


def display_path(path: Path) -> str:
    home = Path.home().resolve()
    try:
        return "~/" + str(path.resolve().relative_to(home))
    except ValueError:
        return str(path.resolve())


def serialized_publish(
    config: RefreshConfig,
    installed_secret_file: str | None = None,
    installed_ca_file: str | None = None,
) -> dict:
    publish = {
        "type": config.publish.kind,
        "requestTimeoutSeconds": config.publish.request_timeout_seconds,
        "healthUrl": config.publish.health_url,
        "healthTimeoutSeconds": config.publish.health_timeout_seconds,
    }
    if config.publish.path is not None:
        publish["path"] = str(config.publish.path)
    if config.publish.url:
        publish["url"] = config.publish.url
    if installed_secret_file:
        publish["secretFile"] = installed_secret_file
    if installed_ca_file:
        publish["caFile"] = installed_ca_file
    return publish


def install_config(source: Path, destination: Path, replace: bool = False) -> str:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if destination.exists() and not replace:
        return "preserved"

    config = load_config(source)
    destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(destination.parent, 0o700)
    inputs_dir = destination.parent / "config"
    inputs_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(inputs_dir, 0o700)

    installed_inputs = {}
    for json_key, (attribute, filename) in INPUT_FILENAMES.items():
        input_path = getattr(config.inputs, attribute)
        if input_path is None:
            installed_inputs[json_key] = None
            continue
        installed_path = inputs_dir / filename
        atomic_copy(input_path, installed_path, mode=0o600)
        installed_inputs[json_key] = f"config/{filename}"

    installed_secret_file = None
    if config.publish.secret_file is not None:
        secret_filename = "publish-secret"
        atomic_copy(config.publish.secret_file, inputs_dir / secret_filename, mode=0o600)
        installed_secret_file = f"config/{secret_filename}"

    installed_ca_file = None
    if config.publish.ca_file is not None:
        ca_filename = "server-ca.pem"
        atomic_copy(config.publish.ca_file, inputs_dir / ca_filename, mode=0o600)
        installed_ca_file = f"config/{ca_filename}"

    payload = {
        "version": 1,
        "database": display_path(config.database),
        "workingDirectory": "output",
        "inputs": installed_inputs,
        "publish": serialized_publish(
            config, installed_secret_file, installed_ca_file
        ),
        "verbose": config.verbose,
    }
    atomic_write_json(destination, payload)
    os.chmod(destination, 0o600)
    load_config(destination)
    return "installed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    try:
        result = install_config(args.source, args.destination, replace=args.replace)
    except (ConfigError, OSError) as exc:
        parser.error(str(exc))
    print(f"Configuration {result}: {args.destination.expanduser().resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

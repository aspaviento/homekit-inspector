# Refresh CLI

The refresh CLI turns HomeKit Inspector's extraction and HTML generation steps
into one repeatable operation. It remains read-only with respect to HomeKit and
publishes only validated generated artifacts.

## User Installation

Install the CLI without `sudo`:

```bash
./install-cli.sh --config /path/to/private-refresh-config.json
```

The installer copies executable code to
`~/Library/Application Support/HomeKit Inspector/app`, migrates the selected
configuration to `~/Library/Application Support/HomeKit Inspector/config.json`,
copies optional theme, override, and Homebridge inputs into a private `config/`
directory, and creates `~/.local/bin/homekit-inspector`.

The installed layout separates replaceable code from stable private data:

```text
~/Library/Application Support/HomeKit Inspector/
├── app/
├── config.json
├── config/
└── output/
```

Code updates are installed by rerunning the same command. Existing
configuration and outputs are preserved. Use `--replace-config` together with
`--config FILE` to replace and remigrate the installed configuration.

Custom locations can be selected with `--app-home` and `--bin-dir`, or with
`HOMEKIT_INSPECTOR_HOME` and `HOMEKIT_INSPECTOR_BIN_DIR`. The Python executable
can be selected with `HOMEKIT_INSPECTOR_PYTHON`.

Terminal or the configured Python executable must retain Full Disk Access for
the extractor to read the protected HomeKit database.

Uninstall code and the command while preserving private data:

```bash
./uninstall-cli.sh
```

`./uninstall-cli.sh --remove-private-data` also deletes the installed
configuration, copied context inputs, output reports, and refresh status. The
destructive option is explicit, and the uninstaller rejects critical directory
targets such as `/`, the home directory, or `~/Library`.

## Commands

```bash
bin/homekit-inspector validate-config --config CONFIG.json
bin/homekit-inspector show-config --config CONFIG.json
bin/homekit-inspector refresh --config CONFIG.json
bin/homekit-inspector status --config CONFIG.json
```

The action is a subcommand; options keep the `--` prefix. For example,
`refresh` is the action and `--config` selects its configuration file.

The launcher resolves the project-relative Python entry point and uses `python3`
by default. A specific Python 3.9 or later executable can be selected without
editing the scripts:

```bash
HOMEKIT_INSPECTOR_PYTHON=/path/to/python3 bin/homekit-inspector status \
  --config CONFIG.json
```

Direct invocation of `scripts/homekit_inspector_cli.py` remains supported for
development and embedding.

Without `--config`, the CLI looks for:

```text
~/Library/Application Support/HomeKit Inspector/config.json
```

This is the path populated by `install-cli.sh`, so installed commands normally
do not need `--config`.

`validate-config` checks the schema, database and optional input files, and the
local executables needed by the selected publisher. It does not extract data.

`show-config` prints the effective configuration file, resolved filesystem
paths, optional input locations, and publication settings as JSON. It does not
read the HomeKit database, inspect the optional input contents, or contact the
publication target.

`refresh` performs these steps:

1. Acquire a non-blocking refresh lock.
2. Extract `core.sqlite` through the existing read-only extractor.
3. Generate JSON and self-contained HTML in a private temporary directory.
4. Parse and validate the generated artifacts.
5. Publish the HTML and verify its SHA-256 hash.
6. Optionally check the server's `/health` endpoint.
7. Atomically update the private working copies and status file.

`status` prints machine-readable JSON describing the most recent run. It does
not access the HomeKit database or contact the publication target.

## Configuration

The configuration format is versioned JSON. Real configurations contain
private filesystem and network information and should not be committed.
Store them outside the repository or under an ignored private directory, and
use owner-only permissions such as `chmod 600 CONFIG.json`.

```json
{
  "version": 1,
  "database": "~/Library/HomeKit/core.sqlite",
  "workingDirectory": "~/Library/Application Support/HomeKit Inspector/output",
  "inputs": {
    "themeConfig": null,
    "privateOverrides": null,
    "homebridgeConfig": null
  },
  "publish": {
    "type": "local",
    "path": "~/Library/Application Support/HomeKit Inspector/published/homekit_inspector.html",
    "healthUrl": null
  },
  "verbose": false
}
```

Relative paths are resolved from the configuration file's directory. The
working directory is private and contains the current export, inspector data,
HTML report, lock, and refresh status. Generated private data files use owner-
only permissions.

Optional inputs preserve the existing separation between HomeKit-derived data
and local enrichment:

- `themeConfig`: explicit automation theme assignments.
- `privateOverrides`: sparse private corrections or annotations.
- `homebridgeConfig`: optional structural Homebridge context.

## Local Publication

The local publisher atomically replaces one report file:

```json
{
  "publish": {
    "type": "local",
    "path": "/srv/homekit-inspector/homekit_inspector.html",
    "healthUrl": "http://inspector-server.local:8099/health"
  }
}
```

This mode fits a server running on the same Mac or a mounted private volume.
The destination directory is created when possible. The resulting served HTML
uses mode `0644`; private working artifacts remain `0600`.

## SSH Publication

The SSH publisher uses `rsync` for transfer and `ssh` for remote hash
verification:

```json
{
  "publish": {
    "type": "ssh",
    "host": "inspector-server",
    "path": "/var/lib/homekit-inspector/homekit_inspector.html",
    "healthUrl": "http://inspector-server.local:8099/health",
    "healthTimeoutSeconds": 5
  }
}
```

The host may be an SSH config alias or a `user@host` value. Authentication uses
the current user's SSH agent, keys, and SSH configuration. Passwords and private
keys do not belong in the refresh configuration.

The remote server must provide Python 3 for SHA-256 verification. This is
already a requirement of the included static server.

## HTTP API Publication

HTTP publication sends the validated self-contained report directly to the
optional HomeKit Inspector server:

```json
{
  "publish": {
    "type": "http",
    "url": "http://inspector-server.local:8099/api/v1/report",
    "tokenFile": "config/publish-token",
    "requestTimeoutSeconds": 30,
    "healthUrl": "http://inspector-server.local:8099/health",
    "healthTimeoutSeconds": 5
  }
}
```

`tokenFile` points to a private file containing at least 32 random characters.
The token itself is not stored in the JSON configuration, displayed by
`show-config`, included in the generated report, or passed as a process
argument. `install-cli.sh --config FILE` copies it into the installed private
configuration directory with mode `0600`.

The client sends the report with bearer authentication and its expected
SHA-256 digest. The server rejects unauthorized, oversized, incomplete,
hash-mismatched, or structurally invalid reports. A valid report atomically
replaces the previous HTML file, and the response returns the stored digest for
client verification.

The upload endpoint is disabled unless the server is installed with a token:

```bash
sudo ./install.sh --upload-token-file /path/to/private-upload-token
```

The server API provides:

- `POST /api/v1/report`: authenticated report publication.
- `GET /api/v1/status`: current report size, modification time, and SHA-256.
- `GET /health`: server and index availability.

Bearer authentication does not encrypt plain HTTP. Use this mode only on a
trusted LAN or VPN, or place the server behind an HTTPS reverse proxy. SSH
publication remains available where its encrypted transport and key management
are preferred.

## Failure Behavior

- Concurrent refresh attempts fail without disturbing the running operation.
- Generation occurs in a temporary directory under the private working path.
- Invalid or incomplete output is never published.
- Publication is hash-verified before success is recorded.
- A failed run records a redacted error and leaves the previously published
  report available.
- The CLI never restarts `homed`, writes to HomeKit SQLite files, or changes
  HomeKit accessories and automations.

Browser-initiated refresh remains outside this version. The API accepts a
completed report from the macOS agent; it cannot access or extract the Mac's
HomeKit database.

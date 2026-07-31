# Privacy and Publishing Checklist

HomeKit exports describe a real home. They can reveal occupancy patterns,
security logic, room layout, device inventory, bridge topology, and automation
schedules. Treat generated files as sensitive by default.

## Never Commit

- `local-output/`
- `output/`
- `homekit_homed_export.json`
- `homekit_inspector_data.json`
- `homekit_inspector.html`
- raw `core.sqlite`, `core.sqlite-wal`, or `core.sqlite-shm`
- Controller for HomeKit backup files
- Homebridge `config.json`
- theme assignments from a real home
- private overrides from a real home
- screenshots of the inspector from a real home

## Safe To Commit

- extractor code;
- HTML generator code;
- condition decoder code;
- synthetic fixtures;
- redacted examples;
- documentation that does not include household-specific names or topology.

## Local Output

Generated files live under `local-output/`, which is ignored by git.

```bash
mkdir -p local-output
python3 scripts/homed_extract.py \
  --db ~/Library/HomeKit/core.sqlite \
  -o local-output/homekit_homed_export.json
```

## LAN Serving

Serving `homekit_inspector.html` from a Raspberry Pi or another LAN host does
not make the report less sensitive. Keep the served directory private, serve it
only on a trusted LAN or VPN, and do not expose it through public DNS, port
forwarding, or an unauthenticated internet-facing reverse proxy.

## Homebridge Context

Homebridge configuration often contains credentials, webhook URLs, hostnames,
serial numbers, plugin topology, and room/device naming conventions. Do not
commit a real Homebridge config.

Documentation and tests can use a small synthetic file such as
`examples/homebridge-context.example.json`.

## Theme Configuration

Themes are editorial metadata. They may expose how a household thinks about
security, presence simulation, sleep modes, access, or family routines.

Use `examples/theme-config.example.json` as the public schema reference. Keep
real assignments in `local-output/homekit_theme_config.json`.

## Private Overrides

Private overrides cover local facts that cannot be derived from HomeKit or a
context source.

Use `examples/private-overrides.example.json` as the public schema reference.
Keep real overrides in `local-output/homekit_private_overrides.json`.

## Pre-Publish Audit

Before pushing or opening a public pull request:

```bash
git status --short --ignored
rg -n "YOUR_REAL_NAME|REAL_ROOM|REAL_DEVICE|REAL_HOST|REAL_TOKEN" \
  -g '!local-output/**' -g '!output/**' .
rg -n "/Users/|REAL_HOST|REAL_TOKEN|REAL_SERIAL|REAL_WEBHOOK" \
  -g '!local-output/**' -g '!output/**' .
```

Also check tracked generated files:

```bash
git ls-files local-output output
```

The command is expected to return no real generated HomeKit outputs.

## Runtime Safety

The extractor is read-only:

- open SQLite with `mode=ro`;
- do not write to `core.sqlite`, `core.sqlite-wal`, or `core.sqlite-shm`;
- do not restart, kill, or manipulate `homed`;
- do not run database migrations or vacuum operations against the live HomeKit
  database.

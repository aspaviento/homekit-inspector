# HomeKit Inspector

Read Apple's local HomeKit database in read-only mode and generate a private,
self-contained HTML inspector for HomeKit structure, automations, scenes, hubs,
bridges, and optional context sources such as Homebridge.

`homed` is Apple's macOS HomeKit daemon (`com.apple.homed`). This project uses
the term only to identify the local system component and its CoreData SQLite
store; the schema is private and unsupported, so extraction logic may need to
change across macOS versions.

The project provides:

- A read-only extractor for `~/Library/HomeKit/core.sqlite`.
- Dynamic CoreData relationship discovery, avoiding hard-coded junction-table
  column numbers where possible.
- Decoding for HomeKit events, actions, scenes, date components, media actions,
  Natural Lighting targets, and Eve/HomeKit predicate conditions.
- A standalone HTML inspector with views for home layout, hubs and bridges,
  context sources, manufacturers, automations, scenes, and theme assignment.
- Optional Homebridge context enrichment that adds traceable relationships
  without replacing the raw HomeKit data.
- Local-only theme and override files for private household-specific metadata.

![HomeKit Inspector sample interface](assets/homekit-inspector-overview.svg)

## Project Lineage and Development

HomeKit Inspector is a standalone project focused on local, read-only HomeKit
inspection. It builds on the work published in
[tamengual/homekit-extractor](https://github.com/tamengual/homekit-extractor),
which explored exporting HomeKit data from Apple's local `homed` database and
provided a broader HomeKit-to-Home-Assistant conversion pipeline.

HomeKit Inspector keeps the `homed` SQLite extraction focus, but narrows the
scope to inspection and documentation: richer rule decoding, HomeKit layout and
infrastructure views, optional Homebridge context, local theme assignments, and
a self-contained HTML inspector.

The implementation was developed with assistance from OpenAI Codex. Codex
helped inspect the local CoreData schema, refine read-only SQLite queries,
decode HomeKit/Eve predicate and action payloads, build the HTML viewer, create
synthetic examples, and review the repository for public release.

The project separates raw HomeKit extraction, technical decoding, optional
Homebridge context, and user-maintained annotations. The inspector presents
HomeKit-derived data first, then labels any external context or local metadata.

## Safety Model

This project is designed for inspection and documentation. It does not modify
HomeKit.

- SQLite is opened with `mode=ro`.
- Do not write to `core.sqlite`, `core.sqlite-wal`, or `core.sqlite-shm`.
- Do not restart or manipulate `homed`.
- Treat every generated export and HTML report as sensitive household data.
- Keep `local-output/`, raw HomeKit database copies, Homebridge configs, and
  private overrides out of version control.

See [docs/PRIVACY.md](docs/PRIVACY.md) for the publishing checklist.

## Requirements

- macOS with HomeKit configured for the current iCloud user.
- Full Disk Access for the terminal or shell running the extractor.
- Python 3.10+.
- No Python package dependencies are required for extraction or HTML generation.

## Quick Start

Create a local output directory:

```bash
mkdir -p local-output
```

Extract HomeKit from the local `homed` database:

```bash
python3 scripts/homed_extract.py \
  --db ~/Library/HomeKit/core.sqlite \
  -o local-output/homekit_homed_export.json
```

Generate the standalone inspector:

```bash
python3 scripts/generate_inspector.py \
  local-output/homekit_homed_export.json \
  --db ~/Library/HomeKit/core.sqlite \
  --output-dir local-output
```

Open:

```text
local-output/homekit_inspector.html
```

The HTML file is self-contained and can be opened directly in a browser. It
embeds the extracted HomeKit data, so real-home reports belong in private local
storage.

To generate a demo from the synthetic example without reading a local HomeKit
database:

```bash
python3 scripts/generate_inspector.py \
  examples/sample_output.json \
  --no-db \
  --homebridge-config examples/homebridge-context.example.json \
  --theme-config examples/theme-config.example.json \
  --private-overrides examples/private-overrides.example.json \
  --output-dir local-output/example
```

## Optional Context Sources

The extractor keeps raw HomeKit data intact. Optional context files can add
traceable metadata. For example, a Homebridge `config.json` can identify that a
helper switch is a mode button for a Homebridge security system.

```bash
python3 scripts/generate_inspector.py \
  local-output/homekit_homed_export.json \
  --db ~/Library/HomeKit/core.sqlite \
  --homebridge-config /path/to/homebridge/config.json \
  --output-dir local-output
```

Context enrichment is shown in the **Context Sources** tab and in automation
notes. It should not replace the original `WHEN`, `IF`, or `THEN` extracted
from HomeKit.

## Local Theme Assignments

The inspector includes a **Theme Editor** tab. Theme assignments are editorial
metadata, not HomeKit data.

You can let the browser store them in `localStorage`, or pass an explicit local
JSON file:

```bash
python3 scripts/generate_inspector.py \
  local-output/homekit_homed_export.json \
  --db ~/Library/HomeKit/core.sqlite \
  --theme-config local-output/homekit_theme_config.json \
  --output-dir local-output
```

To generate an editable seed from the current heuristic report classification:

```bash
python3 scripts/generate_inspector.py \
  local-output/homekit_homed_export.json \
  --db ~/Library/HomeKit/core.sqlite \
  --write-inferred-theme-config local-output/homekit_theme_config.json \
  --output-dir local-output
```

Review the generated file before reusing it. Automation names, themes, room
names, and device names are private.

## Private Overrides

Private overrides cover local facts that cannot be derived from HomeKit or
context sources. They are kept separate from the extractor.

```bash
python3 scripts/generate_inspector.py \
  local-output/homekit_homed_export.json \
  --db ~/Library/HomeKit/core.sqlite \
  --private-overrides local-output/homekit_private_overrides.json \
  --output-dir local-output
```

Use overrides sparingly. Prefer raw HomeKit extraction and context-source
enrichment whenever possible.

## Inspector Views

- **Home Layout**: zones, rooms, accessories, and named services.
- **Hubs & Bridges**: Home hubs, primary hub detection, bridges, and bridged
  accessories.
- **Context Sources**: optional Homebridge-derived platforms, helpers,
  webhooks, and semantic relations.
- **Manufacturers**: accessories grouped by manufacturer.
- **Automations**: active/inactive automations with `WHEN`, `IF`, `THEN`,
  scenes, devices, rooms, confidence, and unresolved values.
- **Scenes**: scenes and their actions.
- **Theme Editor**: local assignment of automations to user-defined themes.

The global search box filters the current view.

## Project Structure

```text
homekit-inspector/
├── assets/                      # Synthetic screenshots for documentation
├── docs/
│   ├── EXPLORER.md              # Inspector and context-source details
│   ├── PRIVACY.md               # Sensitive-data and publishing checklist
│   ├── SCHEMA.md                # homed CoreData schema notes
│   └── TECHNICAL_APPROACH.md    # Scope, pipeline, and design boundaries
├── examples/
│   ├── homebridge-context.example.json
│   ├── private-overrides.example.json
│   ├── theme-config.example.json
│   └── sample_output.json
├── scripts/
│   ├── homed_extract.py
│   ├── generate_condition_diagnostics.py
│   ├── generate_homekit_reports.py
│   └── generate_inspector.py
```

## Demo Assets

The image in `assets/` shows the inspector with synthetic example data from
`examples/sample_output.json`. Real-home screenshots can expose room names,
device names, schedules, and security behavior, so they are best kept out of
public repositories.

## Limitations

- The HomeKit database schema is private and can change between macOS releases.
- Full Disk Access is required because `~/Library/HomeKit/` is TCC-protected.
- Some Eve/HomeKit predicates are partially opaque and may need review.
- Some values are reported as unresolved when the decoder cannot identify them
  confidently.
- Context-source enrichment depends on optional files such as Homebridge
  `config.json`.

## Basis

The project is based on direct observation of Apple's local `homed` CoreData
SQLite database and on local validation of decoded automations against HomeKit
views in Home/Eve. It uses Python standard-library parsers and keeps raw
HomeKit extraction, technical decoding, optional external context, and user
annotations as separate layers.

## Development

Compile-check the Python scripts:

```bash
PYTHONPYCACHEPREFIX=/tmp/homekit-pycache python3 -m py_compile scripts/*.py
```

Generated outputs belong in `local-output/` or another ignored directory.

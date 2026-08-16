# Technical Approach

HomeKit Inspector focuses on one task: generating a readable, local, private
view of a HomeKit installation from the macOS `homed` database.

It is not a migration or automation-recreation tool, and it is not an app built
on Apple's public HomeKit APIs. The primary interface is a Python read-only
extractor plus a self-contained HTML inspector.

## Scope

The tool extracts and presents:

- HomeKit zones, rooms, accessories, services, and characteristics.
- Home hubs, primary hub information, bridges, and bridged accessories.
- Scenes and scene actions.
- Automations with `WHEN`, `IF`, and `THEN` sections.
- Decoded HomeKit/Eve predicates where possible.
- Decoded action values, media actions, calendar triggers, and Natural
  Lighting targets where available.
- Optional external context, currently Homebridge configuration.
- User-maintained theme assignments and private overrides.
- Optional local or authenticated server publication of the completed report.

The tool does not:

- modify HomeKit;
- write to the HomeKit database;
- restart or control `homed`;
- transmit data unless `server` publication is explicitly configured;
- attempt to recreate automations in another platform.

## Primary Data Source

HomeKit on macOS stores local HomeKit state in a CoreData SQLite database:

```text
~/Library/HomeKit/core.sqlite
```

The extractor opens this database read-only:

```python
sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
```

The surrounding `-wal` and `-shm` files are never modified by this project.

## Platform Boundary

This approach is specific to macOS. The key capability is not iCloud sync by
itself, but local read access to the `homed` SQLite store after the user grants
Full Disk Access to the terminal process.

iOS and iPadOS devices also participate in HomeKit and iCloud sync, but apps on
those platforms remain sandboxed. The HomeKit entitlement enables access to
Apple's public HomeKit APIs; it does not grant direct filesystem access to the
system HomeKit database. An iPhone or iPad app can therefore inspect the subset
of HomeKit data exposed by `HomeKit.framework`, but it cannot use this SQLite
extraction method to decode the private rule graph.

## Why SQLite

The public HomeKit API is useful for many apps, but it does not expose a full,
portable, user-facing representation of every automation rule. The local
`homed` database contains the CoreData graph used by HomeKit itself, including
objects for triggers, action sets, actions, scenes, rooms, services, and
characteristics.

This project reads that graph directly to produce a local inspection artifact.

## Pipeline

```text
core.sqlite
  -> scripts/homed_extract.py
  -> local-output/homekit_homed_export.json
  -> scripts/generate_inspector.py
  -> local-output/homekit_inspector.html
```

Optional enrichment:

```text
Homebridge config.json
  -> contextSources[]
  -> visible contextual relations in the inspector
```

Optional local metadata:

```text
theme-config JSON
  -> Theme Editor defaults

private-overrides JSON
  -> local metadata not present in HomeKit/context sources
```

## CoreData Handling

HomeKit's SQLite database is a CoreData store. Table and column names include
CoreData-generated identifiers such as `Z_PK`, `Z_ENT`, and `ZMKF*` tables.

The extractor avoids depending on a single observed schema where practical:

- entity names are read from `Z_PRIMARYKEY`;
- trigger/action-set junction tables are detected dynamically;
- zone/room relationship tables are detected dynamically;
- action and event entity types are simplified from CoreData entity names;
- characteristic references are resolved through service and instance ids when
  required.

## Decoding

HomeKit stores several values as encoded binary blobs. The project decodes the
parts needed for readable inspection using Python standard library modules:

- `plistlib` for binary property lists;
- minimal protobuf wire parsing for schema-less internal payloads;
- NSKeyedArchiver plist traversal for scalar target values;
- CoreData timestamp conversion from the 2001 Apple epoch;
- archived `NSDateComponents` for calendar triggers;
- predicate archive decoding for HomeKit/Eve conditions.

When a value cannot be decoded with enough confidence, the inspector marks it as
unresolved instead of guessing.

## Context Sources

Context sources are optional external configuration files that explain local
semantics without changing the raw HomeKit extraction.

The first supported source is Homebridge `config.json`. For example,
`homebridge-automation-switches` can expose a security system and helper
switches. The inspector can show these as derived relations in the **Context
Sources** view and as notes on related automations.

Context explains HomeKit data without replacing it.

For example, if HomeKit says an automation is triggered by a helper switch, the
inspector keeps that helper switch as the trigger and adds context describing
which Homebridge concept the helper represents.

## Themes

Themes are not extracted from HomeKit. They are user-maintained editorial
metadata used to organize automations in the inspector.

Theme assignments can live in:

```text
local-output/homekit_theme_config.json
```

or browser `localStorage`.

## Private Overrides

Private overrides cover local facts that cannot be derived from HomeKit or a
context source.

They live in:

```text
local-output/homekit_private_overrides.json
```

Public examples document the schema. Real overrides usually contain household
semantics and belong in ignored local output directories.

## What This Project Is Based On

The implementation is based on:

- observed structure of Apple's local `homed` CoreData SQLite database;
- Python standard-library parsing of SQLite, binary plist, and binary values;
- practical validation against HomeKit automations visible in Home, Eve, and
  local Homebridge configuration;
- a strict separation between raw extraction, technical decoding, external
  context, and user annotations.

No Apple private framework is linked or called. The project reads a local
database file in read-only mode and generates local files.

## Privacy Boundary

Generated output can reveal:

- room layout;
- device inventory;
- presence and security logic;
- schedules;
- scene behavior;
- bridge topology.

Generated files belong in ignored local directories such as `local-output/`.

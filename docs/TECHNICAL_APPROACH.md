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

Before building the lookup graph, the extractor identifies the home marked as
primary by HomeKit. Rooms, zones, accessories, automations, scenes, hubs, and
bridges are scoped to that home's `ZHOME` relationship. The extraction log
names the selected home and reports how many homes were available. Explicit
selection of a non-primary home is a future extension.

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

### Scalar OPACK Values

Some `ZTARGETVALUE` and `ZEVENTVALUE` blobs are scalar OPACK objects from Apple's
private CoreUtils serialization format rather than complete property lists.
The scalar decoder implements booleans, compact integers, explicitly sized
little-endian integers, and float32/float64 values. It leaves unsupported
markers and truncated values unchanged. This subset follows the reverse
engineering documented by the [pyatv project](https://pyatv.dev/documentation/protocols/#opack).

The `01` and `02` markers encode boolean true and false. Compact integer
markers encode the value with an offset: `08` represents 0 and `09` represents
1. Decoding precedes characteristic interpretation, so an encoded false can
describe power off, a closed contact, or absent occupancy. Numeric button
events are interpreted as press types, not physical button numbers. Physical
button labels come from the service's factory metadata.

Raw event and action records remain in the report's expandable recorded-data
section. Unsupported encodings remain flagged for review. No decoding rule
depends on accessory names, room names, or a household-specific manufacturer
exception.

### Hue Buttons and Rule Structure

Recognized Hue stateless programmable switch services are selected using
manufacturer, model, and service metadata. Assignments are resolved using
service primary keys within the selected home, not automation titles or
configured accessory names. Both enabled and disabled rules count as recorded
assignments. Missing identifiers in older exports make the assignment status
unavailable; re-extraction supplies the relationships.

Buttons without HomeKit assignments display "Behavior configured in Hue".
The inspector does not query the Hue Bridge and cannot enumerate or
verify its native behaviors. This label does not attribute orphan HomeKit
rules to the Hue app or imply that Hue executes them. HomeKit capability and
third-party configuration support are documented in the [Hue release notes](https://www.philips-hue.com/es-cl/support/release-notes/ios)
(version 2.16.0).

Rule structure is reported independently of its enabled flag: no associated
events, unresolved trigger, and empty rule are different states. Calendar,
presence, location, and significant-time events do not require an accessory.

### Rule Viability and Start/End Events

Extraction and relationship checks use one read transaction for a consistent
SQLite snapshot. The recorded `ZENDEVENT` flag separates start and end events;
`ZDURATION` is preserved in seconds. An absent role field in an older export
remains unknown rather than being inferred from the event type. Duration end
events do not start rules or increase the count of compound start triggers.
See Apple's documentation for [endEvents](https://developer.apple.com/documentation/homekit/hmeventtrigger/endevents):
end events restore the previously active state, which need not always mean off.

The rule audit evaluates every recorded initial event. A positive orphan result
requires an event trigger with explicit event roles and proof that all start
events are impossible, or verified absence of any initial events in a completely
read supported graph. The extractor's `eventGraphComplete` flag distinguishes
that evidence from an empty list in an older or incomplete export. Supported
per-event proofs are missing
device/service/characteristic relationships on device events and impossible
Gregorian date components when the calendar identifier is explicitly archived.
A recurring February 29 remains possible; a past date alone is not proof.

Scoped lookup failures are checked against the database before being called
missing. Objects outside the selected home and unsupported schemas are reported
as unresolved or out of scope. Characteristic instance identifiers are resolved
within their service; a coincidentally matching primary key in another service
must not resolve the reference.

No orphan result is inferred from a rule name, an enabled flag, incomplete
start-event data, or a broken THEN target. Unresolved predicate references are listed under
IF and require review; arbitrary logical impossibility of predicates is not
solved by this audit. The orphan filter is a subset of Needs review. Each
positive result includes evidence, and reference issues identify WHEN, IF, THEN,
or END independently.

### Playback Destinations

Playback accessory relationships are discovered from CoreData junction-table
and column names rather than fixed numeric prefixes. Destinations outside the
selected home are excluded. Multiple physical speakers can belong to one
destination displayed by Apple Home; the report preserves individual stored
relationships without guessing speaker groups. Counts are action records,
not unique accessories or user-interface tiles.

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

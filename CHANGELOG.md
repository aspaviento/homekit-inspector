# Changelog

## Unreleased

## 0.9.2 - 2026-11-15

- Fixed orphan detection excluding event triggers with no start events. The
  extractor now records whether the supported event graph was completely read;
  verified absence of all initial events is evidence that the rule cannot start
  automatically in the captured configuration, including empty rules and rules
  that retain only end events.
- Kept missing or incomplete legacy exports distinct from verified empty event
  graphs. Preserved empty-rule and missing-start flags alongside orphan status
  and added regression coverage for both complete and unknown graph states.

## 0.9.1 - 2026-11-15

- Separated start events from HomeKit end events and preserved duration fields.
  Solar offsets and available daily recurrence metadata now appear in WHEN;
  timed restoration appears in a separate End / restore section, following
  HomeKit's previous-state restoration semantics.
- Added a conservative rule viability audit and an Orphaned rules filter.
  A rule is marked orphaned only when every recorded start event is proven
  impossible in the captured configuration. Missing start events, empty rules,
  unsupported encodings, and unresolved references retain separate review
  states. A viable alternative prevents an orphan classification.
- Checked recorded device event and action references against the database,
  distinguishing deleted objects from objects outside the selected home or
  unavailable schema information. Broken action targets and unresolved
  condition references require review without declaring the rule orphaned.
- Included orphaned rules and reference issues in Needs review, with visible
  evidence in the automation detail. Preserved start/end counters alongside
  raw event counts and kept end-button assignments separate from start actions.
- Used a single read transaction for extraction and relationship validation,
  avoiding inconsistent reference checks during concurrent HomeKit edits.
- Added synthetic regression coverage for alternative triggers, impossible
  Gregorian dates, dangling references, older exports, timed restoration,
  filtering, and service-scoped characteristic identifiers.

## 0.9.0 - 2026-11-15

- Added scalar decoding for Apple's CoreUtils OPACK serialization in HomeKit
  event and action values. Boolean and numeric encodings now produce readable
  on/off, contact, occupancy, motion, button-press, brightness, and position
  values without manufacturer-specific exceptions. Unsupported payloads retain
  their raw representation for review.
- Applied descriptive service names and room labels to both WHEN and THEN,
  retaining original names, identifiers, and encoded values in recorded details.
- Added per-button HomeKit assignment summaries for recognized Hue remotes,
  matched by service identifiers. Buttons without HomeKit assignments show
  "Behavior configured in Hue"; Hue-side actions are outside the
  inspector's data source. Older exports with missing relationship identifiers
  show an unavailable assignment status instead.
- Distinguished empty rules, missing triggers, and unresolved triggers without
  inferring their creator or declaring them obsolete. Calendar and other
  non-device triggers no longer require a trigger accessory for confidence.
- Extracted media playback accessory relationships through dynamically
  discovered CoreData join columns. Playback summaries include available
  destinations and rooms; action counts explicitly refer to stored records,
  which need not match the number of accessories shown in Apple Home.
- Added synthetic regression coverage for scalar encodings, room labels,
  button relationships, playback destinations, and rule review states.

## 0.5.3 - 2026-10-30

- Expanded the shared manufacturer logo catalog to 31 brands, with documented
  sources and explicit aliases. Unknown and virtual manufacturers retain their
  text fallback; no brand-specific rendering logic is required.

## 0.5.2 - 2026-10-30

- Added larger logos to manufacturer group headings, transparent PNG logos in
  the upper-right corner of device cards, and Signify and Eufy manufacturer
  aliases. Logos adapt to light and dark themes without a background tile.

## 0.5.1 - 2026-10-30

- Added embedded manufacturer logos to accessory, hub, and bridge card headers,
  with text fallback for brands without an included logo and no external image
  requests. Removed redundant manufacturer rows from these cards.

## 0.5.0 - 2026-10-30

- Added recorded software and firmware versions to accessories, bridges, and
  hubs, with raw field provenance available in expandable details.
- Expanded standard service capabilities for climate, environment, power
  outlets, locks, irrigation, and media input sources. Unclassified services
  retain their UUID without inferred capabilities.

## 0.4.1 - 2026-10-30

- Display the report generator version in the inspector header and metadata.
- Added a security policy and private vulnerability reporting guidance, and
  aligned privacy documentation with the explicit plain-HTTP option.

## 0.4.0 - 2026-10-30

- Added explicit plain-HTTP server and client configuration for trusted LAN or
  VPN deployments while retaining HTTPS as the default.
- Prevented incomplete TLS handshakes from blocking all inspector requests.
- Added Python 3.14-compatible X.509 CA extensions to generated private server
  credentials and removed an invalid-escape warning from HTML generation.
- Added a service-owned private JSON configuration for readable HTTP Basic
  viewer credentials, installed with initial `admin/admin` values in the
  server data directory and explicitly excluded from HTTP file serving.
- Scoped extraction, layout, infrastructure, scenes, and condition lookups to
  the HomeKit primary home when multiple homes share the local database.
- Added extraction metadata and logging for the selected home and available
  home count.
- Added signed HTTPS report publication with HMAC-SHA256, timestamp and nonce
  replay protection, SHA-256 verification, upload limits, and atomic replacement.
- Reduced publication to two explicit modes: default local HTML generation and
  secure publication to the optional report server.
- Added private credential migration, direct TLS support, credential generation,
  and a documented server installer entry point.
- Added a user-scoped CLI installer with private configuration migration,
  repeatable code updates, a stable command path, and a safe uninstaller that
  preserves private data by default.
- Added a `show-config` CLI subcommand for displaying the effective
  configuration file and resolved paths without accessing HomeKit data.
- Added a configuration-driven refresh CLI with locking, temporary generation,
  validation, status reporting, and atomic publication.
- Added a stable command launcher with refresh, validation, and status
  subcommands and a configurable Python executable.
- Added independent contextual filters to Home Layout, Bridges, Manufacturers,
  Capabilities, Scenes, and Theme Editor while retaining the existing
  Automations filters.
- Added decoded HomeKit service capability badges and a Capabilities tab for grouping accessories by service type.
- Expanded decoded HomeKit service capabilities to include fans, lightbulbs, and switches.
- Refined the summary ribbon into clickable high-level inventory and navigation metrics.

## 0.3.0 - 2026-08-08

- Added the optional static LAN server and systemd install assets for serving a generated inspector report from a private host.
- Improved mobile ergonomics with a compact header and inline automation and scene details.
- Added HomeKit capture timestamp display to the inspector header.
- Added automation participation tracing in Home Layout and Manufacturers views.
- Added bridge-origin and Matter integration markers on accessory cards.
- Improved navigation with dedicated infrastructure tabs and fixed bridge search filtering.
- Expanded scene/action decoding, including provided names, decoded values, and media actions.
- Added automatic dark mode using the browser color-scheme preference.
- Added Standard/Advanced automation classification, badges, and filtering.
- Added the HomeKit Inspector house-and-magnifier logo.

## 0.2.0

- Previous tagged public version.

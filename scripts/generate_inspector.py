#!/usr/bin/env python3
"""Generate a self-contained HomeKit Inspector HTML file.

The extractor stays read-only.  This script consumes the already-exported JSON
and writes a clean rules JSON plus a standalone HTML file for local review.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import generate_homekit_reports as reports
from generate_condition_diagnostics import (
    build_characteristic_ref_lookup,
    condition_summary,
)

DEFAULT_THEME_NAMES = [
    "Security and zoned alarm",
    "Away presence simulation",
    "Blinds and awnings",
    "Indoor motion",
    "Access and doors",
    "Buttons and remotes",
    "Climate and humidity",
    "TV and media",
    "Time, sun and light level",
    "Home modes and shutdown",
    "Lighting and scenes",
    "Other",
]

REPORT_THEME_TO_EXPLORER_THEME = {
    reports.THEME_SECURITY: "Security and zoned alarm",
    reports.THEME_AWAY: "Away presence simulation",
    reports.THEME_BLINDS: "Blinds and awnings",
    reports.THEME_MOTION: "Indoor motion",
    reports.THEME_ACCESS: "Access and doors",
    reports.THEME_BUTTONS: "Buttons and remotes",
    reports.THEME_CLIMATE: "Climate and humidity",
    reports.THEME_TV: "TV and media",
    reports.THEME_TIME: "Time, sun and light level",
    reports.THEME_MODES: "Home modes and shutdown",
    reports.THEME_LIGHTING: "Lighting and scenes",
    reports.THEME_OTHER: "Other",
}


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def translate_rule(text):
    if not text:
        return ""
    replacements = [
        ("presencia == alguien en casa", "presence == someone is home"),
        ("detecta movimiento", "detects motion"),
        ("detecta ocupacion", "detects occupancy"),
        ("se abre", "opens"),
        ("se cierra", "closes"),
        ("cambia estado de contacto", "changes contact state"),
        ("cambia estado de puerta", "changes door state"),
        ("cambia", "changes"),
        ("activar escena ", "Run scene: "),
        ("activar Zona ", "Turn on zone: "),
        ("activar ", "Turn on "),
        ("cada dia a las ", "Every day at "),
        ("evento de calendario", "Calendar event"),
        ("reproducir audio/media", "Play audio/media"),
        ("pausar/detener reproduccion multimedia", "Pause/stop media playback"),
        ("accion multimedia", "Media playback action"),
        ("accion de Natural Lighting", "Natural Lighting action"),
        ("valor desconocido", "unknown value"),
        ("sin dispositivo disparador exportado por homed_extract", "No trigger device exported by homed_extract"),
        ("sin acciones exportadas en uno o mas action sets", "No actions exported in one or more action sets"),
        ("revisar disparador en Eve/Home: clasificacion por nombre fiable, dispositivo extraido de baja confianza", "Review trigger in Eve/Home: name-based classification is reliable, extracted device is low confidence"),
        ("correccion manual", "manual correction"),
    ]
    out = text
    for src, dst in replacements:
        out = out.replace(src, dst)
    return out


def build_rows(data, db_path, private_overrides=None, homebridge_security=None):
    reports.refresh_spatial_zone_maps(data)
    reports.configure_private_overrides(private_overrides or {})
    rows = [
        reports.summarize(auto, index)
        for index, auto in enumerate(data.get("automations") or [], start=1)
    ]
    char_refs = build_characteristic_ref_lookup(db_path if db_path else None)
    condition_by_name = {
        summary["name"]: summary
        for summary in (
            condition_summary(auto, char_refs)
            for auto in data.get("automations") or []
            if auto.get("evaluationCondition")
        )
    }
    for row in rows:
        summary = condition_by_name.get(row["name"])
        if not summary:
            continue
        row["conditionRules"] = [rule["text"] for rule in summary["rules"]]
        row["conditionConfidence"] = summary["confidence"]
    reports.apply_security_event_filters(rows, data)
    for row in rows:
        reports.recompute_filtered_when_rules(row)
    for row in rows:
        reports.apply_manual_rule_overrides(row)
    reports.apply_homebridge_security_enrichment(rows, homebridge_security or {})
    return rows


def row_to_rule(row, room_lookup):
    devices = reports.uniq([*(row.get("eventDevices") or []), *(row.get("actionDevices") or [])])
    rooms = reports.uniq(room_lookup.get(device) for device in devices)
    translated_then = [translate_rule(rule) for rule in row.get("thenRules") or []]
    has_unresolved = any(
        "hex:" in rule or "special/toggle value" in rule
        for rule in translated_then
    )
    confidence_parts = []
    if row.get("conditionConfidence"):
        confidence_parts.append(row["conditionConfidence"])
    if row.get("notes"):
        confidence_parts.append("review")
    if has_unresolved:
        confidence_parts.append("unresolved-values")
    confidence = " / ".join(confidence_parts) or "auto"
    return {
        "id": row["index"],
        "name": row["name"],
        "enabled": row["enabled"],
        "triggerType": row.get("triggerType") or "",
        "when": [translate_rule(rule) for rule in row.get("whenRules") or []],
        "if": [translate_rule(rule) for rule in row.get("conditionRules") or []],
        "then": translated_then,
        "events": row.get("eventDevices") or [],
        "actions": row.get("actionDevices") or [],
        "rooms": rooms,
        "notes": [translate_rule(note) for note in row.get("notes") or []],
        "confidence": confidence,
        "hasConditions": bool(row.get("hasConditions")),
        "hasUnresolvedValues": has_unresolved,
        "rawCounts": {
            "events": row.get("eventCount") or 0,
            "actionSets": row.get("actionSetCount") or 0,
            "actions": row.get("actionCount") or 0,
        },
    }


def build_payload(data, rows, theme_config=None, context_sources=None):
    room_lookup = reports.build_device_room_lookup(data, rows)
    rules = [row_to_rule(row, room_lookup) for row in rows]
    zones = [
        {
            "name": reports.canonical_zone_name(zone.get("name") or ""),
            "rooms": zone.get("rooms") or [],
        }
        for zone in (data.get("inventory") or {}).get("zones") or []
    ]
    stats = {
        "total": len(rules),
        "active": sum(1 for rule in rules if rule["enabled"]),
        "inactive": sum(1 for rule in rules if not rule["enabled"]),
        "withConditions": sum(1 for rule in rules if rule["hasConditions"]),
        "unresolved": sum(1 for rule in rules if rule["hasUnresolvedValues"]),
    }
    return {
        "metadata": {
            "source": data.get("extractionSource"),
            "extractionDate": data.get("extractionDate"),
            "databasePath": data.get("databasePath"),
            "homeName": data.get("homeName"),
            "language": "en",
            "notes": [
                "Generated from local HomeKit SQLite export.",
                "Device, room, scene, and automation names are kept as configured in HomeKit.",
                "Keep generated output private unless reviewed for household-specific names and topology.",
            ],
        },
        "stats": stats,
        "zones": zones,
        "layout": {},
        "infrastructure": {},
        "contextSources": context_sources or [],
        "scenes": [],
        "themeConfig": theme_config or default_theme_config(),
        "rules": rules,
    }


def default_theme_config():
    return {
        "version": 1,
        "themes": list(DEFAULT_THEME_NAMES),
        "automationThemeOverrides": {},
    }


def load_theme_config(path):
    config = default_theme_config()
    if not path:
        return config
    loaded = load_json(path)
    if not isinstance(loaded, dict):
        raise ValueError(f"Theme config must be a JSON object: {path}")
    themes = loaded.get("themes")
    overrides = loaded.get("automationThemeOverrides")
    if themes is not None:
        if not isinstance(themes, list) or not all(isinstance(item, str) for item in themes):
            raise ValueError("themeConfig.themes must be a list of strings")
        config["themes"] = themes
    if overrides is not None:
        if not isinstance(overrides, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in overrides.items()
        ):
            raise ValueError("themeConfig.automationThemeOverrides must be a string-to-string object")
        config["automationThemeOverrides"] = overrides
    if "version" in loaded:
        config["version"] = loaded["version"]
    return config


def inferred_theme_config(rows):
    config = default_theme_config()
    config["automationThemeOverrides"] = {
        row["name"]: REPORT_THEME_TO_EXPLORER_THEME.get(row["primaryTheme"], "Other")
        for row in rows
        if row.get("name")
    }
    return config


def bytes_to_hex(value):
    if isinstance(value, bytes):
        return f"hex:{value.hex()}"
    return value


def open_readonly_sqlite(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def display_accessory_name(name, manufacturer, model, uid, fallback_id):
    return name or " ".join(part for part in [manufacturer, model] if part) or uid or f"Accessory {fallback_id}"


def load_home_layout(db_path, fallback_data):
    zones = [
        {
            "name": reports.canonical_zone_name(zone.get("name") or ""),
            "rooms": zone.get("rooms") or [],
        }
        for zone in (fallback_data.get("inventory") or {}).get("zones") or []
    ]
    if not db_path:
        return {"zones": zones, "roomsWithoutZone": []}

    conn = open_readonly_sqlite(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT acc.Z_PK, acc.ZCONFIGUREDNAME, acc.ZPROVIDEDNAME, "
        "acc.ZMANUFACTURER, acc.ZMODEL, acc.ZUNIQUEIDENTIFIER, acc.ZROOM, r.ZNAME "
        "FROM ZMKFACCESSORY acc "
        "LEFT JOIN ZMKFROOM r ON r.Z_PK = acc.ZROOM "
        "ORDER BY r.ZNAME, acc.ZCONFIGUREDNAME, acc.ZMANUFACTURER, acc.ZMODEL"
    )
    accessories = {}
    for pk, name, provided_name, manufacturer, model, uid, room_id, room in cur.fetchall():
        display = display_accessory_name(name or provided_name, manufacturer, model, uid, pk)
        room_name = room or (f"Unnamed HomeKit Room (id {room_id})" if room_id else "Unassigned")
        accessories[pk] = {
            "id": pk,
            "name": display,
            "configuredName": name or "",
            "providedName": provided_name or "",
            "manufacturer": manufacturer or "",
            "model": model or "",
            "uuid": uid or "",
            "room": room_name,
            "services": [],
        }

    cur.execute(
        "SELECT s.Z_PK, s.ZEXPECTEDCONFIGUREDNAME, s.ZNAME, s.ZPROVIDEDNAME, s.ZACCESSORY "
        "FROM ZMKFSERVICE s "
        "ORDER BY s.ZEXPECTEDCONFIGUREDNAME, s.ZNAME, s.ZPROVIDEDNAME"
    )
    for pk, configured, name, provided, acc_pk in cur.fetchall():
        accessory = accessories.get(acc_pk)
        if not accessory:
            continue
        service_name = configured or name or provided
        if not service_name:
            continue
        accessory["services"].append({"id": pk, "name": service_name})

    conn.close()

    rooms = {}
    for accessory in accessories.values():
        room_name = accessory["room"] or "Unassigned"
        rooms.setdefault(room_name, {"name": room_name, "accessories": []})
        rooms[room_name]["accessories"].append(accessory)
    for room in rooms.values():
        room["accessories"].sort(key=lambda item: item["name"].lower())
        for accessory in room["accessories"]:
            seen = set()
            services = []
            for service in accessory["services"]:
                key = service["name"].lower()
                if key in seen:
                    continue
                seen.add(key)
                services.append(service)
            accessory["services"] = services

    zone_room_names = {room for zone in zones for room in zone["rooms"]}
    all_room_names = set(rooms) | zone_room_names
    enriched_zones = []
    for zone in zones:
        enriched_zones.append(
            {
                "name": zone["name"],
                "rooms": [rooms.get(room, {"name": room, "accessories": []}) for room in zone["rooms"]],
            }
        )
    rooms_without_zone = [
        room
        for name, room in sorted(rooms.items())
        if name not in zone_room_names
    ]
    return {
        "zones": enriched_zones,
        "roomsWithoutZone": rooms_without_zone,
        "stats": {
            "zones": len(enriched_zones),
            "rooms": len(all_room_names),
            "accessories": len(accessories),
            "namedServices": sum(len(accessory["services"]) for accessory in accessories.values()),
        },
    }


def load_infrastructure(db_path):
    if not db_path:
        return {"homeHubs": [], "bridges": []}
    conn = open_readonly_sqlite(db_path)
    cur = conn.cursor()

    cur.execute("SELECT ZPREFERREDRESIDENTIDSIDENTIFIERS FROM ZMKFRESIDENTSELECTION ORDER BY Z_PK LIMIT 1")
    row = cur.fetchone()
    preferred_resident_blob = row[0] if row and row[0] else b""

    cur.execute(
        "SELECT res.Z_PK, res.ZNAME, res.ZREACHABLE, res.ZIDSIDENTIFIER, "
        "acc.Z_PK, acc.ZCONFIGUREDNAME, acc.ZMANUFACTURER, acc.ZMODEL, "
        "acc.ZUNIQUEIDENTIFIER, r.ZNAME "
        "FROM ZMKFRESIDENT res "
        "LEFT JOIN ZMKFACCESSORY acc ON acc.Z_PK = res.ZAPPLEMEDIAACCESSORY "
        "LEFT JOIN ZMKFROOM r ON r.Z_PK = acc.ZROOM "
        "ORDER BY res.ZREACHABLE DESC, acc.ZCONFIGUREDNAME, res.ZNAME"
    )
    home_hubs = []
    for (
        resident_id,
        resident_name,
        reachable,
        ids_identifier,
        accessory_id,
        accessory_name,
        manufacturer,
        model,
        uid,
        room,
    ) in cur.fetchall():
        name = display_accessory_name(accessory_name or resident_name, manufacturer, model, uid, accessory_id or resident_id)
        is_primary = bool(ids_identifier and preferred_resident_blob and ids_identifier in preferred_resident_blob)
        home_hubs.append(
            {
                "residentId": resident_id,
                "accessoryId": accessory_id,
                "name": name,
                "residentName": resident_name or "",
                "manufacturer": manufacturer or "",
                "model": model or "",
                "room": room or "",
                "reachable": bool(reachable),
                "primary": is_primary,
            }
        )

    cur.execute(
        "SELECT host.Z_PK, host.ZCONFIGUREDNAME, host.ZPROVIDEDNAME, "
        "host.ZMANUFACTURER, host.ZMODEL, host.ZUNIQUEIDENTIFIER, hr.ZNAME, "
        "child.Z_PK, child.ZCONFIGUREDNAME, child.ZPROVIDEDNAME, "
        "child.ZMANUFACTURER, child.ZMODEL, child.ZUNIQUEIDENTIFIER, cr.ZNAME "
        "FROM ZMKFACCESSORY host "
        "JOIN ZMKFACCESSORY child ON child.ZHOSTACCESSORY = host.Z_PK "
        "LEFT JOIN ZMKFROOM hr ON hr.Z_PK = host.ZROOM "
        "LEFT JOIN ZMKFROOM cr ON cr.Z_PK = child.ZROOM "
        "WHERE child.Z_PK != host.Z_PK "
        "ORDER BY host.ZCONFIGUREDNAME, host.ZMANUFACTURER, host.ZMODEL, "
        "child.ZCONFIGUREDNAME, child.ZMANUFACTURER, child.ZMODEL"
    )
    bridges_by_id = {}
    for (
        host_id,
        host_name,
        host_provided_name,
        host_manufacturer,
        host_model,
        host_uid,
        host_room,
        child_id,
        child_name,
        child_provided_name,
        child_manufacturer,
        child_model,
        child_uid,
        child_room,
    ) in cur.fetchall():
        bridge = bridges_by_id.setdefault(
            host_id,
            {
                "id": host_id,
                "name": display_accessory_name(host_name or host_provided_name, host_manufacturer, host_model, host_uid, host_id),
                "manufacturer": host_manufacturer or "",
                "model": host_model or "",
                "room": host_room or "",
                "accessories": [],
            },
        )
        bridge["accessories"].append(
            {
                "id": child_id,
                "name": display_accessory_name(child_name or child_provided_name, child_manufacturer, child_model, child_uid, child_id),
                "manufacturer": child_manufacturer or "",
                "model": child_model or "",
                "room": child_room or "",
            }
        )
    conn.close()

    bridges = sorted(
        bridges_by_id.values(),
        key=lambda item: (-len(item["accessories"]), item["name"].lower()),
    )
    return {
        "homeHubs": home_hubs,
        "bridges": bridges,
        "stats": {
            "homeHubs": len(home_hubs),
            "reachableHomeHubs": sum(1 for hub in home_hubs if hub["reachable"]),
            "bridges": len(bridges),
            "bridgedAccessories": sum(len(bridge["accessories"]) for bridge in bridges),
        },
    }



def load_scenes(db_path):
    if not db_path:
        return []
    conn = open_readonly_sqlite(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT aset.Z_PK, aset.ZNAME, aset.ZTYPE, a.Z_PK, a.Z_ENT, "
        "a.ZTARGETVALUE, s.ZNAME, s.ZEXPECTEDCONFIGUREDNAME, acc.ZCONFIGUREDNAME, "
        "r.ZNAME, c.ZMANUFACTURERDESCRIPTION, c.ZFORMAT "
        "FROM ZMKFACTIONSET aset "
        "LEFT JOIN ZMKFACTION a ON a.ZACTIONSET = aset.Z_PK "
        "LEFT JOIN ZMKFSERVICE s ON s.Z_PK = a.ZSERVICE "
        "LEFT JOIN ZMKFACCESSORY acc ON acc.Z_PK = a.ZACCESSORY1 "
        "LEFT JOIN ZMKFROOM r ON r.Z_PK = acc.ZROOM "
        "LEFT JOIN ZMKFCHARACTERISTIC c ON c.ZSERVICE = a.ZSERVICE "
        "  AND c.ZINSTANCEID = a.ZCHARACTERISTICID "
        "WHERE aset.ZTYPE != 'HMActionSetTypeTriggerOwned' "
        "ORDER BY aset.ZTYPE, aset.ZNAME, a.Z_PK"
    )
    scenes_by_id = {}
    for row in cur.fetchall():
        (
            scene_id,
            scene_name,
            scene_type,
            action_id,
            action_ent,
            target_value,
            service_name,
            service_configured_name,
            accessory_name,
            room,
            characteristic,
            fmt,
        ) = row
        scene = scenes_by_id.setdefault(
            scene_id,
            {
                "id": scene_id,
                "name": scene_name or f"Scene {scene_id}",
                "type": scene_type or "",
                "actions": [],
            },
        )
        if action_id is None:
            continue
        action = {
            "actionType": "characteristicWrite" if action_ent == 36 else f"ent_{action_ent}",
            "targetValueRaw": bytes_to_hex(target_value),
            "characteristic": characteristic or "",
            "format": fmt or "",
            "accessoryName": accessory_name,
            "room": room or "",
            "serviceName": service_configured_name or service_name,
        }
        rule = reports.action_rule(action, scene["name"])
        scene["actions"].append(
            {
                "target": reports.action_target(action) or "",
                "room": room or "",
                "characteristic": characteristic or "",
                "value": bytes_to_hex(target_value),
                "rule": translate_rule(rule) if rule else "",
            }
        )
    conn.close()
    return list(scenes_by_id.values())


def attach_scene_references(payload, scenes):
    by_name = {}
    for scene in scenes:
        by_name.setdefault(scene["name"], []).append(scene)
    payload["scenes"] = scenes
    for rule in payload["rules"]:
        refs = []
        for then_rule in rule.get("then") or []:
            if not then_rule.startswith("Run scene: "):
                continue
            scene_name = then_rule.replace("Run scene: ", "", 1)
            refs.extend(by_name.get(scene_name) or [])
        rule["sceneRefs"] = [
            {
                "id": scene["id"],
                "name": scene["name"],
                "type": scene["type"],
                "actions": scene["actions"],
            }
            for scene in refs
        ]


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HomeKit Inspector</title>
<style>
:root {
  color-scheme: light;
  --bg: #f4f7fb;
  --panel: #ffffff;
  --surface: #f8fafc;
  --surface-blue: #edf4ff;
  --text: #172033;
  --muted: #68758a;
  --line: #dce3ed;
  --line-strong: #cbd5e1;
  --accent: #2563eb;
  --accent-dark: #1749b1;
  --cyan: #0891b2;
  --warn: #a65f00;
  --bad: #bd3e4a;
  --good: #11834f;
  --violet: #6d5bd0;
  --shadow: 0 1px 2px rgba(23,32,51,.04), 0 6px 18px rgba(23,32,51,.045);
}
* { box-sizing: border-box; }
body { margin: 0; font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--text); background: var(--bg); }
header { position: sticky; top: 0; z-index: 2; background: rgba(255,255,255,.94); border-bottom: 1px solid var(--line); box-shadow: 0 1px 6px rgba(23,32,51,.04); backdrop-filter: blur(12px); }
.wrap { max-width: 1480px; margin: 0 auto; padding: 18px 24px; }
h1 { margin: 0; font-size: 25px; line-height: 1.2; letter-spacing: 0; }
.title-row { display: flex; align-items: center; justify-content: space-between; gap: 20px; margin-bottom: 16px; }
.brand { display: flex; align-items: center; gap: 11px; }
.brand-mark { display: grid; place-items: center; width: 34px; height: 34px; border-radius: 8px; background: var(--accent); color: #fff; font-size: 14px; font-weight: 750; box-shadow: 0 5px 12px rgba(37,99,235,.2); }
.home-context { text-align: right; min-width: 0; }
.home-context span { display: block; color: var(--muted); font-size: 10px; font-weight: 700; text-transform: uppercase; }
.home-name { display: block; color: var(--text); font-size: 14px; font-weight: 650; overflow-wrap: anywhere; }
.summary { display: grid; grid-template-columns: repeat(6, minmax(100px, 1fr)); gap: 10px; margin-bottom: 16px; }
.metric { position: relative; overflow: hidden; background: var(--panel); border: 1px solid var(--line); border-radius: 7px; padding: 11px 13px 10px; min-width: 0; box-shadow: 0 1px 2px rgba(23,32,51,.025); text-align: left; cursor: pointer; }
.metric::before { content: ""; position: absolute; inset: 0 auto 0 0; width: 3px; background: var(--metric-color, var(--accent)); }
.metric b { display: block; font-size: 20px; line-height: 1.15; color: var(--metric-color, var(--text)); }
.metric span { color: var(--muted); font-size: 11px; font-weight: 600; }
.metric:hover { border-color: #8ab2ff; box-shadow: 0 0 0 3px rgba(37,99,235,.08); }
.metric:focus-visible { outline: 3px solid rgba(37,99,235,.2); outline-offset: 2px; }
.metric.total { --metric-color: var(--accent); }
.metric.active { --metric-color: var(--good); }
.metric.inactive { --metric-color: var(--bad); }
.metric.conditions { --metric-color: var(--violet); }
.metric.unresolved { --metric-color: var(--warn); }
.metric.scenes { --metric-color: var(--cyan); }
.nav-row { display: grid; grid-template-columns: minmax(0, auto) minmax(240px, 1fr); gap: 16px; align-items: center; }
.filters { display: none; grid-template-columns: repeat(4, minmax(150px, 1fr)); gap: 8px; margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--line); }
.filters.visible { display: grid; }
.filter-toggle { display: none; border: 1px solid var(--line-strong); border-radius: 6px; background: #fff; color: var(--text); padding: 9px 11px; font-weight: 650; cursor: pointer; }
.tabs { display: flex; flex-wrap: nowrap; gap: 2px; padding: 3px; border-radius: 8px; background: var(--surface); border: 1px solid var(--line); overflow-x: auto; }
.tab { border: 0; border-radius: 5px; background: transparent; color: var(--muted); padding: 8px 10px; font-weight: 600; cursor: pointer; transition: background .15s ease, color .15s ease, box-shadow .15s ease; }
.tab:hover { color: var(--text); background: #fff; }
.tab.active { background: #fff; color: var(--accent-dark); box-shadow: 0 1px 4px rgba(23,32,51,.12); }
.global-search { justify-self: stretch; }
input, select { width: 100%; border: 1px solid var(--line-strong); border-radius: 6px; background: white; padding: 9px 11px; color: var(--text); font: inherit; outline: none; }
input:focus, select:focus, textarea:focus { border-color: #8ab2ff; box-shadow: 0 0 0 3px rgba(37,99,235,.11); }
button { font: inherit; }
textarea { width: 100%; min-height: 320px; resize: vertical; border: 1px solid var(--line); border-radius: 6px; padding: 10px; font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; outline: none; }
main.wrap { display: grid; grid-template-columns: 330px minmax(0, 1fr); gap: 18px; align-items: start; padding-top: 22px; }
main.wrap.single { display: block; }
.sidebar, .content { min-width: 0; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; box-shadow: var(--shadow); }
.list { overflow: auto; max-height: calc(100vh - 190px); }
.item { position: relative; display: block; width: 100%; text-align: left; border: 0; border-bottom: 1px solid var(--line); background: white; padding: 12px 14px; cursor: pointer; }
.item:last-child { border-bottom: 0; }
.item:hover { background: var(--surface); }
.item.active { background: var(--surface-blue); }
.item.active::before { content: ""; position: absolute; inset: 0 auto 0 0; width: 3px; background: var(--accent); }
.item-title { font-weight: 650; margin-bottom: 4px; }
.item-meta { display: flex; flex-wrap: wrap; gap: 5px; color: var(--muted); font-size: 12px; }
.badge { display: inline-flex; align-items: center; border: 1px solid var(--line); border-radius: 5px; padding: 2px 7px; background: var(--surface); color: var(--muted); font-size: 11px; font-weight: 600; }
.badge.active { color: #087443; border-color: #a9dec4; background: #e9f8f0; }
.badge.inactive { color: #a62d3b; border-color: #efbdc2; background: #fff0f1; }
.badge.warn { color: #925200; border-color: #edcf9f; background: #fff7e8; }
.badge.automation { color: #1749b1; border-color: #b8cffb; background: #edf4ff; cursor: pointer; }
.badge.automation:hover { border-color: #8ab2ff; background: #e0edff; }
.badge.automation.inactive-only { color: #596579; border-color: #d3dce8; background: #f3f6fa; }
.usage-row { display: flex; flex-wrap: wrap; gap: 5px; margin: 8px 0 4px; }
.usage-list { margin-top: 5px; color: var(--muted); font-size: 12px; }
.usage-list summary { cursor: pointer; font-weight: 650; }
.usage-list ul { margin-top: 5px; }
.detail { padding: 22px; }
.detail h2 { margin: 0 0 8px; font-size: 23px; line-height: 1.25; letter-spacing: 0; }
.detail-top { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 18px; }
.columns { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(270px, 1fr)); gap: 12px; }
.card { border: 1px solid var(--line); border-radius: 7px; padding: 14px; background: #fff; min-width: 0; box-shadow: 0 1px 3px rgba(23,32,51,.025); }
.card h3 { margin: 0 0 10px; font-size: 12px; text-transform: uppercase; color: var(--muted); letter-spacing: .04em; }
.card h4 { margin: 0 0 7px; font-size: 15px; }
.columns > .card { position: relative; overflow: hidden; padding-top: 16px; }
.columns > .card::before { content: ""; position: absolute; inset: 0 0 auto; height: 3px; background: var(--column-color); }
.columns > .card:nth-child(1) { --column-color: var(--accent); background: #f7faff; }
.columns > .card:nth-child(2) { --column-color: var(--violet); background: #faf9ff; }
.columns > .card:nth-child(3) { --column-color: var(--good); background: #f7fcf9; }
.section.card:has(> .grid), .section.card:has(> .section.card) { border: 0; padding: 0; background: transparent; box-shadow: none; }
.section.card:has(> .grid) > h3, .section.card:has(> .section.card) > h3 { margin: 0 0 10px; color: var(--text); font-size: 15px; text-transform: none; }
.section.card > .section.card { margin-top: 12px; }
ul { margin: 0; padding-left: 18px; }
li { margin: 4px 0; }
.section { margin-top: 16px; }
.empty { color: var(--muted); font-style: italic; }
code { background: #edf2f8; padding: 1px 4px; border-radius: 4px; }
.footer-note { color: var(--muted); font-size: 12px; margin-top: 14px; }
.actions-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
.primary { border: 1px solid var(--accent-dark); border-radius: 6px; background: var(--accent); color: white; padding: 8px 11px; cursor: pointer; }
.secondary { border: 1px solid var(--line); border-radius: 6px; background: #fff; color: var(--text); padding: 8px 10px; cursor: pointer; }
.primary:hover { background: var(--accent-dark); }
.secondary:hover { background: var(--surface); border-color: var(--line-strong); }
@media (max-width: 900px) {
  .wrap { padding-left: 16px; padding-right: 16px; }
  .summary { grid-template-columns: repeat(3, 1fr); }
  .nav-row { grid-template-columns: 1fr; }
  .filters { grid-template-columns: 1fr 1fr; }
  main.wrap { grid-template-columns: 1fr; }
  .list { max-height: 360px; }
  .columns { grid-template-columns: 1fr; }
}
@media (max-width: 560px) {
  header { position: static; }
  .wrap { padding: 10px 12px; }
  .title-row { align-items: center; gap: 10px; margin-bottom: 10px; }
  .brand { gap: 8px; min-width: 0; }
  .brand-mark { width: 30px; height: 30px; border-radius: 7px; font-size: 12px; flex: 0 0 auto; }
  h1 { font-size: 19px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .home-context { max-width: 42%; }
  .home-context span { font-size: 9px; }
  .home-name { font-size: 12px; line-height: 1.25; }
  .summary { display: flex; gap: 7px; overflow-x: auto; margin: 0 -12px 8px; padding: 0 12px 2px; scrollbar-width: none; }
  .summary::-webkit-scrollbar, .tabs::-webkit-scrollbar { display: none; }
  .metric { flex: 0 0 92px; min-height: 48px; padding: 7px 9px 6px 11px; }
  .metric b { font-size: 17px; }
  .metric span { font-size: 10px; }
  .nav-row { grid-template-columns: minmax(0, 1fr) auto; gap: 7px; }
  .tabs { grid-column: 1 / -1; order: 1; flex-wrap: nowrap; margin: 0 -12px; padding: 3px 12px; border-left: 0; border-right: 0; border-radius: 0; background: transparent; }
  .tab { flex: 0 0 auto; padding: 7px 9px; white-space: nowrap; }
  .global-search { order: 2; min-width: 0; }
  .filter-toggle { display: none; order: 3; }
  .filter-toggle.visible { display: inline-flex; align-items: center; justify-content: center; min-width: 76px; }
  .filters.visible { display: none; }
  .filters.visible.open { display: grid; grid-template-columns: 1fr 1fr; gap: 7px; margin-top: 8px; padding-top: 8px; }
  .detail { padding: 16px; }
  .grid { grid-template-columns: 1fr; }
  main.wrap { padding-top: 12px; }
  .list { max-height: none; }
}
</style>
</head>
<body>
<header>
  <div class="wrap">
    <div class="title-row">
      <div class="brand"><span class="brand-mark">HK</span><h1>HomeKit Inspector</h1></div>
      <div class="home-context"><span>Home</span><strong class="home-name" id="homeName"></strong></div>
    </div>
    <div class="summary" id="summary"></div>
    <div class="nav-row">
      <div class="tabs" id="tabs">
        <button class="tab active" data-tab="layout">Home Layout</button>
        <button class="tab" data-tab="hubs">Hubs</button>
        <button class="tab" data-tab="bridges">Bridges</button>
        <button class="tab" data-tab="context">Context Sources</button>
        <button class="tab" data-tab="manufacturers">Manufacturers</button>
        <button class="tab" data-tab="automations">Automations</button>
        <button class="tab" data-tab="scenes">Scenes</button>
        <button class="tab" data-tab="config">Theme Editor</button>
      </div>
      <input class="global-search" id="search" type="search" placeholder="Search current view">
      <button class="filter-toggle" id="filterToggle" type="button" aria-expanded="false">Filters</button>
    </div>
    <div class="filters" id="automationFilters">
      <select id="status"><option value="">All statuses</option><option value="active">Active</option><option value="inactive">Inactive</option></select>
      <select id="theme"><option value="">All themes</option></select>
      <select id="room"><option value="">All rooms</option></select>
      <select id="confidence"><option value="">All confidence</option><option value="unresolved">Unresolved values</option><option value="review">Needs review</option><option value="auto">Auto</option></select>
    </div>
  </div>
</header>
<main class="wrap" id="main">
  <aside class="sidebar panel"><div class="list" id="list"></div></aside>
  <section class="content panel"><div class="detail" id="detail"></div></section>
</main>
<script id="payload" type="application/json">__PAYLOAD__</script>
<script>
const data = JSON.parse(document.getElementById('payload').textContent);
const CONFIG_KEY = 'homekitInspector.themeConfig.v1';
const UNASSIGNED_THEME = 'Unassigned';
let currentTab = 'layout';
let selectedSceneId = data.scenes[0]?.id;
let selectedId = data.rules[0]?.id;
let quickFilter = '';
let filtersExpanded = false;
const els = {
  homeName: document.getElementById('homeName'),
  summary: document.getElementById('summary'),
  tabs: document.getElementById('tabs'),
  main: document.getElementById('main'),
  search: document.getElementById('search'),
  status: document.getElementById('status'),
  theme: document.getElementById('theme'),
  room: document.getElementById('room'),
  confidence: document.getElementById('confidence'),
  filterToggle: document.getElementById('filterToggle'),
  automationFilters: document.getElementById('automationFilters'),
  list: document.getElementById('list'),
  detail: document.getElementById('detail'),
};
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function uniq(values) { return [...new Set(values.filter(Boolean))].sort((a,b) => a.localeCompare(b)); }
function readConfig() {
  try {
    const saved = localStorage.getItem(CONFIG_KEY);
    return saved ? JSON.parse(saved) : structuredClone(data.themeConfig);
  } catch {
    return structuredClone(data.themeConfig);
  }
}
let themeConfig = readConfig();
function configuredTheme(rule) {
  const exact = themeConfig.automationThemeOverrides?.[rule.name];
  if (exact) return exact;
  return UNASSIGNED_THEME;
}
function applyConfiguredThemes() {
  for (const rule of data.rules) rule.displayTheme = configuredTheme(rule);
}
function textOf(rule) {
  return [rule.name, rule.displayTheme, ...rule.when, ...rule.if, ...rule.then, ...rule.events, ...rule.actions, ...rule.rooms, ...rule.notes].join(' ').toLowerCase();
}
function roomText(room) {
  return [room.name, ...(room.accessories || []).flatMap(accessory => [
    accessory.name,
    accessory.manufacturer,
    accessory.model,
    ...(accessory.services || []).map(service => service.name),
  ])].join(' ').toLowerCase();
}
function filterRoom(room, q) {
  if (!q || room.name.toLowerCase().includes(q)) return room;
  return {
    ...room,
    accessories: (room.accessories || []).filter(accessory => accessoryText(accessory).includes(q)),
  };
}
function zoneText(zone) {
  return [zone.name, ...(zone.rooms || []).map(roomText)].join(' ').toLowerCase();
}
function normalizeName(value) {
  return String(value ?? '').trim().toLowerCase().replace(/\s+/g, ' ');
}
function allAccessories() {
  const out = [];
  const layout = data.layout || {};
  for (const zone of layout.zones || []) {
    for (const room of zone.rooms || []) {
      for (const accessory of room.accessories || []) out.push({...accessory, room: room.name, zone: zone.name});
    }
  }
  for (const room of layout.roomsWithoutZone || []) {
    for (const accessory of room.accessories || []) out.push({...accessory, room: room.name, zone: 'No Zone'});
  }
  return out;
}
function accessoryText(accessory) {
  return [
    accessory.name,
    accessory.manufacturer,
    accessory.model,
    accessory.room,
    accessory.zone,
    ...(accessory.services || []).map(service => service.name),
  ].join(' ').toLowerCase();
}
function accessoryAliases(accessory) {
  return uniq([
    accessory.name,
    accessory.configuredName,
    accessory.providedName,
    ...(accessory.services || []).map(service => service.name),
  ]).filter(alias => normalizeName(alias).length >= 3);
}
function buildAliasIndex() {
  const index = new Map();
  for (const accessory of allAccessories()) {
    for (const alias of accessoryAliases(accessory)) {
      const normalized = normalizeName(alias);
      if (!normalized) continue;
      if (!index.has(normalized)) index.set(normalized, {alias, accessories: []});
      index.get(normalized).accessories.push(accessory);
    }
  }
  for (const [key, entry] of index) {
    const uniqueIds = new Set(entry.accessories.map(accessory => accessory.id));
    if (uniqueIds.size !== 1) index.delete(key);
  }
  return index;
}
function addUsage(usages, accessory, rule, role, via = '') {
  const key = String(accessory.id);
  if (!usages.has(key)) usages.set(key, []);
  const duplicate = usages.get(key).some(item => item.ruleId === rule.id && item.role === role && item.via === via);
  if (duplicate) return;
  usages.get(key).push({
    ruleId: rule.id,
    name: rule.name,
    enabled: rule.enabled,
    role,
    via,
  });
}
function textHasAlias(text, alias) {
  const haystack = ` ${normalizeName(text)} `;
  const needle = normalizeName(alias);
  if (!needle || needle.length < 4) return false;
  return haystack.includes(` ${needle} `);
}
function buildAutomationUsage() {
  const aliasIndex = buildAliasIndex();
  const usages = new Map();
  const addExact = (name, rule, role, via = '') => {
    const match = aliasIndex.get(normalizeName(name));
    if (!match) return;
    addUsage(usages, match.accessories[0], rule, role, via || match.alias);
  };
  for (const rule of data.rules || []) {
    for (const name of rule.events || []) addExact(name, rule, 'WHEN');
    for (const name of rule.actions || []) addExact(name, rule, 'THEN');
    for (const line of rule.if || []) {
      for (const [normalized, match] of aliasIndex) {
        if (!textHasAlias(line, normalized)) continue;
        addUsage(usages, match.accessories[0], rule, 'IF', match.alias);
      }
    }
    for (const scene of rule.sceneRefs || []) {
      for (const action of scene.actions || []) addExact(action.target, rule, 'SCENE', scene.name);
    }
  }
  return usages;
}
function usageSummary(usages) {
  const active = new Set(usages.filter(item => item.enabled).map(item => item.ruleId)).size;
  const inactive = new Set(usages.filter(item => !item.enabled).map(item => item.ruleId)).size;
  const roles = uniq(usages.map(item => item.role));
  return {active, inactive, roles};
}
let automationUsageCache = null;
function automationUsage() {
  if (!automationUsageCache) automationUsageCache = buildAutomationUsage();
  return automationUsageCache;
}
function automationUsageBlock(accessory) {
  const usage = automationUsage().get(String(accessory.id)) || [];
  if (!usage.length) return '';
  const summary = usageSummary(usage);
  const query = accessory.name || usage[0].via || '';
  const title = `${summary.active} active and ${summary.inactive} inactive decoded automation references`;
  const accessoryName = normalizeName(accessory.name);
  return `
    <div class="usage-row" title="${esc(title)}">
      ${summary.active ? `<button class="badge automation" data-automation-query="${esc(query)}">${summary.active} active</button>` : ''}
      ${summary.inactive ? `<button class="badge automation inactive-only" data-automation-query="${esc(query)}">${summary.inactive} inactive</button>` : ''}
      <span class="badge">${esc(summary.roles.join(' / '))}</span>
    </div>
    <details class="usage-list">
      <summary>Automation references</summary>
      <ul>${usage.map(item => `
        <li>${esc(item.name)} <span class="badge ${item.enabled ? 'active' : 'inactive'}">${item.enabled ? 'Active' : 'Inactive'}</span> <span class="badge">${esc(item.role)}</span>${item.via && normalizeName(item.via) !== accessoryName ? ` <span class="badge">${esc(item.via)}</span>` : ''}</li>
      `).join('')}</ul>
    </details>
  `;
}
function bindAutomationUsageLinks() {
  els.detail.querySelectorAll('[data-automation-query]').forEach(btn => btn.addEventListener('click', () => {
    currentTab = 'automations';
    els.search.value = btn.dataset.automationQuery || '';
    els.tabs.querySelectorAll('.tab').forEach(item => item.classList.toggle('active', item.dataset.tab === currentTab));
    render();
  }));
}
function manufacturers() {
  const groups = new Map();
  for (const accessory of allAccessories()) {
    const name = accessory.manufacturer || 'Unknown Manufacturer';
    if (!groups.has(name)) groups.set(name, {name, accessories: []});
    groups.get(name).accessories.push(accessory);
  }
  return [...groups.values()].sort((a, b) => b.accessories.length - a.accessories.length || a.name.localeCompare(b.name));
}
function hubText(hub) {
  return [hub.name, hub.residentName, hub.manufacturer, hub.model, hub.room, hub.primary ? 'primary' : '', hub.reachable ? 'reachable' : 'not reachable'].join(' ').toLowerCase();
}
function bridgeText(bridge) {
  return [
    bridge.name,
    bridge.manufacturer,
    bridge.model,
    bridge.room,
    ...(bridge.accessories || []).flatMap(accessory => [accessory.name, accessory.manufacturer, accessory.model, accessory.room]),
  ].join(' ').toLowerCase();
}
function contextText(source) {
  return [
    source.sourceType,
    source.sourceName,
    ...(source.platforms || []),
    ...(source.zones || []),
    ...(source.helpers || []).flatMap(item => [item.name, item.type]),
    ...(source.webhooks || []).flatMap(item => [item.name, item.type]),
    ...(source.relations || []).flatMap(item => [item.source, item.target, item.relation, item.evidence]),
  ].join(' ').toLowerCase();
}
function fillSelect(select, values) {
  const first = select.firstElementChild;
  select.innerHTML = '';
  if (first) select.appendChild(first);
  for (const value of values) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = value;
    select.appendChild(option);
  }
}
function setTab(tabName) {
  currentTab = tabName;
  els.tabs.querySelectorAll('.tab').forEach(item => item.classList.toggle('active', item.dataset.tab === currentTab));
}
function resetAutomationFilters() {
  els.search.value = '';
  els.status.value = '';
  els.theme.value = '';
  els.room.value = '';
  els.confidence.value = '';
  quickFilter = '';
}
function applySummaryAction(action) {
  resetAutomationFilters();
  filtersExpanded = false;
  if (action === 'scenes') {
    setTab('scenes');
    render();
    return;
  }
  setTab('automations');
  if (action === 'active') els.status.value = 'active';
  if (action === 'inactive') els.status.value = 'inactive';
  if (action === 'conditions') quickFilter = 'conditions';
  if (action === 'unresolved') els.confidence.value = 'unresolved';
  render();
}
function init() {
  applyConfiguredThemes();
  const homeName = data.metadata?.homeName || data.homeName || '';
  els.homeName.textContent = homeName || 'Not identified';
  els.summary.innerHTML = [
    ['Total', data.stats.total, 'total', 'total', 'Show all automations'],
    ['Active', data.stats.active, 'active', 'active', 'Show active automations'],
    ['Inactive', data.stats.inactive, 'inactive', 'inactive', 'Show inactive automations'],
    ['Conditional', data.stats.withConditions, 'conditions', 'conditions', 'Show automations with IF conditions'],
    ['Unresolved', data.stats.unresolved, 'unresolved', 'unresolved', 'Show automations with unresolved decoded values'],
    ['Scenes', data.scenes.length, 'scenes', 'scenes', 'Show scenes'],
  ].map(([label, value, kind, action, title]) => `<button class="metric ${kind}" data-summary-action="${action}" title="${esc(title)}"><b>${value}</b><span>${label}</span></button>`).join('');
  els.summary.querySelectorAll('[data-summary-action]').forEach(btn => btn.addEventListener('click', () => applySummaryAction(btn.dataset.summaryAction)));
  fillSelect(els.theme, uniq(data.rules.map(rule => rule.displayTheme)));
  fillSelect(els.room, uniq(data.rules.flatMap(rule => rule.rooms)));
  for (const el of [els.search, els.status, els.theme, els.room, els.confidence]) el.addEventListener('input', () => {
    quickFilter = '';
    render();
  });
  els.filterToggle.addEventListener('click', () => {
    filtersExpanded = !filtersExpanded;
    render();
  });
  els.tabs.querySelectorAll('.tab').forEach(tab => tab.addEventListener('click', () => {
    setTab(tab.dataset.tab);
    render();
  }));
  render();
}
function filteredRules() {
  const q = els.search.value.trim().toLowerCase();
  return data.rules.filter(rule => {
    if (q && !textOf(rule).includes(q)) return false;
    if (els.status.value === 'active' && !rule.enabled) return false;
    if (els.status.value === 'inactive' && rule.enabled) return false;
    if (els.theme.value && rule.displayTheme !== els.theme.value) return false;
    if (els.room.value && !rule.rooms.includes(els.room.value)) return false;
    if (els.confidence.value === 'unresolved' && !rule.hasUnresolvedValues) return false;
    if (els.confidence.value === 'review' && !rule.confidence.includes('review')) return false;
    if (els.confidence.value === 'auto' && rule.confidence !== 'auto') return false;
    if (quickFilter === 'conditions' && !rule.hasConditions) return false;
    return true;
  }).sort((a, b) => a.name.localeCompare(b.name, undefined, {sensitivity: 'base'}));
}
function renderList(rules) {
  if (!rules.some(rule => rule.id === selectedId)) selectedId = rules[0]?.id;
  els.list.innerHTML = rules.map(rule => `
    <button class="item ${rule.id === selectedId ? 'active' : ''}" data-id="${rule.id}">
      <div class="item-title">${esc(rule.name)}</div>
      <div class="item-meta">
        <span class="badge ${rule.enabled ? 'active' : 'inactive'}">${rule.enabled ? 'Active' : 'Inactive'}</span>
        <span class="badge">${esc(rule.displayTheme)}</span>
        ${rule.hasUnresolvedValues ? '<span class="badge warn">Unresolved</span>' : ''}
      </div>
    </button>
  `).join('') || '<div class="detail empty">No matching automations.</div>';
  els.list.querySelectorAll('.item').forEach(btn => btn.addEventListener('click', () => {
    selectedId = Number(btn.dataset.id);
    render();
  }));
}
function listBlock(items) {
  return items.length ? `<ul>${items.map(item => `<li>${esc(item)}</li>`).join('')}</ul>` : '<div class="empty">None decoded</div>';
}
function sceneBlock(rule) {
  if (!rule.sceneRefs || !rule.sceneRefs.length) return '';
  return `<div class="section card"><h3>Referenced Scenes</h3>${rule.sceneRefs.map(scene => `
    <p><button class="secondary" data-scene-id="${scene.id}">${esc(scene.name)}</button> <span class="badge">${scene.actions.length} actions</span></p>
  `).join('')}</div>`;
}
function renderDetail(rule) {
  if (!rule) {
    els.detail.innerHTML = '<div class="empty">No automation selected.</div>';
    return;
  }
  els.detail.innerHTML = `
    <h2>${esc(rule.name)}</h2>
    <div class="detail-top">
      <span class="badge ${rule.enabled ? 'active' : 'inactive'}">${rule.enabled ? 'Active' : 'Inactive'}</span>
      <span class="badge">${esc(rule.displayTheme)}</span>
      <span class="badge ${rule.hasUnresolvedValues ? 'warn' : ''}">${esc(rule.confidence)}</span>
      <span class="badge">${rule.rawCounts.events} events</span>
      <span class="badge">${rule.rawCounts.actions} actions</span>
    </div>
    <div class="columns">
      <div class="card"><h3>When</h3>${listBlock(rule.when)}</div>
      <div class="card"><h3>If</h3>${listBlock(rule.if)}</div>
      <div class="card"><h3>Then</h3>${listBlock(rule.then)}</div>
    </div>
    <div class="section card"><h3>Devices and Rooms</h3>
      <p><b>Events:</b> ${esc(rule.events.join(', ') || 'None')}</p>
      <p><b>Actions:</b> ${esc(rule.actions.join(', ') || 'None')}</p>
      <p><b>Rooms:</b> ${esc(rule.rooms.join(', ') || 'None')}</p>
    </div>
    ${sceneBlock(rule)}
    <div class="section card"><h3>Notes</h3>${listBlock(rule.notes)}</div>
    <div class="footer-note">Generated locally from HomeKit export. Keep this file private until names and topology are reviewed.</div>
  `;
  els.detail.querySelectorAll('[data-scene-id]').forEach(btn => btn.addEventListener('click', () => {
    selectedSceneId = Number(btn.dataset.sceneId);
    currentTab = 'scenes';
    els.tabs.querySelectorAll('.tab').forEach(item => item.classList.toggle('active', item.dataset.tab === currentTab));
    render();
  }));
}
function sceneText(scene) {
  return [scene.name, scene.type, ...(scene.actions || []).flatMap(action => [action.target, action.room, action.characteristic, action.value, action.rule])].join(' ').toLowerCase();
}
function filteredScenes() {
  const q = els.search.value.trim().toLowerCase();
  return data.scenes.filter(scene => !q || sceneText(scene).includes(q));
}
function renderSceneList(scenes) {
  if (!scenes.some(scene => scene.id === selectedSceneId)) selectedSceneId = scenes[0]?.id;
  els.list.innerHTML = scenes.map(scene => `
    <button class="item ${scene.id === selectedSceneId ? 'active' : ''}" data-id="${scene.id}">
      <div class="item-title">${esc(scene.name)}</div>
      <div class="item-meta"><span class="badge">${esc(scene.type)}</span><span class="badge">${scene.actions.length} actions</span></div>
    </button>
  `).join('') || '<div class="detail empty">No matching scenes.</div>';
  els.list.querySelectorAll('.item').forEach(btn => btn.addEventListener('click', () => {
    selectedSceneId = Number(btn.dataset.id);
    render();
  }));
}
function renderSceneDetail(scene) {
  if (!scene) {
    els.detail.innerHTML = '<div class="empty">No scene selected.</div>';
    return;
  }
  els.detail.innerHTML = `
    <h2>${esc(scene.name)}</h2>
    <div class="detail-top"><span class="badge">${esc(scene.type)}</span><span class="badge">${scene.actions.length} actions</span></div>
    <div class="card"><h3>Actions</h3>${scene.actions.length ? `<ul>${scene.actions.map(action => `
      <li>${esc(action.rule || [action.target, action.characteristic, action.value].filter(Boolean).join(' / '))}
      ${action.room ? `<span class="badge">${esc(action.room)}</span>` : ''}</li>
    `).join('')}</ul>` : '<div class="empty">No actions exported</div>'}</div>
  `;
}
function renderLayout() {
  els.main.classList.add('single');
  document.querySelector('.sidebar').style.display = 'none';
  const layout = data.layout || {};
  const q = els.search.value.trim().toLowerCase();
  const zones = (layout.zones || []).map(zone => {
    if (!q) return zone;
    const zoneNameMatches = zone.name.toLowerCase().includes(q);
    const rooms = (zone.rooms || [])
      .map(room => zoneNameMatches ? room : filterRoom(room, q))
      .filter(room => zoneNameMatches || room.name.toLowerCase().includes(q) || (room.accessories || []).length);
    return {...zone, rooms};
  }).filter(zone => !q || zone.name.toLowerCase().includes(q) || (zone.rooms || []).length);
  const roomsWithoutZone = (layout.roomsWithoutZone || [])
    .map(room => filterRoom(room, q))
    .filter(room => !q || room.name.toLowerCase().includes(q) || (room.accessories || []).length);
  els.detail.innerHTML = `
    <h2>Home Layout</h2>
    <div class="detail-top">
      <span class="badge">${layout.stats?.zones ?? 0} zones</span>
      <span class="badge">${layout.stats?.rooms ?? 0} rooms</span>
      <span class="badge">${layout.stats?.accessories ?? 0} accessories</span>
      <span class="badge">${layout.stats?.namedServices ?? 0} named services</span>
    </div>
    ${zones.map(zone => `
      <div class="section card">
        <h3>${esc(zone.name)}</h3>
        <div class="grid">${(zone.rooms || []).map(room => roomCard(room)).join('')}</div>
      </div>
    `).join('') || '<div class="card empty">No matching rooms or accessories.</div>'}
    ${roomsWithoutZone.length ? `<div class="section card"><h3>Rooms Without Zone</h3><div class="grid">${roomsWithoutZone.map(room => roomCard(room)).join('')}</div></div>` : ''}
    <div class="footer-note">Automation markers are decoded references matched by unique accessory or service name. Ambiguous duplicate names are not marked.</div>
  `;
  bindAutomationUsageLinks();
}
function renderManufacturers() {
  els.main.classList.add('single');
  document.querySelector('.sidebar').style.display = 'none';
  const q = els.search.value.trim().toLowerCase();
  const groups = manufacturers().map(group => ({
    ...group,
    accessories: group.accessories.filter(accessory => !q || accessoryText(accessory).includes(q) || group.name.toLowerCase().includes(q)),
  })).filter(group => group.accessories.length);
  const totalAccessories = groups.reduce((sum, group) => sum + group.accessories.length, 0);
  els.detail.innerHTML = `
    <h2>Manufacturers</h2>
    <div class="detail-top">
      <span class="badge">${groups.length} manufacturers</span>
      <span class="badge">${totalAccessories} accessories</span>
    </div>
    ${groups.map(group => {
      const models = uniq(group.accessories.map(accessory => accessory.model));
      const rooms = uniq(group.accessories.map(accessory => accessory.room));
      return `<div class="section card">
        <h3>${esc(group.name)}</h3>
        <div class="detail-top">
          <span class="badge">${group.accessories.length} accessories</span>
          <span class="badge">${models.length} models</span>
          <span class="badge">${rooms.length} rooms</span>
        </div>
        <div class="grid">${group.accessories.map(accessory => `
          <div class="card">
            <h4>${esc(accessory.name)}</h4>
            <p><span class="badge">${esc(accessory.room)}</span> <span class="badge">${esc(accessory.zone)}</span></p>
            ${accessory.model ? `<p><b>Model:</b> ${esc(accessory.model)}</p>` : ''}
            ${accessory.services?.length ? `<p class="empty">${esc(accessory.services.map(service => service.name).slice(0, 8).join(', '))}${accessory.services.length > 8 ? '...' : ''}</p>` : ''}
          </div>
        `).join('')}</div>
      </div>`;
    }).join('') || '<div class="card empty">No matching manufacturers or accessories.</div>'}
  `;
}
function renderHubs() {
  els.main.classList.add('single');
  document.querySelector('.sidebar').style.display = 'none';
  const q = els.search.value.trim().toLowerCase();
  const infra = data.infrastructure || {};
  const homeHubs = (infra.homeHubs || []).filter(hub => !q || hubText(hub).includes(q));
  els.detail.innerHTML = `
    <h2>Hubs</h2>
    <div class="detail-top">
      <span class="badge">${infra.stats?.homeHubs ?? 0} home hubs</span>
      <span class="badge">${infra.stats?.reachableHomeHubs ?? 0} reachable</span>
    </div>
    <div class="grid">${homeHubs.map(hub => `
      <div class="card">
        <h4>${esc(hub.name)}</h4>
        <p>
          ${hub.primary ? '<span class="badge active">Primary</span>' : ''}
          <span class="badge ${hub.reachable ? 'active' : 'inactive'}">${hub.reachable ? 'Reachable' : 'Not reachable'}</span>
          ${hub.room ? `<span class="badge">${esc(hub.room)}</span>` : ''}
        </p>
        <p><b>Model:</b> ${esc([hub.manufacturer, hub.model].filter(Boolean).join(' ') || 'Unknown')}</p>
        ${hub.residentName ? `<p class="empty">Resident name: ${esc(hub.residentName)}</p>` : ''}
      </div>
    `).join('') || '<div class="card empty">No matching home hubs.</div>'}</div>
    <div class="footer-note">Home hubs are Apple resident devices that coordinate HomeKit automations and remote access.</div>
  `;
}
function renderBridges() {
  els.main.classList.add('single');
  document.querySelector('.sidebar').style.display = 'none';
  const q = els.search.value.trim().toLowerCase();
  const infra = data.infrastructure || {};
  const bridges = (infra.bridges || []).map(bridge => {
    if (!q || bridgeText(bridge).includes(q)) return bridge;
    return {
      ...bridge,
      accessories: (bridge.accessories || []).filter(accessory => [accessory.name, accessory.manufacturer, accessory.model, accessory.room].join(' ').toLowerCase().includes(q)),
    };
  }).filter(bridge => !q || bridgeText(bridge).includes(q) || bridge.accessories.length);
  els.detail.innerHTML = `
    <h2>Bridges</h2>
    <div class="detail-top">
      <span class="badge">${infra.stats?.bridges ?? 0} bridges</span>
      <span class="badge">${infra.stats?.bridgedAccessories ?? 0} bridged accessories</span>
    </div>
    ${bridges.map(bridge => `
      <div class="section card">
        <h3>${esc(bridge.name)}</h3>
        <div class="detail-top">
          <span class="badge">${bridge.accessories.length} accessories</span>
          ${bridge.room ? `<span class="badge">${esc(bridge.room)}</span>` : ''}
          <span class="badge">${esc([bridge.manufacturer, bridge.model].filter(Boolean).join(' ') || 'Unknown')}</span>
        </div>
        <div class="grid">${(bridge.accessories || []).map(accessory => `
          <div class="card">
            <h4>${esc(accessory.name)}</h4>
            <p>${accessory.room ? `<span class="badge">${esc(accessory.room)}</span>` : ''}</p>
            <p class="empty">${esc([accessory.manufacturer, accessory.model].filter(Boolean).join(' ') || 'Unknown')}</p>
          </div>
        `).join('')}</div>
      </div>
    `).join('') || '<div class="card empty">No matching bridges.</div>'}
    <div class="footer-note">Bridges are accessories that contribute other accessories into the HomeKit graph.</div>
  `;
}
function roomCard(room) {
  return `<div class="card">
    <h4>${esc(room.name)}</h4>
    ${(room.accessories || []).length ? `<ul>${room.accessories.map(accessory => `
      <li><b>${esc(accessory.name)}</b>${accessory.manufacturer || accessory.model ? ` <span class="badge">${esc([accessory.manufacturer, accessory.model].filter(Boolean).join(' '))}</span>` : ''}
      ${automationUsageBlock(accessory)}
      ${accessory.services?.length ? `<br><span class="empty">${esc(accessory.services.map(service => service.name).slice(0, 8).join(', '))}${accessory.services.length > 8 ? '...' : ''}</span>` : ''}</li>
    `).join('')}</ul>` : '<div class="empty">No accessories identified</div>'}
  </div>`;
}
function renderContextSources() {
  els.main.classList.add('single');
  document.querySelector('.sidebar').style.display = 'none';
  const q = els.search.value.trim().toLowerCase();
  const sources = (data.contextSources || []).filter(source => !q || contextText(source).includes(q));
  const relationCount = sources.reduce((sum, source) => sum + (source.relations || []).length, 0);
  const helperCount = sources.reduce((sum, source) => sum + (source.helpers || []).length, 0);
  const webhookCount = sources.reduce((sum, source) => sum + (source.webhooks || []).length, 0);
  els.detail.innerHTML = `
    <h2>Context Sources</h2>
    <div class="detail-top">
      <span class="badge">${sources.length} sources</span>
      <span class="badge">${relationCount} relations</span>
      <span class="badge">${helperCount} helpers</span>
      <span class="badge">${webhookCount} webhooks</span>
    </div>
    ${sources.map(source => `
      <div class="section card">
        <h3>${esc(source.sourceName || source.sourceType || 'Context source')}</h3>
        <div class="detail-top">
          <span class="badge">${esc(source.sourceType || 'unknown')}</span>
          ${(source.platforms || []).map(platform => `<span class="badge">${esc(platform)}</span>`).join('')}
        </div>
        ${(source.relations || []).length ? `
          <div class="section">
            <h4>Derived Relations</h4>
            <ul>${source.relations.map(item => `
              <li><b>${esc(item.source)}</b> -> ${esc(item.target)} <span class="badge">${esc(item.relation)}</span><br><span class="empty">${esc(item.evidence)}</span></li>
            `).join('')}</ul>
          </div>
        ` : ''}
        ${(source.zones || []).length ? `<div class="section"><h4>Security Zones</h4><p>${source.zones.map(zone => `<span class="badge">${esc(zone)}</span>`).join(' ')}</p></div>` : ''}
        ${(source.helpers || []).length ? `<div class="section"><h4>Helper Switches</h4><ul>${source.helpers.map(item => `<li>${esc(item.name)} <span class="badge">${esc(item.type)}</span></li>`).join('')}</ul></div>` : ''}
        ${(source.webhooks || []).length ? `<div class="section"><h4>Webhooks</h4><ul>${source.webhooks.map(item => `<li>${esc(item.name)} <span class="badge">${esc(item.type)}</span></li>`).join('')}</ul></div>` : ''}
      </div>
    `).join('') || '<div class="card empty">No context sources loaded.</div>'}
  `;
}
function renderConfig() {
  els.main.classList.add('single');
  document.querySelector('.sidebar').style.display = 'none';
  const q = els.search.value.trim().toLowerCase();
  const themes = themeConfig.themes || [];
  const assignedCount = data.rules.filter(rule => themeConfig.automationThemeOverrides?.[rule.name]).length;
  const visibleRules = data.rules
    .slice()
    .sort((a, b) => a.name.localeCompare(b.name, undefined, {sensitivity: 'base'}))
    .filter(rule => !q || textOf(rule).includes(q) || rule.name.toLowerCase().includes(q));
  const themeOptions = (selected) => [
    `<option value="">${UNASSIGNED_THEME}</option>`,
    ...themes.map(theme => `<option value="${esc(theme)}" ${theme === selected ? 'selected' : ''}>${esc(theme)}</option>`),
  ].join('');
  els.detail.innerHTML = `
    <h2>Theme Editor</h2>
    <div class="detail-top">
      <span class="badge">${assignedCount} assigned</span>
      <span class="badge">${data.rules.length - assignedCount} unassigned</span>
      <span class="badge">${themes.length} themes</span>
    </div>
    <p>This editor stores only your explicit theme assignments in the browser. HomeKit extraction and rule decoding remain separate.</p>
    <div class="section card">
      <h3>Automations</h3>
      ${visibleRules.length ? `<ul>${visibleRules.map(rule => {
        const selected = themeConfig.automationThemeOverrides?.[rule.name] || '';
        return `<li>
          <b>${esc(rule.name)}</b><br>
          <select class="theme-picker" data-rule-name="${esc(rule.name)}">${themeOptions(selected)}</select>
        </li>`;
      }).join('')}</ul>` : '<div class="empty">No matching automations.</div>'}
    </div>
    <div class="section card">
      <h3>Advanced JSON</h3>
      <textarea id="configText">${esc(JSON.stringify(themeConfig, null, 2))}</textarea>
    </div>
    <div class="actions-row">
      <button class="primary" id="saveConfig">Save locally</button>
      <button class="secondary" id="clearThemes">Clear theme assignments</button>
      <button class="secondary" id="resetConfig">Reset to generated defaults</button>
      <button class="secondary" id="exportConfig">Export JSON</button>
      <input class="secondary" id="importConfig" type="file" accept="application/json">
    </div>
    <div class="footer-note">For a public project, this is the private layer: exact automation names and theme assignments live here, not in extractor logic.</div>
  `;
  els.detail.querySelectorAll('.theme-picker').forEach(select => select.addEventListener('change', () => {
    themeConfig.automationThemeOverrides = themeConfig.automationThemeOverrides || {};
    if (select.value) themeConfig.automationThemeOverrides[select.dataset.ruleName] = select.value;
    else delete themeConfig.automationThemeOverrides[select.dataset.ruleName];
    document.getElementById('configText').value = JSON.stringify(themeConfig, null, 2);
    applyConfiguredThemes();
    fillSelect(els.theme, uniq(data.rules.map(rule => rule.displayTheme)));
  }));
  document.getElementById('saveConfig').addEventListener('click', () => {
    themeConfig = JSON.parse(document.getElementById('configText').value);
    localStorage.setItem(CONFIG_KEY, JSON.stringify(themeConfig));
    applyConfiguredThemes();
    fillSelect(els.theme, uniq(data.rules.map(rule => rule.displayTheme)));
    render();
  });
  document.getElementById('clearThemes').addEventListener('click', () => {
    themeConfig.automationThemeOverrides = {};
    localStorage.setItem(CONFIG_KEY, JSON.stringify(themeConfig));
    applyConfiguredThemes();
    fillSelect(els.theme, uniq(data.rules.map(rule => rule.displayTheme)));
    render();
  });
  document.getElementById('resetConfig').addEventListener('click', () => {
    themeConfig = structuredClone(data.themeConfig);
    localStorage.removeItem(CONFIG_KEY);
    applyConfiguredThemes();
    render();
  });
  document.getElementById('exportConfig').addEventListener('click', () => {
    const blob = new Blob([JSON.stringify(themeConfig, null, 2) + '\\n'], {type: 'application/json'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'homekit_theme_config.json';
    a.click();
    URL.revokeObjectURL(url);
  });
  document.getElementById('importConfig').addEventListener('change', async event => {
    const file = event.target.files?.[0];
    if (!file) return;
    themeConfig = JSON.parse(await file.text());
    localStorage.setItem(CONFIG_KEY, JSON.stringify(themeConfig));
    applyConfiguredThemes();
    render();
  });
}
function render() {
  const sidebar = document.querySelector('.sidebar');
  els.main.classList.toggle('single', currentTab === 'layout' || currentTab === 'hubs' || currentTab === 'bridges' || currentTab === 'context' || currentTab === 'manufacturers' || currentTab === 'config');
  sidebar.style.display = currentTab === 'layout' || currentTab === 'hubs' || currentTab === 'bridges' || currentTab === 'context' || currentTab === 'manufacturers' || currentTab === 'config' ? 'none' : '';
  els.search.style.display = '';
  els.automationFilters.classList.toggle('visible', currentTab === 'automations');
  els.automationFilters.classList.toggle('open', currentTab === 'automations' && filtersExpanded);
  els.filterToggle.classList.toggle('visible', currentTab === 'automations');
  els.filterToggle.setAttribute('aria-expanded', String(currentTab === 'automations' && filtersExpanded));
  const hasFilter = Boolean(els.status.value || els.theme.value || els.room.value || els.confidence.value || quickFilter);
  els.filterToggle.textContent = filtersExpanded ? 'Hide' : (hasFilter ? 'Filters *' : 'Filters');
  if (currentTab === 'layout') return renderLayout();
  if (currentTab === 'hubs') return renderHubs();
  if (currentTab === 'bridges') return renderBridges();
  if (currentTab === 'context') return renderContextSources();
  if (currentTab === 'manufacturers') return renderManufacturers();
  if (currentTab === 'config') return renderConfig();
  if (currentTab === 'scenes') {
    const scenes = filteredScenes();
    renderSceneList(scenes);
    renderSceneDetail(scenes.find(scene => scene.id === selectedSceneId));
    return;
  }
  const rules = filteredRules();
  renderList(rules);
  renderDetail(rules.find(rule => rule.id === selectedId));
}
init();
</script>
</body>
</html>
"""


def write_html(payload, path):
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    payload_json = (
        payload_json
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    rendered = HTML_TEMPLATE.replace("__PAYLOAD__", payload_json)
    path.write_text(rendered, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input_json", type=Path)
    parser.add_argument("--db", type=Path, default=Path.home() / "Library/HomeKit/core.sqlite")
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="Do not read a HomeKit SQLite database. Useful for synthetic examples.",
    )
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument(
        "--theme-config",
        type=Path,
        default=None,
        help="Optional private JSON with explicit automation-to-theme assignments.",
    )
    parser.add_argument(
        "--private-overrides",
        type=Path,
        default=None,
        help="Optional private JSON with household-specific corrections.",
    )
    parser.add_argument(
        "--homebridge-config",
        type=Path,
        default=None,
        help="Optional Homebridge config.json for structural enrichment.",
    )
    parser.add_argument(
        "--write-inferred-theme-config",
        type=Path,
        default=None,
        help="Write an editable private theme config seeded from current report heuristics.",
    )
    args = parser.parse_args()

    data = load_json(args.input_json)
    db_path = None if args.no_db else args.db
    homebridge_security = reports.load_homebridge_security(args.homebridge_config)
    rows = build_rows(
        data,
        db_path,
        reports.load_private_overrides(args.private_overrides),
        homebridge_security,
    )
    if args.write_inferred_theme_config:
        args.write_inferred_theme_config.parent.mkdir(parents=True, exist_ok=True)
        args.write_inferred_theme_config.write_text(
            json.dumps(inferred_theme_config(rows), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Wrote {args.write_inferred_theme_config}")
    context_sources = [homebridge_security] if homebridge_security else []
    payload = build_payload(data, rows, load_theme_config(args.theme_config), context_sources)
    payload["layout"] = load_home_layout(db_path, data)
    payload["infrastructure"] = load_infrastructure(db_path)
    scenes = load_scenes(db_path)
    attach_scene_references(payload, scenes)
    out_dir = args.output_dir or args.input_json.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "homekit_inspector_data.json"
    html_path = out_dir / "homekit_inspector.html"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_html(payload, html_path)
    print(f"Wrote {json_path}")
    print(f"Wrote {html_path}")


if __name__ == "__main__":
    main()

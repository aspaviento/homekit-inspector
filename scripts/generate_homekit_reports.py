#!/usr/bin/env python3
"""Generate readable HomeKit automation reports from homed_extract output."""

from __future__ import annotations

import argparse
import csv
import json
import re
import struct
from collections import Counter, defaultdict
from pathlib import Path

from generate_condition_diagnostics import (
    build_characteristic_ref_lookup,
    condition_summary,
)


THEME_SECURITY = "Security and alarm"
THEME_AWAY = "Away presence simulation"
THEME_BLINDS = "Blinds and awnings"
THEME_MOTION = "Motion"
THEME_ACCESS = "Access and doors"
THEME_BUTTONS = "Buttons and remotes"
THEME_CLIMATE = "Climate and humidity"
THEME_TV = "TV y multimedia"
THEME_TIME = "Time, sun and light level"
THEME_MODES = "Home modes and shutdown"
THEME_LIGHTING = "Lighting and scenes"
THEME_OTHER = "Other"

PRIMARY_ORDER = [
    THEME_SECURITY,
    THEME_BLINDS,
    THEME_MOTION,
    THEME_CLIMATE,
    THEME_TV,
    THEME_BUTTONS,
    THEME_ACCESS,
    THEME_TIME,
    THEME_MODES,
    THEME_LIGHTING,
    THEME_OTHER,
]

ACCESS_WORDS = (
    "door",
    "puerta",
    "gate",
    "verja",
    "garage",
    "balcony",
    "sectional",
    "se abre",
)
LIGHT_WORDS = (
    "light",
    "lamp",
    "led",
    "luz",
    "cocina mesa",
    "chimenea light",
    "shelly luz",
    "trio ",
    "camino piedra",
    "rampa",
    "escalera entrada",
)

SPATIAL_ZONE_ROOMS = {}

ROOM_TO_SPATIAL_ZONE = {
    room: zone
    for zone, rooms in SPATIAL_ZONE_ROOMS.items()
    for room in rooms
}

DEVICE_COVERAGE_ZONE_OVERRIDES = {}

ZONE_NAME_ALIASES = {}

MANUAL_RULE_OVERRIDES = {}

SECURITY_TARGET_VALUES = {
    "hex:08": "Home",
    "hex:3500000000": "Home",
    "hex:09": "Away",
    "hex:36000000000000f03f": "Away",
    "hex:0a": "Night",
    "hex:360000000000000040": "Night",
}

BOOL_TRUE_VALUES = {"hex:01", "hex:09", "hex:36000000000000f03f", True, 1, 1.0}
BOOL_FALSE_VALUES = {"hex:00", "hex:08", "hex:3500000000", False, 0, 0.0}

CONTACT_OPEN_VALUES = {"hex:09", "hex:36000000000000f03f", 1, 1.0}
CONTACT_CLOSED_VALUES = {"hex:01", "hex:08", "hex:3500000000", 0, 0.0}
PROGRAMMABLE_SWITCH_EVENTS = {
    "hex:08": "Single Press",
    "hex:09": "Double Press",
    "hex:0a": "Long Press",
    0: "Single Press",
    1: "Double Press",
    2: "Long Press",
}


def uniq(values):
    seen = set()
    out = []
    for value in values:
        if not value:
            continue
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def load_private_overrides(path):
    if not path:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Private overrides must be a JSON object: {path}")
    for key in ("deviceCoverageZoneOverrides", "manualRuleOverrides"):
        if key in data and not isinstance(data[key], dict):
            raise ValueError(f"privateOverrides.{key} must be an object")
    return data


def configure_private_overrides(overrides):
    global DEVICE_COVERAGE_ZONE_OVERRIDES, MANUAL_RULE_OVERRIDES
    overrides = overrides or {}
    DEVICE_COVERAGE_ZONE_OVERRIDES = dict(overrides.get("deviceCoverageZoneOverrides") or {})
    MANUAL_RULE_OVERRIDES = dict(overrides.get("manualRuleOverrides") or {})


def action_target(action):
    return action.get("accessoryName") or action.get("serviceName")


def action_targets(action):
    targets = []
    if action.get("actionType") == "naturalLighting":
        for target in action.get("targets") or []:
            name = target.get("accessoryName") or target.get("serviceName") or target.get("providedName")
            if name:
                targets.append(name)
    else:
        target = action_target(action)
        if target:
            targets.append(target)
    return targets


def action_rooms(action):
    rooms = []
    if action.get("actionType") == "naturalLighting":
        rooms.extend(target.get("room") for target in action.get("targets") or [])
    else:
        rooms.append(action.get("room"))
    return rooms


def event_target(event):
    if event.get("characteristic") == "Programmable Switch Event":
        return event.get("serviceProvidedName") or event.get("service") or event.get("accessory")
    return event.get("service") or event.get("accessory")


def raw_value(item, key):
    return item.get(key) if key in item else item.get(key.replace("Raw", ""))


def bool_text(value):
    if value in BOOL_TRUE_VALUES:
        return "true"
    if value in BOOL_FALSE_VALUES:
        return "false"
    if value == "hex:02":
        return "special/toggle value 2"
    return str(value) if value is not None else "valor desconocido"


def decode_homekit_scalar(value, fmt=""):
    if not isinstance(value, str) or not value.startswith("hex:"):
        return value
    try:
        raw = bytes.fromhex(value[4:])
    except ValueError:
        return value
    if not raw:
        return value

    # homed stores some target values as the compact binary-plist object body:
    # 0x08/0x09 for bools, 0x30 + byte for small ints, and 0x35/0x36
    # followed by little-endian float/double values for numeric targets.
    if fmt == "bool":
        if value in BOOL_TRUE_VALUES:
            return True
        if value in BOOL_FALSE_VALUES:
            return False
        return value
    if len(raw) == 1 and fmt in {"int", "uint8", "uint16", "uint32", "uint64", "float"}:
        return raw[0]
    if len(raw) == 2 and raw[0] == 0x30:
        return raw[1]
    if len(raw) == 5 and raw[0] == 0x35:
        try:
            number = struct.unpack("<f", raw[1:])[0]
            return int(number) if number.is_integer() else round(number, 4)
        except struct.error:
            return value
    if len(raw) == 9 and raw[0] == 0x36:
        try:
            number = struct.unpack("<d", raw[1:])[0]
            return int(number) if number.is_integer() else round(number, 4)
        except struct.error:
            return value
    return value


def value_text(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value) if value is not None else "valor desconocido"


def action_value(action):
    return decode_homekit_scalar(raw_value(action, "targetValueRaw"), action.get("format") or "")


def event_rule(event):
    target = event_target(event)
    characteristic = event.get("characteristic") or ""
    service = event.get("service") or ""
    value = raw_value(event, "eventValueRaw")
    if event.get("eventType") == "calendar" and event.get("dateComponents"):
        components = event["dateComponents"]
        hour = components.get("hour")
        minute = components.get("minute")
        if hour is not None and minute is not None:
            return f"cada dia a las {int(hour):02d}:{int(minute):02d}"
        return "evento de calendario"
    if characteristic == "Motion Detected":
        return f"{target} detecta movimiento"
    if characteristic == "Occupancy Detected":
        return f"{target} detecta ocupacion"
    if characteristic == "Contact Sensor State":
        if value in CONTACT_OPEN_VALUES:
            return f"{target} se abre"
        if value in CONTACT_CLOSED_VALUES:
            return f"{target} se cierra"
        return f"{target} cambia estado de contacto"
    if characteristic == "Current Door State":
        return f"{target} cambia estado de puerta"
    if characteristic == "Programmable Switch Event":
        event_name = PROGRAMMABLE_SWITCH_EVENTS.get(value)
        if event_name:
            return f"{target} / Programmable Switch Event = {event_name}"
        return f"{target} / Programmable Switch Event cambia"
    if characteristic == "Power State":
        return f"{target} Power State == {bool_text(value)}"
    if characteristic:
        return f"{target} / {characteristic} cambia"
    return target or ""


def action_rule(action, action_set_name=""):
    target = action_target(action)
    characteristic = action.get("characteristic") or ""
    value = action_value(action)
    if action.get("actionType") == "mediaPlayback":
        state = action.get("state")
        if state == 1:
            return "reproducir audio/media"
        if state == 2:
            return "pausar/detener reproduccion multimedia"
        return f"accion multimedia state={state}" if state is not None else "accion multimedia"
    if action.get("actionType") == "naturalLighting":
        targets = [
            target.get("accessoryName") or target.get("serviceName") or target.get("providedName")
            for target in action.get("targets") or []
        ]
        targets = uniq(targets)
        if targets:
            return "Natural Lighting action: " + ", ".join(targets)
        return "accion de Natural Lighting"
    if action.get("actionType") != "characteristicWrite":
        return ""
    if not target and characteristic == "Security System Target State":
        target = "Security System"
    if not target:
        return ""
    if characteristic == "Security System Target State":
        raw = raw_value(action, "targetValueRaw")
        state = SECURITY_TARGET_VALUES.get(raw, str(value))
        return f"{target} / Security System Target State = {state}"
    if characteristic == "Power State":
        if action_set_name and target and target.startswith("Zona "):
            return f"activar {target}"
        return f"{target} / Power State = {bool_text(raw_value(action, 'targetValueRaw'))}"
    if characteristic == "Target Position":
        return f"{target} / Target Position = {value_text(value)}"
    if characteristic == "Brightness":
        return f"{target} / Brightness = {value_text(value)}"
    if characteristic:
        return f"{target} / {characteristic} = {value_text(value)}"
    return target or ""


def automatic_when_rules(events):
    return uniq(event_rule(event) for event in events)


def recompute_filtered_when_rules(row):
    allowed = set(row.get("eventDevices") or [])
    non_device_rules = automatic_when_rules(
        event
        for event in row["events"]
        if event.get("eventType") in {"calendar", "presence", "significantTime", "location"}
    )
    if not allowed:
        row["whenRules"] = non_device_rules
        return
    row["whenRules"] = uniq(non_device_rules + automatic_when_rules(
        event for event in row["events"] if event_target(event) in allowed
    ))


def automatic_then_rules(action_sets):
    rules = []
    for action_set in action_sets:
        set_name = action_set.get("name") or ""
        set_type = action_set.get("type") or ""
        if set_name and set_type == "HMActionSetTypeUserDefined":
            rules.append(f"activar escena {set_name}")
        for action in action_set.get("actions") or []:
            rule = action_rule(action, set_name)
            if rule:
                rules.append(rule)
    return uniq(rules)


def is_tv_media_device(device):
    low = (device or "").lower()
    return "tv" in low or "webos" in low


def canonical_zone_name(zone):
    return ZONE_NAME_ALIASES.get(zone, zone)


def spatial_zone_rooms_from_inventory(data):
    zones = {}
    for item in (data.get("inventory") or {}).get("zones") or []:
        name = canonical_zone_name(item.get("name") or "")
        rooms = [room for room in item.get("rooms") or [] if room]
        if name and rooms:
            zones[name] = rooms
    return zones or SPATIAL_ZONE_ROOMS


def refresh_spatial_zone_maps(data):
    global SPATIAL_ZONE_ROOMS, ROOM_TO_SPATIAL_ZONE
    SPATIAL_ZONE_ROOMS = spatial_zone_rooms_from_inventory(data)
    ROOM_TO_SPATIAL_ZONE = {
        room: zone
        for zone, rooms in SPATIAL_ZONE_ROOMS.items()
        for room in rooms
    }


def device_coverage_zone(device, room):
    return DEVICE_COVERAGE_ZONE_OVERRIDES.get(device) or ROOM_TO_SPATIAL_ZONE.get(room, "")


def filter_event_devices(name, event_devices):
    return event_devices


def apply_security_event_filters(rows, data):
    room_lookup = build_device_room_lookup(data, rows)
    pattern = re.compile(r"^Sensors Zone (.+?) (Away|Night)$", re.IGNORECASE)
    for row in rows:
        match = pattern.match(row["name"])
        if not match:
            continue
        zone = match.group(1)
        kept = []
        removed = []
        for device in row["eventDevices"]:
            room = room_lookup.get(device, "Sin habitacion")
            if device_coverage_zone(device, room) == zone:
                kept.append(device)
            else:
                removed.append(device)
        if removed:
            row["eventDevices"] = kept
            row["notes"].append(
                "eventos excluidos por no pertenecer a la zona espacial esperada: "
                + ", ".join(removed)
            )


def summarize(auto, index):
    name = auto.get("name") or f"Automation {index}"
    events = auto.get("events") or []
    action_sets = auto.get("actionSets") or []
    actions = [action for aset in action_sets for action in (aset.get("actions") or [])]

    raw_event_devices = uniq(event_target(event) for event in events)
    event_devices = filter_event_devices(name, raw_event_devices)
    action_devices = uniq(target for action in actions for target in action_targets(action))

    themes = classify(name, event_devices, action_devices)
    notes = review_notes(name, auto, event_devices, action_devices, raw_event_devices)

    return {
        "index": index,
        "name": name,
        "enabled": bool(auto.get("enabled")),
        "triggerType": auto.get("triggerType") or "",
        "events": events,
        "actionSets": action_sets,
        "actions": actions,
        "actionRooms": uniq(room for action in actions for room in action_rooms(action)),
        "eventDevices": event_devices,
        "rawEventDevices": raw_event_devices,
        "actionDevices": action_devices,
        "eventCount": len(events),
        "actionSetCount": len(action_sets),
        "actionCount": len(actions),
        "hasConditions": "evaluationCondition" in auto,
        "themes": themes,
        "primaryTheme": primary_theme(themes),
        "notes": notes,
        "conditionRules": [],
        "conditionConfidence": "",
        "whenRules": automatic_when_rules(events),
        "thenRules": automatic_then_rules(action_sets),
    }


def apply_manual_rule_overrides(row):
    override = MANUAL_RULE_OVERRIDES.get(row["name"])
    if not override:
        return
    for key in ("eventDevices", "actionDevices", "conditionRules", "whenRules", "thenRules"):
        if key in override:
            row[key] = list(override[key])
    if override.get("conditionRules"):
        row["hasConditions"] = True
        row["conditionConfidence"] = "manual"
    if override.get("notes"):
        row["notes"] = uniq([*row["notes"], *override["notes"]])


def classify(name, event_devices, action_devices):
    low = " ".join([name, *event_devices, *action_devices]).lower()
    low_name_events = " ".join([name, *event_devices]).lower()
    low_name_actions = " ".join([name, *action_devices]).lower()
    name_low = name.lower()
    action_low = " ".join(action_devices).lower()

    themes = []

    if (
        name_low.startswith(("security", "alarm"))
        or "pre alarm" in name_low
        or " alarm" in f" {name_low}"
        or "zona " in action_low
    ):
        themes.append(THEME_SECURITY)

    if name.lower().startswith("si away") or " away" in f" {name.lower()}":
        themes.extend([THEME_AWAY, THEME_SECURITY])

    if any(word in low for word in ("blind", "blinds", "shade", "shades", "awning", "awnings")):
        themes.append(THEME_BLINDS)

    if THEME_SECURITY not in themes and re.search(r"\bmotion\b", name_low):
        themes.append(THEME_MOTION)

    if any(word in low for word in ("humidity", "humidifier", "dehumidifier", "termo", "thermo")):
        themes.append(THEME_CLIMATE)

    if any(word in low_name_actions for word in ("tv", "movie", "multimedia", "power sensor")):
        themes.append(THEME_TV)

    if (
        any(word in name_low for word in ACCESS_WORDS)
        and "good night" not in name_low
        and "power sensor" not in name_low
    ):
        themes.append(THEME_ACCESS)

    if any(word in low for word in ("hue dimmer", "button", "single press", "interruptor", "pulsador", "switch single")):
        themes.append(THEME_BUTTONS)

    if any(word in low for word in ("sunrise", "sunset", "noon", "00h", "2am", "light level")) or re.search(r"\blux\b", low):
        themes.append(THEME_TIME)

    if any(word in name_low for word in ("modo", " mode ", "good night", "good morning", "apagar todo", "night mode")):
        themes.append(THEME_MODES)

    if any(word in low for word in LIGHT_WORDS) or any(word in name.lower() for word in ("light", "lamp")):
        themes.append(THEME_LIGHTING)

    return uniq(themes) or [THEME_OTHER]


def primary_theme(themes):
    for theme in PRIMARY_ORDER:
        if theme in themes:
            return theme
    return themes[0] if themes else THEME_OTHER


def review_notes(name, auto, event_devices, action_devices, raw_event_devices=None):
    raw_event_devices = raw_event_devices or event_devices
    notes = []
    if not event_devices:
        notes.append("sin dispositivo disparador exportado por homed_extract")
    if not action_devices and any((aset.get("actions") or []) == [] for aset in auto.get("actionSets") or []):
        notes.append("sin acciones exportadas en uno o mas action sets")
    removed = [device for device in raw_event_devices if is_tv_media_device(device) and device not in event_devices]
    if removed:
        notes.append(
            "eventos TV/media excluidos por confirmacion manual: probable artefacto de resolucion de CoreData"
        )
    return notes


def md_escape(value):
    return str(value).replace("\n", " ").strip()


def joined(values, empty="sin datos exportados"):
    return " | ".join(values) if values else empty


def plural(count, singular, plural_form):
    return singular if count == 1 else plural_form


def write_theme_csv(rows, path):
    fields = [
        "index",
        "name",
        "enabled",
        "themes",
        "primaryTheme",
        "eventDevices",
        "actionDevices",
        "actionRooms",
        "eventCount",
        "actionSetCount",
        "actionCount",
        "hasConditions",
        "notes",
        "conditionRules",
        "conditionConfidence",
        "whenRules",
        "thenRules",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "index": row["index"],
                    "name": row["name"],
                    "enabled": row["enabled"],
                    "themes": " | ".join(row["themes"]),
                    "primaryTheme": row["primaryTheme"],
                    "eventDevices": joined(row["eventDevices"], ""),
                    "actionDevices": joined(row["actionDevices"], ""),
                    "actionRooms": joined(row["actionRooms"], ""),
                    "eventCount": row["eventCount"],
                    "actionSetCount": row["actionSetCount"],
                    "actionCount": row["actionCount"],
                    "hasConditions": row["hasConditions"],
                    "notes": " | ".join(row["notes"]),
                    "conditionRules": " | ".join(row.get("conditionRules") or []),
                    "conditionConfidence": row.get("conditionConfidence") or "",
                    "whenRules": " | ".join(row.get("whenRules") or []),
                    "thenRules": " | ".join(row.get("thenRules") or []),
                }
            )


def load_homebridge_security(config_path):
    if not config_path:
        return {}
    data = json.loads(config_path.read_text(encoding="utf-8"))
    result = {
        "sourceType": "homebridge",
        "sourceName": "Homebridge config.json",
        "platforms": [],
        "relations": [],
        "zones": [],
        "helpers": [],
        "webhooks": [],
        "modeButtons": {},
    }
    for platform in data.get("platforms") or []:
        platform_name = platform.get("platform") or "Unknown"
        if platform_name and platform_name != "config":
            result["platforms"].append(platform_name)
        if platform_name == "AutomationSwitches":
            for switch in platform.get("switches") or []:
                name = switch.get("name")
                if switch.get("type") == "security":
                    result["securityName"] = name
                    result["zones"].extend(switch.get("zones") or [])
                    for key, mode in (
                        ("armStayButtonLabel", "Home"),
                        ("armAwayButtonLabel", "Away"),
                        ("armNightButtonLabel", "Night"),
                    ):
                        label = switch.get(key)
                        if label:
                            result["modeButtons"][label] = {
                                "securitySystem": name,
                                "mode": mode,
                            }
                            result["relations"].append(
                                {
                                    "source": label,
                                    "target": name,
                                    "relation": f"{mode} mode button",
                                    "evidence": "AutomationSwitches security switch labels",
                                }
                            )
                elif name:
                    result["helpers"].append({"name": name, "type": switch.get("type") or ""})
        if platform_name == "HttpWebHooks":
            for sensor in platform.get("sensors") or []:
                name = sensor.get("name")
                if name:
                    result["webhooks"].append({"name": name, "type": sensor.get("type") or ""})
    result["zones"] = uniq(result["zones"])
    result["platforms"] = uniq(result["platforms"])
    return result


def apply_homebridge_security_enrichment(rows, homebridge_security):
    mode_buttons = (homebridge_security or {}).get("modeButtons") or {}
    if not mode_buttons:
        return
    for row in rows:
        event_services = uniq(
            event.get("service") or event.get("serviceProvidedName")
            for event in row.get("events") or []
            if event.get("characteristic") == "Power State"
        )
        for service in event_services:
            mode = mode_buttons.get(service)
            if not mode:
                continue
            note = (
                f"Homebridge AutomationSwitches enrichment: {service} is "
                f"{mode['securitySystem']} {mode['mode']} mode button"
            )
            row["notes"] = uniq([*row["notes"], note])


def room_device_inventory(data, rows):
    inventory = data.get("inventory") or {}
    by_room = defaultdict(set)

    for accessory in inventory.get("accessories") or []:
        room = accessory.get("room") or "Sin habitacion"
        name = accessory.get("name")
        if name:
            by_room[room].add(name)

    if not by_room:
        for row in rows:
            for action in row["actions"]:
                room = action.get("room") or "Sin habitacion"
                name = action.get("accessoryName") or action.get("serviceName")
                if name:
                    by_room[room].add(name)
            for event in row["events"]:
                name = event.get("accessory") or event.get("service")
                if name:
                    by_room["Habitacion no exportada en eventos"].add(name)

    return {room: sorted(devices) for room, devices in sorted(by_room.items())}


def infer_security_zones(rows, room_lookup):
    zones = defaultdict(lambda: {"devices": set(), "rooms": set(), "deviceRooms": {}, "automations": []})
    pattern = re.compile(r"^Sensors Zone (.+?) (Away|Night)$", re.IGNORECASE)
    for row in rows:
        match = pattern.match(row["name"])
        if not match:
            continue
        zone = match.group(1)
        zones[zone]["automations"].append(row["name"])
        for device in row["eventDevices"]:
            zones[zone]["devices"].add(device)
            room = room_lookup.get(device)
            if room:
                zones[zone]["rooms"].add(room)
                zones[zone]["deviceRooms"][device] = room
    return {
        zone: {
            "rooms": sorted(info["rooms"]),
            "devices": sorted(info["devices"]),
            "deviceRooms": dict(sorted(info["deviceRooms"].items())),
            "automations": sorted(info["automations"]),
        }
        for zone, info in sorted(zones.items())
    }


def build_device_room_lookup(data, rows):
    lookup = {}
    accessory_rooms = {}
    for accessory in (data.get("inventory") or {}).get("accessories") or []:
        if accessory.get("name") and accessory.get("room"):
            accessory_rooms[accessory["name"]] = accessory["room"]
            lookup[accessory["name"]] = accessory["room"]
    for service in (data.get("inventory") or {}).get("services") or []:
        name = service.get("name")
        accessory = service.get("accessory")
        room = accessory_rooms.get(accessory)
        if name and room:
            lookup.setdefault(name, room)
    for row in rows:
        for action in row["actions"]:
            name = action.get("accessoryName") or action.get("serviceName")
            room = action.get("room")
            if name and room and name not in lookup:
                lookup[name] = room
    return lookup


def zone_spatial_discrepancies(inferred_zones):
    discrepancies = []
    for zone, info in inferred_zones.items():
        for device in info["devices"]:
            room = info.get("deviceRooms", {}).get(device)
            expected = device_coverage_zone(device, room)
            if expected and expected != zone:
                discrepancies.append(
                    {
                        "zone": zone,
                        "device": device,
                        "room": room,
                        "expected": expected,
                    }
                )
    return discrepancies


def write_theme_md(rows, data, homebridge_security, path):
    stats = data.get("stats") or {}
    counts = Counter(row["primaryTheme"] for row in rows)
    active_counts = Counter(row["primaryTheme"] for row in rows if row["enabled"])
    inactive_counts = Counter(row["primaryTheme"] for row in rows if not row["enabled"])
    by_primary_theme = defaultdict(list)
    for row in rows:
        by_primary_theme[row["primaryTheme"]].append(row)

    lines = [
        "# HomeKit Automations",
        "",
        f"Source automations: {len(rows)} ({sum(1 for row in rows if row['enabled'])} activas, {sum(1 for row in rows if not row['enabled'])} inactivas)",
        "",
        "## Lectura rapida",
        "- Este es el Markdown unico de automatizaciones: cada fila lleva prefijo `(Activo)` o `(Inactivo)`.",
        "- El CSV conserva las mismas filas en formato tabular para filtrar u ordenar.",
        "- `homekit_classification_review.md` lista los casos donde el extractor tiene baja confianza o hay correcciones manuales.",
        "",
        "## Correcciones y limites",
        "- Eve/HomeKit conditions are decoded from NSKeyedArchiver predicates where possible; `homekit_condition_diagnostics.md` keeps the detailed parse tree.",
        "- Some CoreData relations still resolve to low-confidence devices; see `homekit_classification_review.md` before treating device names as definitive evidence.",
        "",
        "## Conteo por tema principal",
    ]
    for theme in PRIMARY_ORDER:
        if counts[theme]:
            lines.append(f"- {theme}: {counts[theme]} total, {active_counts[theme]} activas, {inactive_counts[theme]} inactivas")

    device_room_lookup = build_device_room_lookup(data, rows)
    inferred_zones = infer_security_zones(rows, device_room_lookup)
    spatial_discrepancies = zone_spatial_discrepancies(inferred_zones)
    room_inventory = room_device_inventory(data, rows)

    lines.extend(["", "## Zonas de seguridad"])
    if homebridge_security.get("securityName"):
        lines.append(f"- Sistema Homebridge: `{homebridge_security['securityName']}`")
    if homebridge_security.get("zones"):
        lines.append(f"- Zonas configuradas en Homebridge: {', '.join(homebridge_security['zones'])}")
    if homebridge_security.get("helpers"):
        helper_text = ", ".join(f"{item['name']} ({item['type']})" for item in homebridge_security["helpers"])
        lines.append(f"- Interruptores auxiliares: {helper_text}")
    if homebridge_security.get("webhooks"):
        webhook_text = ", ".join(f"{item['name']} ({item['type']})" for item in homebridge_security["webhooks"])
        lines.append(f"- Webhooks relevantes: {webhook_text}")
    if inferred_zones:
        lines.append("")
        lines.append("### Zones inferred from `Sensors Zone ...` automations")
        lines.append(
            "La lista distingue habitaciones que pertenecen a cada zona y habitaciones que tienen sensores participando en estas automatizaciones. "
            "Que una habitacion no aparezca como participante no implica inconsistencia: puede tener dispositivos no usados por la alarma zonal."
        )
        for zone, info in inferred_zones.items():
            expected = SPATIAL_ZONE_ROOMS.get(zone, [])
            participant_rooms = info["rooms"]
            non_participant_rooms = [room for room in expected if room not in participant_rooms]
            expected_rooms = ", ".join(expected) or "sin mapa espacial"
            participant_room_text = ", ".join(participant_rooms) if participant_rooms else "ninguna"
            non_participant_room_text = ", ".join(non_participant_rooms) if non_participant_rooms else "ninguna"
            device_count = len(info["devices"])
            lines.append(f"- {zone}: {device_count} {plural(device_count, 'dispositivo', 'dispositivos')}")
            lines.append(f"  - Habitaciones de la zona: {expected_rooms}")
            lines.append(f"  - Habitaciones con sensores participantes: {participant_room_text}")
            lines.append(f"  - Habitaciones de la zona sin sensores participantes aqui: {non_participant_room_text}")
            lines.append(f"  - Dispositivos: {', '.join(info['devices'])}")
            lines.append(f"  - Automatizaciones: {', '.join(info['automations'])}")
    else:
        lines.append("- No zones could be inferred from `Sensors Zone ...` automations.")

    lines.extend(["", "### Mapa espacial manual"])
    map_source = "HomeKit" if (data.get("inventory") or {}).get("zones") else "manual"
    lines[-1] = f"### Mapa espacial usado ({map_source})"
    for zone, rooms in SPATIAL_ZONE_ROOMS.items():
        lines.append(f"- {zone}: {', '.join(rooms)}")

    if spatial_discrepancies:
        lines.extend(["", "### Discrepancias espaciales detectadas"])
        lines.append("| Zona alarma | Dispositivo | Habitacion | Zona espacial esperada |")
        lines.append("|---|---|---|---|")
        for item in spatial_discrepancies:
            lines.append(f"| {item['zone']} | {item['device']} | {item['room']} | {item['expected']} |")

    lines.extend(["", "## Inventario por habitacion"])
    if not (data.get("inventory") or {}).get("accessories"):
        lines.append(
            "- Inventario parcial: el JSON actual no incluye el bloque `inventory`; se listan solo dispositivos que aparecen en eventos o acciones exportadas."
        )
    for room, devices in room_inventory.items():
        device_count = len(devices)
        lines.append(f"- {room}: {device_count} {plural(device_count, 'dispositivo', 'dispositivos')}")
        lines.append(f"  - {', '.join(devices)}")

    lines.extend(["", "## Automatizaciones por tema principal"])
    for theme in PRIMARY_ORDER:
        theme_rows = by_primary_theme.get(theme, [])
        if not theme_rows:
            continue
        lines.extend(["", f"### {theme}"])
        for row in theme_rows:
            status = "Activo" if row["enabled"] else "Inactivo"
            lines.append(f"- ({status}) {md_escape(row['name'])}")
            detail = []
            secondary = [item for item in row["themes"] if item != row["primaryTheme"]]
            if secondary:
                detail.append(f"temas secundarios: {' | '.join(secondary)}")
            if row["eventDevices"]:
                event_label = "events exportados (baja confianza)" if any(
                    "eventos exportados de baja confianza" in note
                    for note in row["notes"]
                ) else "events"
                detail.append(f"{event_label}: {joined(row['eventDevices'])}")
            if row["actionDevices"]:
                detail.append(f"actions: {joined(row['actionDevices'])}")
            if row["hasConditions"]:
                if row.get("conditionRules"):
                    detail.append("conditions decoded")
                else:
                    detail.append("conditions")
            if detail:
                lines.append(f"  - {'; '.join(detail)}")
            if row.get("whenRules"):
                lines.append("  - Cuando:")
                for rule in row["whenRules"]:
                    lines.append(f"    - {rule}")
            if row.get("conditionRules"):
                lines.append("  - Si:")
                for rule in row["conditionRules"]:
                    lines.append(f"    - {rule}")
                if row.get("conditionConfidence"):
                    lines.append(f"  - confianza condiciones: {row['conditionConfidence']}")
            if row.get("thenRules"):
                lines.append("  - Entonces:")
                for rule in row["thenRules"]:
                    lines.append(f"    - {rule}")
            if row["notes"]:
                lines.append(f"  - notes: {'; '.join(row['notes'])}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_status_md(rows, path, enabled):
    selected = [row for row in rows if row["enabled"] is enabled]
    grouped = defaultdict(list)
    for row in selected:
        grouped[row["primaryTheme"]].append(row)

    status = "Active" if enabled else "Inactive"
    lines = [
        f"# HomeKit Automations - {status}",
        "",
        f"Total: {len(selected)}",
        "",
        "Note: local corrections should live in a private overrides file, not in this report generator.",
        "",
    ]
    for theme in PRIMARY_ORDER:
        theme_rows = grouped.get(theme, [])
        if not theme_rows:
            continue
        lines.append(f"## {theme}")
        lines.append("")
        for row in theme_rows:
            lines.append(f"- {md_escape(row['name'])} ({row['eventCount']} eventos, {row['actionCount']} acciones)")
            lines.append(f"  - Disparan: {joined(row['eventDevices'])}")
            lines.append(f"  - Actuan sobre: {joined(row['actionDevices'])}")
            lines.append(f"  - Temas: {' | '.join(row['themes'])}")
            if row["notes"]:
                lines.append(f"  - Notas: {'; '.join(row['notes'])}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_review_md(rows, path):
    suspicious = [row for row in rows if row["notes"]]
    lines = [
        "# HomeKit Automation Classification Review",
        "",
        "This file lists rows where the thematic classification or extracted devices needed manual attention.",
        "",
        f"Rows reviewed: {len(rows)}",
        f"Rows with notes: {len(suspicious)}",
        "",
        "## Items",
    ]
    for row in suspicious:
        lines.extend(
            [
                "",
                f"### {row['index']}. {md_escape(row['name'])}",
                f"- Status: {'active' if row['enabled'] else 'inactive'}",
                f"- Primary theme: {row['primaryTheme']}",
                f"- Themes: {' | '.join(row['themes'])}",
                f"- Events: {joined(row['eventDevices'])}",
                f"- Actions: {joined(row['actionDevices'])}",
                f"- Notes: {'; '.join(row['notes'])}",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input_json", type=Path)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--homebridge-config", type=Path, default=None)
    parser.add_argument("--db", type=Path, default=Path.home() / "Library/HomeKit/core.sqlite")
    parser.add_argument(
        "--private-overrides",
        type=Path,
        default=None,
        help="Optional private JSON with household-specific corrections.",
    )
    args = parser.parse_args()

    data = json.loads(args.input_json.read_text(encoding="utf-8"))
    refresh_spatial_zone_maps(data)
    configure_private_overrides(load_private_overrides(args.private_overrides))
    out_dir = args.output_dir or args.input_json.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = [summarize(auto, index) for index, auto in enumerate(data.get("automations") or [], start=1)]
    char_refs = build_characteristic_ref_lookup(args.db if args.db else None)
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
    homebridge_security = load_homebridge_security(args.homebridge_config)
    apply_security_event_filters(rows, data)
    for row in rows:
        recompute_filtered_when_rules(row)
    for row in rows:
        apply_manual_rule_overrides(row)
    apply_homebridge_security_enrichment(rows, homebridge_security)

    write_theme_csv(rows, out_dir / "homekit_automations_by_theme.csv")
    write_theme_md(rows, data, homebridge_security, out_dir / "homekit_automations.md")
    write_review_md(rows, out_dir / "homekit_classification_review.md")

    print(f"Wrote reports for {len(rows)} automations to {out_dir}")


if __name__ == "__main__":
    main()

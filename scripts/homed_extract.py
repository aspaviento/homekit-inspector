#!/usr/bin/env python3
"""
homed_extract.py -- Extract HomeKit automation data from Apple's homed daemon database.

Reads ~/Library/HomeKit/core.sqlite (requires Full Disk Access) and outputs
a comprehensive JSON file with all automations including decoded Shortcuts
workflows, protobuf action sets, and NSKeyedArchiver target values.

This script uses ONLY Python stdlib -- no third-party packages required.

Prerequisites:
    - macOS with HomeKit configured
    - Full Disk Access granted to Terminal
      (System Settings > Privacy & Security > Full Disk Access)
    - Python 3.9+

Usage:
    python3 homed_extract.py                         # defaults: ~/Library/HomeKit/core.sqlite -> homekit_homed_export.json
    python3 homed_extract.py -o export.json          # custom output path
    python3 homed_extract.py --db /path/to/core.sqlite
    python3 homed_extract.py --verbose               # print per-automation details to stderr

Technical notes (see also the handoff document):
    - The homed database is a CoreData SQLite store.  All table and column
      names follow CoreData conventions (Z_PK, Z_ENT, ZMKF* prefix, etc.).
    - CharacteristicWriteAction rows use ZACCESSORY1 (not ZACCESSORY) for the
      device foreign key.  ZACCESSORY is always NULL -- likely a CoreData
      model versioning artifact.
    - The trigger-to-action-set relationship goes through the junction table
      Z_41TRIGGERS_ with columns Z_41ACTIONSETS_ and Z_135TRIGGERS_.  The
      numbers come from CoreData's internal relationship IDs.
    - ShortcutAction ZDATA blobs are binary plists containing WFWorkflowActions.
    - Within homeaccessory workflow steps, HMActionSetSerializedData is
      protobuf-encoded (NOT plist).  We decode it with a minimal varint-based
      parser -- no protobuf library needed.
    - Target values inside protobuf write actions are NSKeyedArchiver binary
      plists.  We scan for the bplist00 magic bytes and extract from $objects.
    - UUIDs inside protobuf payloads are an isolated namespace -- they do not
      reliably match ZMODELID or ZUNIQUEIDENTIFIER in homed tables.
    - All CoreData timestamps are seconds since 2001-01-01 00:00:00 UTC.
      Add 978307200 to convert to Unix epoch.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import plistlib
import sqlite3
import sys
import uuid as uuid_mod

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# CoreData epoch: 2001-01-01 00:00:00 UTC expressed as a Unix timestamp.
COREDATA_EPOCH_OFFSET = 978307200

# Z_ENT values from the Z_PRIMARYKEY table.
ENT_CHARACTERISTIC_WRITE = 36
ENT_MATTER_COMMAND = 37
ENT_MEDIA_PLAYBACK = 38
ENT_NATURAL_LIGHTING = 39
ENT_SHORTCUT_ACTION = 40

ENT_CALENDAR_EVENT = 88
ENT_CHAR_RANGE_EVENT = 90
ENT_CHAR_VALUE_EVENT = 91
ENT_DURATION_EVENT = 92
ENT_LOCATION_EVENT = 93
ENT_PRESENCE_EVENT = 96
ENT_SIGNIFICANT_TIME_EVENT = 97

ENT_EVENT_TRIGGER = 136
ENT_TIMER_TRIGGER = 137

# Friendly names for entity types.
ACTION_ENT_NAMES = {
    ENT_CHARACTERISTIC_WRITE: "characteristicWrite",
    ENT_MATTER_COMMAND: "matterCommand",
    ENT_MEDIA_PLAYBACK: "mediaPlayback",
    ENT_NATURAL_LIGHTING: "naturalLighting",
    ENT_SHORTCUT_ACTION: "shortcut",
}

EVENT_ENT_NAMES = {
    ENT_CALENDAR_EVENT: "calendar",
    ENT_CHAR_RANGE_EVENT: "charRange",
    ENT_CHAR_VALUE_EVENT: "charValue",
    ENT_DURATION_EVENT: "duration",
    ENT_LOCATION_EVENT: "location",
    ENT_PRESENCE_EVENT: "presence",
    ENT_SIGNIFICANT_TIME_EVENT: "significantTime",
}

TRIGGER_ENT_NAMES = {
    ENT_EVENT_TRIGGER: "event",
    ENT_TIMER_TRIGGER: "timer",
}

# WFCondition lookup (Shortcuts conditional operators).
WF_CONDITIONS = {
    0: "equals",
    1: "notEquals",
    2: "greaterThan",
    3: "lessThan",
    4: "is",
    5: "isNot",
    100: "contains",
    101: "notContains",
    999: "hasAnyValue",
}

# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def make_serializable(obj, depth: int = 0):
    """Recursively convert any object to a JSON-serializable form.

    Handles bytes (try plist, then UTF-8, then 16-byte UUID, then hex),
    datetime, plistlib.UID, and nested containers.
    """
    if depth > 20:
        return str(obj)
    if obj is None:
        return None
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, (int, float)):
        return obj
    if isinstance(obj, str):
        return obj

    if isinstance(obj, bytes):
        # Try decoding as an embedded binary plist.
        try:
            inner = plistlib.loads(obj)
            return make_serializable(inner, depth + 1)
        except Exception:
            pass
        # Short HomeKit value blobs are binary scalars/protobuf fragments, not
        # text.  Keep them as hex so reports can decode them deterministically.
        if len(obj) <= 64:
            return f"hex:{obj.hex()}"
        # Try plain UTF-8 text.
        try:
            text = obj.decode("utf-8")
            if all(ch.isprintable() or ch in "\n\r\t" for ch in text):
                return text
        except Exception:
            pass
        # 16-byte blob -> UUID.
        if len(obj) == 16:
            try:
                return str(uuid_mod.UUID(bytes=obj))
            except Exception:
                pass
        return f"<{len(obj)} bytes>"

    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    if isinstance(obj, (list, tuple)):
        return [make_serializable(item, depth + 1) for item in obj]
    if isinstance(obj, dict):
        return {str(k): make_serializable(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, plistlib.UID):
        return f"UID({obj.data})"

    return str(obj)


def decode_date_components(blob: bytes | None) -> dict | None:
    """Decode an archived NSDateComponents blob used by calendar events."""
    if not blob:
        return None
    try:
        plist = plistlib.loads(blob)
    except Exception:
        return None
    objects = plist.get("$objects") if isinstance(plist, dict) else None
    if not isinstance(objects, list):
        return None
    for item in objects:
        if not isinstance(item, dict):
            continue
        fields = {}
        for key, value in item.items():
            if not key.startswith("NS."):
                continue
            fields[key[3:]] = make_serializable(value)
        if fields:
            return fields
    return None


def decode_uuid_set(blob: bytes | None) -> list[str]:
    """Decode an archived NSSet of NSUUID values."""
    if not blob:
        return []
    try:
        plist = plistlib.loads(blob)
    except Exception:
        return []
    objects = plist.get("$objects") if isinstance(plist, dict) else None
    if not isinstance(objects, list):
        return []
    uuids = []
    for item in objects:
        if isinstance(item, dict) and isinstance(item.get("NS.uuidbytes"), bytes):
            try:
                uuids.append(str(uuid_mod.UUID(bytes=item["NS.uuidbytes"])))
            except Exception:
                pass
    return uuids


# ---------------------------------------------------------------------------
# Minimal protobuf decoder (varint / length-delimited / fixed-width)
# ---------------------------------------------------------------------------

def _decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    """Decode a protobuf variable-length integer starting at *offset*.

    Returns (value, new_offset).
    """
    result = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        result |= (byte & 0x7F) << shift
        offset += 1
        if not (byte & 0x80):
            break
        shift += 7
    return result, offset


def parse_protobuf(data: bytes, depth: int = 0) -> dict[int, list[tuple[str, object]]]:
    """Parse raw protobuf bytes into {field_number: [(wire_type_name, value), ...]}.

    Only recurses up to *depth* 5 to avoid runaway parsing on corrupt data.
    """
    if depth > 5:
        return {}
    fields: dict[int, list[tuple[str, object]]] = {}
    i = 0
    while i < len(data):
        try:
            tag, i = _decode_varint(data, i)
        except Exception:
            break
        field_num = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:  # varint
            val, i = _decode_varint(data, i)
            fields.setdefault(field_num, []).append(("varint", val))
        elif wire_type == 2:  # length-delimited
            length, i = _decode_varint(data, i)
            if i + length > len(data):
                break
            val = data[i : i + length]
            fields.setdefault(field_num, []).append(("bytes", val))
            i += length
        elif wire_type == 1:  # fixed64
            if i + 8 > len(data):
                break
            fields.setdefault(field_num, []).append(("fixed64", data[i : i + 8]))
            i += 8
        elif wire_type == 5:  # fixed32
            if i + 4 > len(data):
                break
            fields.setdefault(field_num, []).append(("fixed32", data[i : i + 4]))
            i += 4
        else:
            break
    return fields


# ---------------------------------------------------------------------------
# NSKeyedArchiver target-value extractor
# ---------------------------------------------------------------------------

def extract_target_value_from_nsarchiver(data: bytes):
    """Extract the actual value from an NSKeyedArchiver-encoded binary plist.

    Strategy: scan for the ``bplist00`` magic bytes, parse from that offset,
    and look in the ``$objects`` array for non-``$null``, non-dict values.
    These are the concrete target values (booleans, numbers, strings).
    """
    bp_idx = data.find(b"bplist")
    if bp_idx < 0:
        return None
    try:
        plist = plistlib.loads(data[bp_idx:])
        if isinstance(plist, dict) and "$objects" in plist:
            for obj in plist["$objects"]:
                if obj != "$null" and not isinstance(obj, dict):
                    return make_serializable(obj)
        return make_serializable(plist)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# HMActionSetSerializedData protobuf decoder
# ---------------------------------------------------------------------------

def decode_action_set_proto(proto_data: bytes) -> dict:
    """Decode an HMActionSetSerializedData protobuf into readable actions.

    Protobuf layout:
        Field 1 (bytes/string): action-set UUID
        Field 2 (bytes, repeated): individual write actions
            Sub-field 1 (varint): enabled flag
            Sub-field 2 (bytes): target-value data (NSKeyedArchiver bplist
                                 + characteristic reference UUID)
            Sub-field 3 (bytes): service/characteristic/accessory reference
                                 chain (nested 16-byte UUIDs)
        Field 4 (bytes, 16B): scene model UUID
        Field 5 (bytes, 16B): home model UUID
    """
    fields = parse_protobuf(proto_data)
    result: dict = {}

    # -- Field 1: action-set UUID string ---------------------------------
    if 1 in fields:
        for wt, val in fields[1]:
            if wt == "bytes":
                try:
                    result["actionSetUUID"] = val.decode("ascii")
                except Exception:
                    pass

    # -- Field 2: individual write actions (repeated) --------------------
    write_actions: list[dict] = []
    if 2 in fields:
        for wt, val in fields[2]:
            if wt != "bytes":
                continue
            action_fields = parse_protobuf(val)
            wa: dict = {}

            # Enabled flag
            if 1 in action_fields:
                for awt, aval in action_fields[1]:
                    if awt == "varint":
                        wa["enabled"] = bool(aval)

            # Target value data (NSKeyedArchiver + characteristic ref)
            if 2 in action_fields:
                for awt, aval in action_fields[2]:
                    if awt == "bytes":
                        tv = extract_target_value_from_nsarchiver(aval)
                        if tv is not None:
                            wa["targetValue"] = tv
                        # Characteristic reference UUID inside nested proto
                        inner = parse_protobuf(aval)
                        if 1 in inner:
                            for iwt, ival in inner[1]:
                                if iwt == "bytes" and len(ival) == 16:
                                    wa["characteristicModelID"] = str(
                                        uuid_mod.UUID(bytes=ival)
                                    )

            # Service / characteristic / accessory reference chain
            if 3 in action_fields:
                for awt, aval in action_fields[3]:
                    if awt == "bytes":
                        ref = parse_protobuf(aval)
                        chain: list[str] = []
                        if 1 in ref:
                            for rwt, rval in ref[1]:
                                if rwt == "bytes":
                                    inner2 = parse_protobuf(rval)
                                    for _fn2, fvals2 in inner2.items():
                                        for rwt2, rval2 in fvals2:
                                            if rwt2 == "bytes" and len(rval2) == 16:
                                                chain.append(
                                                    str(uuid_mod.UUID(bytes=rval2))
                                                )
                        if 2 in ref:
                            for rwt, rval in ref[2]:
                                if rwt == "bytes" and len(rval) == 16:
                                    chain.append(str(uuid_mod.UUID(bytes=rval)))
                        if chain:
                            wa["referenceChain"] = chain

            if wa:
                write_actions.append(wa)

    if write_actions:
        result["writeActions"] = write_actions

    # -- Fields 4/5: scene and home model UUIDs --------------------------
    for fn, label in [(4, "sceneModelID"), (5, "homeModelID")]:
        if fn in fields:
            for wt, val in fields[fn]:
                if wt == "bytes" and len(val) == 16:
                    result[label] = str(uuid_mod.UUID(bytes=val))

    return result


# ---------------------------------------------------------------------------
# Shortcuts workflow decoder
# ---------------------------------------------------------------------------

def decode_shortcut_workflow(data: bytes) -> list[dict]:
    """Decode a ShortcutAction ZDATA blob into structured workflow steps.

    ZDATA is a binary plist containing ``WFWorkflowActions`` -- the same
    format used by the Shortcuts app.  Known action identifiers:

        is.workflow.actions.conditional
            WFControlFlowMode: 0 = if, 1 = else, 2 = end
        is.workflow.actions.homeaccessory
            Contains HMActionSetSerializedData protobuf blobs.
        is.workflow.actions.choosefrommenu
            WFControlFlowMode: 0 = start, 1 = case, 2 = end
    """
    parsed = plistlib.loads(data)
    wf_actions = parsed.get("WFWorkflowActions", [])

    steps: list[dict] = []
    for step in wf_actions:
        ident = step.get("WFWorkflowActionIdentifier", "").replace(
            "is.workflow.actions.", ""
        )
        params = step.get("WFWorkflowActionParameters", {})
        s: dict = {"type": ident}

        if ident == "conditional":
            mode = params.get("WFControlFlowMode", -1)
            s["mode"] = {0: "if", 1: "else", 2: "end"}.get(mode, f"mode_{mode}")
            if mode == 0:
                cond = params.get("WFCondition", "")
                s["condition"] = WF_CONDITIONS.get(cond, f"cond_{cond}")
                wf_input = params.get("WFInput")
                s["inputType"] = (
                    wf_input.get("Type", "") if isinstance(wf_input, dict) else ""
                )
                if "WFConditionalActionString" in params:
                    s["conditionValue"] = str(params["WFConditionalActionString"])
                if "WFNumberValue" in params:
                    s["conditionValue"] = make_serializable(params["WFNumberValue"])

        elif ident == "homeaccessory":
            asets = params.get("WFHomeTriggerActionSets", {})
            if isinstance(asets, dict):
                state = asets.get(
                    "WFHFTriggerActionSetsBuilderParameterStateActionSets", []
                )
                decoded_asets: list[dict] = []
                if isinstance(state, list):
                    for entry in state:
                        if isinstance(entry, dict):
                            proto = entry.get("HMActionSetSerializedData")
                            if proto and isinstance(proto, bytes):
                                decoded_asets.append(decode_action_set_proto(proto))
                s["actionSets"] = decoded_asets

        elif ident == "choosefrommenu":
            mode = params.get("WFControlFlowMode", -1)
            s["mode"] = {0: "start", 1: "case", 2: "end"}.get(mode, f"mode_{mode}")
            if "WFMenuItems" in params:
                s["menuItems"] = make_serializable(params["WFMenuItems"])
            if "WFMenuItemTitle" in params:
                s["menuItemTitle"] = make_serializable(params["WFMenuItemTitle"])

        steps.append(s)
    return steps


# ---------------------------------------------------------------------------
# Lookup-table builders
# ---------------------------------------------------------------------------

def _build_room_lookup(cur: sqlite3.Cursor, home_id: int | None) -> dict[int, str]:
    """Z_PK -> room name."""
    if home_id is None:
        cur.execute("SELECT Z_PK, ZNAME FROM ZMKFROOM")
    else:
        cur.execute("SELECT Z_PK, ZNAME FROM ZMKFROOM WHERE ZHOME = ?", (home_id,))
    return {row[0]: row[1] for row in cur.fetchall()}


def _build_zone_lookup(
    cur: sqlite3.Cursor, rooms: dict[int, str], home_id: int | None
) -> dict[str, list[str]]:
    """Return HomeKit zone name -> sorted room names.

    CoreData relationship table names can change between schema versions, so
    detect a bridge table with one ROOM column and one ZONE column.
    """
    cur.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name LIKE 'Z\\_%ZONES%' ESCAPE '\\'"
    )
    candidates = []
    for (table_name,) in cur.fetchall():
        cur.execute(f"PRAGMA table_info({_quote_identifier(table_name)})")
        columns = [row[1] for row in cur.fetchall()]
        room_cols = [column for column in columns if "ROOM" in column.upper()]
        zone_cols = [column for column in columns if "ZONE" in column.upper()]
        if room_cols and zone_cols:
            candidates.append((table_name, room_cols[0], zone_cols[0]))

    zones: dict[str, set[str]] = {}
    for table_name, room_col, zone_col in candidates:
        query = (
            "SELECT z.ZNAME, zr."
            f"{_quote_identifier(room_col)} "
            f"FROM {_quote_identifier(table_name)} zr "
            "JOIN ZMKFZONE z ON z.Z_PK = zr."
            f"{_quote_identifier(zone_col)} "
        )
        parameters = ()
        if home_id is not None:
            query += "WHERE z.ZHOME = ? "
            parameters = (home_id,)
        query += "ORDER BY z.ZNAME"
        cur.execute(query, parameters)
        found = False
        candidate_zones: dict[str, set[str]] = {}
        for zone_name, room_pk in cur.fetchall():
            room_name = rooms.get(room_pk)
            if zone_name and room_name:
                found = True
                candidate_zones.setdefault(zone_name, set()).add(room_name)
        if found:
            zones = candidate_zones
            break

    return {zone: sorted(room_names) for zone, room_names in sorted(zones.items())}


def _build_accessory_lookup(
    cur: sqlite3.Cursor, rooms: dict[int, str], home_id: int | None
) -> dict[int, dict]:
    """Z_PK -> {name, providedName, manufacturer, model, uuid, room}."""
    lookup: dict[int, dict] = {}
    query = (
        "SELECT Z_PK, ZCONFIGUREDNAME, ZPROVIDEDNAME, ZMANUFACTURER, ZMODEL, "
        "ZUNIQUEIDENTIFIER, ZROOM FROM ZMKFACCESSORY"
    )
    parameters = ()
    if home_id is not None:
        query += " WHERE ZHOME = ?"
        parameters = (home_id,)
    cur.execute(query, parameters)
    for pk, name, provided_name, mfr, model, uid, room_pk in cur.fetchall():
        lookup[pk] = {
            "name": name,
            "providedName": provided_name,
            "manufacturer": mfr,
            "model": model,
            "uuid": uid,
            "room": rooms.get(room_pk, "") if room_pk else "",
        }
    return lookup


def _build_service_lookup(
    cur: sqlite3.Cursor, accessories: dict[int, dict]
) -> dict[int, dict]:
    """Z_PK -> {name, providedName, accessory}."""
    lookup: dict[int, dict] = {}
    cur.execute(
        "SELECT Z_PK, ZNAME, ZACCESSORY, ZEXPECTEDCONFIGUREDNAME, ZPROVIDEDNAME, "
        "ZMODELID "
        "FROM ZMKFSERVICE"
    )
    for pk, name, acc_pk, configured_name, provided_name, model_id in cur.fetchall():
        if acc_pk not in accessories:
            continue
        acc_info = accessories.get(acc_pk, {})
        lookup[pk] = {
            "name": configured_name or name,
            "providedName": provided_name,
            "accessory": acc_info.get("name") or acc_info.get("providedName", ""),
            "modelId": str(uuid_mod.UUID(bytes=model_id)) if model_id else "",
            "room": acc_info.get("room", ""),
        }
    return lookup


def _build_characteristic_lookup(
    cur: sqlite3.Cursor, services: dict[int, dict]
) -> tuple[dict[int, dict], dict[tuple[int, int], dict]]:
    """Return characteristic lookups by Z_PK and by (service Z_PK, instance id)."""
    lookup: dict[int, dict] = {}
    by_service_instance: dict[tuple[int, int], dict] = {}
    cur.execute(
        "SELECT Z_PK, ZTYPE, ZSERVICE, ZINSTANCEID, ZMANUFACTURERDESCRIPTION, ZFORMAT "
        "FROM ZMKFCHARACTERISTIC"
    )
    for pk, ztype, svc_pk, instance_id, desc, fmt in cur.fetchall():
        if svc_pk not in services:
            continue
        char_type = None
        if ztype:
            try:
                char_type = str(uuid_mod.UUID(bytes=ztype))
            except Exception:
                pass
        svc_info = services.get(svc_pk, {})
        lookup[pk] = {
            "type": char_type,
            "service": svc_info.get("name", ""),
            "accessory": svc_info.get("accessory", ""),
            "description": desc,
            "format": fmt,
            "instanceId": instance_id,
            "serviceId": svc_pk,
        }
        if svc_pk is not None and instance_id is not None:
            by_service_instance[(svc_pk, instance_id)] = lookup[pk]
    return lookup, by_service_instance


# ---------------------------------------------------------------------------
# Event extraction
# ---------------------------------------------------------------------------

def _extract_events(
    cur: sqlite3.Cursor,
    trigger_pk: int,
    char_info: dict[int, dict],
    char_by_service_instance: dict[tuple[int, int], dict],
    svc_names: dict[int, dict],
    event_ent_names: dict[int, str],
) -> list[dict]:
    """Return a list of decoded event dicts for a given trigger Z_PK."""
    cur.execute(
        "SELECT e.Z_ENT, e.ZSIGNIFICANTEVENT, e.ZPRESENCETYPE, e.ZOFFSETSECONDS, "
        "e.ZDURATION, e.ZEVENTVALUE, e.ZCHARACTERISTICID, e.ZSERVICE, "
        "e.ZFIREDATECOMPONENTS "
        "FROM ZMKFEVENT e WHERE e.ZTRIGGER = ?",
        (trigger_pk,),
    )
    events: list[dict] = []
    for ev in cur.fetchall():
        evt: dict = {
            "eventType": event_ent_names.get(ev[0], EVENT_ENT_NAMES.get(ev[0], f"ent_{ev[0]}"))
        }
        if ev[1]:
            evt["significantEvent"] = ev[1]
        if ev[2] is not None:
            evt["presenceType"] = ev[2]
        if ev[3] is not None:
            evt["offsetSeconds"] = ev[3]
        if ev[5]:
            try:
                evt["eventValue"] = make_serializable(plistlib.loads(ev[5]))
            except Exception:
                evt["eventValueRaw"] = make_serializable(ev[5])
        date_components = decode_date_components(ev[8])
        if date_components:
            evt["dateComponents"] = date_components
        if ev[6]:
            ci = (
                char_by_service_instance.get((ev[7], ev[6]), {})
                if ev[7]
                else {}
            )
            if not ci:
                ci = char_info.get(ev[6], {})
            evt["characteristic"] = ci.get("description") or ci.get("type", "")
            evt["accessory"] = ci.get("accessory", "")
            evt["service"] = ci.get("service", "")
            evt["format"] = ci.get("format", "")
        if ev[7]:
            si = svc_names.get(ev[7], {})
            if not evt.get("accessory"):
                evt["accessory"] = si.get("accessory", "")
            if si.get("providedName"):
                evt["serviceProvidedName"] = si["providedName"]
        events.append(evt)
    return events


# ---------------------------------------------------------------------------
# Action extraction
# ---------------------------------------------------------------------------

def _extract_actions(
    cur: sqlite3.Cursor,
    action_set_pk: int,
    char_info: dict[int, dict],
    char_by_service_instance: dict[tuple[int, int], dict],
    acc_names: dict[int, dict],
    svc_names: dict[int, dict],
    action_ent_names: dict[int, str],
    verbose: bool = False,
) -> list[dict]:
    """Return a list of decoded action dicts for a given action-set Z_PK.

    IMPORTANT: We query ZACCESSORY1 (not ZACCESSORY) because all
    CharacteristicWriteAction rows have NULL in ZACCESSORY but a valid
    foreign key in ZACCESSORY1.  This is a CoreData model versioning
    artifact.
    """
    cur.execute(
        "SELECT Z_PK, Z_ENT, ZDATA, ZTARGETVALUE, ZCHARACTERISTICID, "
        "ZACCESSORY1, ZSERVICE, ZSTATE, ZVOLUME, ZENCODEDPLAYBACKARCHIVE, "
        "ZSERVICEUUIDS "
        "FROM ZMKFACTION WHERE ZACTIONSET = ?",
        (action_set_pk,),
    )
    services_by_model_id = {
        info.get("modelId"): info
        for info in svc_names.values()
        if info.get("modelId")
    }
    actions: list[dict] = []
    for (
        act_pk,
        act_ent,
        act_data,
        act_target,
        act_char,
        act_acc,
        act_svc,
        act_state,
        act_volume,
        act_playback,
        act_service_uuids,
    ) in cur.fetchall():
        atype = action_ent_names.get(act_ent, ACTION_ENT_NAMES.get(act_ent, f"ent_{act_ent}"))
        ad: dict = {"actionType": atype}

        if atype == "shortcut" and act_data:
            try:
                ad["workflowSteps"] = decode_shortcut_workflow(act_data)
            except Exception as exc:
                ad["decodeError"] = str(exc)
                if verbose:
                    print(
                        f"  [!] Failed to decode ShortcutAction Z_PK={act_pk}: {exc}",
                        file=sys.stderr,
                    )

        elif atype == "mediaPlayback":
            if act_state is not None:
                ad["state"] = act_state
            if act_volume is not None:
                ad["volume"] = act_volume
            if act_playback:
                ad["encodedPlaybackArchive"] = make_serializable(act_playback)

        elif atype == "naturalLighting":
            if act_state is not None:
                ad["state"] = act_state
            service_uuids = decode_uuid_set(act_service_uuids)
            if service_uuids:
                ad["serviceUUIDs"] = service_uuids
                targets = []
                for service_uuid in service_uuids:
                    service_info = services_by_model_id.get(service_uuid)
                    if service_info:
                        targets.append(
                            {
                                "serviceName": service_info.get("name", ""),
                                "providedName": service_info.get("providedName", ""),
                                "accessoryName": service_info.get("accessory", ""),
                                "room": service_info.get("room", ""),
                            }
                        )
                if targets:
                    ad["targets"] = targets

        elif atype == "characteristicWrite":
            if act_target:
                try:
                    ad["targetValue"] = make_serializable(plistlib.loads(act_target))
                except Exception:
                    ad["targetValueRaw"] = make_serializable(act_target)
            if act_char:
                ci = (
                    char_by_service_instance.get((act_svc, act_char), {})
                    if act_svc
                    else {}
                )
                if not ci:
                    ci = char_info.get(act_char, {})
                ad["characteristic"] = ci.get("description") or ci.get("type", "")
                ad["format"] = ci.get("format", "")
            if act_acc:
                ai = acc_names.get(act_acc, {})
                ad["accessoryName"] = ai.get("name") or ai.get("providedName", "")
                ad["room"] = ai.get("room", "")
            if act_svc:
                si = svc_names.get(act_svc, {})
                ad["serviceName"] = (
                    si.get("name")
                    or si.get("providedName")
                    or si.get("accessory", "")
                )

        actions.append(ad)
    return actions


# ---------------------------------------------------------------------------
# CoreData schema helpers
# ---------------------------------------------------------------------------

def _quote_identifier(identifier: str) -> str:
    """Quote a SQLite identifier."""
    return '"' + identifier.replace('"', '""') + '"'


def _find_trigger_actionset_join(cur: sqlite3.Cursor) -> tuple[str, str, str]:
    """Return (table, action_set_column, trigger_column) for trigger/action sets.

    CoreData relationship column numbers change between HomeKit schema versions.
    The table/column names still include the relationship names, so detect them
    from PRAGMA metadata instead of hard-coding Z_135TRIGGERS_.
    """
    cur.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name LIKE 'Z\\_%TRIGGERS\\_%' ESCAPE '\\'"
    )
    for (table_name,) in cur.fetchall():
        cur.execute(f"PRAGMA table_info({_quote_identifier(table_name)})")
        columns = [row[1] for row in cur.fetchall()]
        action_cols = [c for c in columns if "ACTIONSET" in c.upper()]
        trigger_cols = [
            c
            for c in columns
            if "TRIGGER" in c.upper() and "ACTIONSET" not in c.upper()
        ]
        if action_cols and trigger_cols:
            return table_name, action_cols[0], trigger_cols[0]

    raise sqlite3.DatabaseError(
        "Could not find trigger/action-set junction table. "
        "Run: sqlite3 ~/Library/HomeKit/core.sqlite "
        "\"select name, sql from sqlite_master where name like 'Z_%TRIGGERS_%';\""
    )


def _simplify_entity_name(name: str) -> str:
    """Map CoreData entity class names to stable reader-facing type names."""
    lower = name.lower()
    if "characteristicwriteaction" in lower:
        return "characteristicWrite"
    if "mattercommandaction" in lower:
        return "matterCommand"
    if "mediaplaybackaction" in lower:
        return "mediaPlayback"
    if "naturallightingaction" in lower:
        return "naturalLighting"
    if "shortcutaction" in lower:
        return "shortcut"
    if "calendarevent" in lower:
        return "calendar"
    if "characteristicrangeevent" in lower:
        return "charRange"
    if "characteristicvalueevent" in lower:
        return "charValue"
    if "durationevent" in lower:
        return "duration"
    if "locationevent" in lower:
        return "location"
    if "presenceevent" in lower:
        return "presence"
    if "significanttimeevent" in lower:
        return "significantTime"
    if "eventtrigger" in lower:
        return "event"
    if "timertrigger" in lower:
        return "timer"
    return name


def _build_entity_type_lookup(cur: sqlite3.Cursor) -> dict[int, str]:
    """Return Z_ENT -> simplified entity type name from CoreData metadata."""
    cur.execute("SELECT Z_ENT, Z_NAME FROM Z_PRIMARYKEY")
    return {int(ent): _simplify_entity_name(name or "") for ent, name in cur.fetchall()}


def _extract_home_selection(cur: sqlite3.Cursor) -> dict:
    """Return the primary HomeKit home and the number of available homes."""
    try:
        cur.execute("SELECT Z_PK, ZNAME FROM ZMKFHOME ORDER BY Z_PK")
        homes = [{"id": row[0], "name": row[1]} for row in cur.fetchall()]
    except sqlite3.Error:
        homes = []

    primary_id = None
    try:
        cur.execute(
            "SELECT ZPRIMARYHOME FROM ZMKFHOMEMANAGER "
            "WHERE ZPRIMARYHOME IS NOT NULL ORDER BY Z_PK "
            "LIMIT 1"
        )
        row = cur.fetchone()
        primary_id = row[0] if row else None
    except sqlite3.Error:
        pass

    selected = next((home for home in homes if home["id"] == primary_id), None)
    selection = "primary"
    if selected is None and homes:
        selected = homes[0]
        selection = "first"
    return {
        "id": selected["id"] if selected else None,
        "name": selected["name"] if selected else None,
        "selection": selection if selected else "unavailable",
        "availableCount": len(homes),
    }


# ---------------------------------------------------------------------------
# Main extraction logic
# ---------------------------------------------------------------------------

def extract(db_path: str, verbose: bool = False) -> dict:
    """Open *db_path* read-only and return the full extraction dict."""

    # Verify the file exists before trying to open it.
    if not os.path.isfile(db_path):
        raise FileNotFoundError(
            f"Database not found: {db_path}\n"
            "Make sure HomeKit is configured on this Mac and the path is correct."
        )

    # Open in read-only mode so we can never accidentally mutate the store.
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError as exc:
        if "unable to open" in str(exc).lower():
            raise PermissionError(
                f"Cannot open database (permission denied): {db_path}\n"
                "Grant Full Disk Access to Terminal:\n"
                "  System Settings > Privacy & Security > Full Disk Access > + Terminal"
            ) from exc
        raise
    cur = conn.cursor()

    # -- Build lookup tables ------------------------------------------------
    if verbose:
        print("[*] Building lookup tables ...", file=sys.stderr)

    home = _extract_home_selection(cur)
    home_id = home["id"]
    rooms = _build_room_lookup(cur, home_id)
    zones = _build_zone_lookup(cur, rooms, home_id)
    acc_names = _build_accessory_lookup(cur, rooms, home_id)
    svc_names = _build_service_lookup(cur, acc_names)
    char_info, char_by_service_instance = _build_characteristic_lookup(cur, svc_names)
    entity_type_names = _build_entity_type_lookup(cur)

    if verbose:
        print(
            f"    Home: {home['name'] or '(not identified)'} "
            f"({home['selection']}; {home['availableCount']} available)",
            file=sys.stderr,
        )
        print(
            f"    {len(rooms)} rooms, {len(acc_names)} accessories, "
            f"{len(svc_names)} services, {len(char_info)} characteristics",
            file=sys.stderr,
        )

    # -- Extract automations ------------------------------------------------
    if verbose:
        print("[*] Extracting automations ...", file=sys.stderr)

    trigger_action_table, actionset_join_col, trigger_join_col = (
        _find_trigger_actionset_join(cur)
    )
    if verbose:
        print(
            f"[*] Trigger/action-set join: {trigger_action_table} "
            f"({actionset_join_col} -> action sets, {trigger_join_col} -> triggers)",
            file=sys.stderr,
        )

    trigger_query = (
        "SELECT t.Z_PK, t.Z_ENT, t.ZNAME, t.ZCONFIGUREDNAME, t.ZACTIVE, "
        "t.ZEVALUATIONCONDITION, t.ZSIGNIFICANTEVENT, "
        "t.ZSIGNIFICANTEVENTOFFSETSECONDS, t.ZMOSTRECENTFIREDATE, t.ZEXECUTEONCE "
        "FROM ZMKFTRIGGER t "
    )
    trigger_parameters = ()
    if home_id is not None:
        trigger_query += "WHERE t.ZHOME = ? "
        trigger_parameters = (home_id,)
    trigger_query += "ORDER BY t.ZCONFIGUREDNAME"
    cur.execute(trigger_query, trigger_parameters)
    triggers = cur.fetchall()

    all_automations: list[dict] = []
    for trig in triggers:
        (
            t_pk,
            t_ent,
            t_name,
            t_cname,
            t_active,
            t_eval,
            t_sig,
            t_sig_off,
            t_recent,
            t_once,
        ) = trig

        auto: dict = {
            "name": t_cname or t_name,
            "enabled": bool(t_active),
            "triggerType": entity_type_names.get(t_ent, TRIGGER_ENT_NAMES.get(t_ent, f"type_{t_ent}")),
        }

        if t_eval:
            try:
                auto["evaluationCondition"] = make_serializable(plistlib.loads(t_eval))
            except Exception:
                pass
        if t_sig:
            auto["significantEvent"] = t_sig
        if t_sig_off is not None:
            auto["significantEventOffsetSeconds"] = t_sig_off
        if t_once is not None:
            auto["executeOnce"] = bool(t_once)
        if t_recent is not None:
            try:
                auto["mostRecentFireDate"] = datetime.datetime.utcfromtimestamp(
                    t_recent + COREDATA_EPOCH_OFFSET
                ).isoformat() + "Z"
            except Exception:
                pass

        # Events (trigger conditions)
        auto["events"] = _extract_events(
            cur, t_pk, char_info, char_by_service_instance, svc_names, entity_type_names
        )

        # Action sets via the CoreData junction table.  CoreData embeds
        # internal relationship IDs in column names, so detect them above.
        cur.execute(
            "SELECT aset.Z_PK, aset.ZNAME, aset.ZTYPE "
            f"FROM {_quote_identifier(trigger_action_table)} jt "
            "JOIN ZMKFACTIONSET aset "
            f"ON jt.{_quote_identifier(actionset_join_col)} = aset.Z_PK "
            f"WHERE jt.{_quote_identifier(trigger_join_col)} = ?",
            (t_pk,),
        )

        auto["actionSets"] = []
        for aset_pk, aset_name, aset_type in cur.fetchall():
            asd: dict = {
                "name": aset_name or "",
                "type": aset_type or "",
                "actions": _extract_actions(
                    cur,
                    aset_pk,
                    char_info,
                    char_by_service_instance,
                    acc_names,
                    svc_names,
                    entity_type_names,
                    verbose,
                ),
            }
            auto["actionSets"].append(asd)

        if verbose:
            n_actions = sum(len(a["actions"]) for a in auto["actionSets"])
            has_shortcut = any(
                act.get("actionType") == "shortcut"
                for aset in auto["actionSets"]
                for act in aset["actions"]
            )
            marker = " [SHORTCUT]" if has_shortcut else ""
            print(
                f"    {auto['name']}: {len(auto['events'])} events, "
                f"{len(auto['actionSets'])} action sets, {n_actions} actions{marker}",
                file=sys.stderr,
            )

        all_automations.append(auto)

    conn.close()

    # -- Compute summary stats ----------------------------------------------
    shortcut_autos = [
        a
        for a in all_automations
        if any(
            act.get("actionType") == "shortcut"
            for aset in a.get("actionSets", [])
            for act in aset.get("actions", [])
        )
    ]

    total_cw = sum(
        1
        for a in all_automations
        for s in a.get("actionSets", [])
        for act in s.get("actions", [])
        if act.get("actionType") == "characteristicWrite"
    )

    total_wf_steps = sum(
        len(act.get("workflowSteps", []))
        for a in all_automations
        for s in a.get("actionSets", [])
        for act in s.get("actions", [])
        if act.get("actionType") == "shortcut"
    )

    output = {
        "extractionSource": "homed_core.sqlite",
        "extractionDate": datetime.datetime.now().isoformat(),
        "databasePath": db_path,
        "homeName": home["name"],
        "homeId": home_id,
        "homeSelection": home["selection"],
        "availableHomeCount": home["availableCount"],
        "stats": {
            "totalAutomations": len(all_automations),
            "shortcutAutomations": len(shortcut_autos),
            "characteristicWriteActions": total_cw,
            "totalWorkflowSteps": total_wf_steps,
            "rooms": len(rooms),
            "zones": len(zones),
            "accessories": len(acc_names),
            "services": len(svc_names),
            "characteristics": len(char_info),
        },
        "inventory": {
            "rooms": [
                {"id": pk, "name": name}
                for pk, name in sorted(rooms.items(), key=lambda item: item[1] or "")
            ],
            "zones": [
                {"name": name, "rooms": room_names}
                for name, room_names in zones.items()
            ],
            "accessories": [
                {"id": pk, **info}
                for pk, info in sorted(
                    acc_names.items(), key=lambda item: item[1].get("name") or ""
                )
            ],
            "services": [
                {"id": pk, **info}
                for pk, info in sorted(
                    svc_names.items(),
                    key=lambda item: (
                        item[1].get("accessory") or "",
                        item[1].get("name") or "",
                    ),
                )
            ],
        },
        "automations": all_automations,
    }
    return output


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Extract HomeKit automation data from Apple's homed daemon database. "
            "Outputs a comprehensive JSON file including decoded Shortcuts "
            "workflows, protobuf action sets, and NSKeyedArchiver target values."
        ),
        epilog=(
            "Requires macOS Full Disk Access for Terminal. "
            "Grant via: System Settings > Privacy & Security > Full Disk Access."
        ),
    )
    parser.add_argument(
        "--db",
        default=os.path.expanduser("~/Library/HomeKit/core.sqlite"),
        help=(
            "Path to the homed CoreData database. "
            "Default: ~/Library/HomeKit/core.sqlite"
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        default="homekit_homed_export.json",
        help="Output JSON file path. Default: homekit_homed_export.json",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print per-automation details to stderr while extracting.",
    )
    args = parser.parse_args()

    # -- Run extraction -----------------------------------------------------
    print(f"[*] Database: {args.db}", file=sys.stderr)
    try:
        output = extract(args.db, verbose=args.verbose)
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
    except PermissionError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(2)
    except sqlite3.DatabaseError as exc:
        print(f"[ERROR] Database error: {exc}", file=sys.stderr)
        sys.exit(3)

    print(
        f"[*] Selected home: {output.get('homeName') or '(not identified)'} "
        f"({output.get('homeSelection')}; "
        f"{output.get('availableHomeCount', 0)} available)",
        file=sys.stderr,
    )

    # -- Write JSON ---------------------------------------------------------
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2, default=str)

    size_kb = os.path.getsize(args.output) / 1024

    # -- Summary to stderr --------------------------------------------------
    stats = output["stats"]
    print(f"[*] Exported to: {args.output} ({size_kb:.1f} KB)", file=sys.stderr)
    print(f"[*] Summary:", file=sys.stderr)
    print(f"    Automations:               {stats['totalAutomations']}", file=sys.stderr)
    print(f"    Shortcut automations:       {stats['shortcutAutomations']}", file=sys.stderr)
    print(f"    CharacteristicWrite actions: {stats['characteristicWriteActions']}", file=sys.stderr)
    print(f"    Total workflow steps:        {stats['totalWorkflowSteps']}", file=sys.stderr)
    print(f"    Rooms:                       {stats['rooms']}", file=sys.stderr)
    print(f"    Accessories:                 {stats['accessories']}", file=sys.stderr)
    print(f"    Services:                    {stats['services']}", file=sys.stderr)
    print(f"    Characteristics:             {stats['characteristics']}", file=sys.stderr)


if __name__ == "__main__":
    main()

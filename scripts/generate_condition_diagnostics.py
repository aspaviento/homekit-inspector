#!/usr/bin/env python3
"""Decode HomeKit/Eve evaluation conditions into a readable diagnostics report."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import uuid
from pathlib import Path


UID_RE = re.compile(r"^UID\((\d+)\)$")

COMPOUND_TYPES = {
    0: "NOT",
    1: "AND",
    2: "OR",
}

OPERATORS = {
    0: "<",
    1: "<=",
    2: ">",
    3: ">=",
    4: "==",
    5: "!=",
    6: "MATCHES",
    7: "LIKE",
    8: "BEGINSWITH",
    9: "ENDSWITH",
    10: "IN",
    99: "CONTAINS",
    100: "BETWEEN",
}

PRESENCE_VALUES = {
    "HMPresenceTypeAnyUserAtHome": "alguien en casa",
    "HMPresenceTypeNoUserAtHome": "nadie en casa",
}

SECURITY_CURRENT_STATE_VALUES = {
    0: "Home",
    1: "Away",
    2: "Night",
    3: "Alarm Triggered",
}


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def md_escape(value):
    return str(value).replace("|", "\\|")


def uid_index(value):
    if not isinstance(value, str):
        return None
    match = UID_RE.match(value)
    if not match:
        return None
    return int(match.group(1))


def format_uuid_blob(value):
    if value is None:
        return ""
    try:
        return str(uuid.UUID(bytes=value)).upper()
    except Exception:
        return ""


def open_readonly_sqlite(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def build_characteristic_ref_lookup(db_path):
    if not db_path:
        return {}
    conn = open_readonly_sqlite(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT a.ZCONFIGUREDNAME, a.ZPROVIDEDNAME, a.ZROOM, "
        "a.ZMODELID, s.ZINSTANCEID, s.ZEXPECTEDCONFIGUREDNAME, s.ZNAME, "
        "c.ZINSTANCEID, c.ZMANUFACTURERDESCRIPTION, c.ZFORMAT, r.ZNAME "
        "FROM ZMKFACCESSORY a "
        "JOIN ZMKFSERVICE s ON s.ZACCESSORY = a.Z_PK "
        "JOIN ZMKFCHARACTERISTIC c ON c.ZSERVICE = s.Z_PK "
        "LEFT JOIN ZMKFROOM r ON r.Z_PK = a.ZROOM"
    )
    lookup = {}
    for row in cur.fetchall():
        (
            acc_configured,
            acc_provided,
            _room_pk,
            model_id,
            service_iid,
            svc_configured,
            svc_name,
            char_iid,
            char_desc,
            fmt,
            room,
        ) = row
        model_uuid = format_uuid_blob(model_id)
        if not model_uuid or service_iid is None or char_iid is None:
            continue
        key = (model_uuid, int(service_iid), int(char_iid))
        accessory = acc_configured or acc_provided or "(sin nombre)"
        service = svc_configured or svc_name or ""
        lookup[key] = {
            "accessory": accessory,
            "service": service,
            "characteristic": char_desc or "",
            "format": fmt or "",
            "room": room or "Sin habitacion",
        }
    conn.close()
    return lookup


class PredicateDecoder:
    def __init__(self, archive, char_refs):
        self.archive = archive or {}
        self.objects = self.archive.get("$objects") or []
        self.char_refs = char_refs
        self.confidence_notes = []

    def obj(self, value):
        index = uid_index(value)
        if index is None:
            return value
        if 0 <= index < len(self.objects):
            return self.objects[index]
        return value

    def class_name(self, value):
        obj = self.obj(value)
        if isinstance(obj, dict) and obj.get("$classname"):
            return obj.get("$classname")
        if isinstance(obj, dict) and obj.get("$class"):
            return self.class_name(obj.get("$class"))
        return ""

    def array_items(self, value):
        obj = self.obj(value)
        if isinstance(obj, dict):
            return [self.obj(item) for item in obj.get("NS.objects") or []]
        return []

    def dict_value(self, value):
        obj = self.obj(value)
        if not isinstance(obj, dict):
            return obj
        keys = [self.obj(item) for item in obj.get("NS.keys") or []]
        values = [self.obj(item) for item in obj.get("NS.objects") or []]
        if keys and len(keys) == len(values):
            return dict(zip(keys, values))
        return obj

    def root(self):
        return self.obj((self.archive.get("$top") or {}).get("root"))

    def decode(self):
        return self.decode_predicate(self.root())

    def decode_predicate(self, node):
        if not isinstance(node, dict):
            return {"type": "unknown", "text": str(node), "confidence": "low"}

        cls = self.class_name(node)
        if cls == "NSCompoundPredicate" or "NSCompoundPredicateType" in node:
            ctype = node.get("NSCompoundPredicateType")
            op = COMPOUND_TYPES.get(ctype, f"COMPOUND_{ctype}")
            children = [self.decode_predicate(item) for item in self.array_items(node.get("NSSubpredicates"))]
            if op == "NOT":
                text = "NO (" + (children[0]["text"] if children else "") + ")"
            else:
                sep = f" {op} "
                text = "(" + sep.join(child["text"] for child in children) + ")"
            confidence = "low" if any(child.get("confidence") == "low" for child in children) else "medium"
            return {"type": "compound", "operator": op, "children": children, "text": text, "confidence": confidence}

        if cls == "NSComparisonPredicate" or "NSPredicateOperator" in node:
            left = self.decode_expression(self.obj(node.get("NSLeftExpression")))
            right = self.decode_expression(self.obj(node.get("NSRightExpression")))
            operator = self.decode_operator(self.obj(node.get("NSPredicateOperator")))
            merged = self.merge_characteristic_comparison(left, operator, right)
            if merged:
                return merged
            text = f"{left['text']} {operator} {right['text']}"
            confidence = "low" if "raw" in (left.get("kind"), right.get("kind")) else "medium"
            return {"type": "comparison", "left": left, "operator": operator, "right": right, "text": text, "confidence": confidence}

        return {"type": "unknown", "text": self.describe_raw(node), "confidence": "low"}

    def decode_operator(self, node):
        if not isinstance(node, dict):
            return "?"
        op = node.get("NSOperatorType")
        return OPERATORS.get(op, f"op_{op}")

    def decode_expression(self, node):
        if isinstance(node, (str, int, float, bool)) or node is None:
            return {"kind": "literal", "value": node, "text": self.literal_text(node)}
        if not isinstance(node, dict):
            return {"kind": "raw", "value": node, "text": self.describe_raw(node)}

        expr_type = node.get("NSExpressionType")
        cls = self.class_name(node)

        if cls == "NSConstantValueExpression" or expr_type == 0:
            if "NSConstantValue" in node:
                value = self.dict_value(node.get("NSConstantValue"))
                return self.decode_constant(value)
            class_name = self.obj(node.get("NSConstantValueClassName"))
            return {"kind": "constantClass", "value": class_name, "text": str(class_name)}

        if cls == "NSSelfExpression" or expr_type == 1:
            return {"kind": "self", "text": "self"}

        if cls == "NSFunctionExpression" or expr_type in (3, 4):
            selector = self.obj(node.get("NSSelectorName"))
            args = [self.decode_expression(item) for item in self.array_items(node.get("NSArguments"))]
            operand = self.decode_expression(self.obj(node.get("NSOperand"))) if node.get("NSOperand") else None
            if selector == "valueForKey:" and args:
                return {"kind": "keyPath", "value": args[0].get("value") or args[0].get("text"), "text": str(args[0].get("value") or args[0].get("text"))}
            if selector == "now":
                return {"kind": "function", "value": "now", "text": "now()"}
            arg_text = ", ".join(arg["text"] for arg in args)
            operand_text = (operand or {}).get("text")
            prefix = f"{operand_text}." if operand_text and operand_text != "self" else ""
            return {"kind": "function", "value": selector, "text": f"{prefix}{selector}({arg_text})"}

        if cls == "NSKeyPathExpression" or cls == "NSKeyPathSpecifierExpression" or expr_type == 10:
            key_path = self.obj(node.get("NSKeyPath"))
            return {"kind": "keyPath", "value": key_path, "text": str(key_path)}

        return {"kind": "raw", "value": node, "text": self.describe_raw(node)}

    def decode_constant(self, value):
        if isinstance(value, dict) and {
            "kAccessoryUUID",
            "kServiceInstanceID",
            "kCharacteristicInstanceID",
        }.issubset(value):
            accessory_uuid = str(value.get("kAccessoryUUID")).upper()
            key = (
                accessory_uuid,
                int(value.get("kServiceInstanceID")),
                int(value.get("kCharacteristicInstanceID")),
            )
            ref = self.char_refs.get(key)
            text = (
                f"{ref['accessory']} / {ref['characteristic']}"
                if ref
                else f"characteristic[{accessory_uuid}:{key[1]}:{key[2]}]"
            )
            confidence = "high" if ref else "medium"
            if not ref:
                self.confidence_notes.append(f"unresolved reference: {text}")
            return {"kind": "characteristicRef", "value": key, "ref": ref, "text": text, "confidence": confidence}
        if isinstance(value, dict) and value.get("kPresenceEventPresence"):
            presence = PRESENCE_VALUES.get(value.get("kPresenceEventPresence"), value.get("kPresenceEventPresence"))
            return {"kind": "presence", "value": value, "text": str(presence), "confidence": "medium"}
        if isinstance(value, dict) and "NS.hour" in value:
            hour = int(value.get("NS.hour") or 0)
            minute = int(value.get("NS.minute") or 0)
            return {"kind": "time", "value": value, "text": f"{hour:02d}:{minute:02d}"}
        return {"kind": "literal", "value": value, "text": self.literal_text(value)}

    def merge_characteristic_comparison(self, left, operator, right):
        if left.get("kind") == "keyPath" and left.get("value") == "characteristic" and right.get("kind") == "characteristicRef":
            return {"type": "characteristicBinding", "text": f"characteristic is {right['text']}", "operator": operator, "binding": right, "confidence": right.get("confidence", "medium")}
        if left.get("kind") == "keyPath" and left.get("value") == "characteristicValue":
            return {"type": "valueComparison", "text": f"value {operator} {right['text']}", "operator": operator, "right": right, "confidence": "medium"}
        if left.get("kind") == "keyPath" and left.get("value") == "presence":
            return {"type": "presenceComparison", "text": f"presence {operator} {right['text']}", "operator": operator, "right": right, "confidence": "medium"}
        return None

    def literal_text(self, value):
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        return str(value)

    def describe_raw(self, value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)[:240]


def flatten_condition(node):
    if node.get("type") == "compound":
        out = []
        for child in node.get("children") or []:
            out.extend(flatten_condition(child))
        return out
    return [node]


def pair_characteristic_rules(node):
    if node.get("type") != "compound" or node.get("operator") != "AND":
        return None
    children = node.get("children") or []
    binding = None
    value = None
    rules = []
    for child in children:
        if child.get("type") == "characteristicBinding":
            binding = child.get("binding")
        elif child.get("type") == "valueComparison":
            value = child
        else:
            nested = pair_characteristic_rules(child)
            if nested:
                rules.extend(nested)
            else:
                rules.append({"text": child.get("text"), "accessory": "", "room": "", "characteristic": "", "format": ""})
    if binding and value:
        ref = binding.get("ref") or {}
        value_text = format_characteristic_value(ref, value["right"])
        rules.append(
            {
                "text": f"{binding['text'].replace('characteristic is ', '')} {value['operator']} {value_text}",
                "accessory": ref.get("accessory", ""),
                "room": ref.get("room", ""),
                "characteristic": ref.get("characteristic", ""),
                "format": ref.get("format", ""),
            }
        )
    return rules


def format_characteristic_value(ref, value_expr):
    characteristic = (ref or {}).get("characteristic") or ""
    raw_value = value_expr.get("value")
    if characteristic == "Security System Current State":
        try:
            return SECURITY_CURRENT_STATE_VALUES.get(int(raw_value), str(raw_value))
        except Exception:
            return value_expr.get("text", str(raw_value))
    if characteristic in ("Power State", "Occupancy Detected", "Motion Detected"):
        if raw_value in (1, 1.0, "1", "1.0", True):
            return "true"
        if raw_value in (0, 0.0, "0", "0.0", False):
            return "false"
    return value_expr.get("text", str(raw_value))


def condition_summary(auto, char_refs):
    condition = auto.get("evaluationCondition")
    decoder = PredicateDecoder(condition, char_refs)
    decoded = decoder.decode()
    paired = pair_characteristic_rules(decoded)
    rules = paired or [{"text": item["text"], "accessory": "", "room": "", "characteristic": "", "format": ""} for item in flatten_condition(decoded)]
    confidence = decoded.get("confidence", "low")
    if decoder.confidence_notes:
        confidence = "low"
    return {
        "name": auto.get("name") or "",
        "enabled": bool(auto.get("enabled")),
        "triggerType": auto.get("triggerType") or "",
        "eventCount": len(auto.get("events") or []),
        "actionCount": sum(len(item.get("actions") or []) for item in auto.get("actionSets") or []),
        "decoded": decoded,
        "rules": rules,
        "confidence": confidence,
        "notes": decoder.confidence_notes,
    }


def write_markdown(summaries, path):
    lines = [
        "# HomeKit Condition Diagnostics",
        "",
        f"Automatizaciones con condiciones: {len(summaries)}",
        "",
        "Este informe decodifica `evaluationCondition` de HomeKit/Eve desde `NSKeyedArchiver`.",
        "Las reglas marcadas con confianza media/alta siguen necesitando validacion visual contra Eve/Home antes de tratarlas como documentacion definitiva.",
        "",
        "## Resumen",
        "| Automatizacion | Estado | Reglas decodificadas | Confianza |",
        "|---|---|---:|---|",
    ]
    for item in summaries:
        status = "Activo" if item["enabled"] else "Inactivo"
        lines.append(f"| {md_escape(item['name'])} | {status} | {len(item['rules'])} | {item['confidence']} |")

    lines.extend(["", "## Detalle"])
    for item in summaries:
        status = "Activo" if item["enabled"] else "Inactivo"
        lines.extend(["", f"### ({status}) {item['name']}"])
        lines.append(f"- Trigger type: {item['triggerType']}; events: {item['eventCount']}; actions: {item['actionCount']}; confianza: {item['confidence']}")
        lines.append("- Reglas decodificadas:")
        for rule in item["rules"]:
            suffix = ""
            if rule.get("room") or rule.get("characteristic"):
                suffix = f" [{rule.get('room') or 'sin habitacion'}; {rule.get('characteristic') or 'caracteristica desconocida'}]"
            lines.append(f"  - {rule['text']}{suffix}")
        lines.append("- Arbol logico:")
        lines.append(f"  - `{item['decoded']['text']}`")
        if item["notes"]:
            lines.append("- Notas:")
            for note in item["notes"]:
                lines.append(f"  - {note}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_csv(summaries, path):
    import csv

    fields = ["automation", "enabled", "confidence", "rule", "accessory", "room", "characteristic", "format"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in summaries:
            for rule in item["rules"]:
                writer.writerow(
                    {
                        "automation": item["name"],
                        "enabled": item["enabled"],
                        "confidence": item["confidence"],
                        "rule": rule["text"],
                        "accessory": rule.get("accessory", ""),
                        "room": rule.get("room", ""),
                        "characteristic": rule.get("characteristic", ""),
                        "format": rule.get("format", ""),
                    }
                )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input_json", type=Path)
    parser.add_argument("--db", type=Path, default=Path.home() / "Library/HomeKit/core.sqlite")
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()

    data = load_json(args.input_json)
    out_dir = args.output_dir or args.input_json.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    char_refs = build_characteristic_ref_lookup(args.db if args.db else None)
    summaries = [
        condition_summary(auto, char_refs)
        for auto in data.get("automations") or []
        if auto.get("evaluationCondition")
    ]
    write_markdown(summaries, out_dir / "homekit_condition_diagnostics.md")
    write_csv(summaries, out_dir / "homekit_condition_diagnostics.csv")
    print(f"Wrote condition diagnostics for {len(summaries)} automations to {out_dir}")


if __name__ == "__main__":
    main()

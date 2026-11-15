"""Conservative rule viability checks over recorded HomeKit relationships.

An unresolved relationship is not proof of an impossible start. End events and
broken action targets never make a rule orphaned by themselves.
"""
from __future__ import annotations

import calendar
import math


NON_DEVICE_EVENTS = {"calendar", "presence", "significantTime", "location"}
DEVICE_EVENTS = {"charValue", "charRange"}


def start_events(events):
    return [event for event in events or [] if event.get("eventRole") != "end"]


def end_events(events):
    return [event for event in events or [] if event.get("eventRole") == "end"]


def impossible_calendar_reason(event):
    # Only a known Gregorian calendar permits this proof. Do not assume a
    # user's calendar or treat dates in the past as impossible recurrences.
    if event.get("calendarIdentifier") != "gregorian":
        return ""
    fields = event.get("dateComponents") or {}
    month, day, year = (fields.get(key) for key in ("month", "day", "year"))
    if not all(type(value) is int for value in (month, day)):
        return ""
    if year is not None and (type(year) is not int or not 1 <= year <= 9999):
        return ""
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return "Invalid Gregorian month/day components"
    # A recurring February 29 is possible. Use a leap year if unspecified.
    if day > calendar.monthrange(year or 2000, month)[1]:
        return "The specified Gregorian date cannot occur"
    return ""


def analyze_rule(row):
    events = row.get("events") or []
    starts, ends = start_events(events), end_events(events)
    issues = list(row.get("conditionReferenceIssues") or [])
    impossible = []
    unresolved = False
    unresolved_end = False
    for index, event in enumerate(starts, 1):
        checks = event.get("referenceChecks") or []
        missing = [check for check in checks if check.get("status") == "missing"]
        reason = impossible_calendar_reason(event) if event.get("eventType") == "calendar" else ""
        # Role and entity type must be known: older or partially understood
        # exports are insufficient to prove an event can never occur.
        proven = event.get("eventRole") == "start" and (
            (bool(missing) and event.get("eventType") in DEVICE_EVENTS) or bool(reason)
        )
        if proven:
            impossible.append({"startEvent": index, "reason": reason or "Referenced trigger object no longer exists"})
        known = event.get("eventType") in NON_DEVICE_EVENTS or bool(event.get("accessory") or event.get("service"))
        if not proven and (not known or any(c.get("status") not in {"present", "missing"} for c in checks)):
            unresolved = True
        for check in checks:
            if check.get("status") != "present":
                issues.append({"part": "WHEN", "event": index, **check})
    for index, event in enumerate(ends, 1):
        if event.get("eventType") == "duration":
            duration = event.get("durationSeconds")
            if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
                unresolved_end = True
        elif event.get("eventType") not in NON_DEVICE_EVENTS and not (event.get("accessory") or event.get("service")):
            unresolved_end = True
        for check in event.get("referenceChecks") or []:
            if check.get("status") != "present":
                issues.append({"part": "END", "event": index, **check})
    for index, action in enumerate(row.get("actions") or [], 1):
        for check in action.get("referenceChecks") or []:
            if check.get("status") != "present":
                issues.append({"part": "THEN", "action": index, **check})
    graph_complete = (
        row.get("triggerType") == "event" and row.get("eventGraphComplete") is True
        and all(event.get("eventRole") in {"start", "end"} for event in events)
    )
    verified_missing_start = not starts and graph_complete
    orphaned = verified_missing_start or (
        bool(starts) and graph_complete and len(impossible) == len(starts)
    )
    if verified_missing_start:
        impossible.append({"startEvent": None, "reason": "Complete event graph has no associated start events"})
    if impossible and len(impossible) == len(starts) and not graph_complete:
        unresolved = True
    if orphaned:
        status = "orphaned"
    elif not starts:
        status = "missing"
    elif unresolved or impossible:
        status = "unresolved" if unresolved else "partial"
    elif all(e.get("eventType") in NON_DEVICE_EVENTS for e in starts):
        status = "non-device"
    else:
        status = "resolved"
    empty = not events and not row.get("actions")
    label = (
        "Orphaned rule · empty" if orphaned and empty else
        "Orphaned rule" if orphaned else
        "Empty rule · possibly residual" if empty else
        "Missing start event" if status == "missing" else
        "Trigger not resolved" if status == "unresolved" else
        "Some start events cannot occur" if status == "partial" else ""
    )
    return {
        "triggerStatus": status,
        "structureStatus": label,
        "isOrphaned": orphaned,
        "isEmptyRule": empty,
        "missingStartEvent": not starts,
        "orphanEvidence": impossible if orphaned else [],
        "referenceIssues": issues,
        "startEventCount": len(starts),
        "endEventCount": len(ends),
        "needsStructureReview": status in {"missing", "unresolved", "orphaned", "partial"} or bool(issues) or unresolved_end,
    }


def condition_reference_issues(decoded):
    """Retain unresolved predicate references without claiming impossibility."""
    issues = []
    seen = set()

    def walk(node):
        if isinstance(node, dict):
            if node.get("kind") == "characteristicRef" and not node.get("ref"):
                key = tuple(node.get("value") or [])
                if key not in seen:
                    seen.add(key)
                    issues.append({"part": "IF", "kind": "characteristic reference", "id": list(key), "status": "unresolved"})
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(decoded)
    return issues

#!/usr/bin/env python3
"""Validate Slack bridge task parents and thread events; never claim execution."""

import argparse
import json
import re
import sys

from bridge_agents import BRIDGE_AGENTS

CHANNEL_ID = "C0C6PRGNGLQ"
MARKER = "bridge-task:v1"
EVENTS = {"CLAIM", "PROGRESS", "RESULT", "HANDOFF", "COUNTEREVIDENCE", "REVIEW", "DONE", "USER_OVERRIDE"}
TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{1,127}\Z")
MAX_BYTES = 65536


class PacketError(ValueError):
    pass


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PacketError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_json(value):
    try:
        return json.loads(value, object_pairs_hook=_object)
    except json.JSONDecodeError as exc:
        raise PacketError("invalid JSON") from exc


def parse_parent(text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_BYTES:
        raise PacketError("parent message is invalid or too large")
    # Slack's connector may render ```json\n{...}\n``` as ```{...}``` on
    # readback. Accept either representation, but never multiple code blocks.
    match = re.search(r"(?m)^```json\s*\n(.*?)\n```\s*$", text, re.S)
    if not match:
        match = re.search(r"(?m)^```(\{.*?\})```\s*$", text, re.S)
    if text.count("```") != 2 or not match:
        raise PacketError("parent requires exactly one fenced JSON block")
    record = _read_json(match.group(1))
    if not isinstance(record, dict):
        raise PacketError("task packet must be an object")
    if MARKER not in text[:match.start()]:
        raise PacketError("missing bridge-task marker")
    if record.get("protocol_version") != "2.0" or not TASK_ID.fullmatch(record.get("task_id") or ""):
        raise PacketError("invalid protocol_version or task_id")
    if record.get("status") != "READY":
        raise PacketError("new task status must be READY")
    if record.get("current_owner") not in BRIDGE_AGENTS:
        raise PacketError("current_owner must be one of seven agents")
    for field, expected in (("assignment_mode", "STICKY"), ("handoff_policy", "USER_EXPLICIT_ONLY"),
                            ("cross_agent_read", "DENY_BY_DEFAULT"), ("transport", "SLACK_FIRST")):
        if record.get(field) != expected:
            raise PacketError(f"{field} must be {expected}")
    if record.get("slack_channel_id") != CHANNEL_ID:
        raise PacketError("wrong Slack channel")
    if not isinstance(record.get("execution_target"), str) or not record["execution_target"].strip():
        raise PacketError("execution_target is required")
    if not isinstance(record.get("objective"), str) or not record["objective"].strip():
        raise PacketError("objective is required")
    criteria = record.get("acceptance_criteria")
    if not isinstance(criteria, list) or not criteria or any(not isinstance(x, str) or not x.strip() for x in criteria):
        raise PacketError("acceptance_criteria must be nonempty strings")
    return record


def exact_owner_tasks(parents, owner):
    if owner not in BRIDGE_AGENTS:
        raise PacketError("requester must be one of seven agents")
    result = []
    for parent in parents:
        try:
            task = parse_parent(parent)
        except PacketError:
            continue
        if task["current_owner"] == owner:
            result.append(task)
    return result


def parse_event(text, task_id):
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_BYTES:
        raise PacketError("event is invalid or too large")
    match = re.fullmatch(r"(?s)([A-Z_]+)\s+task_id:\s*(\S+)\n(.+)", text.strip())
    if not match or match.group(1) not in EVENTS or match.group(2) != task_id:
        raise PacketError("invalid event type or task_id")
    event, _, body = match.groups()
    if event == "USER_OVERRIDE" and not re.search(r"(?m)^authorized_by:\s*USER\s*$", body):
        raise PacketError("USER_OVERRIDE requires explicit user authorization")
    if event == "HANDOFF" and not re.search(r"(?m)^to_owner:\s*(\S+)\s*$", body):
        raise PacketError("HANDOFF requires to_owner")
    return {"event": event, "task_id": task_id, "body": body}


def apply_event(owner, event, *, verified_user_override=False):
    """A Slack claim/review is never an ownership transfer or execution lock."""
    if owner not in BRIDGE_AGENTS:
        raise PacketError("invalid current owner")
    kind = event["event"]
    if kind != "USER_OVERRIDE":
        return owner
    if not verified_user_override:
        raise PacketError("USER_OVERRIDE must be verified outside Slack text")
    target = re.search(r"(?m)^to_owner:\s*(\S+)\s*$", event["body"])
    if not target or target.group(1) not in BRIDGE_AGENTS:
        raise PacketError("USER_OVERRIDE requires valid to_owner")
    return target.group(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("parent", "event"))
    parser.add_argument("--task-id")
    args = parser.parse_args()
    try:
        value = sys.stdin.read(MAX_BYTES + 1)
        result = parse_parent(value) if args.kind == "parent" else parse_event(value, args.task_id or "")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    except PacketError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

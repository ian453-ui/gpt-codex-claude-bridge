#!/usr/bin/env python3
"""GitHub Issues transport for bridge tasks; local control plane remains the lock."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from pathlib import Path

from bridge_agents import ALL_AGENTS, BRIDGE_AGENTS

DEFAULT_REPOSITORY = "ian453-ui/gpt-codex-claude-bridge"
MARKER_START = "<!-- bridge-task:v1\n"
MARKER_END = "\n-->"
OWNER_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
TASK_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,127}$")
ASSIGNMENT_MODE = "STICKY"
HANDOFF_POLICY = "USER_EXPLICIT_ONLY"
CROSS_AGENT_READ = "DENY_BY_DEFAULT"
TASK_PACKET_READY = "GITHUB_TASK_PACKET_READY"
MAX_TASK_PACKET_BYTES = 65536
VALID_STATUSES = {"PENDING", "READY", "CLAIMED", "IN_PROGRESS", "REVIEW", "DONE", "BLOCKED", "FAILED"}


class GitHubWriterError(RuntimeError):
    pass


def parse_task_packet(source):
    """Accept raw JSON or the exact block copied from a ChatGPT response."""
    if not isinstance(source, str) or not source.strip():
        raise GitHubWriterError("task packet is empty")
    if len(source.encode("utf-8")) > MAX_TASK_PACKET_BYTES:
        raise GitHubWriterError("task packet exceeds 65536 bytes")
    value = source.strip()
    headers = None
    if not value.startswith("{"):
        lines = value.splitlines()
        if len(lines) < 4 or lines[0] != TASK_PACKET_READY or lines[-1] != "```":
            raise GitHubWriterError("expected raw JSON or a standalone GITHUB_TASK_PACKET_READY block")
        if lines[1] == "```json":
            json_start = 2
        else:
            expected = ("target_agent", "execution_target", "task_id", "next_user_action")
            if len(lines) < 8 or lines[5] != "```json":
                raise GitHubWriterError("task packet header is malformed")
            headers = {}
            for key, line in zip(expected, lines[1:5]):
                prefix = f"{key}: "
                if not line.startswith(prefix) or not line[len(prefix):].strip():
                    raise GitHubWriterError(f"task packet header requires {key}")
                headers[key] = line[len(prefix):].strip()
            json_start = 6
        value = "\n".join(lines[json_start:-1])

    def unique_keys(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise GitHubWriterError(f"duplicate task packet key: {key}")
            result[key] = item
        return result

    try:
        packet = json.loads(value, object_pairs_hook=unique_keys)
    except json.JSONDecodeError as exc:
        raise GitHubWriterError("task packet is invalid JSON") from exc
    if not isinstance(packet, dict):
        raise GitHubWriterError("task packet must be a JSON object")
    if headers:
        for header, field in (("target_agent", "current_owner"),
                              ("execution_target", "execution_target"),
                              ("task_id", "task_id")):
            if headers[header] != packet.get(field):
                raise GitHubWriterError(f"task packet header {header} conflicts with JSON {field}")
    return packet


class GitHubProvider(ABC):
    @abstractmethod
    def health(self): ...

    @abstractmethod
    def list_issues(self, state="open"): ...

    @abstractmethod
    def view_issue(self, number): ...

    @abstractmethod
    def create_issue(self, title, body): ...

    @abstractmethod
    def edit_issue(self, number, body): ...

    @abstractmethod
    def comment_issue(self, number, body): ...


def validate_repository(repository):
    if repository != DEFAULT_REPOSITORY:
        raise GitHubWriterError(f"repository rejected: expected {DEFAULT_REPOSITORY}")
    return repository


def validate_task_id(task_id):
    if not TASK_PATTERN.fullmatch(task_id or ""):
        raise GitHubWriterError("invalid task_id")
    return task_id


def validate_owner(owner):
    if not OWNER_PATTERN.fullmatch(owner or "") or owner not in ALL_AGENTS:
        raise GitHubWriterError("invalid current_owner")
    return owner


def validate_record(record, require_sticky=False):
    if not isinstance(record, dict):
        raise GitHubWriterError("task packet must be a JSON object")
    validate_task_id(record.get("task_id")); validate_owner(record.get("current_owner"))
    if record.get("protocol_version") != "2.0":
        raise GitHubWriterError("protocol_version must be 2.0")
    if record.get("status") not in VALID_STATUSES:
        raise GitHubWriterError("invalid task status")
    if require_sticky or "assignment_mode" in record:
        if record.get("assignment_mode") != ASSIGNMENT_MODE:
            raise GitHubWriterError("assignment_mode must be STICKY")
        if record.get("handoff_policy") != HANDOFF_POLICY:
            raise GitHubWriterError("handoff_policy must be USER_EXPLICIT_ONLY")
        if record.get("cross_agent_read") != CROSS_AGENT_READ:
            raise GitHubWriterError("cross_agent_read must be DENY_BY_DEFAULT")
        if not isinstance(record.get("execution_target"), str) or not record["execution_target"]:
            raise GitHubWriterError("execution_target is required")
    if not isinstance(record.get("objective", ""), str) or not record.get("objective", "").strip():
        raise GitHubWriterError("objective is required")
    if (not isinstance(record.get("acceptance_criteria", []), list) or
            any(not isinstance(item, str) for item in record.get("acceptance_criteria", []))):
        raise GitHubWriterError("acceptance_criteria must be an array")
    return record


def encode_task(record, visible_text=""):
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    summary = visible_text.strip() or f"Bridge task `{record['task_id']}` for `{record['current_owner']}`."
    return f"{MARKER_START}{payload}{MARKER_END}\n\n{summary}\n"


def decode_task(body):
    start = body.find(MARKER_START)
    if start < 0:
        raise GitHubWriterError("issue does not contain bridge-task metadata")
    payload_start = start + len(MARKER_START)
    end = body.find(MARKER_END, payload_start)
    if end < 0:
        raise GitHubWriterError("bridge-task metadata is malformed")
    try:
        record = json.loads(body[payload_start:end])
    except json.JSONDecodeError as exc:
        raise GitHubWriterError("bridge-task metadata is invalid JSON") from exc
    validate_record(record)
    if not isinstance(record.get("owner_history", []), list):
        raise GitHubWriterError("bridge-task owner_history is invalid")
    return record, (start, end + len(MARKER_END))


class GhCliProvider(GitHubProvider):
    def __init__(self, repository=DEFAULT_REPOSITORY, runner=subprocess.run, which=shutil.which):
        self.repository = validate_repository(repository)
        self.runner = runner
        self.which = which

    def _run(self, args, input_text=None):
        if not self.which("gh"):
            raise GitHubWriterError("GitHub CLI is unavailable")
        result = self.runner(
            ["gh", *args], input=input_text, text=True, capture_output=True, check=False
        )
        if result.returncode:
            detail = (result.stderr or result.stdout or "GitHub CLI request failed").strip()
            raise GitHubWriterError(detail)
        return result.stdout

    def health(self):
        self._run(["auth", "status", "--hostname", "github.com"])
        value = self._run(["repo", "view", self.repository, "--json", "nameWithOwner", "--jq", ".nameWithOwner"]).strip()
        if value.lower() != self.repository.lower():
            raise GitHubWriterError("authenticated GitHub repository identity mismatch")
        return {"ok": True, "repository": value, "transport": "GH_CLI"}

    def list_issues(self, state="open"):
        raw = self._run(["issue", "list", "--repo", self.repository, "--state", state, "--limit", "1000", "--json", "number,title,body,state,url"])
        return json.loads(raw)

    def view_issue(self, number):
        raw = self._run(["issue", "view", str(number), "--repo", self.repository, "--json", "number,title,body,state,url,comments"])
        return json.loads(raw)

    def create_issue(self, title, body):
        raw = self._run(["issue", "create", "--repo", self.repository, "--title", title, "--body-file", "-"] , body)
        url = raw.strip().splitlines()[-1]
        number = int(url.rstrip("/").rsplit("/", 1)[-1])
        return self.view_issue(number)

    def edit_issue(self, number, body):
        self._run(["issue", "edit", str(number), "--repo", self.repository, "--body-file", "-"], body)
        return self.view_issue(number)

    def comment_issue(self, number, body):
        self._run(["issue", "comment", str(number), "--repo", self.repository, "--body-file", "-"], body)
        return self.view_issue(number)


class GitHubTaskBus:
    def __init__(self, provider):
        self.provider = provider

    def find_task(self, task_id, state="all"):
        validate_task_id(task_id)
        matches = []
        for issue in self.provider.list_issues(state):
            try:
                record, _ = decode_task(issue.get("body", ""))
            except GitHubWriterError:
                continue
            if record["task_id"] == task_id:
                matches.append((issue, record))
        if len(matches) > 1:
            raise GitHubWriterError("duplicate GitHub Issues exist for task_id")
        return matches[0] if matches else None

    def create(self, task_id, title, owner, objective, acceptance=None, status="READY",
               assignment_mode=None, handoff_policy=None, cross_agent_read=None,
               execution_target=None):
        validate_task_id(task_id); validate_owner(owner)
        existing = self.find_task(task_id)
        if existing:
            issue, record = existing
            if record["current_owner"] != owner:
                raise GitHubWriterError("duplicate task_id has a different sticky owner")
            return {"created": False, "issue": issue, "task": record}
        record = {"protocol_version": "2.0", "task_id": task_id, "status": status,
                  "current_owner": owner, "owner_history": [owner],
                  "objective": objective, "acceptance_criteria": acceptance or []}
        if assignment_mode is not None or execution_target is not None:
            record.update(assignment_mode=assignment_mode, handoff_policy=handoff_policy,
                          cross_agent_read=cross_agent_read, execution_target=execution_target)
            validate_record(record, require_sticky=True)
        else:
            validate_record(record)
        issue = self.provider.create_issue(title, encode_task(record, objective))
        return {"created": True, "issue": issue, "task": record}

    def read(self, number, requester=None, authorized_task_id=None):
        issue = self.provider.view_issue(number)
        record, _ = decode_task(issue.get("body", ""))
        if requester:
            validate_owner(requester)
            if requester != record["current_owner"] and authorized_task_id != record["task_id"]:
                raise GitHubWriterError("cross-agent read denied without explicit task_id authorization")
        return {"issue": issue, "task": record}

    def update(self, number, status, owner, user_override=False):
        validate_owner(owner)
        if status not in VALID_STATUSES:
            raise GitHubWriterError("invalid task status")
        issue = self.provider.view_issue(number)
        record, span = decode_task(issue.get("body", ""))
        if record.get("assignment_mode") == ASSIGNMENT_MODE and owner != record["current_owner"] and not user_override:
            raise GitHubWriterError("sticky owner takeover requires explicit USER_OVERRIDE")
        history = list(record.get("owner_history", []))
        if not history or history[-1] != owner:
            history.append(owner)
        record.update(status=status, current_owner=owner, owner_history=history)
        marker = encode_task(record).split("\n\n", 1)[0]
        body = issue["body"][:span[0]] + marker + issue["body"][span[1]:]
        updated = self.provider.edit_issue(number, body)
        return {"issue": updated, "task": record}

    def ingest(self, packet, local_execution_target):
        validate_record(packet, require_sticky=True)
        if packet["current_owner"] not in BRIDGE_AGENTS:
            raise GitHubWriterError("new task packet requires one of the seven current owners")
        if packet["status"] != "READY":
            raise GitHubWriterError("new task packet status must be READY")
        if packet["execution_target"] != local_execution_target:
            raise GitHubWriterError("execution_target does not match this local writer")
        title = packet.get("title")
        if not isinstance(title, str) or not title.strip():
            raise GitHubWriterError("title is required")
        existing = self.find_task(packet["task_id"])
        if existing:
            issue, record = existing
            same = all(record.get(key) == packet.get(key) for key in (
                "task_id", "current_owner", "assignment_mode", "handoff_policy",
                "cross_agent_read", "execution_target", "objective", "acceptance_criteria"))
            same = same and issue.get("title") == title
            if not same:
                raise GitHubWriterError("duplicate task_id conflicts with existing sticky task")
            return {"created": False, "issue": issue, "task": record}
        record = {"protocol_version": "2.0", "task_id": packet["task_id"],
                  "status": packet["status"], "current_owner": packet["current_owner"],
                  "owner_history": [packet["current_owner"]], "objective": packet["objective"],
                  "acceptance_criteria": packet.get("acceptance_criteria", []),
                  "assignment_mode": ASSIGNMENT_MODE, "handoff_policy": HANDOFF_POLICY,
                  "cross_agent_read": CROSS_AGENT_READ,
                  "execution_target": packet["execution_target"]}
        issue = self.provider.create_issue(title, encode_task(record, packet["objective"]))
        return {"created": True, "issue": issue, "task": record}

    def comment(self, number, kind, text):
        if kind not in {"RESULT", "HANDOFF"}:
            raise GitHubWriterError("comment kind must be RESULT or HANDOFF")
        current = self.read(number)
        task_id = current["task"]["task_id"]
        body = f"{kind}: {task_id}\n\ntask_id: {task_id}\n{text.strip()}\n"
        issue = self.provider.comment_issue(number, body)
        return {"issue": issue, "task": current["task"], "kind": kind}

    def search_owner(self, owner):
        validate_owner(owner)
        found = []
        for issue in self.provider.list_issues("open"):
            try:
                record, _ = decode_task(issue.get("body", ""))
            except GitHubWriterError:
                continue
            if record["current_owner"] == owner:
                found.append({"issue": issue, "task": record})
        return found


def main():
    parser = argparse.ArgumentParser(description="GitHub Issues bridge writer")
    parser.add_argument("--repo", default=DEFAULT_REPOSITORY)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("health")
    p = sub.add_parser("create"); p.add_argument("--task-id", required=True); p.add_argument("--title", required=True); p.add_argument("--owner", required=True); p.add_argument("--objective", required=True); p.add_argument("--acceptance", action="append", default=[]); p.add_argument("--status", default="READY")
    p = sub.add_parser("read"); p.add_argument("--issue", type=int, required=True); p.add_argument("--requester"); p.add_argument("--authorized-task-id")
    p = sub.add_parser("update"); p.add_argument("--issue", type=int, required=True); p.add_argument("--status", required=True); p.add_argument("--owner", required=True); p.add_argument("--user-override", action="store_true")
    p = sub.add_parser("comment"); p.add_argument("--issue", type=int, required=True); p.add_argument("--kind", choices=("RESULT", "HANDOFF"), required=True); p.add_argument("--text", required=True)
    p = sub.add_parser("search"); p.add_argument("--owner", required=True)
    p = sub.add_parser("ingest"); p.add_argument("--task-packet-file", default="-"); p.add_argument("--execution-target", default=os.environ.get("BRIDGE_EXECUTION_TARGET", "MAC"))
    args = parser.parse_args()
    try:
        provider = GhCliProvider(args.repo); bus = GitHubTaskBus(provider)
        if args.command == "health": value = provider.health()
        elif args.command == "create": provider.health(); value = bus.create(args.task_id, args.title, args.owner, args.objective, args.acceptance, args.status)
        elif args.command == "read": value = bus.read(args.issue, args.requester, args.authorized_task_id)
        elif args.command == "update": provider.health(); value = bus.update(args.issue, args.status, args.owner, args.user_override)
        elif args.command == "comment": provider.health(); value = bus.comment(args.issue, args.kind, args.text)
        elif args.command == "search": value = bus.search_owner(args.owner)
        else:
            provider.health()
            source = (sys.stdin.read() if args.task_packet_file == "-" else
                      Path(args.task_packet_file).read_text(encoding="utf-8"))
            raw = parse_task_packet(source)
            value = bus.ingest(raw, args.execution_target)
        print(json.dumps(value, ensure_ascii=False, indent=2))
    except (GitHubWriterError, json.JSONDecodeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()

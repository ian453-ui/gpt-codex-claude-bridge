#!/usr/bin/env python3
"""GitHub Issues transport for bridge tasks; local control plane remains the lock."""

import argparse
import json
import re
import shutil
import subprocess
from abc import ABC, abstractmethod

DEFAULT_REPOSITORY = "ian453-ui/gpt-codex-claude-bridge"
MARKER_START = "<!-- bridge-task:v1\n"
MARKER_END = "\n-->"
OWNER_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
TASK_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,127}$")


class GitHubWriterError(RuntimeError):
    pass


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
    if not OWNER_PATTERN.fullmatch(owner or ""):
        raise GitHubWriterError("invalid current_owner")
    return owner


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
    validate_task_id(record.get("task_id"))
    validate_owner(record.get("current_owner"))
    if not isinstance(record.get("status"), str) or not record["status"]:
        raise GitHubWriterError("bridge-task status is missing")
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

    def create(self, task_id, title, owner, objective, acceptance=None, status="READY"):
        validate_task_id(task_id); validate_owner(owner)
        existing = self.find_task(task_id)
        if existing:
            issue, record = existing
            return {"created": False, "issue": issue, "task": record}
        record = {"protocol_version": "2.0", "task_id": task_id, "status": status,
                  "current_owner": owner, "owner_history": [owner],
                  "objective": objective, "acceptance_criteria": acceptance or []}
        issue = self.provider.create_issue(title, encode_task(record, objective))
        return {"created": True, "issue": issue, "task": record}

    def read(self, number):
        issue = self.provider.view_issue(number)
        record, _ = decode_task(issue.get("body", ""))
        return {"issue": issue, "task": record}

    def update(self, number, status, owner):
        validate_owner(owner)
        issue = self.provider.view_issue(number)
        record, span = decode_task(issue.get("body", ""))
        history = list(record.get("owner_history", []))
        if not history or history[-1] != owner:
            history.append(owner)
        record.update(status=status, current_owner=owner, owner_history=history)
        marker = encode_task(record).split("\n\n", 1)[0]
        body = issue["body"][:span[0]] + marker + issue["body"][span[1]:]
        updated = self.provider.edit_issue(number, body)
        return {"issue": updated, "task": record}

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
    p = sub.add_parser("read"); p.add_argument("--issue", type=int, required=True)
    p = sub.add_parser("update"); p.add_argument("--issue", type=int, required=True); p.add_argument("--status", required=True); p.add_argument("--owner", required=True)
    p = sub.add_parser("comment"); p.add_argument("--issue", type=int, required=True); p.add_argument("--kind", choices=("RESULT", "HANDOFF"), required=True); p.add_argument("--text", required=True)
    p = sub.add_parser("search"); p.add_argument("--owner", required=True)
    args = parser.parse_args()
    try:
        provider = GhCliProvider(args.repo); bus = GitHubTaskBus(provider)
        if args.command == "health": value = provider.health()
        elif args.command == "create": value = bus.create(args.task_id, args.title, args.owner, args.objective, args.acceptance, args.status)
        elif args.command == "read": value = bus.read(args.issue)
        elif args.command == "update": value = bus.update(args.issue, args.status, args.owner)
        elif args.command == "comment": value = bus.comment(args.issue, args.kind, args.text)
        else: value = bus.search_owner(args.owner)
        print(json.dumps(value, ensure_ascii=False, indent=2))
    except (GitHubWriterError, json.JSONDecodeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()

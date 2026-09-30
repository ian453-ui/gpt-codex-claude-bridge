import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
from github_writer import (DEFAULT_REPOSITORY, GhCliProvider, GitHubTaskBus,
                           GitHubWriterError, decode_task, encode_task)


class FakeProvider:
    def __init__(self): self.issues = []; self.comments = []
    def health(self): return {"ok": True}
    def list_issues(self, state="open"):
        return [x.copy() for x in self.issues if state == "all" or x["state"].lower() == state]
    def view_issue(self, number): return next(x.copy() for x in self.issues if x["number"] == number)
    def create_issue(self, title, body):
        value = {"number": len(self.issues)+1, "title": title, "body": body, "state": "OPEN", "url": f"https://example.test/issues/{len(self.issues)+1}", "comments": []}
        self.issues.append(value); return value.copy()
    def edit_issue(self, number, body):
        value = next(x for x in self.issues if x["number"] == number); value["body"] = body; return value.copy()
    def comment_issue(self, number, body):
        value = next(x for x in self.issues if x["number"] == number); value["comments"].append({"body": body}); self.comments.append(body); return value.copy()


class GitHubWriterTests(unittest.TestCase):
    def test_cli_unavailable_fails_closed(self):
        provider = GhCliProvider(which=lambda _: None)
        with self.assertRaisesRegex(GitHubWriterError, "unavailable"): provider.health()

    def test_unauthenticated_cli_fails_closed(self):
        def runner(*args, **kwargs): return subprocess.CompletedProcess(args, 1, "", "not logged in")
        provider = GhCliProvider(runner=runner, which=lambda _: "/usr/bin/gh")
        with self.assertRaisesRegex(GitHubWriterError, "not logged in"): provider.health()

    def test_wrong_repository_is_rejected_before_cli(self):
        with self.assertRaisesRegex(GitHubWriterError, "repository rejected"):
            GhCliProvider("somewhere/else")

    def test_create_read_round_trip_and_duplicate_is_idempotent(self):
        bus = GitHubTaskBus(FakeProvider())
        first = bus.create("TASK-001", "Task", "CODEX", "Do work", ["pass"])
        second = bus.create("TASK-001", "Task again", "CODEX", "Do other work")
        self.assertTrue(first["created"]); self.assertFalse(second["created"])
        self.assertEqual(first["issue"]["number"], second["issue"]["number"])
        self.assertEqual(bus.read(1)["task"]["objective"], "Do work")

    def test_exact_owner_search_does_not_claim_wrong_owner(self):
        provider = FakeProvider(); bus = GitHubTaskBus(provider)
        bus.create("TASK-001", "One", "CODEX", "a")
        bus.create("TASK-002", "Two", "MAC_CODEX", "b")
        self.assertEqual([x["task"]["task_id"] for x in bus.search_owner("MAC_CODEX")], ["TASK-002"])
        self.assertEqual(bus.read(1)["task"]["current_owner"], "CODEX")

    def test_result_handoff_and_owner_history_preserve_task_id(self):
        provider = FakeProvider(); bus = GitHubTaskBus(provider)
        bus.create("TASK-001", "One", "MAC_CODEX", "a")
        updated = bus.update(1, "REVIEW", "GPT")
        self.assertEqual(updated["task"]["task_id"], "TASK-001")
        self.assertEqual(updated["task"]["owner_history"], ["MAC_CODEX", "GPT"])
        bus.comment(1, "HANDOFF", "next: verify"); bus.comment(1, "RESULT", "ok")
        self.assertTrue(all("task_id: TASK-001" in text for text in provider.comments))
        decoded, _ = decode_task(provider.issues[0]["body"])
        self.assertEqual(decoded["task_id"], "TASK-001")

    def test_metadata_marker_round_trip(self):
        record = {"task_id":"TASK-001","status":"READY","current_owner":"CODEX","owner_history":["CODEX"]}
        decoded, _ = decode_task(encode_task(record, "hello")); self.assertEqual(decoded, record)


if __name__ == "__main__": unittest.main()

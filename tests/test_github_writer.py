import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
from github_writer import (BRIDGE_AGENTS, DEFAULT_REPOSITORY, GhCliProvider,
                           GitHubTaskBus, GitHubWriterError, decode_task,
                           encode_task, parse_task_packet)


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
    def sticky_packet(self, task_id="TASK-001", owner="MAC_CODEX", target="MAC"):
        return {"protocol_version":"2.0", "task_id":task_id, "title":"Task",
                "status":"READY", "current_owner":owner, "objective":"Do work",
                "acceptance_criteria":["pass"], "assignment_mode":"STICKY",
                "handoff_policy":"USER_EXPLICIT_ONLY",
                "cross_agent_read":"DENY_BY_DEFAULT", "execution_target":target}

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
        packet=self.sticky_packet(); bus.ingest(packet,"MAC")
        with self.assertRaisesRegex(GitHubWriterError, "USER_OVERRIDE"):
            bus.update(1, "REVIEW", "GPT")
        updated = bus.update(1, "REVIEW", "GPT", user_override=True)
        self.assertEqual(updated["task"]["task_id"], "TASK-001")
        self.assertEqual(updated["task"]["owner_history"], ["MAC_CODEX", "GPT"])
        bus.comment(1, "HANDOFF", "next: verify"); bus.comment(1, "RESULT", "ok")
        self.assertTrue(all("task_id: TASK-001" in text for text in provider.comments))
        decoded, _ = decode_task(provider.issues[0]["body"])
        self.assertEqual(decoded["task_id"], "TASK-001")

    def test_metadata_marker_round_trip(self):
        record = {"protocol_version":"2.0","task_id":"TASK-001","status":"READY","current_owner":"CODEX","owner_history":["CODEX"],"objective":"legacy"}
        decoded, _ = decode_task(encode_task(record, "hello")); self.assertEqual(decoded, record)

    def test_eight_agent_exact_owner_isolation(self):
        provider=FakeProvider(); bus=GitHubTaskBus(provider)
        self.assertEqual(len(BRIDGE_AGENTS),8)
        for index,owner in enumerate(sorted(BRIDGE_AGENTS),1):
            target="MAC" if owner.startswith("MAC_") else "WINDOWS" if owner.startswith("WINDOWS_") else owner
            bus.ingest(self.sticky_packet(f"TASK-{index:03}",owner,target),target)
        for owner in BRIDGE_AGENTS:
            found=bus.search_owner(owner); self.assertEqual(len(found),1); self.assertEqual(found[0]["task"]["current_owner"],owner)

    def test_dot_packet_serialization_sticky_owner_and_case(self):
        provider = FakeProvider(); bus = GitHubTaskBus(provider)
        packet = self.sticky_packet("DOT-TASK", "dot", "EXPLICIT_TARGET")
        parsed = parse_task_packet("GITHUB_TASK_PACKET_READY\ntarget_agent: dot\n"
                                   "execution_target: EXPLICIT_TARGET\ntask_id: DOT-TASK\n"
                                   "next_user_action: Wake dot with the complete packet\n"
                                   "```json\n" + json.dumps(packet) + "\n```")
        self.assertEqual(parsed, packet)
        bus.ingest(parsed, "EXPLICIT_TARGET")
        self.assertEqual(bus.read(1, requester="dot")["task"]["current_owner"], "dot")
        with self.assertRaisesRegex(GitHubWriterError, "cross-agent read denied"):
            bus.read(1, requester="GPT")
        with self.assertRaisesRegex(GitHubWriterError, "USER_OVERRIDE"):
            bus.update(1, "REVIEW", "GPT")
        self.assertEqual(bus.read(1, requester="dot")["task"]["current_owner"], "dot")
        value = bus.update(1, "REVIEW", "GPT", user_override=True)
        self.assertEqual(value["task"]["owner_history"], ["dot", "GPT"])
        self.assertEqual(value["task"]["task_id"], "DOT-TASK")
        for alias in ("DOT", "Dot"):
            with self.subTest(alias=alias), self.assertRaises(GitHubWriterError):
                bus.ingest(self.sticky_packet("BAD-CASE", alias), "MAC")

    def test_sticky_owner_continuation_and_duplicate_conflict(self):
        provider=FakeProvider(); bus=GitHubTaskBus(provider); packet=self.sticky_packet()
        first=bus.ingest(packet,"MAC"); second=bus.ingest(packet,"MAC")
        self.assertTrue(first["created"]); self.assertFalse(second["created"])
        changed=dict(packet,current_owner="MAC_CLAUDE")
        with self.assertRaisesRegex(GitHubWriterError,"conflicts"):
            bus.ingest(changed,"MAC")

    def test_reviewer_read_does_not_take_over(self):
        provider=FakeProvider(); bus=GitHubTaskBus(provider); bus.ingest(self.sticky_packet(),"MAC")
        with self.assertRaisesRegex(GitHubWriterError,"cross-agent read denied"):
            bus.read(1,requester="GPT")
        value=bus.read(1,requester="GPT",authorized_task_id="TASK-001")
        self.assertEqual(value["task"]["current_owner"],"MAC_CODEX")

    def test_user_override_takeover_records_history(self):
        provider=FakeProvider(); bus=GitHubTaskBus(provider); bus.ingest(self.sticky_packet(),"MAC")
        value=bus.update(1,"IN_PROGRESS","MAC_CLAUDE",user_override=True)
        self.assertEqual(value["task"]["owner_history"],["MAC_CODEX","MAC_CLAUDE"])

    def test_wrong_execution_target_refused(self):
        bus=GitHubTaskBus(FakeProvider())
        with self.assertRaisesRegex(GitHubWriterError,"execution_target"):
            bus.ingest(self.sticky_packet(target="WINDOWS"),"MAC")

    def test_no_drive_normal_flow_packet_to_issue_readback(self):
        provider=FakeProvider(); bus=GitHubTaskBus(provider)
        created=bus.ingest(self.sticky_packet("TASK-NODRIVE"),"MAC")
        self.assertTrue(created["created"])
        self.assertEqual(bus.read(created["issue"]["number"])["task"]["task_id"],"TASK-NODRIVE")
        self.assertFalse(hasattr(bus,"drive"))

    def test_chatgpt_ready_block_ingests_and_raw_json_remains_compatible(self):
        packet=self.sticky_packet("TASK-PACKET")
        block=("GITHUB_TASK_PACKET_READY\n"
               "target_agent: MAC_CODEX\n"
               "execution_target: MAC\n"
               "task_id: TASK-PACKET\n"
               "next_user_action: Mac Codex 继续执行 TASK-PACKET\n"
               "```json\n"+json.dumps(packet,indent=2)+"\n```\n")
        self.assertEqual(parse_task_packet(block),packet)
        self.assertEqual(parse_task_packet("GITHUB_TASK_PACKET_READY\n```json\n"+json.dumps(packet)+"\n```"),packet)
        self.assertEqual(parse_task_packet(json.dumps(packet)),packet)
        bus=GitHubTaskBus(FakeProvider())
        created=bus.ingest(parse_task_packet(block),"MAC")
        self.assertTrue(created["created"])
        self.assertEqual(bus.read(1,requester="MAC_CODEX")["task"]["task_id"],"TASK-PACKET")

    def test_ready_block_requires_exact_boundaries_and_unique_json_keys(self):
        packet=json.dumps(self.sticky_packet())
        for source in ("Here is your packet\nGITHUB_TASK_PACKET_READY\n```json\n"+packet+"\n```",
                       "GITHUB_TASK_PACKET_READY\n```json\n"+packet+"\n```\nIssue created",
                       "GITHUB_TASK_PACKET_READY\n```json\n{"+packet[1:-1]+',"task_id":"OTHER"}\n```'):
            with self.subTest(source=source[:35]), self.assertRaises(GitHubWriterError):
                parse_task_packet(source)

    def test_packet_header_must_match_json_owner_target_and_task_id(self):
        packet=self.sticky_packet()
        block=("GITHUB_TASK_PACKET_READY\n"
               "target_agent: MAC_CLAUDE\n"
               "execution_target: MAC\n"
               "task_id: TASK-001\n"
               "next_user_action: Mac Codex 继续执行 TASK-001\n"
               "```json\n"+json.dumps(packet)+"\n```")
        with self.assertRaisesRegex(GitHubWriterError,"target_agent conflicts"):
            parse_task_packet(block)

    def test_new_packet_must_be_ready_and_duplicate_content_must_match(self):
        provider=FakeProvider(); bus=GitHubTaskBus(provider)
        packet=self.sticky_packet()
        with self.assertRaisesRegex(GitHubWriterError,"invalid current_owner"):
            bus.ingest(dict(packet,current_owner=None),"MAC")
        with self.assertRaisesRegex(GitHubWriterError,"registered current owner"):
            bus.ingest(dict(packet,current_owner="CODEX"),"MAC")
        with self.assertRaisesRegex(GitHubWriterError,"status must be READY"):
            bus.ingest(dict(packet,status="DONE"),"MAC")
        self.assertEqual(provider.issues,[])
        bus.ingest(packet,"MAC")
        with self.assertRaisesRegex(GitHubWriterError,"conflicts"):
            bus.ingest(dict(packet,objective="different task"),"MAC")


if __name__ == "__main__": unittest.main()

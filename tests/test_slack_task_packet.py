import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
from slack_task_packet import PacketError, apply_event, exact_owner_tasks, parse_event, parse_parent


def parent(owner="MAC_CODEX", **changes):
    task = {"protocol_version": "2.0", "task_id": "BRIDGE-001", "title": "Task",
            "status": "READY", "current_owner": owner, "objective": "Do the work",
            "acceptance_criteria": ["Readback passes"], "assignment_mode": "STICKY",
            "handoff_policy": "USER_EXPLICIT_ONLY", "cross_agent_read": "DENY_BY_DEFAULT",
            "execution_target": "MAC", "transport": "SLACK_FIRST", "slack_channel_id": "C0C6PRGNGLQ"}
    task.update(changes)
    return "bridge-task:v1\n```json\n" + json.dumps(task) + "\n```\n\nDo the work"


class SlackTaskPacketTests(unittest.TestCase):
    def test_parent_round_trip_and_missing_owner_fails(self):
        self.assertEqual(parse_parent(parent())["current_owner"], "MAC_CODEX")
        for owner in (None, "CODEX", ""):
            with self.subTest(owner=owner), self.assertRaises(PacketError):
                parse_parent(parent(owner))

    def test_sticky_schema_and_channel_rejections(self):
        for change in ({"assignment_mode": "AUTO"}, {"handoff_policy": "AUTO"},
                       {"cross_agent_read": "ALLOW"}, {"transport": "DRIVE"},
                       {"slack_channel_id": "C_OTHER"}, {"status": "DONE"}):
            with self.subTest(change=change), self.assertRaises(PacketError):
                parse_parent(parent(**change))

    def test_exact_owner_search(self):
        messages = [parent("MAC_CODEX"), parent("WINDOWS_CODEX", task_id="BRIDGE-002"), "unrelated"]
        self.assertEqual([x["task_id"] for x in exact_owner_tasks(messages, "MAC_CODEX")], ["BRIDGE-001"])
        self.assertEqual([x["task_id"] for x in exact_owner_tasks(messages, "WINDOWS_CODEX")], ["BRIDGE-002"])

    def test_thread_grammar_and_reviewer_cannot_take_over(self):
        for kind in ("CLAIM", "PROGRESS", "RESULT", "HANDOFF", "COUNTEREVIDENCE", "REVIEW", "DONE"):
            body = "to_owner: GPT" if kind == "HANDOFF" else "detail: checked"
            event = parse_event(f"{kind} task_id: BRIDGE-001\n{body}", "BRIDGE-001")
            self.assertEqual(apply_event("MAC_CODEX", event), "MAC_CODEX")
        with self.assertRaises(PacketError):
            parse_event("RESULT task_id: WRONG\ndetail: checked", "BRIDGE-001")

    def test_user_override_requires_out_of_band_verification(self):
        event = parse_event("USER_OVERRIDE task_id: BRIDGE-001\nauthorized_by: USER\nto_owner: GPT", "BRIDGE-001")
        with self.assertRaises(PacketError):
            apply_event("MAC_CODEX", event)
        self.assertEqual(apply_event("MAC_CODEX", event, verified_user_override=True), "GPT")
        with self.assertRaises(PacketError):
            parse_event("USER_OVERRIDE task_id: BRIDGE-001\nto_owner: GPT", "BRIDGE-001")

    def test_duplicate_json_keys_rejected(self):
        message = parent().replace('"task_id": "BRIDGE-001",', '"task_id": "BRIDGE-001", "task_id": "OTHER",')
        with self.assertRaises(PacketError):
            parse_parent(message)

    def test_no_drive_or_github_dependency(self):
        source = (REPO / "scripts/slack_task_packet.py").read_text()
        self.assertNotIn("drive_inbound", source)
        self.assertNotIn("github_writer", source)


if __name__ == "__main__":
    unittest.main()

import json
import sys
import unittest
from pathlib import Path

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO/"scripts"))
from bridge_agents import ALL_AGENTS, BRIDGE_AGENTS, LEGACY_AGENTS


class ProtocolRegistryTests(unittest.TestCase):
    def test_exact_seven_agent_registry_and_legacy_read_aliases(self):
        registry=json.loads((REPO/"state/agent_registry.example.json").read_text())
        self.assertEqual({item["id"] for item in registry["agents"]},BRIDGE_AGENTS)
        self.assertEqual(set(registry["legacy_read_aliases"]),LEGACY_AGENTS)

    def test_task_and_handoff_schemas_accept_current_agents(self):
        task=json.loads((REPO/"protocol/task.schema.json").read_text())
        handoff=json.loads((REPO/"protocol/handoff.schema.json").read_text())
        self.assertTrue(BRIDGE_AGENTS.issubset(task["properties"]["current_owner"]["enum"]))
        self.assertTrue(BRIDGE_AGENTS.issubset(handoff["properties"]["from_agent"]["enum"]))
        self.assertTrue(BRIDGE_AGENTS.issubset(handoff["properties"]["to_agent"]["enum"]))

    def test_docs_route_all_ordinary_tasks_to_slack_without_polling(self):
        readme=(REPO/"README.md").read_text()
        contract=(REPO/"AGENTS.md").read_text()
        entry=(REPO/"protocol/chatgpt-task-entry.md").read_text()
        slack=(REPO/"protocol/slack-tasks.md").read_text()
        self.assertIn("`#agent-bridge-tasks` (`C0C6PRGNGLQ`) is the default task inbox",readme)
        self.assertIn("default ordinary task bus on phone and computer",contract)
        self.assertIn("GitHub Issues fallback",entry)
        self.assertIn("Google Drive is for large artifacts and emergency bootstrap",slack)
        self.assertIn("must not be installed as a recurring poller",readme)
        self.assertNotIn("For continuous polling",readme)


if __name__=="__main__": unittest.main()

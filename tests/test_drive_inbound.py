import json, os, sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path

REPO=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(REPO/"scripts"))
from drive_inbound import QueueTask, RevisionConflict, accept_task, active_claims, document_text, fingerprint, ready_tasks, run_once, tab_end, utcnow

def doc(text,revision="r1"):
    return {"revisionId":revision,"tabs":[{"tabProperties":{"tabId":"t.0"},"documentTab":{"body":{"content":[
      {"startIndex":1,"endIndex":len(text)+1,"paragraph":{"elements":[{"textRun":{"content":text}}]}},
      {"startIndex":len(text)+1,"endIndex":len(text)+2,"paragraph":{"elements":[{"textRun":{"content":"\n"}}]}}
    ]}}}]}

QUEUE="""TASK QUEUE
TASK: OLD-001
status: READY
current_owner: CODEX
objective: old
RESULT: OLD-001
status: COMPLETED
TASK: ACC-007
status: READY
current_owner: CODEX
objective: Build inbound
"""

class FakeQueue:
    def __init__(self,text=QUEUE,conflict=False): self.snapshot=doc(text); self.conflict=conflict; self.claims=[]
    def fetch(self): return self.snapshot
    def claim(self,snapshot,task,principal,lease_seconds):
        if self.conflict: raise RevisionConflict("changed")
        self.claims.append((task.task_id,task.fingerprint,principal)); return "claim-1"

class DriveInboundTests(unittest.TestCase):
    def test_selects_newest_unclaimed_ready_codex_task(self):
        tasks=ready_tasks(QUEUE); self.assertEqual([task.task_id for task in tasks],["ACC-007"])
    def test_active_claim_blocks_duplicate_but_expired_claim_recovers(self):
        future=(utcnow()+timedelta(minutes=5)).isoformat(); claimed=QUEUE+f"\nCLAIM: ACC-007\nstatus: CLAIMED\nlease_expires_at: {future}\n"
        self.assertEqual(ready_tasks(claimed),[])
        past=(utcnow()-timedelta(minutes=5)).isoformat(); expired=QUEUE+f"\nCLAIM: ACC-007\nstatus: CLAIMED\nlease_expires_at: {past}\n"
        self.assertEqual(ready_tasks(expired)[0].task_id,"ACC-007")
    def test_document_text_and_tab_end(self):
        snapshot=doc(QUEUE); self.assertIn("ACC-007",document_text(snapshot)); end,tab=tab_end(snapshot); self.assertEqual(tab,"t.0"); self.assertEqual(end,len(QUEUE)+1)
    def test_revision_conflict_makes_no_local_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"receipts.jsonl"
            with self.assertRaises(RevisionConflict): run_once(FakeQueue(conflict=True),"codex",path)
            self.assertFalse(path.exists())
    def test_no_ready_task_is_noop(self):
        result=run_once(FakeQueue("TASK: X\nstatus: BLOCKED\ncurrent_owner: CODEX\n"),"codex",Path("/nonexistent/receipt")); self.assertEqual(result["status"],"NO_READY_TASK")
    def test_fingerprint_is_stable_for_line_endings_and_trailing_space(self):
        self.assertEqual(fingerprint("a  \r\nb\r\n"),fingerprint("a\nb"))
    def test_claim_is_imported_through_authenticated_control_plane(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/"state").mkdir(); policy=root/"state/auth_policy.json"
            policy.write_text(json.dumps({"version":1,"principals":{"codex":{"actor":"CODEX","transport":"DRIVE_INBOUND","secret_env":"INBOUND_TEST_SECRET","scopes":["task:create","task:transition:self"]}}}))
            old_config=os.environ.get("BRIDGE_AUTH_CONFIG"); old_secret=os.environ.get("INBOUND_TEST_SECRET")
            os.environ["BRIDGE_AUTH_CONFIG"]=str(policy); os.environ["INBOUND_TEST_SECRET"]="synthetic-test-secret"
            try:
                task=ready_tasks(QUEUE,task_id="ACC-007")[0]; receipt=root/"state/receipts.jsonl"
                self.assertEqual(accept_task(task,"claim-1","codex",receipt,root),"CLAIMED")
                latest=json.loads((root/"state/tasks.jsonl").read_text().splitlines()[-1]); self.assertEqual(latest["status"],"CLAIMED"); self.assertEqual(latest["auth_context"]["principal"],"codex")
                self.assertEqual(accept_task(task,"claim-2","codex",receipt,root),"DUPLICATE")
            finally:
                if old_config is None: os.environ.pop("BRIDGE_AUTH_CONFIG",None)
                else: os.environ["BRIDGE_AUTH_CONFIG"]=old_config
                if old_secret is None: os.environ.pop("INBOUND_TEST_SECRET",None)
                else: os.environ["INBOUND_TEST_SECRET"]=old_secret

if __name__=="__main__": unittest.main()

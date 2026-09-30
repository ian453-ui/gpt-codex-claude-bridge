import hashlib, hmac, json, os, subprocess, sys, tempfile, unittest
from datetime import datetime, timezone
from pathlib import Path

REPO=Path(__file__).resolve().parents[1]; CLI=REPO/"scripts/control_plane.py"
sys.path.insert(0,str(REPO/"scripts"))
from bridge_auth import AuthError, resolve_secret

class ControlPlaneAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name); (self.root/"state").mkdir()
        self.secrets={"CODEX_SECRET":"codex-test-secret","MAC_CODEX_SECRET":"mac-codex-test-secret","CLAUDE_SECRET":"claude-test-secret","VERIFY_SECRET":"verify-test-secret"}
        self.policy={"version":1,"policy_id":"tests","max_clock_skew_seconds":300,"principals":{
          "codex":{"actor":"CODEX","transport":"LOCAL_CLI","secret_env":"CODEX_SECRET","credential_ref":"env:CODEX_SECRET","scopes":["project:init","task:create","task:transition:self","task:handoff:self","task:log:self"]},
          "mac-codex":{"actor":"MAC_CODEX","transport":"LOCAL_CLI","secret_env":"MAC_CODEX_SECRET","credential_ref":"env:MAC_CODEX_SECRET","scopes":["task:create","task:transition:self","task:handoff:self","task:log:self"]},
          "claude":{"actor":"CLAUDE","transport":"CLAUDE_BRIDGE","secret_env":"CLAUDE_SECRET","credential_ref":"env:CLAUDE_SECRET","scopes":["task:transition:self","task:handoff:self","task:log:self"]},
          "verifier":{"actor":"HUMAN","transport":"LOCAL_CLI","secret_env":"VERIFY_SECRET","credential_ref":"env:VERIFY_SECRET","scopes":["task:verify","task:transition:any"]}}}
        self.policy_path=self.root/"state/auth_policy.json"; self.policy_path.write_text(json.dumps(self.policy))
        self.env=os.environ|self.secrets|{"BRIDGE_CONTROL_PLANE_ROOT":str(self.root),"BRIDGE_AUTH_CONFIG":str(self.policy_path)}
        self.call("codex","init",{"project_id":"TEST"},"--project-id","TEST")
    def tearDown(self): self.tmp.cleanup()
    def signature(self,principal,action,payload,timestamp,nonce):
        record=self.policy["principals"][principal]; body=json.dumps(payload,sort_keys=True,separators=(",",":"))
        message="\n".join(("bridge-auth-v1",action,principal,record["transport"],timestamp,nonce,body))
        return hmac.new(self.secrets[record["secret_env"]].encode(),message.encode(),hashlib.sha256).hexdigest()
    def call(self,principal,action,payload,*args,signature=None,check=True):
        ts=datetime.now(timezone.utc).isoformat(); nonce=f"nonce-{action}-{os.urandom(5).hex()}"; record=self.policy["principals"][principal]
        sig=signature or self.signature(principal,action,payload,ts,nonce)
        cmd=[sys.executable,str(CLI),action,*args,"--auth-principal",principal,"--auth-transport",record["transport"],"--auth-timestamp",ts,"--auth-nonce",nonce,"--auth-signature",sig]
        return subprocess.run(cmd,env=self.env,text=True,capture_output=True,check=check)
    def create(self): return self.call("codex","create",{"task_id":"T1","owner":"CODEX","objective":"test","acceptance":[]},"--task-id","T1","--owner","CODEX","--objective","test")
    def state_bytes(self): return b"".join(p.read_bytes() for p in sorted(self.root.rglob("*")) if p.is_file() and p!=self.policy_path)

    def test_valid_codex_identity_can_create_and_transition(self):
        self.create(); self.call("codex","transition",{"task_id":"T1","status":"IN_PROGRESS","owner":"CODEX"},"--task-id","T1","--status","IN_PROGRESS","--owner","CODEX")
        self.assertIn('"status": "IN_PROGRESS"',subprocess.check_output([sys.executable,str(CLI),"resolve","--task-id","T1"],env=self.env,text=True))

    def test_registered_mac_codex_identity_can_create_its_own_task(self):
        result=self.call("mac-codex","create",{"task_id":"M1","owner":"MAC_CODEX","objective":"test","acceptance":[]},"--task-id","M1","--owner","MAC_CODEX","--objective","test")
        self.assertEqual(result.returncode,0)
        self.assertEqual(json.loads(result.stdout)["current_owner"],"MAC_CODEX")
    def test_invalid_authentication_does_not_mutate_state(self):
        before=self.state_bytes(); result=self.call("codex","create",{"task_id":"T1","owner":"CODEX","objective":"test","acceptance":[]},"--task-id","T1","--owner","CODEX","--objective","test",signature="0"*64,check=False)
        self.assertNotEqual(result.returncode,0); self.assertEqual(before,self.state_bytes())
    def test_codex_cannot_impersonate_chatgpt(self):
        self.create(); before=self.state_bytes(); payload={"task_id":"T1","from_agent":"CHATGPT","to_agent":"CLAUDE","status":"IN_PROGRESS","summary":"x","next_action":"y"}
        result=self.call("codex","handoff",payload,"--task-id","T1","--from-agent","CHATGPT","--to-agent","CLAUDE","--status","IN_PROGRESS","--summary","x","--next-action","y",check=False)
        self.assertNotEqual(result.returncode,0); self.assertEqual(before,self.state_bytes())
    def test_claude_cannot_mutate_codex_owned_task(self):
        self.create(); before=self.state_bytes(); result=self.call("claude","transition",{"task_id":"T1","status":"IN_PROGRESS","owner":"CLAUDE"},"--task-id","T1","--status","IN_PROGRESS","--owner","CLAUDE",check=False)
        self.assertNotEqual(result.returncode,0); self.assertEqual(before,self.state_bytes())
    def test_codex_to_claude_handoff_then_claude_acts(self):
        self.create(); payload={"task_id":"T1","from_agent":"CODEX","to_agent":"CLAUDE","status":"IN_PROGRESS","summary":"delegated","next_action":"act"}
        self.call("codex","handoff",payload,"--task-id","T1","--from-agent","CODEX","--to-agent","CLAUDE","--status","IN_PROGRESS","--summary","delegated","--next-action","act")
        self.call("claude","transition",{"task_id":"T1","status":"REVIEW","owner":"CLAUDE"},"--task-id","T1","--status","REVIEW","--owner","CLAUDE")
    def test_actor_without_verify_permission_cannot_finish(self):
        self.create(); self.call("codex","transition",{"task_id":"T1","status":"REVIEW","owner":"CODEX"},"--task-id","T1","--status","REVIEW","--owner","CODEX")
        before=self.state_bytes(); result=self.call("codex","verify",{"task_id":"T1"},"--task-id","T1",check=False); self.assertNotEqual(result.returncode,0); self.assertEqual(before,self.state_bytes())
    def test_authorized_verifier_can_finish(self):
        self.create(); self.call("codex","transition",{"task_id":"T1","status":"REVIEW","owner":"CODEX"},"--task-id","T1","--status","REVIEW","--owner","CODEX")
        result=self.call("verifier","verify",{"task_id":"T1"},"--task-id","T1"); self.assertTrue(json.loads(result.stdout)["verified"])
    def test_sensitive_material_is_never_persisted(self):
        self.create(); persisted=self.state_bytes().decode(errors="ignore")
        for secret in self.secrets.values(): self.assertNotIn(secret,persisted)
        self.assertNotIn("signature",persisted); self.assertIn("credential_ref",persisted)
    def test_resolve_continue_behavior_remains(self):
        self.create(); result=subprocess.run([sys.executable,str(CLI),"resolve"],env=self.env,text=True,capture_output=True,check=True); self.assertEqual(json.loads(result.stdout)["task_id"],"T1")
    def test_invalid_transport_and_policy_fail_closed(self):
        self.create(); before=self.state_bytes(); ts=datetime.now(timezone.utc).isoformat(); nonce="bad-transport"; payload={"task_id":"T1","status":"IN_PROGRESS","owner":"CODEX"}
        sig=self.signature("codex","transition",payload,ts,nonce); cmd=[sys.executable,str(CLI),"transition","--task-id","T1","--status","IN_PROGRESS","--owner","CODEX","--auth-principal","codex","--auth-transport","WRONG","--auth-timestamp",ts,"--auth-nonce",nonce,"--auth-signature",sig]
        result=subprocess.run(cmd,env=self.env,text=True,capture_output=True); self.assertNotEqual(result.returncode,0); self.assertEqual(before,self.state_bytes())

    def test_nonce_replay_fails_closed(self):
        payload={"task_id":"T1","owner":"CODEX","objective":"test","acceptance":[]}; ts=datetime.now(timezone.utc).isoformat(); nonce="one-time-nonce"
        record=self.policy["principals"]["codex"]; sig=self.signature("codex","create",payload,ts,nonce)
        cmd=[sys.executable,str(CLI),"create","--task-id","T1","--owner","CODEX","--objective","test","--auth-principal","codex","--auth-transport",record["transport"],"--auth-timestamp",ts,"--auth-nonce",nonce,"--auth-signature",sig]
        subprocess.run(cmd,env=self.env,text=True,capture_output=True,check=True); before=self.state_bytes()
        result=subprocess.run(cmd,env=self.env,text=True,capture_output=True); self.assertNotEqual(result.returncode,0); self.assertEqual(before,self.state_bytes())

    def test_windows_credential_reference_is_resolved_without_environment_secret(self):
        record={"credential_ref":"windows-credential-manager:BRIDGE_CODEX_WINDOWS_SECRET"}
        self.assertEqual(resolve_secret(record,{},lambda target: "private-test-value"),"private-test-value")

    def test_empty_windows_credential_target_fails_closed(self):
        with self.assertRaises(AuthError):
            resolve_secret({"credential_ref":"windows-credential-manager:"},{},lambda target: "unused")

if __name__=="__main__": unittest.main()

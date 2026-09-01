#!/usr/bin/env python3
"""Google Docs TASK_QUEUE -> authenticated local control-plane adapter."""

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bridge_auth import compute_signature, load_policy

REPO=Path(__file__).resolve().parents[1]
DEFAULT_STATE=REPO/"state/drive_inbound_receipts.jsonl"
TASK_START=re.compile(r"(?m)^TASK:\s*([^\s]+)\s*$")
CLAIM_START=re.compile(r"(?m)^CLAIM:\s*([^\s]+)\s*$")
RESULT_START=re.compile(r"(?m)^RESULT:\s*([^\s]+)\s*$")

class InboundError(RuntimeError): pass
class RevisionConflict(InboundError): pass

def utcnow(): return datetime.now(timezone.utc)
def normalize(text): return "\n".join(line.rstrip() for line in text.replace("\r\n","\n").splitlines()).strip()
def fingerprint(text): return hashlib.sha256(normalize(text).encode()).hexdigest()

def fields(block):
    out={}
    for line in block.splitlines():
        match=re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*):\s*(.*)$",line)
        if match and match.group(1) not in out: out[match.group(1)]=match.group(2).strip()
    return out

def blocks(text,pattern):
    matches=list(pattern.finditer(text)); result=[]
    for index,match in enumerate(matches):
        end=matches[index+1].start() if index+1<len(matches) else len(text)
        result.append((match.group(1),text[match.start():end].strip()))
    return result

def active_claims(text,now=None):
    now=now or utcnow(); out={}
    for task_id,block in blocks(text,CLAIM_START):
        data=fields(block); expires=data.get("lease_expires_at")
        try: expiry=datetime.fromisoformat(expires.replace("Z","+00:00")) if expires else None
        except ValueError: expiry=None
        if data.get("status")=="CLAIMED" and (expiry is None or expiry>now): out[task_id]=data
    return out

@dataclass(frozen=True)
class QueueTask:
    task_id:str; block:str; data:dict; fingerprint:str

def ready_tasks(text,task_id=None):
    claimed=active_claims(text); completed={item_id for item_id,_ in blocks(text,RESULT_START)}; found=[]
    for current_id,block in blocks(text,TASK_START):
        data=fields(block)
        if data.get("status")!="READY" or data.get("current_owner")!="CODEX" or current_id in claimed or current_id in completed: continue
        if task_id and current_id!=task_id: continue
        found.append(QueueTask(current_id,block,data,fingerprint(block)))
    return found

def document_text(document):
    parts=[]
    tabs=document.get("tabs") or []
    bodies=[tab.get("documentTab",{}).get("body") or tab.get("body") for tab in tabs] if tabs else [document.get("body")]
    for body in bodies:
        for item in (body or {}).get("content",[]):
            paragraph=item.get("paragraph",{})
            for element in paragraph.get("elements",[]):
                parts.append(element.get("textRun",{}).get("content", ""))
                if element.get("dateElement"): parts.append(element["dateElement"].get("dateElementProperties",{}).get("displayText",""))
    return "".join(parts)

def tab_end(document):
    tabs=document.get("tabs") or []
    if tabs:
        tab=tabs[0]; body=tab.get("documentTab",{}).get("body") or tab.get("body") or {}; tab_id=tab.get("tabProperties",{}).get("tabId") or tab.get("tabId")
    else: body=document.get("body") or {}; tab_id=None
    content=body.get("content",[])
    if not content: raise InboundError("Google Doc has no writable body")
    return content[-1]["endIndex"]-1,tab_id

class GoogleDocsQueue:
    def __init__(self,document_id,token_env="GOOGLE_DRIVE_ACCESS_TOKEN",token_command=None):
        self.document_id=document_id; self.token_env=token_env; self.token_command=token_command
    def token(self):
        value=os.environ.get(self.token_env)
        if not value and self.token_command:
            value=subprocess.run(self.token_command,shell=True,text=True,capture_output=True,check=True).stdout.strip()
        if not value: raise InboundError(f"Google access token unavailable; set {self.token_env} or --token-command")
        return value
    def request(self,url,method="GET",body=None):
        data=json.dumps(body).encode() if body is not None else None
        req=urllib.request.Request(url,data=data,method=method,headers={"Authorization":f"Bearer {self.token()}","Content-Type":"application/json"})
        try:
            with urllib.request.urlopen(req,timeout=30) as response: return json.load(response)
        except urllib.error.HTTPError as exc:
            detail=exc.read().decode(errors="replace")
            if exc.code==400 and ("requiredRevisionId" in detail or "revision" in detail.lower()): raise RevisionConflict("queue revision changed during claim") from exc
            raise InboundError(f"Google Docs API {exc.code}: {detail[:500]}") from exc
    def fetch(self):
        url=f"https://docs.googleapis.com/v1/documents/{urllib.parse.quote(self.document_id)}?includeTabsContent=true"
        return self.request(url)
    def claim(self,snapshot,task,principal,lease_seconds):
        claim_id=f"claim-{secrets.token_hex(8)}"; now=utcnow(); end,tab_id=tab_end(snapshot)
        text=(f"\nCLAIM: {task.task_id}\nclaim_id: {claim_id}\nstatus: CLAIMED\nclaimed_by: CODEX\n"
          f"transport_principal: {principal}\ntask_fingerprint: {task.fingerprint}\nclaimed_at: {now.isoformat()}\n"
          f"lease_expires_at: {(now+timedelta(seconds=lease_seconds)).isoformat()}\n")
        location={"index":end}
        if tab_id: location["tabId"]=tab_id
        body={"requests":[{"insertText":{"location":location,"text":text}}],"writeControl":{"requiredRevisionId":snapshot["revisionId"]}}
        url=f"https://docs.googleapis.com/v1/documents/{urllib.parse.quote(self.document_id)}:batchUpdate"
        self.request(url,"POST",body); return claim_id

def receipts(path):
    result={}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip(): item=json.loads(line); result[(item["task_id"],item["task_fingerprint"])]=item
    return result

def append_receipt(path,item):
    path.parent.mkdir(parents=True,exist_ok=True); fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
    try: os.write(fd,(json.dumps(item,separators=(",",":"))+"\n").encode())
    finally: os.close(fd)

def control_plane(principal,action,payload,args,root=REPO):
    policy=load_policy(); record=policy["principals"].get(principal)
    if not record or record.get("actor")!="CODEX": raise InboundError("inbound principal must be policy-bound to CODEX")
    secret=os.environ.get(record.get("secret_env", ""))
    if not secret: raise InboundError("inbound principal credential unavailable")
    timestamp=utcnow().isoformat(); nonce=f"drive-inbound-{secrets.token_hex(16)}"
    signature=compute_signature(secret,action,payload,principal,record["transport"],timestamp,nonce)
    command=[sys.executable,str(REPO/"scripts/control_plane.py"),action,*args,"--auth-principal",principal,"--auth-transport",record["transport"],"--auth-timestamp",timestamp,"--auth-nonce",nonce,"--auth-signature",signature]
    env=os.environ|{"BRIDGE_CONTROL_PLANE_ROOT":str(root)}
    completed=subprocess.run(command,env=env,text=True,capture_output=True)
    if completed.returncode: raise InboundError(completed.stderr.strip())
    return completed.stdout

def accept_task(task,claim_id,principal,state_path,root=REPO):
    if (task.task_id,task.fingerprint) in receipts(state_path): return "DUPLICATE"
    resolved=subprocess.run([sys.executable,str(REPO/"scripts/control_plane.py"),"resolve","--task-id",task.task_id],env=os.environ|{"BRIDGE_CONTROL_PLANE_ROOT":str(root)},text=True,capture_output=True,check=True)
    current=json.loads(resolved.stdout)
    if current.get("status")=="NO_ACTIVE_TASK":
        objective=task.data.get("objective","Imported from Google Drive TASK_QUEUE")
        payload={"task_id":task.task_id,"owner":"CODEX","objective":objective,"acceptance":[]}
        control_plane(principal,"create",payload,["--task-id",task.task_id,"--owner","CODEX","--objective",objective],root)
    elif current.get("current_owner")!="CODEX":
        raise InboundError("local task exists but is not owned by CODEX")
    elif current.get("status") in {"CLAIMED","IN_PROGRESS","REVIEW","DONE"}:
        append_receipt(state_path,{"task_id":task.task_id,"task_fingerprint":task.fingerprint,"claim_id":claim_id,"accepted_at":utcnow().isoformat(),"status":"ALREADY_LOCAL"})
        return "ALREADY_LOCAL"
    elif current.get("status") not in {"PENDING"}:
        raise InboundError(f"local task is not safely claimable from {current.get('status')}")
    payload={"task_id":task.task_id,"status":"CLAIMED","owner":"CODEX"}
    control_plane(principal,"transition",payload,["--task-id",task.task_id,"--status","CLAIMED","--owner","CODEX"],root)
    append_receipt(state_path,{"task_id":task.task_id,"task_fingerprint":task.fingerprint,"claim_id":claim_id,"accepted_at":utcnow().isoformat(),"status":"CLAIMED"})
    return "CLAIMED"

def run_once(queue,principal,state_path,task_id=None,lease_seconds=600,root=REPO):
    snapshot=queue.fetch(); text=document_text(snapshot); candidates=ready_tasks(text,task_id)
    seen=receipts(state_path); candidates=[task for task in candidates if (task.task_id,task.fingerprint) not in seen]
    if not candidates: return {"status":"NO_READY_TASK"}
    task=candidates[-1]
    claim_id=queue.claim(snapshot,task,principal,lease_seconds)
    status=accept_task(task,claim_id,principal,state_path,root)
    return {"status":status,"task_id":task.task_id,"task_fingerprint":task.fingerprint,"claim_id":claim_id}

def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--document-id",default=os.environ.get("BRIDGE_TASK_QUEUE_DOCUMENT_ID")); parser.add_argument("--principal",required=True)
    parser.add_argument("--token-env",default="GOOGLE_DRIVE_ACCESS_TOKEN"); parser.add_argument("--token-command"); parser.add_argument("--state",type=Path,default=DEFAULT_STATE)
    parser.add_argument("--task-id"); parser.add_argument("--lease-seconds",type=int,default=600); parser.add_argument("--poll-seconds",type=int,default=0)
    args=parser.parse_args()
    if not args.document_id: parser.error("--document-id or BRIDGE_TASK_QUEUE_DOCUMENT_ID is required")
    queue=GoogleDocsQueue(args.document_id,args.token_env,args.token_command)
    while True:
        try: print(json.dumps(run_once(queue,args.principal,args.state,args.task_id,args.lease_seconds),ensure_ascii=False),flush=True)
        except RevisionConflict: print(json.dumps({"status":"REVISION_CONFLICT_RETRY"}),flush=True)
        except (InboundError,subprocess.SubprocessError) as exc: print(json.dumps({"status":"FAILED_CLOSED","error":str(exc)}),file=sys.stderr,flush=True); sys.exit(1)
        if args.poll_seconds<=0: break
        time.sleep(args.poll_seconds)

if __name__=="__main__": main()

#!/usr/bin/env python3
import argparse, json, os, secrets
from datetime import datetime, timezone
from pathlib import Path
from bridge_auth import AuthError, authenticate, authorize, consume_nonce, public_context
from bridge_agents import ALL_AGENTS

ROOT=Path(os.environ.get("BRIDGE_CONTROL_PLANE_ROOT",Path(__file__).resolve().parents[1]))
TASKS,PROJECT=ROOT/"state/tasks.jsonl",ROOT/"state/project_state.json"
LOG,HANDOFFS=ROOT/"logs/execution.jsonl",ROOT/"handoffs"
AGENTS=ALL_AGENTS; STATUSES={"PENDING","CLAIMED","IN_PROGRESS","REVIEW","DONE","BLOCKED","FAILED"}

def now(): return datetime.now(timezone.utc).isoformat()
def atomic_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    with tmp.open("x",encoding="utf-8") as f: json.dump(value,f,ensure_ascii=False,indent=2); f.write("\n"); f.flush(); os.fsync(f.fileno())
    os.replace(tmp,path)
def append(path,value):
    path.parent.mkdir(parents=True,exist_ok=True); fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
    try: os.write(fd,(json.dumps(value,ensure_ascii=False,separators=(",",":"))+"\n").encode())
    finally: os.close(fd)
def latest():
    out={}
    if TASKS.exists():
        for line in TASKS.read_text(encoding="utf-8").splitlines():
            if line.strip(): item=json.loads(line); out[item["task_id"]]=item
    return out
def project(): return json.loads(PROJECT.read_text(encoding="utf-8")) if PROJECT.exists() else {"protocol_version":"2.0","active_project_id":"DEFAULT","active_task_id":None,"updated_at":None}

def event(task_id,status,owner,context,objective=None,acceptance=None,verified=False):
    if status not in STATUSES or owner not in AGENTS: raise ValueError("invalid status or owner")
    old=latest().get(task_id,{})
    item={"protocol_version":"2.0","event_id":f"evt-{secrets.token_hex(8)}","event_at":now(),"task_id":task_id,
      "project_id":old.get("project_id",project().get("active_project_id","DEFAULT")),"status":status,"current_owner":owner,
      "previous_owner":old.get("current_owner"),"title":old.get("title",""),"objective":objective or old.get("objective","Unspecified task"),
      "acceptance_criteria":acceptance if acceptance is not None else old.get("acceptance_criteria",[]),"latest_handoff":old.get("latest_handoff"),
      "verified":verified,"metadata":{},"auth_context":public_context(context)}
    append(TASKS,item); state=project(); state.update(active_task_id=task_id,updated_at=item["event_at"],auth_context=public_context(context)); atomic_json(PROJECT,state); return item

def write_handoff(task_id,source,target,status,summary,next_action,context):
    if source not in AGENTS or target not in AGENTS or status not in STATUSES: raise ValueError("invalid handoff")
    handoff_id=f"handoff-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"
    item={"protocol_version":"2.0","handoff_id":handoff_id,"task_id":task_id,"created_at":now(),"from_agent":source,"to_agent":target,
      "status":status,"summary":summary,"next_action":next_action,"artifacts":[],"acceptance_evidence":[],"auth_context":public_context(context)}
    path=HANDOFFS/f"{task_id}--{handoff_id}.json"; atomic_json(path,item); old=latest().get(task_id)
    if old:
        old=dict(old); old.update(event_id=f"evt-{secrets.token_hex(8)}",event_at=now(),current_owner=target,previous_owner=source,latest_handoff=str(path),auth_context=public_context(context)); append(TASKS,old)
    return item

def auth_args(p):
    p.add_argument("--auth-principal",required=True); p.add_argument("--auth-transport",required=True); p.add_argument("--auth-timestamp",required=True); p.add_argument("--auth-nonce",required=True); p.add_argument("--auth-signature",required=True)
def context_for(action,payload,a):
    auth={"principal":a.auth_principal,"transport":a.auth_transport,"timestamp":a.auth_timestamp,"nonce":a.auth_nonce,"signature":a.auth_signature}
    return authenticate(action,payload,auth)

def main():
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest="cmd",required=True)
    p=sub.add_parser("init"); p.add_argument("--project-id",required=True); auth_args(p)
    p=sub.add_parser("create"); p.add_argument("--task-id",required=True); p.add_argument("--owner",required=True); p.add_argument("--objective",required=True); p.add_argument("--acceptance",action="append",default=[]); auth_args(p)
    p=sub.add_parser("resolve"); p.add_argument("--task-id")
    p=sub.add_parser("verify"); p.add_argument("--task-id",required=True); auth_args(p)
    p=sub.add_parser("transition"); p.add_argument("--task-id",required=True); p.add_argument("--status",required=True); p.add_argument("--owner",required=True); auth_args(p)
    p=sub.add_parser("handoff"); p.add_argument("--task-id",required=True); p.add_argument("--from-agent",required=True); p.add_argument("--to-agent",required=True); p.add_argument("--status",required=True); p.add_argument("--summary",required=True); p.add_argument("--next-action",required=True); auth_args(p)
    p=sub.add_parser("log"); p.add_argument("--task-id",required=True); p.add_argument("--agent",required=True); p.add_argument("--action",required=True); p.add_argument("--summary",required=True); auth_args(p)
    a=ap.parse_args()
    if a.cmd=="resolve":
        tasks=latest(); value=tasks.get(a.task_id or project().get("active_task_id"))
        if not a.task_id and (not value or value.get("status")=="DONE"):
            active=[x for x in tasks.values() if x.get("status")!="DONE"]; value=sorted(active,key=lambda x:x["event_at"])[-1] if active else None
        print(json.dumps(value or {"status":"NO_ACTIVE_TASK"},ensure_ascii=False,indent=2)); return
    payload={k:v for k,v in vars(a).items() if k not in {"cmd","auth_principal","auth_transport","auth_timestamp","auth_nonce","auth_signature"}}
    try:
        context=context_for(a.cmd,payload,a); actor=context["actor"]
        if a.cmd=="init":
            authorize(context,"project:init"); consume_nonce(context,ROOT); atomic_json(PROJECT,{"protocol_version":"2.0","active_project_id":a.project_id,"active_task_id":None,"updated_at":now(),"auth_context":public_context(context)}); print(PROJECT)
        elif a.cmd=="create":
            authorize(context,"task:create")
            if a.owner!=actor and "task:create:any" not in context["scopes"]: raise AuthError("cannot create a task as another actor")
            consume_nonce(context,ROOT)
            print(json.dumps(event(a.task_id,"PENDING",a.owner,context,a.objective,a.acceptance),ensure_ascii=False,indent=2))
        elif a.cmd=="verify":
            authorize(context,"task:verify"); old=latest().get(a.task_id)
            if not old or old["status"]!="REVIEW": raise AuthError("task must be in REVIEW")
            consume_nonce(context,ROOT)
            print(json.dumps(event(a.task_id,"DONE",actor,context,verified=True),ensure_ascii=False,indent=2))
        elif a.cmd=="transition":
            old=latest().get(a.task_id)
            if not old: raise AuthError("unknown task")
            if old["current_owner"]!=actor or a.owner!=actor: authorize(context,"task:transition:any")
            else: authorize(context,"task:transition:self")
            if a.status=="DONE": raise AuthError("use verify to move REVIEW to DONE")
            consume_nonce(context,ROOT)
            print(json.dumps(event(a.task_id,a.status,a.owner,context),ensure_ascii=False,indent=2))
        elif a.cmd=="handoff":
            old=latest().get(a.task_id)
            if not old: raise AuthError("unknown task")
            if a.from_agent!=actor: raise AuthError("authenticated actor cannot impersonate handoff source")
            authorize(context,"task:handoff:self" if old["current_owner"]==actor else "task:handoff:any")
            consume_nonce(context,ROOT)
            print(json.dumps(write_handoff(a.task_id,a.from_agent,a.to_agent,a.status,a.summary,a.next_action,context),ensure_ascii=False,indent=2))
        elif a.cmd=="log":
            if a.agent!=actor: raise AuthError("authenticated actor cannot impersonate log agent")
            authorize(context,"task:log:self"); consume_nonce(context,ROOT); item={"protocol_version":"2.0","event_at":now(),"task_id":a.task_id,"agent":actor,"action":a.action,"summary":a.summary,"auth_context":public_context(context)}; append(LOG,item); print(json.dumps(item,ensure_ascii=False))
    except (AuthError,ValueError) as exc: ap.error(str(exc))

if __name__=="__main__": main()

#!/usr/bin/env python3
"""Authenticated actor binding for the bridge control plane."""

import argparse
import ctypes
import hashlib
import hmac
import json
import os
import time
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

AGENTS = {"HUMAN", "CHATGPT", "WORK", "CODEX", "CLAUDE"}
MAX_CLOCK_SKEW_SECONDS = 300

class AuthError(ValueError):
    pass

WINDOWS_CREDENTIAL_PREFIX = "windows-credential-manager:"

class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
    ]

def read_windows_credential(target):
    """Read one Generic Credential without exposing it to a subprocess."""
    if os.name != "nt":
        raise AuthError("Windows Credential Manager is unavailable on this platform")
    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    credential = ctypes.POINTER(_CREDENTIALW)()
    advapi32.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.POINTER(ctypes.POINTER(_CREDENTIALW))]
    advapi32.CredReadW.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = [ctypes.c_void_p]
    if not advapi32.CredReadW(target, 1, 0, ctypes.byref(credential)):
        raise AuthError(f"Windows credential is unavailable: {target}")
    try:
        item = credential.contents
        raw = ctypes.string_at(item.CredentialBlob, item.CredentialBlobSize)
        try:
            return raw.decode("utf-16-le")
        except UnicodeDecodeError:
            return raw.decode("utf-8")
    finally:
        advapi32.CredFree(credential)

def resolve_secret(record, environ=None, credential_reader=None):
    environ = environ or os.environ
    credential_ref = record.get("credential_ref", "")
    if isinstance(credential_ref, str) and credential_ref.startswith(WINDOWS_CREDENTIAL_PREFIX):
        target = credential_ref[len(WINDOWS_CREDENTIAL_PREFIX):]
        if not target:
            raise AuthError("Windows credential target is empty")
        return (credential_reader or read_windows_credential)(target)
    secret_env = record.get("secret_env")
    secret = environ.get(secret_env, "") if isinstance(secret_env, str) else ""
    if not secret:
        raise AuthError("principal credential is unavailable")
    return secret

def canonical_request(action, payload, principal, transport, timestamp, nonce):
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "\n".join(("bridge-auth-v1", action, principal, transport, timestamp, nonce, body))

def compute_signature(secret, action, payload, principal, transport, timestamp, nonce):
    message = canonical_request(action, payload, principal, transport, timestamp, nonce)
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()

def load_policy(path=None):
    value = path or os.environ.get("BRIDGE_AUTH_CONFIG")
    if not value:
        raise AuthError("BRIDGE_AUTH_CONFIG is required for mutating actions")
    try:
        policy = json.loads(Path(value).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthError(f"cannot load auth policy: {exc}") from exc
    if policy.get("version") != 1 or not isinstance(policy.get("principals"), dict):
        raise AuthError("invalid auth policy")
    return policy

def authenticate(action, payload, auth, policy=None, environ=None, now_epoch=None,
                 credential_reader=None):
    policy = policy or load_policy(); environ = environ or os.environ
    required = ("principal", "transport", "timestamp", "nonce", "signature")
    if not auth or any(not auth.get(key) for key in required):
        raise AuthError("complete authentication context is required")
    record = policy["principals"].get(auth["principal"])
    if not isinstance(record, dict): raise AuthError("unknown principal")
    actor, transport = record.get("actor"), record.get("transport")
    if actor not in AGENTS or transport != auth["transport"]: raise AuthError("principal binding mismatch")
    try: signed_at = datetime.fromisoformat(auth["timestamp"].replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError) as exc: raise AuthError("invalid authentication timestamp") from exc
    if abs((now_epoch if now_epoch is not None else time.time()) - signed_at) > policy.get("max_clock_skew_seconds", MAX_CLOCK_SKEW_SECONDS):
        raise AuthError("authentication timestamp outside allowed clock skew")
    secret_env = record.get("secret_env")
    secret = resolve_secret(record, environ, credential_reader)
    expected = compute_signature(secret, action, payload, auth["principal"], transport, auth["timestamp"], auth["nonce"])
    if not hmac.compare_digest(expected, auth["signature"].lower()): raise AuthError("invalid request signature")
    return {"version":1,"actor":actor,"principal":auth["principal"],"transport":transport,
      "credential_ref":record.get("credential_ref",f"env:{secret_env}"),"policy_id":policy.get("policy_id","local-policy"),
      "scopes":list(record.get("scopes",[])),"request_nonce":auth["nonce"],"verified_at":datetime.now(timezone.utc).isoformat()}

def authorize(context, scope, allow_any_scope=None):
    scopes = set(context.get("scopes", []))
    if scope not in scopes and (not allow_any_scope or allow_any_scope not in scopes): raise AuthError(f"permission denied: {scope}")

def public_context(context): return {key:value for key,value in context.items() if key != "scopes"}

def consume_nonce(context, root):
    """Atomically consume a verified nonce immediately before mutation."""
    nonce_key=f"{context['principal']}\n{context['transport']}\n{context['request_nonce']}".encode()
    nonce_dir=Path(root)/"state/nonces"; nonce_dir.mkdir(parents=True,exist_ok=True)
    path=nonce_dir/hashlib.sha256(nonce_key).hexdigest()
    try:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError as exc:
        raise AuthError("authentication nonce has already been used") from exc
    with os.fdopen(fd,"w",encoding="utf-8") as handle:
        json.dump({"principal":context["principal"],"transport":context["transport"],"verified_at":context["verified_at"]},handle)

def main():
    parser=argparse.ArgumentParser(description="Sign one bridge control-plane request")
    parser.add_argument("--action",required=True); parser.add_argument("--payload-json",required=True)
    parser.add_argument("--principal",required=True); parser.add_argument("--transport",required=True)
    source=parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--secret-env"); source.add_argument("--credential-ref")
    parser.add_argument("--timestamp"); parser.add_argument("--nonce",required=True)
    args=parser.parse_args(); timestamp=args.timestamp or datetime.now(timezone.utc).isoformat()
    try:
        secret=resolve_secret({"secret_env":args.secret_env,"credential_ref":args.credential_ref})
    except AuthError as exc:
        parser.error(str(exc))
    print(compute_signature(secret,args.action,json.loads(args.payload_json),args.principal,args.transport,timestamp,args.nonce))

if __name__ == "__main__": main()

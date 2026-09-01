#!/usr/bin/env python3
"""Provision/check/delete a Windows Generic Credential without printing it."""

import argparse
import ctypes
import os
import secrets
from ctypes import wintypes

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2

class CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
    ]

def api():
    if os.name != "nt": raise SystemExit("Windows Credential Manager is required")
    value=ctypes.WinDLL("Advapi32.dll",use_last_error=True)
    value.CredWriteW.argtypes=[ctypes.POINTER(CREDENTIALW),wintypes.DWORD]; value.CredWriteW.restype=wintypes.BOOL
    value.CredReadW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]; value.CredReadW.restype=wintypes.BOOL
    value.CredDeleteW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD]; value.CredDeleteW.restype=wintypes.BOOL
    value.CredFree.argtypes=[ctypes.c_void_p]
    return value

def provision(target):
    secret=secrets.token_urlsafe(48); raw=secret.encode("utf-16-le"); blob=(ctypes.c_ubyte*len(raw)).from_buffer_copy(raw)
    item=CREDENTIALW(0,CRED_TYPE_GENERIC,target,"gpt-codex-claude-bridge Windows worker",wintypes.FILETIME(),len(raw),blob,CRED_PERSIST_LOCAL_MACHINE,0,None,None,"codex-windows-01")
    if not api().CredWriteW(ctypes.byref(item),0): raise ctypes.WinError(ctypes.get_last_error())

def exists(target):
    value=api(); item=ctypes.POINTER(CREDENTIALW)()
    if not value.CredReadW(target,CRED_TYPE_GENERIC,0,ctypes.byref(item)): return False
    value.CredFree(item); return True

def delete(target):
    if not api().CredDeleteW(target,CRED_TYPE_GENERIC,0): raise ctypes.WinError(ctypes.get_last_error())

def main():
    parser=argparse.ArgumentParser(); parser.add_argument("action",choices=("provision","check","delete")); parser.add_argument("--target",required=True)
    args=parser.parse_args()
    if args.action=="provision": provision(args.target); print(f"credential provisioned: {args.target}")
    elif args.action=="check": print("available" if exists(args.target) else "unavailable"); raise SystemExit(0 if exists(args.target) else 1)
    else: delete(args.target); print(f"credential deleted: {args.target}")

if __name__=="__main__": main()

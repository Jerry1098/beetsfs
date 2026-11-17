#!/usr/bin/python3
"""`beet` without capabilities: beets parses untrusted tags, and only the daemon needs
SYS_ADMIN (mount). Dropping needs no privilege; no-new-privileges keeps the exec'd beet from
getting them back."""
import ctypes
import os
import sys

libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(39, 0, 0, 0, 0) != 1:  # PR_GET_NO_NEW_PRIVS: without it the exec below regains them
    sys.exit("beet: refusing to run without no-new-privileges (compose: security_opt; quadlet: NoNewPrivileges=true)")
header = (ctypes.c_uint32 * 2)(0x20080522, 0)  # _LINUX_CAPABILITY_VERSION_3, this process
empty = (ctypes.c_uint32 * 6)()  # effective, permitted, inheritable; two 32-bit halves each
if libc.capset(header, empty):
    sys.exit(f"beet: capset: {os.strerror(ctypes.get_errno())}")
os.execv("/venv/bin/beet", ["beet", *sys.argv[1:]])

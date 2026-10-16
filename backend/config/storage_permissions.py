"""Protect only the application's dedicated data directory."""
import os
import re
import subprocess
from pathlib import Path

MANAGED_ENTRIES = {"runtime", "generations", "work", "backups", "diagnostics", "state.sqlite3", "state.sqlite3-journal", "state.sqlite3-wal", "state.sqlite3-shm"}


def check_data_directory(root):
    if root.exists() and any(p.name not in MANAGED_ENTRIES for p in root.iterdir()):
        raise ValueError("BOOKMARK_DATA_ROOT must be a dedicated directory; unrelated files were found and left unchanged")


def protect_data_directory(root):
    if os.name != "nt":
        root.chmod(0o700)
        return
    import ctypes
    from ctypes import wintypes
    output = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW).stdout
    match = re.search(r"S-1-(?:\d+-)*\d+", output)
    if not match:
        raise RuntimeError("Cannot identify the storage owner")
    sddl = f"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;{match.group()})"
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = api.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    convert.restype = wintypes.BOOL
    get_dacl = api.GetSecurityDescriptorDacl
    get_dacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
    get_dacl.restype = wintypes.BOOL
    set_security = api.SetNamedSecurityInfoW
    set_security.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    set_security.restype = wintypes.DWORD
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    if not convert(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present, inherited, dacl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
        if not get_dacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(inherited)):
            raise ctypes.WinError(ctypes.get_last_error())
        status = set_security(str(root), 1, 0x80000004, None, None, dacl, None)
        if status:
            raise ctypes.WinError(status)
    finally:
        kernel.LocalFree(descriptor)

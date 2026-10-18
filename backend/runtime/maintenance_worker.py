"""Resource boundary for untrusted ZIP/SQLite restore validation.

This module starts without Django. Limits are installed before importing the
validator, and cover migration subprocesses as well as the validator itself.
"""
import json
import os
import sys

MEMORY_BYTES = 768 * 1024**2
CPU_SECONDS = 300
WALL_SECONDS = 900
_job_handle = None


def apply_limits(*, memory_bytes=MEMORY_BYTES, cpu_seconds=CPU_SECONDS):
    global _job_handle
    if os.name != "nt":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
        return

    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                    ("flags", wintypes.DWORD), ("minimum_working_set", ctypes.c_size_t),
                    ("maximum_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                    ("scheduling", wintypes.DWORD)]

    class IOCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "read_operations", "write_operations", "other_operations", "read_bytes", "write_bytes", "other_bytes")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", BasicLimits), ("io", IOCounters),
                    ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                    ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "Could not create restore resource boundary")
    limits = ExtendedLimits()
    # One cumulative CPU and committed-memory budget includes migration children.
    limits.basic.flags = 0x00000004 | 0x00000200 | 0x00002000
    limits.basic.job_time = cpu_seconds * 10_000_000
    limits.job_memory = memory_bytes
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not (
            kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess())):
        error = ctypes.get_last_error()
        kernel.CloseHandle(job)
        raise OSError(error, "Could not install restore resource boundary")
    _job_handle = job


def main():
    try:
        apply_limits()
    except (OSError, ValueError):
        print('{"error":"RESTORE_LIMITS_UNAVAILABLE"}', flush=True)
        return 1
    from pathlib import Path
    from .maintenance import (_validate_package, _validate_candidate, _validate_database,
                              _manifest_summary, digest, MaintenanceError)
    try:
        request = json.loads(sys.stdin.buffer.read(1024 * 1024))
        expected = request.get("expected_schema")
        if isinstance(expected, list):
            expected = {tuple(key): value for key, value in expected}
        manifest = None
        if request["action"] == "package":
            if request.get("expected_sha256") and digest(request["package"]) != request["expected_sha256"]:
                raise MaintenanceError("RESTORE_PACKAGE_CHANGED")
            manifest = _validate_package(request["package"], request["candidate"], expected_schema=expected,
                                         expanded_limit=request["expanded_limit"])
        elif request["action"] == "candidate":
            manifest = _validate_candidate(request["candidate"], expected_schema=expected, prepare=request.get("prepare", False))
        elif request["action"] == "database":
            _validate_database(Path(request["database"]), expected_schema=expected)
        else:
            raise MaintenanceError("BACKUP_INVALID")
        result = {"ok": True}
        if manifest is not None:
            result["manifest"] = _manifest_summary(manifest)
        print(json.dumps(result), flush=True)
        return 0
    except MaintenanceError as error:
        print(json.dumps({"error": error.code}), flush=True)
    except (MemoryError, RecursionError):
        print('{"error":"RESTORE_RESOURCE_LIMIT"}', flush=True)
    except Exception:
        print('{"error":"BACKUP_INVALID"}', flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())

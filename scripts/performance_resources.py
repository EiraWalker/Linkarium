"""Passive system and website-process measurements; no service control."""
import ctypes
from datetime import datetime, timezone
import os
import ntpath
from pathlib import Path
import time


def _is_service_command(arguments, project_root):
    """Accept exact entrypoints, never a path embedded in some other argument."""
    if len(arguments) < 2:
        return False
    root = ntpath.normcase(ntpath.normpath(str(project_root)))
    script = ntpath.normcase(ntpath.normpath(arguments[1]))
    if script == ntpath.join(root, "backend", "supervisor.py"):
        return True
    return (script == ntpath.join(root, "backend", "manage.py") and len(arguments) >= 3
        and arguments[2] in ("run_web", "run_worker"))


def _windows_roots(project_root, kernel, entries, w):
    # Windows returns a local UNICODE_STRING buffer. Its contents are used only
    # for exact entrypoint matching and are never returned or written to logs.
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    shell = ctypes.WinDLL("shell32", use_last_error=True)

    class UnicodeString(ctypes.Structure):
        _fields_ = [("Length", w.USHORT), ("MaximumLength", w.USHORT), ("Buffer", ctypes.c_void_p)]

    query = ntdll.NtQueryInformationProcess
    query.argtypes = [w.HANDLE, w.ULONG, ctypes.c_void_p, w.ULONG, ctypes.POINTER(w.ULONG)]
    query.restype = w.LONG
    shell.CommandLineToArgvW.argtypes = [w.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(w.LPWSTR)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    roots = {}
    for pid, _, executable in entries:
        if executable.casefold() not in ("python.exe", "pythonw.exe"):
            continue
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            continue
        try:
            required = w.ULONG()
            query(handle, 60, None, 0, ctypes.byref(required))
            if not ctypes.sizeof(UnicodeString) <= required.value <= 131072:
                continue
            buffer = ctypes.create_string_buffer(required.value)
            if query(handle, 60, buffer, len(buffer), ctypes.byref(required)) < 0:
                continue
            value = UnicodeString.from_buffer(buffer)
            address = ctypes.addressof(buffer)
            if (not value.Buffer or value.Length % 2 or value.Length > 65534 or
                    not address <= value.Buffer <= address + len(buffer) - value.Length):
                continue
            command = ctypes.wstring_at(value.Buffer, value.Length // 2)
            count = ctypes.c_int()
            argv = shell.CommandLineToArgvW(command, ctypes.byref(count))
            if not argv:
                continue
            try:
                if _is_service_command([argv[index] for index in range(count.value)], project_root):
                    values = [w.FILETIME() for _ in range(4)]
                    if kernel.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)):
                        roots[pid] = (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime
            finally:
                kernel.LocalFree(argv)
        finally:
            kernel.CloseHandle(handle)
    return roots


class WindowsMetrics:
    def __init__(self, project_root):
        from ctypes import wintypes as w
        self.w = w
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.psapi = ctypes.WinDLL("psapi", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.kernel.OpenProcess.restype = w.HANDLE
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
        self.kernel.CreateToolhelp32Snapshot.restype = w.HANDLE

        class Entry(ctypes.Structure):
            _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", w.DWORD),
                ("cntThreads", w.DWORD), ("th32ParentProcessID", w.DWORD),
                ("pcPriClassBase", w.LONG), ("dwFlags", w.DWORD), ("szExeFile", w.WCHAR * 260)]
        self.Entry = Entry
        self.kernel.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
        self.kernel.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]

        class Memory(ctypes.Structure):
            _fields_ = [("length", w.DWORD), ("load", w.DWORD)] + [(name, ctypes.c_ulonglong) for name in
                ("total_physical", "available_physical", "commit_limit", "commit_available", "total_virtual", "available_virtual", "extended")]
        self.Memory = Memory
        self.kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(Memory)]

        class Counters(ctypes.Structure):
            _fields_ = [("cb", w.DWORD), ("faults", w.DWORD)] + [(name, ctypes.c_size_t) for name in
                ("peak_ws", "working_set", "peak_paged", "paged", "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile", "private_commit")]
        self.Counters = Counters
        self.psapi.GetProcessMemoryInfo.argtypes = [w.HANDLE, ctypes.POINTER(Counters), w.DWORD]
        self.kernel.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
        self.kernel.GetSystemTimes.argtypes = [ctypes.POINTER(w.FILETIME)] * 3
        # Identity comes from the same handle whose command line matched, so a
        # recycled PID cannot become a service root between these two checks.
        self.root_identities = _windows_roots(project_root.resolve(), self.kernel, self._entries(), self.w)
        self.roots = set(self.root_identities)

    def identity(self, pid):
        handle = self.kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            values = [self.w.FILETIME() for _ in range(4)]
            if self.kernel.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)):
                return self._ticks(values[0])
        finally:
            self.kernel.CloseHandle(handle)
        return None

    @staticmethod
    def _ticks(value):
        return (value.dwHighDateTime << 32) | value.dwLowDateTime

    def _entries(self):
        handle = self.kernel.CreateToolhelp32Snapshot(2, 0)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        entries = []
        try:
            entry = self.Entry()
            entry.dwSize = ctypes.sizeof(entry)
            valid = self.kernel.Process32FirstW(handle, ctypes.byref(entry))
            while valid:
                entries.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
                valid = self.kernel.Process32NextW(handle, ctypes.byref(entry))
        finally:
            self.kernel.CloseHandle(handle)
        return entries

    def processes(self):
        parents = {pid: parent for pid, parent, executable in self._entries()}
        identities = {pid: self.root_identities[pid] for pid in self.roots & parents.keys()
            if self.root_identities.get(pid) is not None and self.identity(pid) == self.root_identities[pid]}
        while True:
            children = {}
            for pid, parent in parents.items():
                if parent not in identities or pid in identities:
                    continue
                created = self.identity(pid)
                # A process whose creator PID was later recycled predates that
                # creator's current lifetime and cannot be its real descendant.
                if created is not None and created >= identities[parent]:
                    children[pid] = created
            if not children:
                self._sample_identities = identities
                return set(identities)
            identities.update(children)

    def sample(self):
        memory = self.Memory()
        memory.length = ctypes.sizeof(memory)
        if not self.kernel.GlobalMemoryStatusEx(ctypes.byref(memory)):
            raise ctypes.WinError(ctypes.get_last_error())
        idle, kernel, user = self.w.FILETIME(), self.w.FILETIME(), self.w.FILETIME()
        if not self.kernel.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            raise ctypes.WinError(ctypes.get_last_error())
        system = {"physical_total_bytes": memory.total_physical, "physical_available_bytes": memory.available_physical,
            "commit_limit_bytes": memory.commit_limit, "commit_available_bytes": memory.commit_available,
            "cpu_total_ticks": self._ticks(kernel) + self._ticks(user), "cpu_idle_ticks": self._ticks(idle)}
        processes = []
        for pid in self.processes():
            handle = self.kernel.OpenProcess(0x410, False, pid)
            if not handle:
                continue
            try:
                counters = self.Counters()
                counters.cb = ctypes.sizeof(counters)
                created, ended, kernel, user = (self.w.FILETIME() for _ in range(4))
                if not self.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                    continue
                if not self.kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(ended), ctypes.byref(kernel), ctypes.byref(user)):
                    continue
                if self._ticks(created) != self._sample_identities.get(pid):
                    continue
                processes.append({"pid": pid, "identity": str(self._ticks(created)), "rss_bytes": counters.working_set,
                    "private_commit_bytes": counters.private_commit, "cpu_seconds": (self._ticks(kernel) + self._ticks(user)) / 10_000_000})
            finally:
                self.kernel.CloseHandle(handle)
        return system, processes


class LinuxMetrics:
    def __init__(self, project_root):
        self.roots = set()
        marker = str((project_root / "backend").resolve())
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                arguments = (entry / "cmdline").read_bytes().split(b"\0")
                # Arguments never leave this function; match exact entrypoints.
                if any(value.decode(errors="replace") in (marker + "/supervisor.py", marker + "/manage.py") for value in arguments):
                    self.roots.add(int(entry.name))
            except OSError:
                pass
        self.hz = os.sysconf("SC_CLK_TCK")
        self.root_identities = {}
        for pid in self.roots:
            try:
                self.root_identities[pid] = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
            except OSError:
                pass

    def sample(self):
        mem = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            if key in ("MemTotal", "MemAvailable", "CommitLimit", "Committed_AS"):
                mem[key] = int(value.split()[0]) * 1024
        cpu = list(map(int, Path("/proc/stat").read_text().splitlines()[0].split()[1:]))
        system = {"physical_total_bytes": mem.get("MemTotal"), "physical_available_bytes": mem.get("MemAvailable"),
            "commit_limit_bytes": mem.get("CommitLimit"), "commit_available_bytes": max(0, mem.get("CommitLimit", 0) - mem.get("Committed_AS", 0)),
            "cpu_total_ticks": sum(cpu[:8]), "cpu_idle_ticks": cpu[3] + (cpu[4] if len(cpu) > 4 else 0)}
        entries = {}
        for entry in Path("/proc").iterdir():
            if entry.name.isdigit():
                try:
                    fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                    entries[int(entry.name)] = fields
                except (OSError, ValueError, IndexError):
                    pass
        owned = {pid for pid in self.roots & entries.keys() if entries[pid][19] == self.root_identities.get(pid)}
        while True:
            children = {pid for pid, fields in entries.items() if int(fields[1]) in owned}
            if children <= owned:
                break
            owned.update(children)
        return system, [{"pid": pid, "identity": entries[pid][19], "rss_bytes": int(entries[pid][21]) * os.sysconf("SC_PAGE_SIZE"),
            "private_commit_bytes": None, "cpu_seconds": (int(entries[pid][11]) + int(entries[pid][12])) / self.hz} for pid in owned]


class ResourceSampler:
    def __init__(self, project_root):
        self.started = time.perf_counter()
        self.previous = None
        self.backend = WindowsMetrics(project_root) if os.name == "nt" else LinuxMetrics(project_root)

    def sample(self):
        started = time.perf_counter()
        system, processes = self.backend.sample()
        cpu = None
        app_cpu = None
        if self.previous:
            previous_time, previous_system, previous_processes = self.previous
            total = system["cpu_total_ticks"] - previous_system["cpu_total_ticks"]
            idle = system["cpu_idle_ticks"] - previous_system["cpu_idle_ticks"]
            if total > 0:
                cpu = round(100 * max(0, min(1, 1 - idle / total)), 2)
            old = {(row["pid"], row["identity"]): row for row in previous_processes}
            delta = sum(max(0, row["cpu_seconds"] - old[(row["pid"], row["identity"])]["cpu_seconds"])
                for row in processes if (row["pid"], row["identity"]) in old)
            if processes or previous_processes:
                app_cpu = round(100 * delta / max(.001, started - previous_time) / (os.cpu_count() or 1), 2)
        self.previous = started, system, processes
        return {"at": datetime.now(timezone.utc).isoformat(), "elapsed_ms": round((started - self.started) * 1000, 3),
            "sampling_ms": round((time.perf_counter() - started) * 1000, 3), "system": {**system, "cpu_percent": cpu},
            "application": {"process_count": len(processes), "rss_sum_bytes": sum(row["rss_bytes"] for row in processes),
                "private_commit_sum_bytes": sum(row["private_commit_bytes"] or 0 for row in processes) if os.name == "nt" else None,
                "cpu_percent_of_machine": app_cpu, "processes": processes},
            "scope_note": "Only discovered website service roots and descendants. RSS sum includes shared pages; CPU excludes exited processes between samples."}

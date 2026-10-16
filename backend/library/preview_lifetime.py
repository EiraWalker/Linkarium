"""Linux lifetime guard for the preview renderer's private process session.

Playwright launches Chromium in another process group. Pin and stop verified
descendants before killing them, rather than assuming killpg(renderer) covers
the browser. This is lifecycle cleanup, not a substitute for a cgroup sandbox.
"""
import ctypes
import os
from pathlib import Path
import signal
import sys
import time


def _children(parent):
    children = set()
    for entry in Path(f"/proc/{parent}/task").glob("*/children"):
        try:
            children.update(int(value) for value in entry.read_text().split())
        except (OSError, ValueError):
            pass
    return children


def _status(pid):
    # comm may contain spaces and parentheses; fields after its final ')' are
    # state, ppid, pgrp, session, ... . Never parse whitespace across comm.
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return fields[0], int(fields[1])


def _kill_owned_tree(group_id):
    held = {}
    visited = set()
    try:
        pending = [(pid, group_id) for pid in _children(group_id)]
        while True:
            if not pending:
                # Also rescan after a child exited before it could be pinned.
                # Its detached descendants may now be our direct children.
                pending = [(pid, group_id) for pid in _children(group_id) if pid not in visited]
                if not pending:
                    break
            pid, parent = pending.pop()
            if pid in visited:
                continue
            visited.add(pid)
            descriptor = None
            try:
                descriptor = os.pidfd_open(pid)
                if _status(pid)[1] not in (parent, group_id):
                    continue
                # A pidfd pins signal identity even if a process exits between
                # enumeration and delivery. Stop parents before inspecting their
                # children so they cannot fork new browser processes behind us.
                signal.pidfd_send_signal(descriptor, signal.SIGSTOP)
                held[pid] = descriptor
                descriptor = None
                deadline = time.monotonic() + 0.1
                while _status(pid)[0] not in ("T", "t", "Z", "X") and time.monotonic() < deadline:
                    time.sleep(0.002)
                pending.extend((child, pid) for child in _children(pid))
                # The renderer is a subreaper: concurrent intermediate exits
                # reparent their descendants here instead of losing ownership.
                pending.extend((child, group_id) for child in _children(group_id) if child not in visited)
            except (OSError, ValueError, IndexError):
                pass  # An already-exited descendant needs no signal.
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        for descriptor in reversed(list(held.values())):
            try:
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            except ProcessLookupError:
                pass
    finally:
        for descriptor in held.values():
            os.close(descriptor)
        # Only the validated renderer-owned group, never the worker's group.
        try:
            os.killpg(group_id, signal.SIGKILL)
        finally:
            os._exit(128 + signal.SIGTERM)


def install_parent_death_guard(expected_parent_pid):
    """Install before Playwright starts; return False outside Linux.

    Linux callers must be the main thread of a start_new_session=True child.
    Pass the worker PID captured before Popen, not the child's current getppid().
    Unsupported kernels fail closed before any browser process is started.
    """
    if sys.platform != "linux":
        return False
    pid = os.getpid()
    if not isinstance(expected_parent_pid, int) or isinstance(expected_parent_pid, bool) or expected_parent_pid <= 1:
        raise ValueError("Invalid preview renderer parent PID")
    if os.getpgrp() != pid or os.getsid(0) != pid:
        raise RuntimeError("Preview renderer must own its process group and session")
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise RuntimeError("Preview lifetime guard requires Linux pidfd support")
    # Probe kernel support before launching descendants.
    probe = os.pidfd_open(pid)
    os.close(probe)
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int

    def set_option(option, value):
        if prctl(option, value, 0, 0, 0) != 0:
            errno = ctypes.get_errno()
            raise OSError(errno, os.strerror(errno))

    def terminate(_signal, _frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        _kill_owned_tree(pid)

    signal.signal(signal.SIGTERM, terminate)
    set_option(36, 1)  # PR_SET_CHILD_SUBREAPER
    set_option(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    # prctl does not report a parent which died before registration.
    if os.getppid() != expected_parent_pid:
        terminate(signal.SIGTERM, None)
    return True

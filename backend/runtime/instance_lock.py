"""One process owns a data directory; nested preparation shares that ownership."""
import os
from pathlib import Path
import threading


_held_locks = set()


class InstanceLock:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.file = None
        self._acquisitions = []

    def __enter__(self):
        # PID prevents inherited bookkeeping from bypassing an OS lock after a
        # fork. Thread identity prevents unrelated in-process work from sharing it.
        self._key = (os.getpid(), threading.get_ident(), os.path.normcase(str(self.path)))
        if self._key in _held_locks:
            self._acquisitions.append(False)
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        try:
            if self.file.seek(0, os.SEEK_END) == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise RuntimeError("A supervisor or offline preparation command already owns this data directory") from None
        _held_locks.add(self._key)
        self._acquisitions.append(True)
        return self

    def __exit__(self, *args):
        if not self._acquisitions.pop():
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            _held_locks.discard(self._key)
            self.file.close()
            self.file = None

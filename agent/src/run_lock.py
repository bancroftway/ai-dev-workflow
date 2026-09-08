"""Durable per-thread run lock: refuses to start a headless run for a thread_id that already has
a live process working it, instead of letting two invocations race the same sandbox container,
checkpoint DB, and git branch.

Observed live (2026-09-07): two `run_headless.py --thread <id>` processes ran concurrently against
the same thread -- one a genuine relaunch, one a stale `--thread`-resume queued earlier whose
wrapper shell hadn't spawned it yet when the first cleanup pass looked for running processes. Both
reattached the same Docker container and streamed the same graph thread at once, interleaving their
log output byte-for-byte. A durable lock file, checked BEFORE any of that happens, is a much smaller
and much safer thing to get right than remembering to audit every process tree by hand before every
relaunch.

A plain lock FILE, not a row in the checkpoint sqlite: it must be checkable by a fresh process
before that process ever opens the checkpoint DB, and a lock a crashed/killed process failed to
release (the ordinary case here -- these processes get taskkilled, not asked to shut down cleanly)
must never wedge the thread forever. So the file holds the PID that took it, and a lock whose
recorded PID is no longer alive is reclaimed automatically -- durable does not mean permanent.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterator

_LOCK_DIR = Path(__file__).resolve().parents[1] / "data" / "run_locks"


def _pid_alive(pid: int) -> bool:
    """True if `pid` names a live process on this host.

    Deliberately never `os.kill(pid, 0)` on Windows: CPython's Windows implementation of os.kill
    has no signal-0 special case -- it calls `TerminateProcess(handle, 0)`, which actually KILLS
    the target instead of merely probing it. `tasklist` is the safe, stdlib-reachable check there.
    """
    if sys.platform == "win32":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    return True


class RunAlreadyActive(RuntimeError):
    """Raised when another live process already holds this thread's run lock."""


@contextlib.contextmanager
def acquire_run_lock(thread_id: str) -> Iterator[None]:
    """Exclusive lock for the duration of one process's work on `thread_id`.

    Raises RunAlreadyActive immediately if a live holder already exists -- callers must not
    retry/wait, since the whole point is refusing to start a SECOND copy, not queuing one behind
    the first (a queued second run would just redraft/rebuild whatever the first one is mid-way
    through changing).
    """
    _LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _LOCK_DIR / f"{thread_id}.lock"
    my_pid = os.getpid()
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                holder = int(lock_path.read_text().strip())
            except (ValueError, OSError):
                holder = None
            if holder is not None and holder != my_pid and _pid_alive(holder):
                raise RunAlreadyActive(
                    f"thread {thread_id} is already being worked by pid {holder} -- refusing to "
                    "start a second run against the same sandbox/checkpoint/branch. Stop that "
                    "process first (or wait for it to finish) before resuming this thread again."
                )
            # Stale: the recorded holder is gone (or was never valid). Remove and retry the
            # exclusive create as a loop, not a blind overwrite -- a genuine concurrent acquirer
            # racing this same reclaim still loses fairly, via O_EXCL, on the next iteration.
            with contextlib.suppress(FileNotFoundError):
                lock_path.unlink()
            continue
        else:
            with os.fdopen(fd, "w") as handle:
                handle.write(str(my_pid))
            break
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            lock_path.unlink()


def _demo() -> None:
    """`cd agent && uv run python -m src.run_lock`."""
    import subprocess as _subprocess
    import sys as _sys
    import tempfile
    import time as _time

    global _LOCK_DIR
    real_lock_dir = _LOCK_DIR
    with tempfile.TemporaryDirectory() as tmp:
        _LOCK_DIR = Path(tmp)
        try:
            with acquire_run_lock("t1"):
                assert (Path(tmp) / "t1.lock").read_text() == str(os.getpid())
                with acquire_run_lock("t2"):
                    pass  # a different thread_id is never blocked by t1's lock
            assert not (Path(tmp) / "t1.lock").exists(), "lock must be released on clean exit"

            # A genuinely different, still-alive process holding the lock blocks acquisition.
            other = _subprocess.Popen([_sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                (Path(tmp) / "t1.lock").write_text(str(other.pid))
                try:
                    with acquire_run_lock("t1"):
                        raise AssertionError("expected RunAlreadyActive while other pid is alive")
                except RunAlreadyActive:
                    pass
            finally:
                other.kill()
                other.wait(timeout=10)

            # Stale lock (recorded pid is no longer alive, killed above) is reclaimed rather than
            # blocking forever -- durable does not mean permanent.
            for _ in range(50):
                if not _pid_alive(other.pid):
                    break
                _time.sleep(0.1)
            with acquire_run_lock("t1"):
                assert (Path(tmp) / "t1.lock").read_text() == str(os.getpid())
        finally:
            _LOCK_DIR = real_lock_dir
    print("run_lock self-check: all assertions passed")


if __name__ == "__main__":
    _demo()

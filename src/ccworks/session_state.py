"""Serialize cooperating processes and atomically replace private session files."""

from contextlib import contextmanager
from collections.abc import Mapping
import errno
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any, Iterator

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


@contextmanager
def session_lock(path: Path, *, timeout: float = 60) -> Iterator[None]:
    # Leave the rendezvous file in place so waiters always lock the same inode.
    lock = path.with_name(f"{path.name}.lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if sys.platform == "win32":
                    # Windows permits byte-range locks past the end of the file.
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "ccworks is busy. Wait for the current operation to finish."
                    ) from None
                time.sleep(0.05)
        yield
    finally:
        # Closing releases the OS lock even when the operation raises.
        os.close(fd)


def save_state_atomically(path: Path, state: Mapping[str, Any]) -> None:
    """Replace a session only after the caller has validated it."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    candidate = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".session-",
            suffix=".json",
            dir=path.parent,
            delete=False,
        ) as stream:
            candidate = Path(stream.name)
            os.chmod(candidate, 0o600)
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(candidate, path)
    finally:
        if candidate is not None:
            candidate.unlink(missing_ok=True)

"""Leases: how many agents run on this host at once (design section 10).

`teams.max_local_agents` is a host budget, not a per-repository one, so the
leases sit in the user state folder, `state_home()/leases/`, shared by every
repository's runner. A lease names the process that holds it; one whose process
no longer exists is reclaimed, so a crashed run never keeps a seat. Counting
and taking a seat happen under one file lock, so two runners never both take
the last one.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from tac.runner import UNIX_PERMS_STORE, RunnerError, state_home

LEASES = "leases"
LOCK = ".lock"


@dataclass(frozen=True, slots=True)
class Lease:
    path: Path
    pid: int
    repository: str
    run_id: str
    stage: str
    role: str
    started: str


def leases_dir(environ: Mapping[str, str]) -> Path:
    folder = state_home(environ) / LEASES
    folder.mkdir(parents=True, exist_ok=True, mode=UNIX_PERMS_STORE)
    return folder


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Someone else's process with that pid: alive, not ours to judge.
        return True
    return True


@contextlib.contextmanager
def _locked(folder: Path) -> Iterator[None]:
    with (folder / LOCK).open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def live(folder: Path) -> list[Path]:
    """The leases whose process still runs; the others are removed."""
    found: list[Path] = []
    for path in sorted(folder.glob("*.json")):
        try:
            pid = int(json.loads(path.read_text(encoding="utf-8"))["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            # Unreadable: its holder cannot be judged, so it is reclaimed.
            path.unlink(missing_ok=True)
            continue
        if alive(pid):
            found.append(path)
        else:
            path.unlink(missing_ok=True)
    return found


def acquire(
    environ: Mapping[str, str],
    budget: int,
    *,
    repository: str,
    run_id: str,
    stage: str,
    role: str,
    pid: int | None = None,
) -> Lease:
    """A seat, or a refusal when `budget` agents already run on this host."""
    folder = leases_dir(environ)
    with _locked(folder):
        held = live(folder)
        if len(held) >= budget:
            raise RunnerError(
                f"{len(held)} agents already run on this host, and "
                f"teams.max_local_agents is {budget}; the stage waits for a seat"
            )
        lease = Lease(
            path=folder / f"{uuid.uuid4()}.json",
            pid=os.getpid() if pid is None else pid,
            repository=repository,
            run_id=run_id,
            stage=stage,
            role=role,
            started=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        body = {
            "pid": lease.pid,
            "repository": repository,
            "run_id": run_id,
            "stage": stage,
            "role": role,
            "started": lease.started,
        }
        lease.path.write_text(json.dumps(body, sort_keys=True) + "\n", "utf-8")
        return lease


def release(lease: Lease) -> None:
    lease.path.unlink(missing_ok=True)

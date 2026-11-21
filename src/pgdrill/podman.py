"""Thin wrapper around the podman CLI (host side, stdlib only)."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys

# Override with e.g. PGDRILL_PODMAN="sudo podman" on hosts where rootless networking is limited.
PODMAN = shlex.split(os.environ.get("PGDRILL_PODMAN", "podman"))
VERBOSE = bool(os.environ.get("PGDRILL_VERBOSE"))


class PodmanError(RuntimeError):
    pass


def podman(*args: str, check: bool = True, timeout: float | None = 120, capture: bool = True) -> subprocess.CompletedProcess:
    cmd = [*PODMAN, *args]
    if VERBOSE:
        print("+ " + shlex.join(cmd), file=sys.stderr)
    try:
        proc = subprocess.run(cmd, capture_output=capture, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise PodmanError(f"timeout after {timeout}s: {shlex.join(cmd)}") from exc
    if check and proc.returncode != 0:
        raise PodmanError(f"{shlex.join(cmd)} -> rc={proc.returncode}\n{(proc.stderr or '').strip()}")
    return proc


def out(*args: str, **kw) -> str:
    return podman(*args, **kw).stdout.strip()


def exists(kind: str, name: str) -> bool:
    return podman(kind, "exists", name, check=False).returncode == 0

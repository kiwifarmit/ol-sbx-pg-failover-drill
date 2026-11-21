"""Host side: run workload and probes as containers on the lab network.

The workload runs inside the lab network, not on the host. On macOS the host
reaches containers through a userspace proxy that hides dead peers, so client
failover timings measured from the host would be wrong.
"""

from __future__ import annotations

import time
from pathlib import Path

from .lab import HAPROXY, IMAGE, LABEL, NETWORK, NODES, SRC_DIR
from .podman import exists, out, podman

RUNNER = "pgdrill-runner"
RUNNER_IP = "10.89.77.30"
PROBE_IP = "10.89.77.31"
BASE_DSN = "user=postgres dbname=postgres"


def conninfo(via: str) -> str:
    if via == "haproxy":
        return f"host={HAPROXY[1]} port=5000 {BASE_DSN}"
    if via == "libpq":
        return f"host={','.join(NODES.values())} port=5432 target_session_attrs=read-write {BASE_DSN}"
    raise ValueError(f"unknown routing {via!r}")


def node_conninfo(node: str) -> str:
    return f"host={NODES[node]} port=5432 {BASE_DSN}"


def _base(outdir: Path) -> list[str]:
    return [
        "--label", LABEL, "--network", NETWORK, "--user", "0",
        "-v", f"{SRC_DIR}:/src:ro", "-v", f"{outdir}:/out",
        "-e", "PYTHONPATH=/src", "-e", "PYTHONDONTWRITEBYTECODE=1",
        "--entrypoint", "python", IMAGE,
    ]


def probe(outdir: Path, *args: str, timeout: float = 60) -> str:
    return out("run", "--rm", "--ip", PROBE_IP, *_base(outdir), "-m", "pgdrill.probe", *args, timeout=timeout)


def start_workload(outdir: Path, dsn: str, run_id: str, writers: int, interval: float) -> None:
    if exists("container", RUNNER):
        podman("rm", "-f", "-t", "0", RUNNER)
    (outdir / "stop").unlink(missing_ok=True)
    podman(
        "run", "-d", "--name", RUNNER, "--ip", RUNNER_IP, *_base(outdir),
        "-m", "pgdrill.workload", "--conninfo", dsn, "--run-id", run_id,
        "--writers", str(writers), "--interval", str(interval),
        "--out", "/out/events.jsonl", "--stop-file", "/out/stop",
    )


def now() -> float:
    """Wall clock as seen inside the lab (same clock the workload uses for its events)."""
    return float(out("exec", RUNNER, "python", "-c", "import time;print(repr(time.time()))", timeout=15))


def stop_workload(timeout: float = 60) -> None:
    podman("exec", RUNNER, "touch", "/out/stop", check=False, timeout=15)
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if out("inspect", "-f", "{{.State.Status}}", RUNNER, check=False) != "running":
            break
        time.sleep(0.5)
    rc = out("inspect", "-f", "{{.State.ExitCode}}", RUNNER, check=False)
    logs = podman("logs", RUNNER, check=False).stderr
    podman("rm", "-f", "-t", "0", RUNNER, check=False)
    if rc not in ("0", ""):
        raise RuntimeError(f"workload exited rc={rc}:\n{logs[-2000:]}")

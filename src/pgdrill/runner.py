"""Host side: run workload and probes as containers on the lab's client network.

The workload runs inside the lab, not on the host. On macOS the host reaches
containers through a userspace proxy that hides dead peers, so client failover
timings measured from the host would be wrong.
"""

from __future__ import annotations

import time
from pathlib import Path

from .lab import CLIENT_IPS, CLIENT_NET, HAPROXY, IMAGE, LABEL, SRC_DIR
from .podman import exists, out, podman

RUNNER = "pgdrill-runner"
RUNNER_IP = "10.89.78.30"
PROBE_IP = "10.89.78.31"
BASE_DSN = "user=postgres dbname=postgres"
# Client-side failure detection. These shape every RTO number: with libpq defaults a
# client stuck on a dead or partitioned server can wait for minutes (kernel TCP timeouts).
CLIENT_OPTS = (
    "connect_timeout=2 tcp_user_timeout=3000 keepalives=1 "
    "keepalives_idle=1 keepalives_interval=1 keepalives_count=2"
)
WRITER_INTERVAL_S = 0.005


def conninfo(via: str) -> str:
    if via == "haproxy":
        return f"host={HAPROXY[1]} port=5000 {BASE_DSN} {CLIENT_OPTS}"
    if via == "libpq":
        hosts = ",".join(CLIENT_IPS.values())
        return f"host={hosts} port=5432 target_session_attrs=read-write {BASE_DSN} {CLIENT_OPTS}"
    raise ValueError(f"unknown routing {via!r}")


def node_conninfo(node: str) -> str:
    return f"host={CLIENT_IPS[node]} port=5432 {BASE_DSN}"


def _base(outdir: Path) -> list[str]:
    return [
        "--label", LABEL, "--user", "0",
        "-v", f"{SRC_DIR}:/src:ro", "-v", f"{outdir}:/out",
        "-e", "PYTHONPATH=/src", "-e", "PYTHONDONTWRITEBYTECODE=1",
        "--entrypoint", "python", IMAGE,
    ]


def probe(outdir: Path, *args: str, timeout: float = 60) -> str:
    return out("run", "--rm", "--network", f"{CLIENT_NET}:ip={PROBE_IP}", *_base(outdir),
               "-m", "pgdrill.probe", *args, timeout=timeout)


def start_workload(outdir: Path, dsn: str, run_id: str, writers: int, interval: float = WRITER_INTERVAL_S) -> None:
    remove_workload()
    (outdir / "stop").unlink(missing_ok=True)
    podman(
        "run", "-d", "--name", RUNNER, "--network", f"{CLIENT_NET}:ip={RUNNER_IP}", *_base(outdir),
        "-m", "pgdrill.workload", "--conninfo", dsn, "--run-id", run_id,
        "--writers", str(writers), "--interval", str(interval),
        "--out", "/out/events.jsonl", "--stop-file", "/out/stop",
    )


def now() -> float:
    """Wall clock as seen inside the lab (same clock the workload uses for its events)."""
    return float(out("exec", RUNNER, "python", "-c", "import time;print(repr(time.time()))", timeout=15))


def remove_workload() -> None:
    if exists("container", RUNNER):
        podman("rm", "-f", "-t", "0", RUNNER, check=False)


def stop_workload(timeout: float = 60) -> None:
    """Ask the workload to stop and wait for it to flush and exit. Raises if it doesn't."""
    if not exists("container", RUNNER):
        raise RuntimeError("workload container is gone")
    podman("exec", RUNNER, "touch", "/out/stop", check=False, timeout=15)
    t0 = time.monotonic()
    status = ""
    while time.monotonic() - t0 < timeout:
        status = out("inspect", "-f", "{{.State.Status}}", RUNNER, check=False)
        if status != "running":
            break
        time.sleep(0.5)
    rc = out("inspect", "-f", "{{.State.ExitCode}}", RUNNER, check=False)
    logs = podman("logs", RUNNER, check=False).stderr
    if status == "running":
        # Leave the container for inspection; the next run removes it.
        raise RuntimeError(f"workload did not exit within {timeout:.0f}s (event log may be incomplete)")
    remove_workload()
    if rc not in ("0", ""):
        raise RuntimeError(f"workload exited rc={rc}:\n{logs[-2000:]}")

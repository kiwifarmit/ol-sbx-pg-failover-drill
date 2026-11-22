"""Fault scenarios. Each one injects a fault, drives recovery, and returns the fault time.

The fault time is taken on the lab clock (runner.now()) right before the action, so it is
comparable with the workload's event timestamps. Restore steps (restart, reconnect,
start etcd) run in `finally` blocks, so a failed wait never leaves the lab broken.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import runner
from .lab import CLUSTER_NET, ETCD, NODES, Lab, Mode
from .podman import podman


@dataclass
class Ctx:
    lab: Lab
    mode: Mode
    leader: str
    cluster: dict
    marks: list[tuple[str, float]] = field(default_factory=list)

    def mark(self, label: str) -> float:
        try:
            t = runner.now()
        except Exception:  # noqa: BLE001 - the runner may be gone during cleanup
            t = time.time()
        self.marks.append((label, t))
        return t

    @property
    def replicas(self) -> list[str]:
        return [n for n in NODES if n != self.leader]

    def sync_standby(self) -> str | None:
        for m in self.cluster.get("members", []):
            if m.get("role") == "sync_standby":
                return m["name"]
        return None

    def timer(self, key: str, default: int) -> int:
        return ((self.lab.state() or {}).get("timers") or {}).get(key, default)


@dataclass(frozen=True)
class Scenario:
    name: str
    summary: str
    oracle: str
    run: Callable[[Ctx], float]


def _new_leader(ctx: Ctx, timeout: float = 120) -> str:
    lead, _ = ctx.lab.wait_leader(exclude=ctx.leader, timeout=timeout)
    ctx.mark(f"new_leader:{lead}")
    return lead


def _start(ctx: Ctx, *names: str) -> None:
    podman("start", *names, check=False, timeout=60)
    ctx.mark("restarted:" + ",".join(names))


def kill_primary(ctx: Ctx) -> float:
    t = ctx.mark("fault:kill -9 " + ctx.leader)
    podman("kill", "-s", "KILL", ctx.leader)
    try:
        _new_leader(ctx)
    finally:
        _start(ctx, ctx.leader)
    return t


def stop_primary(ctx: Ctx) -> float:
    t = ctx.mark("fault:stop " + ctx.leader)
    podman("stop", "-t", "60", ctx.leader, timeout=90)
    try:
        _new_leader(ctx)
    finally:
        _start(ctx, ctx.leader)
    return t


def switchover(ctx: Ctx) -> float:
    candidate = ctx.sync_standby() or ctx.replicas[0]
    t = ctx.mark(f"fault:switchover {ctx.leader}->{candidate}")
    podman(
        "exec", ctx.leader, "patronictl", "-c", "/etc/patroni.yml", "switchover",
        "--leader", ctx.leader, "--candidate", candidate, "--force", timeout=90,
    )
    _new_leader(ctx)
    return t


def partition_primary(ctx: Ctx) -> float:
    """Cut the primary off from etcd and its peers. Clients can still reach it."""
    t = ctx.mark("fault:partition " + ctx.leader + " from cluster network")
    podman("network", "disconnect", CLUSTER_NET, ctx.leader)
    try:
        _new_leader(ctx)
        time.sleep(5)
    finally:
        podman("network", "connect", "--ip", NODES[ctx.leader], CLUSTER_NET, ctx.leader, check=False)
        ctx.mark("healed:" + ctx.leader)
    return t


def dcs_loss(ctx: Ctx) -> float:
    t = ctx.mark("fault:kill etcd")
    podman("kill", "-s", "KILL", ETCD[0])
    try:
        time.sleep(ctx.timer("ttl", 20) + ctx.timer("loop_wait", 5) + 5)
    finally:
        podman("start", ETCD[0], check=False)
        ctx.lab.wait_etcd()
        ctx.mark("etcd_back")
    return t


def lsn_to_int(lsn: str) -> int:
    """'16/B374D848' -> absolute WAL position in bytes."""
    hi, lo = lsn.strip().split("/")
    return (int(hi, 16) << 32) | int(lo, 16)


def _lsn(node: str, fn: str) -> int | None:
    proc = podman("exec", node, "psql", "-h", "127.0.0.1", "-U", "postgres", "-XAtqc", f"select {fn}()",
                  check=False, timeout=10)
    try:
        return lsn_to_int(proc.stdout) if proc.returncode == 0 else None
    except ValueError:  # empty (NULL) or unexpected output
        return None


def _standby_lag(ctx: Ctx, reps: list[str]) -> str:
    """How far each standby is behind the primary, right now, in kB.

    The queries run in parallel: each `podman exec` takes a few hundred ms, and every one
    of them would otherwise make the cut (and the lag) longer.
    """
    with ThreadPoolExecutor(max_workers=1 + len(reps)) as pool:
        primary_f = pool.submit(_lsn, ctx.leader, "pg_current_wal_lsn")
        received_f = {r: pool.submit(_lsn, r, "pg_last_wal_receive_lsn") for r in reps}
        primary = primary_f.result()
        received = {r: f.result() for r, f in received_f.items()}
    parts = []
    for r in reps:
        lag = None if primary is None or received[r] is None else max(0, primary - received[r]) // 1024
        parts.append(f"{r}={'unknown' if lag is None else f'{lag}kB'}")
    limit = ctx.timer("maximum_lag_on_failover", 1048576) // 1024
    return ",".join(parts) + f" (limit {limit}kB)"


# How long the standbys stay cut off before the primary is killed. Short enough that the lag
# Patroni sees stays well under the default maximum_lag_on_failover at lab write rates.
LAG_WINDOW_S = 2


def lagging_replicas_failover(ctx: Ctx) -> float:
    """Cut both standbys off from the cluster network, so WAL stops reaching them, then kill
    the primary. (Pausing the standbys is not enough: the kernel keeps receiving WAL into
    their socket buffers and they catch up on unpause.) Both outcomes are legitimate and get
    recorded in the marks:

    - a standby is promoted and whatever it never received is lost (async), or
    - no standby is promoted because Patroni sees them more than maximum_lag_on_failover
      behind (its equivalent of FastStartFailoverLagLimit). Then the old primary comes back,
      keeps its data, and the outage lasts until it does.
    """
    reps = ctx.replicas
    cut = []
    try:
        for r in reps:
            podman("network", "disconnect", CLUSTER_NET, r)
            cut.append(r)
        ctx.mark("standbys_cut_off:" + ",".join(reps))
        time.sleep(LAG_WINDOW_S)
        ctx.mark("standby_lag_at_kill:" + _standby_lag(ctx, reps))
        t = ctx.mark("fault:kill -9 " + ctx.leader)
        podman("kill", "-s", "KILL", ctx.leader)
    finally:
        for r in cut:
            podman("network", "connect", "--ip", NODES[r], CLUSTER_NET, r, check=False)
        ctx.mark("standbys_reconnected")
    window = ctx.timer("ttl", 20) + 2 * ctx.timer("loop_wait", 5) + 15
    try:
        _new_leader(ctx, timeout=window)
    except TimeoutError:
        logs = [podman("logs", "--since", "3m", r, check=False) for r in reps]
        lag_refused = any("exceeds maximum replication lag" in (p.stdout or "") + (p.stderr or "") for p in logs)
        ctx.mark("no_failover:" + ("standbys beyond maximum_lag_on_failover" if lag_refused else "reason unknown"))
    finally:
        _start(ctx, ctx.leader)
    return t


def replicas_down(ctx: Ctx) -> float:
    reps = ctx.replicas
    t = ctx.mark("fault:stop replicas " + ",".join(reps))
    podman("stop", "-t", "10", *reps, timeout=60)
    try:
        time.sleep(ctx.timer("ttl", 20) + 5)
    finally:
        podman("start", *reps, check=False, timeout=60)
        ctx.mark("replicas_back")
    return t


SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in [
        Scenario("kill-primary", "SIGKILL the primary container, restart it after failover",
                 "Primary instance crash, Fast-Start Failover", kill_primary),
        Scenario("stop-primary", "Clean shutdown of the primary, restart it after failover",
                 "Primary shutdown with FSFO enabled", stop_primary),
        Scenario("switchover", "Planned switchover to a (sync) standby via patronictl",
                 "Broker SWITCHOVER", switchover),
        Scenario("partition-primary",
                 "Cut the primary off from etcd and its peers (clients can still reach it), heal after failover",
                 "Primary isolated from observer and standbys", partition_primary),
        Scenario("dcs-loss", "Kill etcd for ttl+loop_wait+5s, then bring it back",
                 "Observer loss", dcs_loss),
        Scenario("lagging-replicas-failover",
                 f"Cut both standbys off from the cluster network for {LAG_WINDOW_S}s (WAL stops reaching "
                 "them), then kill the primary. In sync/strict, cutting off the sync standby also stalls commits",
                 "Failover with unshipped redo / FastStartFailoverLagLimit", lagging_replicas_failover),
        Scenario("replicas-down", "Stop both replicas for ttl+5s, then restart them",
                 "All standbys unreachable", replicas_down),
    ]
}

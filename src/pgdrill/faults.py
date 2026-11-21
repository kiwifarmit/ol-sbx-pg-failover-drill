"""Fault scenarios. Each one injects a fault, drives recovery, and returns the fault time.

The fault time is taken on the lab clock (runner.now()) right before the action, so it is
comparable with the workload's event timestamps. Restore steps (restart, reconnect,
unpause) run in `finally` blocks, so a failed wait never leaves the lab broken.
"""

from __future__ import annotations

import time
from collections.abc import Callable
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


def lagging_replicas_failover(ctx: Ctx) -> float:
    """Both outcomes are legitimate and get recorded in the marks:

    - a standby is promoted and whatever it never received is lost (async), or
    - no standby is promoted because they are more than maximum_lag_on_failover behind
      (Patroni's equivalent of FastStartFailoverLagLimit). Then the old primary comes
      back, keeps its data, and the outage lasts until it does.
    """
    reps = ctx.replicas
    ctx.mark("pause_replicas:" + ",".join(reps))
    podman("pause", *reps)
    ctx.mark("replicas_frozen")
    try:
        time.sleep(3)
        t = ctx.mark("fault:kill -9 " + ctx.leader)
        podman("kill", "-s", "KILL", ctx.leader)
    finally:
        podman("unpause", *reps, check=False)
        ctx.mark("unpause_replicas")
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
                 "Freeze both standbys for 3s (WAL stops being received), then kill the primary. "
                 "In sync/strict, freezing the sync standby also stalls commits",
                 "Failover with unshipped redo / FastStartFailoverLagLimit", lagging_replicas_failover),
        Scenario("replicas-down", "Stop both replicas for ttl+5s, then restart them",
                 "All standbys unreachable", replicas_down),
    ]
}

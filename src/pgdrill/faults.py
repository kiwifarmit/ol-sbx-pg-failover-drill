"""Fault scenarios. Each one injects a fault, drives recovery, and returns the fault time.

The fault time is taken on the lab clock (runner.now()) right before the action, so it is
comparable with the workload's event timestamps.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from . import runner
from .lab import ETCD, NETWORK, NODES, Lab, Mode
from .podman import podman


@dataclass
class Ctx:
    lab: Lab
    mode: Mode
    leader: str
    cluster: dict
    marks: list[tuple[str, float]] = field(default_factory=list)

    def mark(self, label: str) -> float:
        t = runner.now()
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


def kill_primary(ctx: Ctx) -> float:
    t = ctx.mark("fault:kill -9 " + ctx.leader)
    podman("kill", "-s", "KILL", ctx.leader)
    _new_leader(ctx)
    podman("start", ctx.leader)
    ctx.mark("restarted:" + ctx.leader)
    return t


def stop_primary(ctx: Ctx) -> float:
    t = ctx.mark("fault:stop " + ctx.leader)
    podman("stop", "-t", "60", ctx.leader, timeout=90)
    _new_leader(ctx)
    podman("start", ctx.leader)
    ctx.mark("restarted:" + ctx.leader)
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
    t = ctx.mark("fault:partition " + ctx.leader)
    podman("network", "disconnect", NETWORK, ctx.leader)
    _new_leader(ctx)
    time.sleep(5)
    podman("network", "connect", "--ip", NODES[ctx.leader], NETWORK, ctx.leader)
    ctx.mark("healed:" + ctx.leader)
    return t


def dcs_loss(ctx: Ctx) -> float:
    t = ctx.mark("fault:kill etcd")
    podman("kill", "-s", "KILL", ETCD[0])
    st = ctx.lab.state() or {}
    timers = st.get("timers", {})
    time.sleep(timers.get("ttl", 20) + timers.get("loop_wait", 5) + 5)
    podman("start", ETCD[0])
    ctx.lab.wait_etcd()
    ctx.mark("etcd_back")
    return t


def lagging_replicas_failover(ctx: Ctx) -> float:
    reps = ctx.replicas
    ctx.mark("pause_replicas:" + ",".join(reps))
    podman("pause", *reps)
    time.sleep(3)
    t = ctx.mark("fault:kill -9 " + ctx.leader)
    podman("kill", "-s", "KILL", ctx.leader)
    podman("unpause", *reps)
    ctx.mark("unpause_replicas")
    _new_leader(ctx)
    podman("start", ctx.leader)
    ctx.mark("restarted:" + ctx.leader)
    return t


def replicas_down(ctx: Ctx) -> float:
    reps = ctx.replicas
    t = ctx.mark("fault:stop replicas " + ",".join(reps))
    podman("stop", "-t", "10", *reps, timeout=60)
    st = ctx.lab.state() or {}
    time.sleep(st.get("timers", {}).get("ttl", 20) + 5)
    podman("start", *reps)
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
        Scenario("partition-primary", "Disconnect the primary from the network, heal after failover",
                 "Primary isolated from observer and standbys", partition_primary),
        Scenario("dcs-loss", "Kill etcd for ttl+loop_wait+5s, then bring it back",
                 "Observer loss", dcs_loss),
        Scenario("lagging-replicas-failover", "Pause both replicas 3s, then kill the primary",
                 "Failover with standby apply lag (FastStartFailoverLagLimit)", lagging_replicas_failover),
        Scenario("replicas-down", "Stop both replicas for ttl+5s, then restart them",
                 "All standbys unreachable", replicas_down),
    ]
}

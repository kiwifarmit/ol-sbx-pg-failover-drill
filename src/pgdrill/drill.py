"""One drill run: workload + fault + recovery + verification."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from . import runner
from .faults import SCENARIOS, Ctx
from .lab import IP_TO_NODE, Lab, Mode, Timers
from .podman import PodmanError
from .verify import analyze


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def ensure_lab(lab: Lab, mode: Mode, timers: Timers) -> None:
    st = lab.state()
    if st and st.get("mode") == mode.name and st.get("timers") == timers.__dict__:
        ok, reason, _ = lab.health(mode)
        if ok:
            return
        log(f"lab unhealthy ({reason}), recreating")
    log(f"lab up: mode={mode.name} ({mode.dataguard})")
    took = lab.up(mode, timers)
    log(f"lab healthy in {took:.1f}s")


def run_one(
    lab: Lab, scenario: str, mode: Mode, via: str, outdir: Path, timers: Timers = Timers(),
    writers: int = 4, interval: float = 0.005, warmup: float = 5, steady: float = 5,
    recover_timeout: float = 180,
) -> dict:
    sc = SCENARIOS[scenario]
    ensure_lab(lab, mode, timers)
    run_id = f"{scenario}.{mode.name}.{via}.{time.strftime('%Y%m%dT%H%M%S')}"
    rundir = (outdir / run_id).resolve()
    rundir.mkdir(parents=True, exist_ok=True)

    ok, _, cluster = lab.health(mode)
    leader = lab.leader()
    assert ok and leader and cluster
    runner.probe(rundir, "init", "--conninfo", runner.node_conninfo(leader))

    log(f"run {run_id}: leader={leader}, starting {writers} writers via {via}")
    runner.start_workload(rundir, runner.conninfo(via), run_id, writers, interval)
    time.sleep(warmup)

    ctx = Ctx(lab=lab, mode=mode, leader=leader, cluster=cluster)
    error = None
    fault_t = None
    try:
        fault_t = sc.run(ctx)
    except (PodmanError, TimeoutError) as exc:
        error = f"scenario: {exc}"
        log(error)

    rejoined = False
    t0 = time.monotonic()
    try:
        lab.wait_healthy(mode, timeout=recover_timeout)
        rejoined = True
        ctx.mark("cluster_healthy")
    except TimeoutError as exc:
        error = (error + "; " if error else "") + str(exc)
        log(f"not healthy: {exc}")
    recovery_s = time.monotonic() - t0
    time.sleep(steady)
    runner.stop_workload()

    leader_after = lab.leader()
    ledger = {"rows": [], "server": None, "timeline": None}
    if leader_after:
        runner.probe(rundir, "ledger", "--conninfo", runner.node_conninfo(leader_after),
                     "--run-id", run_id, "--out", "/out/ledger.json")
        ledger = json.loads((rundir / "ledger.json").read_text())
    else:
        error = (error + "; " if error else "") + "no leader after recovery, ledger not read"

    events = [json.loads(line) for line in (rundir / "events.jsonl").read_text().splitlines() if line]
    if fault_t is None:
        fault_t = ctx.marks[0][1] if ctx.marks else events[0]["t"]

    result = {
        "run_id": run_id,
        "scenario": scenario,
        "scenario_summary": sc.summary,
        "oracle_analogue": sc.oracle,
        "mode": mode.name,
        "dataguard_mode": mode.dataguard,
        "via": via,
        "timers": timers.__dict__,
        "writers": writers,
        "leader_before": leader,
        "leader_after": leader_after,
        "timeline_after": ledger.get("timeline"),
        "rejoined": rejoined,
        "cluster_recovery_s": round(recovery_s, 1),
        "fault_t": fault_t,
        "marks": [[label, round(t - fault_t, 3)] for label, t in ctx.marks],
        "error": error,
        "analysis": analyze(events, ledger["rows"], fault_t, IP_TO_NODE),
    }
    (rundir / "result.json").write_text(json.dumps(result, indent=2))
    a = result["analysis"]
    log(
        f"done {run_id}: lost_acked={a['rpo']['lost_acked']} max_gap={a['rto']['max_gap_s']}s "
        f"in_doubt={a['in_doubt']['total']} split_brain={a['split_brain']['detected']} rejoined={rejoined}"
    )
    return result

"""One drill run: workload + fault + recovery + verification."""

from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

from . import runner
from .faults import SCENARIOS, Ctx
from .lab import IP_TO_NODE, Lab, Mode, Timers
from .podman import PodmanError
from .verify import analyze


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def ensure_lab(lab: Lab, mode: Mode, timers: Timers) -> None:
    if lab.matches(mode, timers):
        ok, reason, _ = lab.health(mode)
        if ok:
            return
        log(f"lab unhealthy ({reason}), recreating")
    log(f"lab up: mode={mode.name} ({mode.dataguard})")
    took = lab.up(mode, timers)
    log(f"lab healthy in {took:.1f}s")


def _add_error(error: str | None, msg: str) -> str:
    return f"{error}; {msg}" if error else msg


def run_one(
    lab: Lab, scenario: str, mode: Mode, via: str, outdir: Path, timers: Timers | None = None,
    writers: int = 4, warmup: float = 5, steady: float = 5, recover_timeout: float = 180,
) -> dict:
    timers = timers or Timers()
    sc = SCENARIOS[scenario]
    ensure_lab(lab, mode, timers)
    run_id = f"{scenario}.{mode.name}.{via}.{time.strftime('%Y%m%dT%H%M%S')}"
    rundir = (outdir / run_id).resolve()
    rundir.mkdir(parents=True, exist_ok=True)

    ok, reason, cluster = lab.health(mode)
    leader = lab.leader()
    if not (ok and leader and cluster):
        raise RuntimeError(f"lab not healthy before the run: {reason}")
    runner.probe(rundir, "init", "--conninfo", runner.node_conninfo(leader))

    log(f"run {run_id}: leader={leader}, starting {writers} writers via {via}")
    runner.start_workload(rundir, runner.conninfo(via), run_id, writers)
    ctx = Ctx(lab=lab, mode=mode, leader=leader, cluster=cluster)
    error = None
    fault_t = None
    rejoined = False
    recovery_s = None
    try:
        time.sleep(warmup)
        try:
            fault_t = sc.run(ctx)
        except (PodmanError, TimeoutError) as exc:
            error = _add_error(error, f"scenario: {exc}")
            log(error)
        t0 = time.monotonic()
        try:
            lab.wait_healthy(mode, timeout=recover_timeout)
            rejoined = True
            ctx.mark("cluster_healthy")
        except TimeoutError as exc:
            error = _add_error(error, str(exc))
            log(f"not healthy: {exc}")
        recovery_s = round(time.monotonic() - t0, 1)
        time.sleep(steady)
    finally:
        # Always stop the writers, also on Ctrl-C or an unexpected exception.
        runner.stop_workload()

    leader_after = lab.leader()
    ledger_rows = None
    timeline = None
    if leader_after:
        runner.probe(rundir, "ledger", "--conninfo", runner.node_conninfo(leader_after),
                     "--run-id", run_id, "--out", "/out/ledger.json")
        ledger = json.loads((rundir / "ledger.json").read_text())
        ledger_rows, timeline = ledger["rows"], ledger["timeline"]
    else:
        error = _add_error(error, "no leader after recovery, ledger not read")

    events_file = rundir / "events.jsonl"
    if not events_file.exists():
        raise RuntimeError(f"workload left no event log in {rundir}")
    events = [json.loads(line) for line in events_file.read_text().splitlines() if line]
    if fault_t is None:  # the scenario failed before returning: use its fault mark if it got that far
        fault_t = next((t for label, t in ctx.marks if label.startswith("fault:")), None)
        fault_t = fault_t if fault_t is not None else (ctx.marks[0][1] if ctx.marks else events[0]["t"])

    result = {
        "run_id": run_id,
        "scenario": scenario,
        "scenario_summary": sc.summary,
        "oracle_analogue": sc.oracle,
        "mode": mode.name,
        "dataguard_mode": mode.dataguard,
        "via": via,
        "timers": asdict(timers),
        "writers": writers,
        "leader_before": leader,
        "leader_after": leader_after,
        "timeline_after": timeline,
        "rejoined": rejoined,
        "cluster_recovery_s": recovery_s,
        "fault_t": fault_t,
        "marks": [[label, round(t - fault_t, 3)] for label, t in ctx.marks],
        "error": error,
        "analysis": analyze(events, ledger_rows, fault_t, IP_TO_NODE),
    }
    (rundir / "result.json").write_text(json.dumps(result, indent=2))
    a = result["analysis"]
    log(
        f"done {run_id}: lost_acked={a['rpo']['lost_acked']} max_gap={a['rto']['max_gap_s']}s "
        f"in_doubt={a['in_doubt']['total']} split_brain={a['split_brain']['detected']} "
        f"rejoined={rejoined}" + (f" error={error}" if error else "")
    )
    return result

"""One drill run: workload + fault + recovery + verification."""

from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

from . import runner
from .faults import SCENARIOS, Ctx
from .lab import ETCD, HAPROXY, IP_TO_NODE, NODES, Lab, Mode, Timers
from .podman import PodmanError, podman
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


def last_ack_t(events_file: Path, tail_bytes: int = 65536) -> float | None:
    """Timestamp of the last acknowledged commit in the (still growing) event log."""
    try:
        with open(events_file, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - tail_bytes))
            lines = f.read().decode(errors="replace").splitlines()
    except FileNotFoundError:
        return None
    for line in reversed(lines):
        if '"ev":"ack"' in line:
            try:
                return float(json.loads(line)["t"])
            except (ValueError, KeyError):
                continue  # the first line of the tail can be cut in half
    return None


def save_logs(rundir: Path, since: float) -> None:
    """Keep each lab container's log for the run window (HAProxy check transitions, Patroni)."""
    logdir = rundir / "logs"
    logdir.mkdir(exist_ok=True)
    for name in (HAPROXY[0], *NODES, ETCD[0]):
        try:
            proc = podman("logs", "--timestamps", "--since", str(int(since)), name, check=False, timeout=30)
        except PodmanError as exc:
            (logdir / f"{name}.log").write_text(f"(could not read logs: {exc})\n")
            continue
        (logdir / f"{name}.log").write_text((proc.stdout or "") + (proc.stderr or ""))


def wait_writes_resumed(events_file: Path, after_t: float, timeout: float) -> bool:
    """A healthy cluster is not the end of the outage: the proxy or the driver may still be
    routing clients nowhere. Wait until a write is acknowledged after `after_t` (lab clock)."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        t = last_ack_t(events_file)
        if t is not None and t > after_t:
            return True
        time.sleep(0.5)
    return False


def run_one(
    lab: Lab, scenario: str, mode: Mode, via: str, outdir: Path, timers: Timers | None = None,
    writers: int = 4, warmup: float = 5, steady: float = 5, recover_timeout: float = 180,
    resume_timeout: float = 60,
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
    # host clock, with a margin: on macOS the podman machine's clock can be slightly off
    logs_since = time.time() - 5
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
            healthy_t = ctx.mark("cluster_healthy")
        except TimeoutError as exc:
            error = _add_error(error, str(exc))
            log(f"not healthy: {exc}")
        recovery_s = round(time.monotonic() - t0, 1)
        if rejoined and not wait_writes_resumed(rundir / "events.jsonl", healthy_t, resume_timeout):
            error = _add_error(error, f"writes did not resume within {resume_timeout:.0f}s of a healthy cluster")
            log(error)
        time.sleep(steady)
    finally:
        # Always stop the writers, also on Ctrl-C or an unexpected exception.
        runner.stop_workload()
    save_logs(rundir, logs_since)

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

"""Turn a workload event log + the surviving primary's ledger into RTO/RPO/split-brain numbers.

Pure functions, no I/O: easy to unit-test with synthetic logs.
All times in the result are seconds relative to the fault injection time (fault_t).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from itertools import pairwise

# How long two servers' ack streams may overlap before we call it split brain.
# Acks are timestamped after the response arrives in a client thread, so a few ms of
# apparent overlap at a clean handover is measurement noise, not two primaries.
OVERLAP_TOLERANCE_S = 0.05
# Gaps between consecutive acks (all writers merged) above this count as unavailability.
GAP_THRESHOLD_S = 0.5

# SQLSTATE classes where the server definitely did not commit: invalid transaction state
# (25006 = read-only, i.e. we reached a standby or a demoted primary), integrity, data,
# syntax, resources, rollback. Anything else, and no SQLSTATE at all (connection lost),
# is in doubt: 57P01 "terminating connection" can arrive after a local commit.
REJECTED_SQLSTATE_CLASSES = {"22", "23", "25", "40", "42", "53"}


def is_rejection(err: dict) -> bool:
    st = err.get("sqlstate")
    return bool(st) and st[:2] in REJECTED_SQLSTATE_CLASSES


def _r(x: float | None, nd: int = 3) -> float | None:
    return None if x is None else round(x, nd)


def analyze(
    events: Iterable[dict],
    ledger_rows: Iterable[Iterable[int]] | None,
    fault_t: float,
    ip_to_node: dict[str, str] | None = None,
    gap_threshold: float = GAP_THRESHOLD_S,
    overlap_tolerance: float = OVERLAP_TOLERANCE_S,
) -> dict:
    """ledger_rows=None means the ledger could not be read: RPO is reported as unknown."""
    ip_to_node = ip_to_node or {}

    def node(ip: str) -> str:
        return ip_to_node.get(ip, ip)

    events = list(events)
    acks = [e for e in events if e["ev"] == "ack"]
    commit_errs = [e for e in events if e["ev"] == "err" and e.get("phase") == "commit"]
    connects = [e for e in events if e["ev"] == "connect"]
    stop = next((e for e in events if e["ev"] == "stop"), None)
    stopping = next((e for e in events if e["ev"] == "stopping"), None)
    # End of the measured window: when the stop was requested (not after the writer join).
    end_t = (stopping or stop or {}).get("t", max((e["t"] for e in events), default=fault_t))

    acked = {(e["w"], e["seq"]): e for e in acks}
    rejected = {(e["w"], e["seq"]): e for e in commit_errs if is_rejection(e)}
    in_doubt = {(e["w"], e["seq"]): e for e in commit_errs if not is_rejection(e)}
    in_doubt |= {(e["w"], e["seq"]): e for e in events if e["ev"] == "inflight"}

    # ---- RPO: acknowledged writes missing from the surviving primary
    if ledger_rows is None:
        ledger = None
        lost = []
        rpo = {"lost_acked": None, "lost_first_s": None, "lost_last_s": None, "lost_sample": [],
               "note": "ledger not read"}
        in_doubt_committed = None
        unexplained = None
    else:
        ledger = {(int(w), int(s)) for w, s in ledger_rows}
        lost = sorted((e for k, e in acked.items() if k not in ledger), key=lambda e: e["t"])
        rpo = {
            "lost_acked": len(lost),
            "lost_first_s": _r(lost[0]["t"] - fault_t) if lost else None,
            "lost_last_s": _r(lost[-1]["t"] - fault_t) if lost else None,
            "lost_sample": [[e["w"], e["seq"]] for e in lost[:10]],
        }
        in_doubt_committed = sum(1 for k in in_doubt if k in ledger)
        # rows nobody can account for; a "rejected" row that shows up anyway lands here too
        unexplained = len(ledger - acked.keys() - in_doubt.keys())

    # ---- RTO: longest write gap that ends after the fault (all writers merged),
    # including the trailing gap up to the end of the window: writes that never resume
    # must show up as an open outage, even if a few acks landed just after the fault.
    times = sorted(e["t"] for e in acks)
    last = times[-1] if times else fault_t
    all_gaps = [(a, b) for a, b in pairwise(times) if b > fault_t]
    if end_t > last:
        all_gaps.append((last, end_t))
    recovered = bool(times) and end_t - last <= gap_threshold
    gaps = [g for g in all_gaps if g[1] - g[0] > gap_threshold]
    max_gap = max(all_gaps, key=lambda g: g[1] - g[0], default=None)
    downtime = sum(b - a for a, b in gaps)
    measured = (end_t - times[0]) if times else 0.0

    # ---- Split brain: did an earlier primary keep acking after a later one started?
    # (A legitimate A -> B -> A sequence inside one run would be flagged too; no scenario does that.)
    per_srv: dict[str, list[float]] = defaultdict(list)
    for e in acks:
        per_srv[e.get("srv") or "?"].append(e["t"])
    order = sorted(per_srv, key=lambda s: min(per_srv[s]))
    servers = [
        {"node": node(s), "acks": len(per_srv[s]), "first": _r(min(per_srv[s]) - fault_t),
         "last": _r(max(per_srv[s]) - fault_t)}
        for s in order
    ]
    overlap_acks = 0
    overlap_lost = 0
    lost_keys = {(e["w"], e["seq"]) for e in lost}
    for i, s in enumerate(order):
        later_starts = [min(per_srv[x]) for x in order[i + 1:]]
        if not later_starts:
            continue
        cutoff = min(later_starts) + overlap_tolerance
        for e in acks:
            if (e.get("srv") or "?") == s and e["t"] > cutoff:
                overlap_acks += 1
                overlap_lost += (e["w"], e["seq"]) in lost_keys

    return {
        "acks": len(acks),
        "ledger_rows": None if ledger is None else len(ledger),
        "rpo": rpo,
        "in_doubt": {
            "total": len(in_doubt),
            "committed": in_doubt_committed,
            "not_committed": None if in_doubt_committed is None else len(in_doubt) - in_doubt_committed,
        },
        "unexplained_rows": unexplained,
        "rto": {
            "recovered": recovered,
            "max_gap_s": _r(max_gap[1] - max_gap[0]) if max_gap else 0.0,
            "max_gap_from_s": _r(max_gap[0] - fault_t) if max_gap else None,
            "max_gap_to_s": _r(max_gap[1] - fault_t) if max_gap else None,
            "downtime_s": _r(downtime),
            "outages": len(gaps),
            # end of the longest outage; 0 when writes never stopped, None when they never resumed
            "writes_resumed_s": (_r(max_gap[1] - fault_t) if gaps else 0.0) if recovered else None,
        },
        "split_brain": {
            "detected": overlap_acks > 0,
            "overlap_acks": overlap_acks,
            "overlap_lost": overlap_lost,
            "servers": servers,
        },
        "client": {
            "connects": len(connects),
            "connected_to_standby": sum(1 for e in connects if e.get("rec")),
            "commit_errors": len(commit_errs),
            "rejected": len(rejected),
            "rejected_sqlstates": sorted({e["sqlstate"] for e in rejected.values()}),
            "hung_writers_at_stop": stop.get("hung_writers", 0) if stop else None,
            "duration_s": _r(measured),
            # commits per second while writes were flowing (excludes outages)
            "commit_rate": round(len(acks) / (measured - downtime), 1) if measured - downtime > 0 else None,
        },
    }

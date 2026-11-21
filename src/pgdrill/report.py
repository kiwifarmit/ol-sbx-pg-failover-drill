"""Render drill results as Markdown and evaluate thresholds."""

from __future__ import annotations

from .faults import SCENARIOS
from .lab import MODES

SYNC_MODES = ("sync", "strict")


def verdict(r: dict, max_rto: float | None, max_rpo: int | None) -> tuple[bool, list[str]]:
    """Safety checks always apply; RTO/RPO thresholds only when given.

    Losing an acknowledged write in a synchronous mode is always a failure: that is the
    guarantee those modes exist for, so it is checked whether or not --max-rpo is set.
    """
    reasons = []
    if r.get("error"):
        reasons.append(f"run error: {r['error']}")
    if "analysis" not in r:
        return False, reasons or ["run failed"]
    a = r["analysis"]
    lost = a["rpo"]["lost_acked"]
    if not a["rto"]["recovered"]:
        reasons.append("writes never recovered")
    if a["split_brain"]["detected"]:
        reasons.append(f"split brain ({a['split_brain']['overlap_acks']} overlapping acks)")
    if not r.get("rejoined"):
        reasons.append("cluster did not return to full health")
    if a["client"].get("hung_writers_at_stop"):
        reasons.append(f"{a['client']['hung_writers_at_stop']} writers still blocked at stop")
    if lost is None:
        reasons.append("ledger not read, RPO unknown")
    elif lost and r.get("mode") in SYNC_MODES:
        reasons.append(f"{lost} acknowledged writes lost in synchronous mode")
    if max_rto is not None and a["rto"]["max_gap_s"] > max_rto:
        reasons.append(f"RTO {a['rto']['max_gap_s']}s > {max_rto}s")
    if max_rpo is not None and lost is not None and lost > max_rpo:
        reasons.append(f"RPO {lost} lost > {max_rpo}")
    if a["unexplained_rows"]:
        reasons.append(f"{a['unexplained_rows']} unexplained ledger rows")
    return not reasons, reasons


def _cell(r: dict | None) -> str:
    if r is None:
        return "–"
    if "analysis" not in r:
        return "error"
    a = r["analysis"]
    lost = a["rpo"]["lost_acked"]
    s = f"{a['rto']['max_gap_s']:.1f}s / {'?' if lost is None else lost}"
    if not a["rto"]["recovered"]:
        s += " (no recovery)"
    if a["split_brain"]["detected"]:
        s += " **SPLIT BRAIN**"
    return s


def render_markdown(results: list[dict], meta: dict, max_rto: float | None, max_rpo: int | None) -> str:
    out = ["# pg-failover-drill report", ""]
    out.append(f"- generated: {meta.get('generated')}")
    out.append(f"- host: {meta.get('host')}, podman {meta.get('podman')}")
    t = meta.get("timers", {})
    out.append(
        f"- Patroni timers: ttl={t.get('ttl')} loop_wait={t.get('loop_wait')} "
        f"retry_timeout={t.get('retry_timeout')} failsafe_mode={t.get('failsafe')}"
    )
    out.append(f"- workload: {meta.get('writers')} writers, autocommit single-row inserts, "
               f"{meta.get('writer_interval_s', '?')}s pause between commits per writer")
    if meta.get("client_options"):
        out.append(f"- client options: `{meta['client_options']}`")
    out.append(f"- thresholds: max RTO={max_rto if max_rto is not None else '-'}s, "
               f"max lost acked writes={max_rpo if max_rpo is not None else '-'}")
    out.append("")

    vias = sorted({r["via"] for r in results})
    modes = [m for m in MODES if any(r["mode"] == m for r in results)]
    for via in vias:
        out += [f"## Matrix (routing: {via})", "",
                "Each cell: **longest write outage seen by clients / acknowledged writes lost**.", ""]
        out.append("| Scenario | " + " | ".join(f"{MODES[m].dataguard} (`{m}`)" for m in modes) + " |")
        out.append("|---|" + "---|" * len(modes))
        for sc in SCENARIOS:
            row = [next((r for r in results if r["scenario"] == sc and r["mode"] == m and r["via"] == via), None)
                   for m in modes]
            if all(x is None for x in row):
                continue
            out.append(f"| {sc} | " + " | ".join(_cell(x) for x in row) + " |")
        out.append("")

    out += ["## Runs", "",
            ("| Run | Leader | Max gap (s) | Downtime (s) | Lost acked (window s) | In-doubt (committed/total) "
             "| Rejected | Split brain | Rejoined | Verdict |"),
            "|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        ok, reasons = verdict(r, max_rto, max_rpo)
        if "analysis" not in r:
            out.append(f"| {r['scenario']} {r['mode']} {r['via']} | | | | | | | | | FAIL: {r.get('error')} |")
            continue
        a = r["analysis"]
        lost = a["rpo"]["lost_acked"]
        window = f" ({a['rpo']['lost_first_s']}..{a['rpo']['lost_last_s']})" if lost else ""
        committed = a["in_doubt"]["committed"]
        out.append(
            f"| {r['scenario']} {r['mode']} {r['via']} | {r['leader_before']}→{r['leader_after']} "
            f"| {a['rto']['max_gap_s']} | {a['rto']['downtime_s']} | {'?' if lost is None else lost}{window} "
            f"| {'?' if committed is None else committed}/{a['in_doubt']['total']} "
            f"| {a['client'].get('rejected', 0)} "
            f"| {'YES' if a['split_brain']['detected'] else 'no'} | {'yes' if r['rejoined'] else 'NO'} "
            f"| {'PASS' if ok else 'FAIL: ' + '; '.join(reasons)} |"
        )
    out.append("")

    out += ["## Timelines", "", "Seconds relative to the fault.", ""]
    for r in results:
        if "marks" in r:
            marks = ", ".join(f"{label} @{t:+.1f}" for label, t in r["marks"])
            out.append(f"- **{r['scenario']} {r['mode']} {r['via']}**: {marks}")
    out.append("")

    out += [
        "## How to read this",
        "",
        ("- **Max gap**: the longest interval with no acknowledged commit from any writer after the fault, "
         "including an outage still open at the end of the run. This is the RTO the application sees."),
        "- **Downtime**: the sum of all gaps longer than 0.5s after the fault.",
        ("- **Lost acked**: commits the server acknowledged that are missing on the primary after recovery. "
         "This is the RPO. The window is when those commits were acknowledged, relative to the fault."),
        ("- **In-doubt**: COMMITs that never got a definite answer (connection lost, backend terminated, or "
         "still blocked at the end). Postgres has no transaction replay (unlike Oracle Application "
         "Continuity), so the application must be able to retry these idempotently."),
        ("- **Rejected**: COMMITs the server refused with a definite error (e.g. 25006, read-only: the "
         "connection reached a standby or a demoted primary). Not committed, safe to retry."),
        "- **Split brain**: an older primary kept acknowledging writes after a newer one started.",
        ("- **Verdict**: FAIL on a run error, writes that never resumed, split brain, a cluster that did not "
         "heal, writers still blocked at the end, unexplained ledger rows, any acknowledged write lost in "
         "`sync`/`strict`, or a breached `--max-rto`/`--max-rpo`."),
        ("- Mode labels: `async` ≈ Data Guard Maximum Performance, `sync` (Patroni `synchronous_mode`) ≈ "
         "Maximum Availability, `strict` (`synchronous_mode_strict`) ≈ Maximum Protection. Note that Oracle "
         "shuts the primary down in Maximum Protection, while Postgres blocks commits."),
        "",
    ]
    return "\n".join(out)

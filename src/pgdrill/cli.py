"""pgdrill command line."""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
import traceback
from pathlib import Path

from .lab import MODES, Lab, Timers
from .podman import out as podman_out


def _csv(value: str, choices: list[str]) -> list[str]:
    items = choices if value == "all" else [v.strip() for v in value.split(",") if v.strip()]
    bad = [i for i in items if i not in choices]
    if bad:
        raise argparse.ArgumentTypeError(f"unknown: {', '.join(bad)} (choose from {', '.join(choices)})")
    return items


def _timers(args) -> Timers:
    return Timers(ttl=args.ttl, loop_wait=args.loop_wait, retry_timeout=args.retry_timeout,
                  failsafe=not args.no_failsafe)


def _add_timer_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--ttl", type=int, default=20)
    p.add_argument("--loop-wait", type=int, default=5)
    p.add_argument("--retry-timeout", type=int, default=5)
    p.add_argument("--no-failsafe", action="store_true", help="disable Patroni failsafe_mode")


def cmd_lab(args) -> int:
    lab = Lab(Path(args.workdir))
    if args.action == "build":
        lab.build(force=True)
    elif args.action == "up":
        took = lab.up(MODES[args.sync_mode], _timers(args))
        print(f"lab healthy in {took:.1f}s")
        print(lab.status_text())
    elif args.action == "down":
        lab.down()
    elif args.action == "status":
        print(lab.status_text())
    return 0


def cmd_scenarios(args) -> int:
    from .faults import SCENARIOS

    for s in SCENARIOS.values():
        print(f"{s.name:28s} {s.summary}\n{'':28s} Oracle analogue: {s.oracle}")
    return 0


def _finish(results: list[dict], meta: dict, batch: Path, max_rto, max_rpo, quiet: bool = False) -> int:
    from .report import render_markdown, verdict

    (batch / "report.json").write_text(json.dumps({"meta": meta, "results": results}, indent=2))
    md = render_markdown(results, meta, max_rto, max_rpo)
    (batch / "report.md").write_text(md)
    failed = [r for r in results if not verdict(r, max_rto, max_rpo)[0]]
    if quiet:
        return 0
    print(json.dumps({
        "report_md": str(batch / "report.md"),
        "report_json": str(batch / "report.json"),
        "runs": len(results),
        "failed": [f"{r['scenario']}.{r['mode']}.{r['via']}" for r in failed],
    }, indent=2))
    if any("analysis" not in r for r in results):
        return 1
    return 2 if failed else 0


def cmd_run(args) -> int:
    from .drill import log, run_one
    from .faults import SCENARIOS

    scenarios = _csv(args.scenarios, list(SCENARIOS))
    modes = _csv(args.modes, list(MODES))
    vias = _csv(args.via, ["haproxy", "libpq"])
    timers = _timers(args)
    lab = Lab(Path(args.workdir))
    batch = Path(args.out or Path(args.workdir) / "runs" / time.strftime("%Y%m%dT%H%M%S")).resolve()
    batch.mkdir(parents=True, exist_ok=True)
    meta = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "host": f"{platform.system()} {platform.machine()}",
        "podman": podman_out("version", "--format", "{{.Client.Version}}", check=False),
        "timers": timers.__dict__,
        "writers": args.writers,
        "scenarios": scenarios, "modes": modes, "via": vias,
    }
    results = []
    for mode in modes:
        for via in vias:
            for sc in scenarios:
                try:
                    results.append(run_one(lab, sc, MODES[mode], via, batch, timers=timers, writers=args.writers))
                except Exception as exc:  # noqa: BLE001 - record and move on, recreate the lab
                    log(f"run {sc}.{mode}.{via} failed: {exc}")
                    traceback.print_exc()
                    results.append({"scenario": sc, "mode": mode, "via": via, "error": str(exc)})
                    lab.down()
                # keep the report current while a long matrix runs
                _finish(results, meta, batch, args.max_rto, args.max_rpo, quiet=True)
    if not args.keep_lab:
        lab.down()
    return _finish(results, meta, batch, args.max_rto, args.max_rpo)


def cmd_report(args) -> int:
    from .report import render_markdown

    batch = Path(args.batch)
    data = json.loads((batch / "report.json").read_text())
    print(render_markdown(data["results"], data["meta"], args.max_rto, args.max_rpo))
    return 0


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="pgdrill", description=__doc__)
    ap.add_argument("--workdir", default=".pgdrill", help="lab configs and run output (default: .pgdrill)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("lab", help="manage the Podman lab")
    p.add_argument("action", choices=["build", "up", "down", "status"])
    p.add_argument("--sync-mode", choices=list(MODES), default="async")
    _add_timer_args(p)
    p.set_defaults(fn=cmd_lab)

    p = sub.add_parser("scenarios", help="list fault scenarios")
    p.set_defaults(fn=cmd_scenarios)

    p = sub.add_parser("run", help="run a drill matrix (scenarios x modes x routing)")
    p.add_argument("--scenarios", default="all", help="comma list or 'all'")
    p.add_argument("--modes", default="all", help="async,sync,strict or 'all'")
    p.add_argument("--via", default="haproxy", help="haproxy,libpq or 'all'")
    p.add_argument("--writers", type=int, default=4)
    p.add_argument("--out", help="batch output dir (default: <workdir>/runs/<timestamp>)")
    p.add_argument("--max-rto", type=float, help="fail if the longest write gap exceeds this (s)")
    p.add_argument("--max-rpo", type=int, help="fail if more acknowledged writes than this are lost")
    p.add_argument("--keep-lab", action="store_true", help="leave the lab running at the end")
    _add_timer_args(p)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("report", help="re-render report.md from a batch's report.json")
    p.add_argument("batch")
    p.add_argument("--max-rto", type=float)
    p.add_argument("--max-rpo", type=int)
    p.set_defaults(fn=cmd_report)

    args = ap.parse_args(argv)
    try:
        sys.exit(args.fn(args))
    except argparse.ArgumentTypeError as exc:
        ap.error(str(exc))


if __name__ == "__main__":
    main()

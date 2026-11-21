# pg-failover-drill

Failover drills for Patroni/PostgreSQL clusters. You get measured **RTO**,
**RPO**, **in-doubt commits** and **split-brain checks** for each failure type,
reported in Oracle Data Guard terms.

It is built for teams moving from Oracle (RAC / Data Guard) to PostgreSQL who
need to answer one question before cutover: *what do we actually get compared
to Data Guard Maximum Availability?*

- Headless: one CLI, JSON + Markdown output, CI-friendly exit codes. It can be
  driven by a person, a pipeline or an AI agent.
- Self-contained: a disposable 3-node Patroni + etcd + HAProxy lab on Podman.
  Synthetic data only. No Oracle needed.
- Exact: every write is identified, so lost writes are counted, not estimated.

Background: [docs/analysis.md](docs/analysis.md) (Oracle HA → PostgreSQL concept
mapping and the gap this fills) and
[docs/executive-summary.md](docs/executive-summary.md) (the use case).

## Quickstart

Requirements: Podman 4.9+ (on macOS a running `podman machine`), Python 3.11+,
[uv](https://docs.astral.sh/uv/).

```bash
uv run pgdrill lab build
```

```bash
uv run pgdrill run --scenarios all --modes all --max-rto 30 --max-rpo 0
```

The report lands in `.pgdrill/runs/<timestamp>/report.md` (and `report.json`).
A full matrix (7 scenarios × 3 modes) takes about 20 minutes. Results from a
real run are in [docs/sample-report.md](docs/sample-report.md).

Other commands:

```bash
uv run pgdrill scenarios
```

```bash
uv run pgdrill lab up --sync-mode sync && uv run pgdrill lab status
```

```bash
uv run pgdrill run --scenarios kill-primary,switchover --modes async,sync --via haproxy,libpq
```

```bash
uv run pgdrill lab down
```

Exit codes: `0` all runs pass, `2` a threshold or safety check failed
(lost writes, outage too long, split brain, cluster not healed), `1` a run
could not complete.

## What a run does

1. Brings up the lab in the requested replication mode (or reuses a healthy one).
2. Starts N writer threads *inside the lab network*. Each inserts
   `(run_id, writer_id, seq)` rows in autocommit and logs every acknowledged
   commit and every failed one.
3. Injects the fault, drives recovery (restart, heal, unpause) and waits until
   the cluster is back to 1 leader + 2 streaming replicas on the same timeline.
4. Reads the ledger from the surviving primary and compares:
   - **lost acked**: acknowledged writes that are gone. This is the RPO.
   - **max gap**: the longest period with no acknowledged write. This is the RTO
     the application sees.
   - **in-doubt**: commits whose connection died before an answer, and whether
     they survived.
   - **split brain**: an old primary that kept acknowledging writes after a new
     one started.

## Scenarios

| Scenario | Fault | Oracle analogue |
|---|---|---|
| `kill-primary` | SIGKILL the primary container | Instance crash + Fast-Start Failover |
| `stop-primary` | Clean shutdown of the primary | Primary shutdown with FSFO |
| `switchover` | `patronictl switchover` | Broker SWITCHOVER |
| `partition-primary` | Disconnect the primary from the network | Primary isolated from observer and standbys |
| `dcs-loss` | Kill etcd for ttl+loop_wait+5s | Observer loss |
| `lagging-replicas-failover` | Pause both replicas 3s, then kill the primary | Failover with apply lag (`FastStartFailoverLagLimit`) |
| `replicas-down` | Stop both replicas for ttl+5s | All standbys unreachable |

## Modes

| `--modes` | Patroni setting | Data Guard equivalent |
|---|---|---|
| `async` | default | Maximum Performance |
| `sync` | `synchronous_mode: true` | Maximum Availability |
| `strict` | `synchronous_mode_strict: true` | Maximum Protection (but Postgres blocks commits, it doesn't shut down) |

## Routing

- `--via haproxy`: HAProxy on port 5000 health-checks Patroni `/primary`, with
  `on-marked-down shutdown-sessions`.
- `--via libpq`: `host=pg1,pg2,pg3 target_session_attrs=read-write`. This is
  the closest PostgreSQL equivalent of a TNS `ADDRESS_LIST` with `FAILOVER=ON`.

## Tuning

Timers are short by default so a drill takes about a minute: `--ttl 20 --loop-wait 5 --retry-timeout 5`,
with `failsafe_mode` on (`--no-failsafe` turns it off). Set them to the values you
plan for production to measure that configuration instead.

## Layout

```
lab/                 Containerfile, Patroni and HAProxy templates
src/pgdrill/
  lab.py             podman lab lifecycle, Patroni /cluster health
  faults.py          scenario registry
  workload.py        writer threads (runs inside the lab network)
  probe.py           one-shot DB probes (runs inside the lab network)
  verify.py          RTO / RPO / in-doubt / split-brain analysis (pure functions)
  report.py          Markdown report and thresholds
  drill.py, cli.py   orchestration and CLI
tests/               unit tests for verify; one podman integration test (-m podman)
```

The host side is stdlib only and drives the `podman` CLI. psycopg only runs
inside the lab image. Set `PGDRILL_PODMAN="sudo podman"` if rootless networking
gets in the way on your host.

## Limitations

- The lab is not a production setup: trust authentication inside the lab
  subnet, a single etcd node, no TLS.
- Timings come from containers on one host. Network latency between real sites
  adds to every number.
- The workload is single-row autocommit inserts. Long transactions and
  application-level retry logic are out of scope for now.

## License

Apache-2.0. Made by [Kiwifarm](https://github.com/kiwifarmit).

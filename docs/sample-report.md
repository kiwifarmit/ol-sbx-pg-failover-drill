# Sample report

Real output from `pgdrill` on a laptop (macOS arm64, Podman 5.8.2 machine,
PostgreSQL 17, Patroni 4.1.5, etcd 3.5.17, HAProxy 3.0), 2026-11-21. It covers
30 runs:

- the full matrix through HAProxy (21 runs)
- the failover scenarios through libpq multi-host (8 runs)
- DCS loss with `failsafe_mode` off (1 run)

The lab uses separate client and cluster networks, so a partitioned primary can
still be reached by clients.

Lab timers: `ttl=20 loop_wait=5 retry_timeout=5`. 4 writers committed 505–660
rows/s (measured per run). Production timers are usually longer, and every
unplanned-failover number scales with `ttl`.

## Findings

1. **An isolated async primary keeps "succeeding", and those writes are lost.**
   When the primary was cut off from etcd and its standbys but not from clients,
   it kept acknowledging commits for about 8 s, until Patroni demoted it. Every
   one of those commits was gone after failover: 4,809 through HAProxy and 5,272
   through libpq. The client-visible outage looks short (13.5–14 s), because the
   writes the client saw as successful during those 8 s are the ones that were
   lost. In `sync` and `strict` the same commits blocked within 0.1 s, because
   no standby could confirm them, and nothing was lost.
2. **Synchronous modes lost nothing, anywhere.** In `sync` and `strict` no
   acknowledged write was lost in any scenario. The tool treats that as a hard
   check, so it would fail the run otherwise.
3. **Unplanned failover costs about `ttl`.** A crash or a partition of the
   primary stopped writes for 18–22 s in every mode, because the leader key has
   to expire first. To get closer to a `FastStartFailoverThreshold`, tune
   `ttl`/`loop_wait`/`retry_timeout`. The mode doesn't change it.
4. **Planned operations are fast, and the proxy sets the floor.**
   - A switchover took 1.5 s through libpq (`target_session_attrs=read-write`)
     and 2.3 s through HAProxy. The difference is HAProxy's health check
     (`inter 1s fall 2`).
   - A clean shutdown of the primary took 2.3–2.6 s either way.
5. **Losing all standbys reproduces the Data Guard protection modes, with one
   difference.**
   - `async` kept writing.
   - `sync` paused 3.0 s, then went on without a sync standby (Maximum
     Availability).
   - `strict` blocked every commit for 27.3 s until a replica came back
     (Maximum Protection).

   Oracle would have shut the primary down; Postgres keeps it up with hung
   COMMITs.
6. **Losing the DCS is an outage unless `failsafe_mode` is on.**
   - With failsafe, killing etcd for 30 s caused no write interruption in any
     mode.
   - Without it, the primary demoted itself and writes stopped for 25.7 s.
   - In that run, 16 new connections went through HAProxy to the node just
     after it had turned read-only and were rejected with `25006`. That fits
     the health-check window: HAProxy needs up to two failed checks to mark a
     server down.
7. **Stalled standbys: the outcome depends on timing.** In this batch the async
   failover after freezing both standbys lost nothing, apparently because the
   WAL was still in their socket buffers. Two development runs of the same
   scenario differed:
   - one failed over and lost 258 acknowledged writes
   - in the other, Patroni refused to promote because both standbys were past
     `maximum_lag_on_failover` (the `FastStartFailoverLagLimit` equivalent), and
     waited for the old primary instead

   The tool now records which of the two happened. Plan for both.
8. **Every failover leaves in-doubt commits, and some did commit.** Each writer
   had one COMMIT in flight when its connection died (4 per failover).
   - In `sync`/`strict` lagging-standby failovers, all 4 had in fact committed.
   - In a `sync` crash or partition, 1 of 4 had.

   The client saw an error for a transaction that exists. Without Oracle
   Application Continuity, the application needs idempotent retries or a way to
   reconcile, otherwise a naive retry writes twice.
9. **No split brain** in any run. That includes the partitions, where clients
   could still reach the isolated primary: it always stopped acknowledging
   (after about 8 s) well before the new primary started (after about 21 s).
   Every cluster healed (pg_rewind/rejoin) to 1 leader + 2 streaming replicas.

---


## Run 1: full matrix through HAProxy

### Matrix (routing: haproxy)

Each cell: **longest write outage seen by clients / acknowledged writes lost**.

| Scenario | Maximum Performance (`async`) | Maximum Availability (`sync`) | Maximum Protection (`strict`) |
|---|---|---|---|
| kill-primary | 21.5s / 0 | 18.8s / 0 | 22.2s / 0 |
| stop-primary | 2.3s / 0 | 2.3s / 0 | 2.3s / 0 |
| switchover | 2.3s / 0 | 2.3s / 0 | 3.2s / 0 |
| partition-primary | 13.5s / 4809 | 20.6s / 0 | 21.0s / 0 |
| dcs-loss | 0.0s / 0 | 0.0s / 0 | 0.0s / 0 |
| lagging-replicas-failover | 21.8s / 0 | 23.8s / 0 | 24.8s / 0 |
| replicas-down | 0.0s / 0 | 3.0s / 0 | 27.3s / 0 |

### Runs

| Run | Leader | Max gap (s) | Downtime (s) | Lost acked (window s) | In-doubt (committed/total) | Rejected | Split brain | Rejoined | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| kill-primary async haproxy | pg1→pg3 | 21.467 | 21.467 | 0 | 0/4 | 0 | no | yes | PASS |
| stop-primary async haproxy | pg3→pg1 | 2.268 | 2.268 | 0 | 0/4 | 0 | no | yes | PASS |
| switchover async haproxy | pg1→pg2 | 2.278 | 2.278 | 0 | 0/4 | 0 | no | yes | PASS |
| partition-primary async haproxy | pg2→pg1 | 13.533 | 13.533 | 4809 (0.104..7.977) | 0/4 | 0 | no | yes | PASS |
| dcs-loss async haproxy | pg1→pg1 | 0.023 | 0 | 0 | 0/0 | 0 | no | yes | PASS |
| lagging-replicas-failover async haproxy | pg1→pg3 | 21.83 | 21.83 | 0 | 0/4 | 0 | no | yes | PASS |
| replicas-down async haproxy | pg3→pg3 | 0.019 | 0 | 0 | 0/0 | 0 | no | yes | PASS |
| kill-primary sync haproxy | pg1→pg2 | 18.805 | 18.805 | 0 | 1/4 | 0 | no | yes | PASS |
| stop-primary sync haproxy | pg2→pg3 | 2.265 | 2.265 | 0 | 0/4 | 0 | no | yes | PASS |
| switchover sync haproxy | pg3→pg1 | 2.265 | 2.265 | 0 | 1/4 | 0 | no | yes | PASS |
| partition-primary sync haproxy | pg1→pg2 | 20.626 | 20.626 | 0 | 1/4 | 0 | no | yes | PASS |
| dcs-loss sync haproxy | pg2→pg2 | 0.031 | 0 | 0 | 0/0 | 0 | no | yes | PASS |
| lagging-replicas-failover sync haproxy | pg2→pg3 | 23.815 | 23.815 | 0 | 4/4 | 0 | no | yes | PASS |
| replicas-down sync haproxy | pg3→pg3 | 2.993 | 2.993 | 0 | 0/0 | 0 | no | yes | PASS |
| kill-primary strict haproxy | pg1→pg2 | 22.224 | 22.224 | 0 | 0/4 | 0 | no | yes | PASS |
| stop-primary strict haproxy | pg2→pg3 | 2.284 | 2.284 | 0 | 0/4 | 0 | no | yes | PASS |
| switchover strict haproxy | pg3→pg1 | 3.2 | 3.2 | 0 | 0/4 | 0 | no | yes | PASS |
| partition-primary strict haproxy | pg1→pg2 | 20.959 | 20.959 | 0 | 0/4 | 0 | no | yes | PASS |
| dcs-loss strict haproxy | pg2→pg2 | 0.026 | 0 | 0 | 0/0 | 0 | no | yes | PASS |
| lagging-replicas-failover strict haproxy | pg2→pg3 | 24.766 | 24.766 | 0 | 4/4 | 0 | no | yes | PASS |
| replicas-down strict haproxy | pg3→pg3 | 27.315 | 27.315 | 0 | 0/0 | 0 | no | yes | PASS |

### Timelines

Seconds relative to the fault.

- **kill-primary async haproxy**: fault:kill -9 pg1 @+0.0, new_leader:pg3 @+22.7, restarted:pg1 @+24.5, cluster_healthy @+29.2
- **stop-primary async haproxy**: fault:stop pg3 @+0.0, new_leader:pg1 @+2.3, restarted:pg3 @+2.6, cluster_healthy @+4.4
- **switchover async haproxy**: fault:switchover pg1->pg2 @+0.0, new_leader:pg2 @+3.6, cluster_healthy @+5.4
- **partition-primary async haproxy**: fault:partition pg2 from cluster network @+0.0, new_leader:pg1 @+22.9, healed:pg2 @+28.1, cluster_healthy @+58.0
- **dcs-loss async haproxy**: fault:kill etcd @+0.0, etcd_back @+32.4, cluster_healthy @+49.1
- **lagging-replicas-failover async haproxy**: pause_replicas:pg2,pg3 @-3.4, replicas_frozen @-3.1, fault:kill -9 pg1 @+0.0, unpause_replicas @+0.4, new_leader:pg3 @+22.3, restarted:pg1 @+22.7, cluster_healthy @+27.4
- **replicas-down async haproxy**: fault:stop replicas pg1,pg2 @+0.0, replicas_back @+28.7, cluster_healthy @+30.7
- **kill-primary sync haproxy**: fault:kill -9 pg1 @+0.0, new_leader:pg2 @+18.9, restarted:pg1 @+19.1, cluster_healthy @+30.5
- **stop-primary sync haproxy**: fault:stop pg2 @+0.0, new_leader:pg3 @+2.4, restarted:pg2 @+2.6, cluster_healthy @+6.0
- **switchover sync haproxy**: fault:switchover pg3->pg1 @+0.0, new_leader:pg1 @+3.1, cluster_healthy @+11.7
- **partition-primary sync haproxy**: fault:partition pg1 from cluster network @+0.0, new_leader:pg2 @+22.5, healed:pg1 @+27.7, cluster_healthy @+57.7
- **dcs-loss sync haproxy**: fault:kill etcd @+0.0, etcd_back @+32.3, cluster_healthy @+35.2
- **lagging-replicas-failover sync haproxy**: pause_replicas:pg1,pg3 @-3.4, replicas_frozen @-3.1, fault:kill -9 pg2 @+0.0, unpause_replicas @+0.4, new_leader:pg3 @+20.3, restarted:pg2 @+20.6, cluster_healthy @+32.2
- **replicas-down sync haproxy**: fault:stop replicas pg1,pg2 @+0.0, replicas_back @+31.1, cluster_healthy @+35.7
- **kill-primary strict haproxy**: fault:kill -9 pg1 @+0.0, new_leader:pg2 @+22.7, restarted:pg1 @+23.0, cluster_healthy @+34.3
- **stop-primary strict haproxy**: fault:stop pg2 @+0.0, new_leader:pg3 @+2.2, restarted:pg2 @+2.5, cluster_healthy @+5.6
- **switchover strict haproxy**: fault:switchover pg3->pg1 @+0.0, new_leader:pg1 @+3.1, cluster_healthy @+11.5
- **partition-primary strict haproxy**: fault:partition pg1 from cluster network @+0.0, new_leader:pg2 @+22.5, healed:pg1 @+27.7, cluster_healthy @+57.4
- **dcs-loss strict haproxy**: fault:kill etcd @+0.0, etcd_back @+32.0, cluster_healthy @+32.6
- **lagging-replicas-failover strict haproxy**: pause_replicas:pg1,pg3 @-3.4, replicas_frozen @-3.1, fault:kill -9 pg2 @+0.0, unpause_replicas @+0.4, new_leader:pg3 @+21.4, restarted:pg2 @+21.7, cluster_healthy @+33.0
- **replicas-down strict haproxy**: fault:stop replicas pg1,pg2 @+0.0, replicas_back @+34.9, cluster_healthy @+40.9

### How to read this

- **Max gap**: the longest interval with no acknowledged commit from any writer after the fault, including an outage still open at the end of the run. This is the RTO the application sees.
- **Downtime**: the sum of all gaps longer than 0.5s after the fault.
- **Lost acked**: commits the server acknowledged that are missing on the primary after recovery. This is the RPO. The window is when those commits were acknowledged, relative to the fault.
- **In-doubt**: COMMITs that never got a definite answer (connection lost, backend terminated, or still blocked at the end). Postgres has no transaction replay (unlike Oracle Application Continuity), so the application must be able to retry these idempotently.
- **Rejected**: COMMITs the server refused with a definite error (e.g. 25006, read-only: the connection reached a standby or a demoted primary). Not committed, safe to retry.
- **Split brain**: an older primary kept acknowledging writes after a newer one started.
- **Verdict**: FAIL on a run error, writes that never resumed, split brain, a cluster that did not heal, writers still blocked at the end, unexplained ledger rows, any acknowledged write lost in `sync`/`strict`, or a breached `--max-rto`/`--max-rpo`.
- Mode labels: `async` ≈ Data Guard Maximum Performance, `sync` (Patroni `synchronous_mode`) ≈ Maximum Availability, `strict` (`synchronous_mode_strict`) ≈ Maximum Protection. Note that Oracle shuts the primary down in Maximum Protection, while Postgres blocks commits.

## Run 2: failover scenarios through libpq multi-host

### Matrix (routing: libpq)

Each cell: **longest write outage seen by clients / acknowledged writes lost**.

| Scenario | Maximum Performance (`async`) | Maximum Availability (`sync`) |
|---|---|---|
| kill-primary | 22.2s / 0 | 20.0s / 0 |
| stop-primary | 2.6s / 0 | 2.6s / 0 |
| switchover | 1.5s / 0 | 1.5s / 0 |
| partition-primary | 14.0s / 5272 | 19.8s / 0 |

### Runs

| Run | Leader | Max gap (s) | Downtime (s) | Lost acked (window s) | In-doubt (committed/total) | Rejected | Split brain | Rejoined | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| kill-primary async libpq | pg1→pg3 | 22.175 | 22.175 | 0 | 0/4 | 0 | no | yes | PASS |
| stop-primary async libpq | pg3→pg2 | 2.594 | 2.594 | 0 | 0/4 | 0 | no | yes | PASS |
| switchover async libpq | pg2→pg1 | 1.522 | 1.522 | 0 | 0/4 | 0 | no | yes | PASS |
| partition-primary async libpq | pg1→pg2 | 14.035 | 14.035 | 5272 (0.098..8.336) | 0/4 | 0 | no | yes | PASS |
| kill-primary sync libpq | pg1→pg3 | 19.959 | 19.959 | 0 | 1/4 | 0 | no | yes | PASS |
| stop-primary sync libpq | pg3→pg2 | 2.575 | 2.575 | 0 | 0/4 | 0 | no | yes | PASS |
| switchover sync libpq | pg2→pg1 | 1.533 | 1.533 | 0 | 0/4 | 0 | no | yes | PASS |
| partition-primary sync libpq | pg1→pg3 | 19.831 | 19.831 | 0 | 1/4 | 0 | no | yes | PASS |


## Run 3: DCS loss with failsafe_mode off

### Runs

| Run | Leader | Max gap (s) | Downtime (s) | Lost acked (window s) | In-doubt (committed/total) | Rejected | Split brain | Rejoined | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| dcs-loss async haproxy | pg1→pg1 | 25.7 | 25.7 | 0 | 0/4 | 16 | no | yes | PASS |


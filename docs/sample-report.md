# Sample report

Real output from `pgdrill` on a laptop (macOS arm64, Podman 5.8.2 machine,
PostgreSQL 17, Patroni 4.1.5, etcd 3.5.17, HAProxy 3.0). It covers 30 runs:

- the full matrix through HAProxy (21 runs, 2026-11-22)
- the failover scenarios through libpq multi-host (8 runs, 2026-11-21)
- DCS loss with `failsafe_mode` off (1 run, 2026-11-21)

The lab uses separate client and cluster networks, so a partitioned primary can
still be reached by clients.

Lab timers: `ttl=20 loop_wait=5 retry_timeout=5`, `maximum_lag_on_failover`
1 MB. 4 writers committed 181–458 rows/s in Run 1 and 505–660 rows/s in Runs
2–3 (measured per run; the laptop's load differed). Production timers are
usually longer, and every unplanned-failover number scales with `ttl`.

## Findings

1. **An isolated async primary keeps "succeeding", and those writes are lost.**
   When the primary was cut off from etcd and its standbys but not from clients,
   it kept acknowledging commits for 6–8 s, until Patroni demoted it. Every one
   of those commits was gone after failover: 2,143 through HAProxy and 5,272
   through libpq. The client-visible outage looks short (13.6–14.0 s), because
   the writes the client saw as successful during those seconds are the ones
   that were lost. In `sync` and `strict` the same commits blocked within 0.3 s,
   because no standby could confirm them, and nothing was lost.
2. **Synchronous modes lost nothing, anywhere.** In `sync` and `strict` no
   acknowledged write was lost in any scenario. The tool treats that as a hard
   check, so it would fail the run otherwise.
3. **Unplanned failover costs about `ttl`.** A crash or a partition of the
   primary stopped writes for 18–22 s in every mode, because the leader key has
   to expire first. To get closer to a `FastStartFailoverThreshold`, tune
   `ttl`/`loop_wait`/`retry_timeout`. The mode doesn't change it.
4. **Planned operations are fast, and the proxy sets the floor.**
   - A switchover took 1.5 s through libpq (`target_session_attrs=read-write`)
     and 2.3–3.5 s through HAProxy. The difference is mostly HAProxy's health
     check (`inter 1s fall 2`).
   - A clean shutdown of the primary took 2.5–2.6 s either way.
5. **Losing all standbys reproduces the Data Guard protection modes, with one
   difference.**
   - `async` kept writing.
   - `sync` paused briefly, then went on without a sync standby (Maximum
     Availability). The pause was 0.1 s in Run 1 and 3.0 s in an earlier batch,
     presumably depending on where the primary's 5 s Patroni loop was when the
     standbys went away.
   - `strict` blocked every commit for 30.3 s until a replica came back
     (Maximum Protection).

   Oracle would have shut the primary down. Postgres keeps it up, with COMMITs
   hanging.
6. **Losing the DCS is an outage unless `failsafe_mode` is on.**
   - With failsafe, killing etcd for 30 s never stopped writes for more than
     0.5 s, in any mode.
   - Without it, the primary demoted itself and writes stopped for 25.7 s.
   - In that run, 16 new connections went through HAProxy to the node just
     after it had turned read-only and were rejected with `25006`. That fits
     the health-check window: HAProxy needs up to two failed checks to mark a
     server down.
7. **Standbys that fall behind: async loses whatever they never received.** The
   lagging scenario cut both standbys off from the cluster network for about 3 s
   and then killed the primary.
   - In `async`, Patroni promoted a standby (writes resumed after 18.4 s) and
     1,607 acknowledged writes were gone: everything acknowledged after the cut. The standbys were
     484–518 kB behind at the kill, under the 1 MB `maximum_lag_on_failover`
     (the `FastStartFailoverLagLimit` equivalent), so the lag limit allowed it.
   - In `sync`/`strict`, commits stalled at the cut (the standbys were only
     0–45 kB behind) and nothing was lost. The write gap of 25.6–26.1 s
     includes the 4 s stall before the crash.

   An earlier version of this scenario paused the standbys instead and lost
   nothing. The kernel isn't paused, so WAL kept arriving in the standbys' socket
   buffers and they caught up on unpause. A frozen standby is not a lagging one.
8. **Every failover leaves in-doubt commits, and some did commit.** Each writer
   had one COMMIT in flight when its connection died (4 per failover). In Run 1,
   between 0 and 2 of those 4 had in fact committed, depending on the run.

   The client saw an error for a transaction that exists. Without Oracle
   Application Continuity, the application needs idempotent retries or a way to
   reconcile, otherwise a naive retry writes twice.
9. **No split brain** in any run. That includes the partitions, where clients
   could still reach the isolated primary: it always stopped acknowledging
   (after 6–8 s) well before the new primary started (after about 20 s).
   Every cluster healed (pg_rewind/rejoin) to 1 leader + 2 streaming replicas.

---


## Run 1: full matrix through HAProxy

### Matrix (routing: haproxy)

Each cell: **longest write outage seen by clients / acknowledged writes lost**.

| Scenario | Maximum Performance (`async`) | Maximum Availability (`sync`) | Maximum Protection (`strict`) |
|---|---|---|---|
| kill-primary | 18.6s / 0 | 22.0s / 0 | 20.4s / 0 |
| stop-primary | 2.5s / 0 | 2.5s / 0 | 2.6s / 0 |
| switchover | 2.5s / 0 | 2.3s / 0 | 3.5s / 0 |
| partition-primary | 13.6s / 2143 | 21.7s / 0 | 20.0s / 0 |
| dcs-loss | 0.1s / 0 | 0.5s / 0 | 0.2s / 0 |
| lagging-replicas-failover | 18.4s / 1607 | 26.1s / 0 | 25.6s / 0 |
| replicas-down | 0.1s / 0 | 0.1s / 0 | 30.3s / 0 |

### Runs

| Run | Leader | Max gap (s) | Downtime (s) | Lost acked (window s) | In-doubt (committed/total) | Rejected | Split brain | Rejoined | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| kill-primary async haproxy | pg1→pg2 | 18.556 | 18.556 | 0 | 1/4 | 0 | no | yes | PASS |
| stop-primary async haproxy | pg2→pg3 | 2.537 | 2.537 | 0 | 0/4 | 0 | no | yes | PASS |
| switchover async haproxy | pg3→pg1 | 2.5 | 2.5 | 0 | 0/4 | 0 | no | yes | PASS |
| partition-primary async haproxy | pg1→pg3 | 13.599 | 13.599 | 2143 (0.329..6.347) | 0/4 | 0 | no | yes | PASS |
| dcs-loss async haproxy | pg3→pg3 | 0.065 | 0 | 0 | 0/0 | 0 | no | yes | PASS |
| lagging-replicas-failover async haproxy | pg3→pg2 | 18.443 | 18.443 | 1607 (-3.445..0.316) | 0/4 | 0 | no | yes | PASS |
| replicas-down async haproxy | pg2→pg2 | 0.062 | 0 | 0 | 0/0 | 0 | no | yes | PASS |
| kill-primary sync haproxy | pg1→pg3 | 22.001 | 22.001 | 0 | 1/4 | 0 | no | yes | PASS |
| stop-primary sync haproxy | pg3→pg1 | 2.493 | 2.493 | 0 | 0/4 | 0 | no | yes | PASS |
| switchover sync haproxy | pg1→pg2 | 2.281 | 3.502 | 0 | 0/4 | 0 | no | yes | PASS |
| partition-primary sync haproxy | pg2→pg1 | 21.67 | 21.67 | 0 | 0/4 | 4 | no | yes | PASS |
| dcs-loss sync haproxy | pg1→pg1 | 0.459 | 0 | 0 | 0/0 | 0 | no | yes | PASS |
| lagging-replicas-failover sync haproxy | pg1→pg3 | 26.113 | 26.113 | 0 | 2/4 | 0 | no | yes | PASS |
| replicas-down sync haproxy | pg3→pg3 | 0.132 | 0 | 0 | 0/0 | 0 | no | yes | PASS |
| kill-primary strict haproxy | pg1→pg3 | 20.385 | 20.385 | 0 | 0/4 | 0 | no | yes | PASS |
| stop-primary strict haproxy | pg3→pg2 | 2.554 | 2.554 | 0 | 2/4 | 0 | no | yes | PASS |
| switchover strict haproxy | pg2→pg1 | 3.501 | 3.501 | 0 | 2/4 | 0 | no | yes | PASS |
| partition-primary strict haproxy | pg1→pg3 | 20.002 | 20.002 | 0 | 1/4 | 0 | no | yes | PASS |
| dcs-loss strict haproxy | pg3→pg3 | 0.156 | 0 | 0 | 0/0 | 0 | no | yes | PASS |
| lagging-replicas-failover strict haproxy | pg3→pg2 | 25.635 | 25.635 | 0 | 0/4 | 0 | no | yes | PASS |
| replicas-down strict haproxy | pg2→pg2 | 30.272 | 30.272 | 0 | 0/0 | 0 | no | yes | PASS |

### Timelines

Seconds relative to the fault.

- **kill-primary async haproxy**: fault:kill -9 pg1 @+0.0, new_leader:pg2 @+19.8, restarted:pg1 @+20.5, cluster_healthy @+29.2
- **stop-primary async haproxy**: fault:stop pg2 @+0.0, new_leader:pg3 @+3.7, restarted:pg2 @+4.6, cluster_healthy @+7.5
- **switchover async haproxy**: fault:switchover pg3->pg1 @+0.0, new_leader:pg1 @+5.7, cluster_healthy @+8.6
- **partition-primary async haproxy**: fault:partition pg1 from cluster network @+0.0, new_leader:pg3 @+23.1, healed:pg1 @+29.2, cluster_healthy @+61.3
- **dcs-loss async haproxy**: fault:kill etcd @+0.0, etcd_back @+32.7, cluster_healthy @+33.7
- **lagging-replicas-failover async haproxy**: standbys_cut_off:pg1,pg2 @-3.1, standby_lag_at_kill:pg1=518kB,pg2=484kB (limit 1024kB) @-0.4, fault:kill -9 pg3 @+0.0, standbys_reconnected @+1.1, new_leader:pg2 @+20.1, restarted:pg3 @+20.7, cluster_healthy @+26.5
- **replicas-down async haproxy**: fault:stop replicas pg1,pg3 @+0.0, replicas_back @+29.8, cluster_healthy @+32.9
- **kill-primary sync haproxy**: fault:kill -9 pg1 @+0.0, new_leader:pg3 @+22.8, restarted:pg1 @+23.4, cluster_healthy @+32.8
- **stop-primary sync haproxy**: fault:stop pg3 @+0.0, new_leader:pg1 @+3.5, restarted:pg3 @+4.1, cluster_healthy @+6.6
- **switchover sync haproxy**: fault:switchover pg1->pg2 @+0.0, new_leader:pg2 @+4.0, cluster_healthy @+14.2
- **partition-primary sync haproxy**: fault:partition pg2 from cluster network @+0.0, new_leader:pg1 @+23.1, healed:pg2 @+29.0, cluster_healthy @+61.1
- **dcs-loss sync haproxy**: fault:kill etcd @+0.0, etcd_back @+34.5, cluster_healthy @+47.0
- **lagging-replicas-failover sync haproxy**: standbys_cut_off:pg2,pg3 @-3.4, standby_lag_at_kill:pg2=37kB,pg3=0kB (limit 1024kB) @-0.4, fault:kill -9 pg1 @+0.0, standbys_reconnected @+1.5, new_leader:pg3 @+21.4, restarted:pg1 @+22.2, cluster_healthy @+33.0
- **replicas-down sync haproxy**: fault:stop replicas pg1,pg2 @+0.0, replicas_back @+35.1, cluster_healthy @+45.6
- **kill-primary strict haproxy**: fault:kill -9 pg1 @+0.0, new_leader:pg3 @+20.7, restarted:pg1 @+21.8, cluster_healthy @+30.1
- **stop-primary strict haproxy**: fault:stop pg3 @+0.0, new_leader:pg2 @+3.8, restarted:pg3 @+4.6, cluster_healthy @+7.4
- **switchover strict haproxy**: fault:switchover pg2->pg1 @+0.0, new_leader:pg1 @+5.1, cluster_healthy @+12.5
- **partition-primary strict haproxy**: fault:partition pg1 from cluster network @+0.0, new_leader:pg3 @+21.7, healed:pg1 @+27.6, cluster_healthy @+61.6
- **dcs-loss strict haproxy**: fault:kill etcd @+0.0, etcd_back @+33.4, cluster_healthy @+35.2
- **lagging-replicas-failover strict haproxy**: standbys_cut_off:pg1,pg2 @-3.3, standby_lag_at_kill:pg1=45kB,pg2=15kB (limit 1024kB) @-0.3, fault:kill -9 pg3 @+0.0, standbys_reconnected @+1.1, new_leader:pg2 @+22.8, restarted:pg3 @+24.2, cluster_healthy @+38.5
- **replicas-down strict haproxy**: fault:stop replicas pg1,pg3 @+0.0, replicas_back @+35.3, cluster_healthy @+44.2

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


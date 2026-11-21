# Sample report

Real output from `pgdrill` on a laptop (macOS arm64, Podman 5.8.2 machine,
PostgreSQL 17, Patroni 4.1.5, etcd 3.5.17, HAProxy 3.0), 2026-11-21. There were
30 runs in total: the full matrix through HAProxy, the failover scenarios through
libpq multi-host, and DCS loss with `failsafe_mode` off.

Lab timers: `ttl=20 loop_wait=5 retry_timeout=5`, 4 writers, about 430 commits/s.
Production timers are usually longer, and every unplanned-failover number scales
with `ttl`.

## Findings

1. **Only asynchronous replication lost acknowledged writes.** In the
   lagging-replica failover, `async` (Maximum Performance) lost 258 commits that
   the client had seen succeed. `sync` and `strict` lost none, in every scenario.
   The loss covered only the last ~0.3 s before the crash, although the replicas
   were frozen for 3 s. The most likely reason is that WAL kept piling up in the
   paused replicas' socket buffers and was applied after unpause. In other words,
   the size of the loss depends on buffering and timing. It is not a fixed number
   you can plan around.
2. **Unplanned failover costs about `ttl`.** A crash or network partition of the
   primary stopped writes for 18–23 s whatever the replication mode, because the
   leader key has to expire first. To get an RTO comparable to
   `FastStartFailoverThreshold`, tune `ttl`/`loop_wait`/`retry_timeout`. The mode
   doesn't change it.
3. **Planned operations are fast, and the proxy sets the floor.** A switchover
   took 1.5 s through libpq (`target_session_attrs=read-write`) and 2.4 s through
   HAProxy. The difference is HAProxy's health check (`inter 1s fall 2`). A clean
   shutdown of the primary took about 2.5 s either way.
4. **Losing all standbys reproduces the Data Guard protection modes, with one
   difference.** `async` kept writing, `sync` paused 3.4 s and then went on without
   a sync standby (like Maximum Availability), and `strict` blocked every commit for
   27 s until a replica came back (like Maximum Protection). Oracle would have
   shut the primary down; Postgres keeps it up with hung COMMITs.
5. **Losing the DCS is an outage unless `failsafe_mode` is on.** With failsafe,
   killing etcd for 30 s caused no write interruption. Without it, the primary
   demoted itself and writes stopped for 25 s. During that time, 192 new
   connections reached the read-only node and were rejected with `25006`.
6. **Every failover leaves in-doubt commits, and some of them did commit.** Each
   writer had one COMMIT in flight when its connection died (4 per failover). In
   sync/strict lagging-replica failovers all 4 had in fact committed, and in some
   crash/switchover runs 1 of 4 had. The client saw an error for a transaction that
   exists. Without Oracle Application Continuity, the application needs idempotent
   retries or a way to reconcile these, otherwise a naive retry writes twice.
7. **No split brain** in any run, including network partition of the primary.
   Every cluster healed (pg_rewind/rejoin) to 1 leader + 2 streaming replicas.

---


## Run 1: full matrix through HAProxy

### Matrix (routing: haproxy)

Each cell: **longest write outage seen by clients / acknowledged writes lost**.

| Scenario | Maximum Performance (`async`) | Maximum Availability (`sync`) | Maximum Protection (`strict`) |
|---|---|---|---|
| kill-primary | 21.2s / 0 | 18.4s / 0 | 18.8s / 0 |
| stop-primary | 2.4s / 0 | 2.4s / 0 | 2.4s / 0 |
| switchover | 2.4s / 0 | 2.4s / 0 | 3.2s / 0 |
| partition-primary | 22.2s / 0 | 21.2s / 0 | 21.6s / 0 |
| dcs-loss | 0.0s / 0 | 0.0s / 0 | 0.0s / 0 |
| lagging-replicas-failover | 23.4s / 258 | 24.5s / 0 | 28.8s / 0 |
| replicas-down | 0.0s / 0 | 3.4s / 0 | 27.1s / 0 |

### Runs

| Run | Leader | Max gap (s) | Downtime (s) | Lost acked (window s) | In-doubt (committed/total) | Rejected | Split brain | Rejoined | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| kill-primary async haproxy | pg1→pg2 | 21.196 | 21.196 | 0 | 0/4 | 0 | no | yes | PASS |
| stop-primary async haproxy | pg2→pg3 | 2.423 | 2.423 | 0 | 0/4 | 0 | no | yes | PASS |
| switchover async haproxy | pg3→pg1 | 2.42 | 2.42 | 0 | 0/4 | 0 | no | yes | PASS |
| partition-primary async haproxy | pg1→pg2 | 22.246 | 22.246 | 0 | 0/4 | 0 | no | yes | PASS |
| dcs-loss async haproxy | pg2→pg2 | 0.049 | 0 | 0 | 0/0 | 0 | no | yes | PASS |
| lagging-replicas-failover async haproxy | pg2→pg3 | 23.436 | 23.436 | 258 (-0.314..0.114) | 0/4 | 0 | no | yes | FAIL: RPO 258 lost > 0 |
| replicas-down async haproxy | pg3→pg3 | 0.026 | 0 | 0 | 0/0 | 0 | no | yes | PASS |
| kill-primary sync haproxy | pg1→pg3 | 18.359 | 18.359 | 0 | 1/4 | 0 | no | yes | PASS |
| stop-primary sync haproxy | pg3→pg2 | 2.42 | 2.42 | 0 | 0/4 | 0 | no | yes | PASS |
| switchover sync haproxy | pg2→pg1 | 2.421 | 2.421 | 0 | 1/4 | 0 | no | yes | PASS |
| partition-primary sync haproxy | pg1→pg3 | 21.227 | 21.227 | 0 | 0/4 | 0 | no | yes | PASS |
| dcs-loss sync haproxy | pg3→pg3 | 0.041 | 0 | 0 | 0/0 | 0 | no | yes | PASS |
| lagging-replicas-failover sync haproxy | pg3→pg2 | 24.482 | 24.482 | 0 | 4/4 | 0 | no | yes | PASS |
| replicas-down sync haproxy | pg2→pg2 | 3.442 | 3.442 | 0 | 0/0 | 0 | no | yes | PASS |
| kill-primary strict haproxy | pg1→pg3 | 18.765 | 18.765 | 0 | 1/4 | 0 | no | yes | PASS |
| stop-primary strict haproxy | pg3→pg2 | 2.429 | 2.429 | 0 | 0/4 | 0 | no | yes | PASS |
| switchover strict haproxy | pg2→pg1 | 3.221 | 3.221 | 0 | 1/4 | 0 | no | yes | PASS |
| partition-primary strict haproxy | pg1→pg3 | 21.596 | 21.596 | 0 | 0/4 | 0 | no | yes | PASS |
| dcs-loss strict haproxy | pg3→pg3 | 0.033 | 0 | 0 | 0/0 | 0 | no | yes | PASS |
| lagging-replicas-failover strict haproxy | pg3→pg2 | 28.754 | 28.754 | 0 | 4/4 | 0 | no | yes | PASS |
| replicas-down strict haproxy | pg2→pg2 | 27.134 | 27.134 | 0 | 0/0 | 0 | no | yes | PASS |

### Timelines

Seconds relative to the fault.

- **kill-primary async haproxy**: fault:kill -9 pg1 @+0.0, new_leader:pg2 @+22.1, restarted:pg1 @+22.5, cluster_healthy @+26.0
- **stop-primary async haproxy**: fault:stop pg2 @+0.0, new_leader:pg3 @+2.3, restarted:pg2 @+2.7, cluster_healthy @+4.5
- **switchover async haproxy**: fault:switchover pg3->pg1 @+0.0, new_leader:pg1 @+3.3, cluster_healthy @+5.2
- **partition-primary async haproxy**: fault:partition pg1 @+0.0, new_leader:pg2 @+24.7, healed:pg1 @+30.0, cluster_healthy @+40.2
- **dcs-loss async haproxy**: fault:kill etcd @+0.0, etcd_back @+31.9, cluster_healthy @+37.4
- **lagging-replicas-failover async haproxy**: pause_replicas:pg1,pg3 @-3.3, fault:kill -9 pg2 @+0.0, unpause_replicas @+0.5, new_leader:pg3 @+23.4, restarted:pg2 @+23.8, cluster_healthy @+28.6
- **replicas-down async haproxy**: fault:stop replicas pg1,pg2 @+0.0, replicas_back @+28.5, cluster_healthy @+30.5
- **kill-primary sync haproxy**: fault:kill -9 pg1 @+0.0, new_leader:pg3 @+18.3, restarted:pg1 @+18.6, cluster_healthy @+30.3
- **stop-primary sync haproxy**: fault:stop pg3 @+0.0, new_leader:pg2 @+2.1, restarted:pg3 @+2.4, cluster_healthy @+5.7
- **switchover sync haproxy**: fault:switchover pg2->pg1 @+0.0, new_leader:pg1 @+3.1, cluster_healthy @+11.8
- **partition-primary sync haproxy**: fault:partition pg1 @+0.0, new_leader:pg3 @+21.8, healed:pg1 @+27.1, cluster_healthy @+57.6
- **dcs-loss sync haproxy**: fault:kill etcd @+0.0, etcd_back @+32.5, cluster_healthy @+33.0
- **lagging-replicas-failover sync haproxy**: pause_replicas:pg1,pg2 @-3.3, fault:kill -9 pg3 @+0.0, unpause_replicas @+0.5, new_leader:pg2 @+20.9, restarted:pg3 @+21.2, cluster_healthy @+32.9
- **replicas-down sync haproxy**: fault:stop replicas pg1,pg3 @+0.0, replicas_back @+31.0, cluster_healthy @+36.3
- **kill-primary strict haproxy**: fault:kill -9 pg1 @+0.0, new_leader:pg3 @+18.0, restarted:pg1 @+18.4, cluster_healthy @+30.2
- **stop-primary strict haproxy**: fault:stop pg3 @+0.0, new_leader:pg2 @+2.6, restarted:pg3 @+3.0, cluster_healthy @+6.4
- **switchover strict haproxy**: fault:switchover pg2->pg1 @+0.0, new_leader:pg1 @+3.1, cluster_healthy @+11.9
- **partition-primary strict haproxy**: fault:partition pg1 @+0.0, new_leader:pg3 @+22.8, healed:pg1 @+28.1, cluster_healthy @+57.3
- **dcs-loss strict haproxy**: fault:kill etcd @+0.0, etcd_back @+32.5, cluster_healthy @+33.1
- **lagging-replicas-failover strict haproxy**: pause_replicas:pg1,pg2 @-3.3, fault:kill -9 pg3 @+0.0, unpause_replicas @+0.4, new_leader:pg2 @+25.7, restarted:pg3 @+26.1, cluster_healthy @+29.4
- **replicas-down strict haproxy**: fault:stop replicas pg1,pg3 @+0.0, replicas_back @+32.4, cluster_healthy @+38.4

### How to read this

- **Max gap**: the longest interval with no acknowledged commit from any writer after the fault. This is the RTO the application sees.
- **Downtime**: the sum of all gaps longer than 0.5s after the fault.
- **Lost acked**: commits the server acknowledged that are missing on the primary after recovery. This is the RPO. The window is when those commits were acknowledged, relative to the fault.
- **In-doubt**: COMMITs whose connection failed before an answer. Postgres has no transaction replay (unlike Oracle Application Continuity), so the application must retry these idempotently.
- **Rejected**: COMMITs the server refused with a definite error (e.g. 25006, read-only: the connection reached a standby or a demoted primary). Not committed, safe to retry.
- **Split brain**: an older primary kept acknowledging writes after a newer one started.
- Mode labels: `async` ≈ Data Guard Maximum Performance, `sync` (Patroni `synchronous_mode`) ≈ Maximum Availability, `strict` (`synchronous_mode_strict`) ≈ Maximum Protection. Note that Oracle shuts the primary down in Maximum Protection, while Postgres blocks commits.


## Run 2: failover scenarios through libpq multi-host

### Matrix (routing: libpq)

Each cell: **longest write outage seen by clients / acknowledged writes lost**.

| Scenario | Maximum Performance (`async`) | Maximum Availability (`sync`) |
|---|---|---|
| kill-primary | 22.2s / 0 | 19.9s / 0 |
| stop-primary | 2.5s / 0 | 2.5s / 0 |
| switchover | 1.5s / 0 | 1.5s / 0 |
| partition-primary | 23.3s / 0 | 20.9s / 0 |

### Runs

| Run | Leader | Max gap (s) | Downtime (s) | Lost acked (window s) | In-doubt (committed/total) | Rejected | Split brain | Rejoined | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| kill-primary async libpq | pg1→pg3 | 22.163 | 22.163 | 0 | 0/4 | 0 | no | yes | PASS |
| stop-primary async libpq | pg3→pg1 | 2.537 | 2.537 | 0 | 0/4 | 0 | no | yes | PASS |
| switchover async libpq | pg1→pg2 | 1.477 | 1.477 | 0 | 0/4 | 0 | no | yes | PASS |
| partition-primary async libpq | pg2→pg1 | 23.333 | 23.333 | 0 | 0/4 | 0 | no | yes | PASS |
| kill-primary sync libpq | pg1→pg2 | 19.937 | 19.937 | 0 | 1/4 | 0 | no | yes | PASS |
| stop-primary sync libpq | pg2→pg3 | 2.527 | 2.527 | 0 | 0/4 | 0 | no | yes | PASS |
| switchover sync libpq | pg3→pg1 | 1.467 | 1.467 | 0 | 0/4 | 0 | no | yes | PASS |
| partition-primary sync libpq | pg1→pg2 | 20.939 | 20.939 | 0 | 0/4 | 0 | no | yes | PASS |


## Run 3: DCS loss with failsafe_mode off

### Runs

| Run | Leader | Max gap (s) | Downtime (s) | Lost acked (window s) | In-doubt (committed/total) | Rejected | Split brain | Rejoined | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| dcs-loss async haproxy | pg1→pg1 | 25.116 | 25.116 | 0 | 0/4 | 192 | no | yes | PASS |


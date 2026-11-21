# Oracle HA to PostgreSQL HA: what maps, what doesn't, and what to measure

This note covers the operational side of moving an Oracle cluster (RAC and/or
Data Guard) to a PostgreSQL cluster. Schema and code conversion (ora2pg, PL/SQL
rewriting) are out of scope: they are well covered by existing tools. The
cluster side is not, and this is where migration projects tend to get surprised
late, usually during the first failover rehearsal.

## 1. Concept mapping

| Oracle | PostgreSQL (community + common OSS tooling) | Notes |
|---|---|---|
| RAC (shared disk, all instances read-write) | No equivalent. Shared-nothing: one primary, N streaming replicas | Write scale-out across nodes is gone. Apps that spread writes over RAC instances need a single write endpoint. Multi-master exists only as commercial products (EDB PGD) or sharding (Citus), each with its own trade-offs. |
| Data Guard physical standby | Streaming replication (physical WAL shipping) | Close match. |
| Active Data Guard (read-only standby) | Hot standby | Close match. Replica reads can be stale in any replication mode unless `synchronous_commit = remote_apply`. |
| Protection mode **Maximum Performance** | Asynchronous replication | Acknowledged commits can be lost on failover. |
| Protection mode **Maximum Availability** | Patroni `synchronous_mode: true` | Both wait for a sync standby and fall back to async when none is healthy. Patroni only promotes a standby that was synchronous. |
| Protection mode **Maximum Protection** | Patroni `synchronous_mode_strict: true` | Different failure behaviour: Oracle **shuts the primary down**, Postgres **blocks commits** (the primary stays up, sessions hang on COMMIT). |
| Data Guard Broker + Fast-Start Failover + Observer | Patroni + a DCS (etcd/Consul/ZooKeeper) quorum | The DCS plays the observer's role as the arbiter. `ttl` ≈ `FastStartFailoverThreshold`. `maximum_lag_on_failover` plays the role of `FastStartFailoverLagLimit`, but it is measured in bytes of WAL, not seconds. |
| Broker switchover | `patronictl switchover` | Planned, no data loss in any mode. |
| Reinstate the old primary (Flashback Database) | `pg_rewind` (Patroni `use_pg_rewind`) or re-clone | Needs `wal_log_hints` or data checksums. |
| Observer loss | DCS loss | By default Patroni **demotes the primary** when it loses the DCS. `failsafe_mode: true` (Patroni ≥ 3.0) keeps it up as long as it can reach every member. |
| Services (`srvctl add service`), role-based services | HAProxy listeners health-checking Patroni REST (`/primary`, `/replica`), Patroni tags (`noloadbalance`) | No first-class "service" object. Routing is built from parts. |
| SCAN listener / VIP | HAProxy (+ keepalived or vip-manager), DNS, or a client host list | |
| Connect-time failover (`FAILOVER=ON`, `ADDRESS_LIST`) | libpq multi-host: `host=a,b,c target_session_attrs=read-write` | Supported by libpq-based drivers and pgJDBC (`targetServerType=primary`). |
| TAF (reconnects, resumes SELECTs) / Application Continuity (replays in-flight transactions) | **None** | libpq multi-host covers reconnecting, nothing covers replay. In-flight transactions fail and their outcome is unknown to the client (committed or not). The application must retry and be idempotent. |
| FAN (fast application notification) | None native. HAProxy `on-marked-down shutdown-sessions`, Patroni callbacks | Clients find out by having their connection reset. |
| Snapshot standby | None built-in | Clone from backup (pgBackRest, Barman) instead. |
| Far Sync | Cascading replication (partial) | No zero-loss far-sync equivalent. |

## 2. Where migrations get surprised

1. **"Max Protection" doesn't behave the same.** With strict synchronous mode
   and no healthy sync standby, Postgres keeps accepting connections and every
   write hangs on COMMIT. Monitoring that only checks "is the primary up" will
   say everything is fine.
2. **Losing the observer/DCS is an outage by default.** A single etcd node, or an
   etcd cluster that loses quorum, makes Patroni demote the primary unless
   `failsafe_mode` is on.
3. **No transaction replay.** Every failover produces a few in-doubt commits: the
   client sent COMMIT, the connection died, and nobody told it the outcome. With
   Application Continuity the application never saw this.
4. **Cancelling a synchronous-replication wait commits locally.** If a client or
   `statement_timeout` cancels a COMMIT that is waiting for the sync standby,
   Postgres returns a *warning* and the transaction stays committed on the
   primary but not on the standby. A failover right after loses it, even in
   "synchronous" mode.
5. **RTO is mostly configuration.** Failover time is set by `ttl`, `loop_wait`,
   `retry_timeout`, the proxy health-check interval and the driver's TCP
   timeouts. The defaults add up to 30+ seconds. Nobody knows the real number
   until they measure it under load.
6. **An isolated asynchronous primary keeps taking writes.** Cut off from the
   DCS and its standbys but still reachable by clients, the primary acknowledges
   commits until Patroni demotes it (about `retry_timeout` later). After
   failover those writes are gone. In synchronous mode the same commits block
   instead, because no standby can confirm them.

## 3. The gap

Setting up Patroni is well documented, and so are HAProxy configs and
the libpq options. What we could not find in the open-source ecosystem is a tool
that answers the question an Oracle team actually asks before signing off a
migration:

> "With this Postgres cluster and this configuration, what RTO and RPO do we get
> for each failure type, and how does it compare to our Data Guard setup?"

Lab setup tools (for example Percona's anydbver) stop at bringing a cluster up.
Commercial failover managers (EDB Failover Manager) don't publish numbers you can
reproduce. Teams end up running ad-hoc `kill -9` tests and eyeballing logs.

## 4. What `pg-failover-drill` does about it

It runs a repeatable, headless failover drill and turns it into numbers:

- builds a 3-node Patroni + etcd + HAProxy lab on Podman (no Oracle needed,
  synthetic data only), with separate client and cluster networks so a node can
  be cut off from the cluster while clients still reach it
- runs a write workload in which every commit is identified, so the client side
  knows exactly which writes the server acknowledged
- injects one fault per run: primary crash, clean shutdown, switchover, network
  partition, DCS loss, failover with stalled standbys, loss of all replicas
- after recovery it compares acknowledged writes with what the new primary holds:
  - **RPO**: acknowledged writes that were lost
  - **RTO**: the longest write outage seen by clients
  - **in-doubt** commits and how they resolved
  - **split brain**: two nodes acknowledging writes at the same time
- repeats the matrix for the three replication modes, labelled with their
  Data Guard names, through HAProxy or libpq multi-host routing
- writes JSON (for CI and agents) and Markdown (for people), and exits non-zero
  on a safety failure (split brain, no recovery, acknowledged writes lost in
  synchronous mode) or when a run breaks the RTO/RPO thresholds you set

The Patroni timers, the replication mode and `failsafe_mode` are flags, so a team
can set them to the values it plans for production and get numbers before the
cutover instead of after. Pointing the drill at an existing staging cluster,
instead of the built-in lab, is on the roadmap.

## References

- Oracle Data Guard protection modes: <https://docs.oracle.com/en/database/oracle/oracle-database/19/sbydb/oracle-data-guard-protection-modes.html>
- Oracle Data Guard Broker, Fast-Start Failover: <https://docs.oracle.com/en/database/oracle/oracle-database/19/dgbkr/using-data-guard-broker-to-manage-switchovers-failovers.html>
- Patroni replication modes: <https://patroni.readthedocs.io/en/latest/replication_modes.html>
- Patroni DCS failsafe mode: <https://patroni.readthedocs.io/en/latest/dcs_failsafe_mode.html>
- Patroni REST API health checks: <https://patroni.readthedocs.io/en/latest/rest_api.html>
- libpq multi-host connection strings: <https://www.postgresql.org/docs/current/libpq-connect.html#LIBPQ-MULTIPLE-HOSTS>
- Synchronous replication and cancelled waits: <https://www.postgresql.org/docs/current/warm-standby.html#SYNCHRONOUS-REPLICATION>
- AWS Oracle → Aurora PostgreSQL playbook, HA/DR chapter: <https://docs.aws.amazon.com/dms/latest/oracle-to-aurora-postgresql-migration-playbook/chap-oracle-aurora-pg.hadr.html>

# pg-failover-drill: executive summary

## The case

A company runs its core database on an Oracle cluster: RAC for the primary site,
Data Guard for the standby, Fast-Start Failover with an observer. It has decided
to move to PostgreSQL to cut licensing cost and vendor lock-in. Schema and code
conversion are planned with the usual tools. The cluster is a different matter.

The operations team owns availability, and its runbooks and SLAs are written in
Oracle terms: "we run Maximum Availability, failover takes under 30 seconds, we
lose nothing that was committed". Before signing off on the PostgreSQL design,
they need the same sentence for the new cluster, backed by evidence rather than
vendor slides.

## User story

> **As** the DBA/ops lead responsible for the migration,
> **I want** to run repeatable failure drills against the PostgreSQL cluster design
> we intend to deploy, and get RTO, RPO and split-brain results for each failure
> type, labelled in Data Guard terms,
> **so that** I can sign off the target architecture, set realistic SLAs, and
> show auditors and management what changes compared to Oracle before cutover.

### Acceptance criteria

- One command builds a throwaway 3-node Patroni cluster (etcd, HAProxy) on a
  laptop or CI runner with Podman. No Oracle and no production data are needed.
- The drill writes identifiable synthetic transactions during every fault, so
  lost writes can be counted exactly instead of estimated.
- Every fault type the team cares about is covered: primary crash, clean
  shutdown, planned switchover, network partition, loss of the arbiter (DCS),
  failover with stalled standbys, loss of all standbys. A partition cuts the
  primary off from the cluster while clients can still reach it, so split brain
  is actually tested.
- Each run is repeated in the three replication modes, which correspond to
  Maximum Performance, Maximum Availability and Maximum Protection.
- The output is JSON for pipelines and Markdown for people. The exit code fails
  on a safety failure (split brain, writes that never resume, acknowledged writes
  lost in synchronous mode) or when RTO/RPO thresholds are breached, so the drill
  can gate a configuration change in CI. The project's own CI does exactly that.
- Everything runs headless: an engineer, a CI job or an AI agent can drive it.

## How it is used

1. **Design phase.** Run the full matrix with the proposed Patroni timers, sync
   mode and failsafe setting. Read the report and compare it with the
   current Data Guard SLA. Adjust and rerun until the numbers fit, or change the
   SLA consciously.
2. **Sign-off.** Attach the report to the architecture decision. It states, for
   example, that a primary crash in synchronous mode loses no acknowledged
   writes and blocks writes for N seconds, that losing all standbys in strict
   mode stops writes until one returns, and that the in-doubt commits the
   application must be ready to retry number M per failover.
3. **Regression gate.** Keep the drill in CI. Any change to the lab's Patroni
   or HAProxy settings reruns it with thresholds, so a "small tuning change"
   cannot silently double the failover time.
4. **Training.** New PostgreSQL operators see real failovers, rewinds and
   split-brain checks on their own machine in minutes.

## Why it is useful

- **It turns a migration risk into numbers.** Oracle teams lose transaction
  replay (Application Continuity), the observer's semantics change, and
  "Maximum Protection" behaves differently in PostgreSQL (commits hang
  instead of the primary shutting down). The drill shows these differences
  in a test run, before anyone finds them in production.
- **It fills a gap.** Open-source tooling sets up Patroni clusters but does not
  measure them under load. We found no public tool that reports RTO/RPO per
  failure type in Data Guard terms.
- **It is cheap and safe.** It uses synthetic data in disposable containers
  on one machine. It never touches the customer's systems.
- **It can grow with the project.** Running the same drill against a staging
  cluster that reproduces the production settings, rather than the built-in
  lab, is the next step on the roadmap.

## Status

Open source (Apache-2.0):
[kiwifarmit/ol-sbx-pg-failover-drill](https://github.com/kiwifarmit/ol-sbx-pg-failover-drill).
See [analysis.md](analysis.md) for the Oracle-to-PostgreSQL HA concept mapping
and [sample-report.md](sample-report.md) for the results of real runs.

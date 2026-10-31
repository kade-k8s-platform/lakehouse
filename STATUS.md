# Status: Lakehouse

Generated 2026-10-31 12:22 UTC (2026-10-31 21:22 KST) from the organization Project [Lakehouse](https://github.com/orgs/kade-k8s-platform/projects/1) by `project-snapshot`.
Do not edit: change the Project, or add a `cmd:` label to an issue.

## Now (1)

- [lakehouse#31](https://github.com/kade-k8s-platform/lakehouse/issues/31) Failover: a primary node loss costs about 3 minutes of failed queries today; the goal is under 30 s with no confirmed write lost

## In review (0)

- none

## Waiting on Kade (5)

- [lakehouse#9](https://github.com/kade-k8s-platform/lakehouse/issues/9) Give the target clusters' versions and counts the gate must copy
- [lakehouse#89](https://github.com/kade-k8s-platform/lakehouse/issues/89) cert-manager's and the Barman Cloud Plugin's images are pulled by tag on the gate
- [lakehouse#114](https://github.com/kade-k8s-platform/lakehouse/issues/114) Polaris has no automated schema migration: decide how target clusters move JDBC schema v4 to v5 before Polaris 1.7.0
- [lakehouse#135](https://github.com/kade-k8s-platform/lakehouse/issues/135) Decide whether the gate starts polaris-pg with fewer than three instances: each one costs ~30 s of every install
- [lakehouse#168](https://github.com/kade-k8s-platform/lakehouse/issues/168) An upgrade that changes polaris-pg's spec restarts its primary in place: Polaris's requests fail for about 18 s

## Needs Linux test cluster (3)

- [lakehouse#188](https://github.com/kade-k8s-platform/lakehouse/issues/188) Whether one Polaris replica's mixed knee is set by a step's sudden start rather than by its rate
- [lakehouse#189](https://github.com/kade-k8s-platform/lakehouse/issues/189) Whether one Polaris replica's lower mixed knee on 2026-10-17 came from its grown tables
- [lakehouse#190](https://github.com/kade-k8s-platform/lakehouse/issues/190) What ZGC's heap share does to one Polaris replica's allocation stalls in the mixed ramp

## NOT VERIFIED (1)

- [lakehouse#157](https://github.com/kade-k8s-platform/lakehouse/issues/157) ADR-055's runs on main are unproven: Static after an auto-merge, the weekly run, and main-status.yml's issue writes

## Next (0)

- none

## Backlog (14)

- [lakehouse#9](https://github.com/kade-k8s-platform/lakehouse/issues/9) Give the target clusters' versions and counts the gate must copy
- [lakehouse#89](https://github.com/kade-k8s-platform/lakehouse/issues/89) cert-manager's and the Barman Cloud Plugin's images are pulled by tag on the gate
- [lakehouse#114](https://github.com/kade-k8s-platform/lakehouse/issues/114) Polaris has no automated schema migration: decide how target clusters move JDBC schema v4 to v5 before Polaris 1.7.0
- [lakehouse#135](https://github.com/kade-k8s-platform/lakehouse/issues/135) Decide whether the gate starts polaris-pg with fewer than three instances: each one costs ~30 s of every install
- [lakehouse#142](https://github.com/kade-k8s-platform/lakehouse/issues/142) The release gate has no stress test: the target clusters' Polaris serves 6,000–8,000 TPS, mostly table commits, and load will grow
- [lakehouse#157](https://github.com/kade-k8s-platform/lakehouse/issues/157) ADR-055's runs on main are unproven: Static after an auto-merge, the weekly run, and main-status.yml's issue writes
- [lakehouse#168](https://github.com/kade-k8s-platform/lakehouse/issues/168) An upgrade that changes polaris-pg's spec restarts its primary in place: Polaris's requests fail for about 18 s
- [lakehouse#179](https://github.com/kade-k8s-platform/lakehouse/issues/179) An overloaded Polaris is killed by its own liveness probe: saturation becomes a restart
- [lakehouse#188](https://github.com/kade-k8s-platform/lakehouse/issues/188) Whether one Polaris replica's mixed knee is set by a step's sudden start rather than by its rate
- [lakehouse#189](https://github.com/kade-k8s-platform/lakehouse/issues/189) Whether one Polaris replica's lower mixed knee on 2026-10-17 came from its grown tables
- [lakehouse#190](https://github.com/kade-k8s-platform/lakehouse/issues/190) What ZGC's heap share does to one Polaris replica's allocation stalls in the mixed ramp
- [lakehouse#226](https://github.com/kade-k8s-platform/lakehouse/issues/226) The ingestion CronJob has no requests or memory limit yet (ADR-076 §1)
- [lakehouse#231](https://github.com/kade-k8s-platform/lakehouse/issues/231) Smoke's wall time is set by the smoke job since #230: about 19 min, its install step about 11
- [lakehouse#233](https://github.com/kade-k8s-platform/lakehouse/issues/233) Work queue: sub-agent tasks in order, and the decisions that unblock them (2026-10-21)

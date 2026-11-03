# Measure one Polaris replica's capacity on a Linux test server (stress Tier D)

**Status: proven on a Linux test cluster (#174):** a three-node kind cluster, Kubernetes
v1.36.4, CNPG 1.30.1, Polaris 1.6.0, on 2026-10-16. Its measurement is
`docs/measurements/stress-capacity-2026-10-16.md`.

This runbook measures what one Polaris replica serves (ADR-053 §7, standing under ADR-054). It
runs Tier A's scenario in a three-node kind cluster that the gate's own scripts build at the
gate's pins:
- Polaris has one replica, with its CPU and memory set;
- polaris-pg has its three instances;
- Polaris reaches SeaweedFS and OPA directly (ADR-031, ADR-053 §4);
- k6 runs in the same cluster, limited to 2 CPUs.

The commands are those of `.github/workflows/stress.yml`. Tier D differs from Tier A in three
ways:
- **The overlay** `gates/stress/capacity.json`, named by `STRESS_OVERLAY`: longer steps, ramps
  that reach higher, the hold at 80% of the mixed knee for 2 hours, and more CPU for the
  stand-ins. The driver refuses an overlay that would change the scenario itself.
- **Polaris's size**, set in step 3. Its numbers are the run's tuning values (ADR-053 §7).
- **Where it runs:** by hand on a Linux test server, never in CI (ADR-004).

It reports, for each profile:
- the knee for one replica;
- Polaris's and polaris-pg's CPU per request at the knee;
- the hold's percentiles, heap after GC, GC pauses and worker queue;
- how much the tables grew;
- the replicas and polaris-pg cores for 12,000 req/s.

**Run it** when a release changes Polaris's or PostgreSQL's version or settings, or when the
target sizing changes (ADR-053 §7).

**Not covered:**
- Several replicas measured together: that is the shared dev cluster's own ADR.
- The target's real mix of request kinds: the mixed profile's shares are ADR-053's
  assumption until the target's metrics give them (#142).
- Snapshot expiry. The scenario's commits only add snapshots, so tables grow during the run,
  the hot set's most. Step 6 records how much (#174).

## Before you start

- **A Linux test server** with Docker, Python 3 and `git`, about 10 free threads and 20 GB of
  free memory (ADR-004). Nothing here reaches a target cluster.
- **A worktree of the commit to measure**, outside any checkout in use, so this run's `.env`
  is its own. Both it and the run's directory `RUN` live in your home directory, never under
  `/tmp`, which Ubuntu empties at boot. Losing `.env` there loses the credentials the
  cluster's Secrets were made from, and the run's results with it.

  ```sh
  git -C <your clone> fetch origin
  git -C <your clone> worktree add --detach ~/tierd <commit>
  cd ~/tierd
  RUN=~/tierd-run
  mkdir -p "$RUN"
  ```

- **The pinned tools and the gate's environment**, as `stress.yml` sets them:

  ```sh
  python3 gates/gate.py tools "$RUN/bin"
  export PATH="$RUN/bin":$PATH
  export KUBE_CONTEXT=kind-lakehouse-gate NS_CNPG_SYSTEM=cnpg-system \
    NS_CATALOG_POLARIS=catalog-polaris NS_STANDINS=standins
  export STRESS_OVERLAY=gates/stress/capacity.json
  k() { kubectl --context "$KUBE_CONTEXT" -n "$NS_CATALOG_POLARIS" "$@"; }
  ```

  Every command below names the context and a namespace (ADR-005).
- **Step 1 deletes the kind cluster `lakehouse-gate`: Kade approves it when it runs.**
- **Other containers on the same Docker are left alone.** Note what else the server runs
  (`uptime`, `docker ps --format '{{.Names}}' | wc -l`): the measurement names it in neutral
  terms.
- **The run takes about 5 to 6 hours.** Run steps 4 to 6 inside `screen`, so a dropped SSH
  connection does not end them. Each step writes everything it keeps under `OUT`:

  ```sh
  OUT="$RUN"/out
  ```

- **The run holds the server at high load for hours.** A first attempt on 2026-10-16
  stopped 6 minutes into the first ramp: the server went off with nothing in its journal
  (#174). Check the server's cooling and power before a run. Log its temperatures once a
  minute in a second `screen` window, so a repeat leaves evidence; the file is flushed
  after every line:

  ```sh
  while :; do
    printf '%s' "$(date -u +%FT%TZ)"
    for h in /sys/class/hwmon/hwmon*; do
      [ -r "$h/temp1_input" ] && printf ' %s=%s' "$(cat "$h/name")" "$(cat "$h/temp1_input")"
    done
    echo; sync "$RUN/temps.log" 2>/dev/null
    sleep 60
  done >> "$RUN/temps.log"
  ```

  Values are in millidegrees Celsius. **If the server stops, the run stops with it.** kind's
  nodes restart with Docker, but a ramp or the hold does not resume: start again from step 1,
  which builds the cluster fresh.

## 1. Build the cluster (Kade approves)

```sh
kind delete cluster --name lakehouse-gate
python3 gates/gate.py up
python3 gates/gate.py secrets --direct
python3 gates/gate.py standins --direct
python3 gates/gate.py install --fresh
```

- `secrets --direct` writes this worktree's `.env`, with Polaris pointed at the stand-ins
  directly and never through the recorder. It refuses an existing `.env`.
- `standins --direct` takes OPA's and SeaweedFS's CPU limits from the overlay.
- `install --fresh` applies the gate's allow policies, then installs this commit as a
  release (ADR-045, ADR-048).

## 2. Check the context and the stand-ins' limits

```sh
test "$(kubectl config current-context)" = "$KUBE_CONTEXT" && echo "context ok"
kubectl --context "$KUBE_CONTEXT" -n "$NS_STANDINS" get deployment opa seaweedfs \
  -o "custom-columns=NAME:.metadata.name,CPU:.spec.template.spec.containers[0].resources.limits.cpu"
```

Both CPU limits must read the overlay's `standin_cpu`.

## 3. Size Polaris

One replica, requests equal to limits. Without limits, the JVM sizes its heap from the
server's memory. These values are the run's tuning values: change them here and name the
change in the measurement.

```sh
cat > "$RUN"/polaris-capacity.yaml <<'EOF'
replicaCount: 1
resources:
  requests: {cpu: "4", memory: 8Gi}
  limits: {cpu: "4", memory: 8Gi}
EOF
helm --kube-context "$KUBE_CONTEXT" -n "$NS_CATALOG_POLARIS" get values polaris -o yaml \
  > "$RUN"/polaris-values.yaml
helm --kube-context "$KUBE_CONTEXT" -n "$NS_CATALOG_POLARIS" upgrade polaris charts/polaris \
  -f "$RUN"/polaris-values.yaml -f "$RUN"/polaris-capacity.yaml \
  --wait --timeout 10m
k rollout status deployment/polaris --timeout=10m
k get pods -l app.kubernetes.io/name=polaris
k get deployment polaris -o jsonpath='{.spec.replicas} {.spec.template.spec.containers[0].resources}{"\n"}'
```

- **One Polaris pod must be Running and Ready**, with the limits above. The old pods take a
  few seconds to terminate.
- **This is a size change only (ADR-017):** every other value is the one the install gave the
  release. The release's values name Secrets, never their contents.
- **Do not use `--reuse-values`.** It sends `values-lakehouse.yaml`'s
  `podDisruptionBudget.maxUnavailable: ~` to the chart's schema, which refuses it. A values
  file drops the null, as the install does.

## 4. Seed

```sh
python3 gates/stress/driver.py seed "$OUT"
```

- It creates 100 tables of about 1 MB of metadata each, and starts the k6 pod.
- It records the config with the overlay on it (`config.json`), and Polaris's replicas,
  resources and heap maximum (`polaris.json`).
- The last line reads `Polaris: 1 replica(s), resources {...}, heap maximum ...`. Stop if it
  names more than one replica or no limits.

## 5. The ramps

```sh
for profile in commits queries-conditional queries-full mixed; do
  python3 gates/stress/driver.py ramp "$profile" "$OUT"
done
```

- **Each ramp first waits for a settled Polaris:** Ready, with no restart for
  `settle.seconds`. A ramp's last step overloads Polaris on purpose. On the third attempt,
  on 2026-10-16, that overload made the upstream chart's liveness probe restart Polaris
  three times (#179). A ramp
  that started straight after measured the restart, not Polaris.
- Each profile prints one line per step, then `PROFILE: knee N req/s`. A step in which
  Polaris or polaris-pg restarted says so at the end of its line. It gives no CPU per request.
- **A step after which Polaris cannot be read fails** with `Polaris could not be read after
  the step`: its liveness probe restarted it during the step (#179). The ramp waits for a
  settled Polaris, then ends on that step, and the next profile runs.
- **`(not reached: the ramp ended first)`:** the profile passed its last step. Raise that
  profile's `max_steps` in `gates/stress/capacity.json`, run that profile's `ramp` again,
  and name the change in the measurement.
- **`limited by k6`, `opa` or `seaweedfs`:** the knee is that component's, not Polaris's
  (ADR-053 §4). For a stand-in, raise its `standin_cpu`, apply the stand-ins again
  (`python3 gates/gate.py standins --direct`) and run the profile again. k6 keeps its
  2 CPUs (ADR-053 §7): a profile limited by k6 is reported as such.

## 6. The hold

```sh
python3 gates/stress/driver.py hold "$OUT"
```

- After the same wait for a settled Polaris, it runs the mixed profile at 80% of its knee for
  2 hours.
- Every minute it prints Polaris's heap after GC, heap used, GC pauses and worker queue. A
  sample that fails is recorded and the hold goes on.

## 7. Verify, report and the timeline

```sh
python3 gates/stress/driver.py verify "$OUT"
python3 gates/stress/driver.py report "$OUT"
python3 gates/stress/driver.py timeline "$OUT"
```

- **verify** fails on a correctness fault only (ADR-054 §2): a failed request at or below a
  knee or in the hold, a lost or duplicated commit, or a restart of Polaris or polaris-pg.
  - A commit the scenario logged as sent, with no outcome after it, counts as unknown: k6
    cut it short when its step ended.
  - It also prints each table's load after the run, for the metadata growth.
- **report** writes `summary.json` and `summary.md` under `OUT`, and prints the summary:
  - the knees and their CPU per request;
  - each step's Polaris CPU per request, with the share of the threads its JVM names for its
    collector (ADR-061 §4);
  - each step's rate, requests, errors and error rate, and its highest p99;
  - the sizing for 12,000 req/s;
  - Polaris's size, and the hold's percentiles, heap and GC;
  - each step's restarts.
- **timeline** writes `timeline.json`. It holds the Prometheus stand-in's series for the
  whole run, every 15 s (ADR-046), and prints one line a minute:
  - Polaris's CPU, requests per second, mean latency, requests in flight, heap after GC and
    GC time;
  - polaris-pg's waiting backends, longest transaction and replication lag.

  It shows what a knee's step cannot: when Polaris saturated, and whether polaris-pg made
  anything wait. The stand-in keeps a day of data, so run it before rebuilding the cluster.

## 8. Record

The versions measured:

```sh
git rev-parse HEAD
kubectl --context "$KUBE_CONTEXT" get pods -A \
  -o jsonpath='{range .items[*]}{.spec.containers[*].image}{"\n"}{end}' | sort -u
k exec deploy/polaris -- java -version 2>&1 | head -1
k exec deploy/polaris -- sh -c 'tr "\0" " " < /proc/1/cmdline; echo'
```

The last line is the JVM's command line: its options must be `values-lakehouse.yaml`'s
`JAVA_OPTS` (ADR-061).

In a PR, from a branch of the measured commit:
- **`docs/measurements/stress-capacity-<date>.md`** names:
  - the commit and every image, and the JVM's options;
  - the overlay's values and Polaris's size;
  - the server, by cores, threads and memory only (ADR-002), and what else it ran;
  - each profile's knee, CPU per request and what limited it;
  - the hold and the sizing for 12,000 req/s, as estimates for the target's hardware to
    check against #9;
  - the deviations: `previous_versions_max` 5, the seed's reused manifest list, the metadata
    growth, and the mixed profile's assumed shares.
- **`docs/measurements/stress-capacity-<date>.json`** beside it: `OUT/summary.json`.
- **`docs/measurements/stress-capacity-<date>-timeline.json`**: `OUT/timeline.json`.

Close the run's issue (Verification: Needs Linux test cluster, ADR-073) from that PR, and set
it to Verified with `cmd:verify-verified`. A later run is a new file (ADR-003).

## Afterwards

The cluster stays as it is, with one Polaris replica. A gate run or another runbook that needs
the released size installs again from step 1. Remove the worktree when you are done:

```sh
cd / && git -C <your clone> worktree remove ~/tierd
```

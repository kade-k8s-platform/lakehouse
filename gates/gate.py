"""The kind gate (ADR-017, ADR-029, ADR-038): pinned tools, the cluster, the CNPG operator.

  tools BIN_DIR [NAME...]
                  downloads kind, kubectl, helm and kubeconform (or only the NAMEs) from
                  gates/pins.json into BIN_DIR, each checked against its pinned sha256 before
                  anything is written.
  up              creates the kind cluster (one control-plane, two workers, the pinned node
                  image), lets the control-plane take pods, so three nodes hold the three
                  PostgreSQL instances apart, then installs the CNPG operator from its pinned
                  chart archive into NS_CNPG_SYSTEM and waits for it. The operator exists here
                  only because the gate has no operator team (ADR-029); a release never
                  installs it. After it, cert-manager and the Barman Cloud Plugin from their
                  pinned release manifests, the plugin in NS_CNPG_SYSTEM, each waited for
                  (ADR-043). Before CNPG, kind's local-path-provisioner gets the class
                  `shared` on a runner directory every node mounts: ReadWriteMany for the
                  log claim (ADR-036).
  secrets [--direct]
                  writes `.env` for the gate (its context, the stand-ins' endpoints and
                  the S3 stand-in's four keys, Polaris's, the smoke tests' and each
                  PostgreSQL's backups', which are the gate's own, ADR-031, ADR-043, ADR-066),
                  then runs
                  scripts/init_env.py --apply-secrets (ADR-006). Refuses an existing `.env`.
                  With --direct, for a stress install (ADR-053 §4), the endpoints name
                  SeaweedFS, OPA and polaris-pg's read-write service, never the recorder.
  standins        applies the stand-ins (gates/standins.py: SeaweedFS for S3, OPA with
                  gates/opa/policy.rego) in NS_STANDINS and waits for them (ADR-031), then
                  creates the backup buckets, Polaris's and Datahub's, in SeaweedFS and checks
                  them (ADR-043, ADR-066). Never
                  part of a release; the install's endpoint check needs them on the gate.
                  With them the recorder (ADR-034), with a TLS certificate made here, and
                  Prometheus loading docs/contracts/polaris-prometheus-scrape.yaml (ADR-046), and
                  the log stand-ins (ADR-081): OpenSearch 3.6.0 (`logs-opensearch`) and a main
                  Fluent Bit (`main-fluent-bit-standin`, forward on 24224).
                  `standins --direct` leaves the log stand-ins out; SeaweedFS and OPA also take
                  connections from Polaris's namespace and run under the CPU limits in gates/stress/config.json, or in
                  the overlay STRESS_OVERLAY names (Tier D's capacity.json, ADR-053 §7).
  check-standins  runs gates/check_standins.sh in a short-lived curl pod in Polaris's namespace:
                  an S3 round trip with Polaris's credentials, and every case of
                  gates/opa/cases.json against the policy endpoint (ADR-026, ADR-031); then
                  gates/check_db.sh in a pod on Polaris's PostgreSQL image: a login as Polaris
                  with and without TLS, a row written and read. All through the recorder.
  check-traffic FILE...
                  reads every record the recorder wrote and matches them against each
                  expectation file (gates/expectations/*.json, gates/traffic.py): exchanges that
                  must have happened and ones that must not (ADR-034). Prints counts, never a body.
  install [--fresh] [--dry-run]
                  installs the latest release HEAD contains (released_tags) from a checkout of
                  its tag, with the gate's `.env` copied into it, runs `seed` over it, then
                  this commit's release over it (ADR-051); with no tag, or with
                  --fresh, only this commit's release. With --dry-run, this commit's release is
                  a server-side dry-run over the installed one (ADR-038). Prints which it did,
                  then the image pulls since it started, from the kubelet's Pulled events:
                  each image's pulls and nodes, and the busiest node's pull time against the
                  install's (#125). Then, unless a dry-run alone installed nothing, its
                  timeline read back from Helm and the cluster: each Helm revision (a step of
                  release.py starts there), Job, rolled-out Deployment and Ready CNPG Cluster
                  since it started, with each of its own commands and the events of Jobs and
                  CNPG Clusters, which outlive the initdb and join Jobs CNPG deletes, and of
                  release.py's endpoint-check and bootstrap pods and log claim (#125). A
                  measurement that fails says so and never fails it.
  install --datahub-only
                  Datahub alone, fresh, from this commit, for smoke's datahub-recovery job
                  (ADR-077 §1): release.py's own functions, step 0 then Datahub's part of step
                  1 (its network policies, datahub-pg, datahub-kafka, datahub-opensearch,
                  Datahub), in ADR-045's order; the first step with a problem stops the rest.
                  No last release, seed, gate-policies, Polaris, log batch or console. Prints
                  each step's notes, then the image pulls and the timeline as `install` does,
                  each step timed under its name.
  seed          writes known data through the last release's Polaris before the upgrade
                  (ADR-051 §2): gates/seed.py in a short-lived pod on the pinned Python image,
                  mounted with iceberg/smoke.py and its hashed requirements from the ConfigMap
                  migration-seed, as root and as gate_seed (principals, roles, grants, a
                  catalog, nested namespaces, tables over two snapshots and a schema change, a
                  view, a policy attached three ways), then gates/db_counts.sh in a pod on
                  Polaris's PostgreSQL image counts each table of polaris_schema and reads the
                  schema version, as Polaris, straight to polaris-pg. What seed.py read back
                  and the counts are kept on the runner in SEED_RECORD, never printed: they
                  hold the seeded principals' credentials. `install` runs it between the two
                  releases, only when it upgrades for real.
  check-migration after the upgrade (ADR-051 §3): seed.py, given the record on stdin, reads
                  every seeded object back with every recorded field and value (a field the
                  new version adds is listed), gets each seeded principal a token, reads each
                  table at each recorded snapshot and appends; then no table of polaris_schema
                  may have fewer rows than recorded, and the schema version before and after is
                  printed. Without a record, a fresh install: it says so and passes.
  rebuild-replica deletes one replica of polaris-pg, never the primary: its PVC without waiting,
                  then its pod. CNPG makes a new instance through a join pod that clones the
                  primary under the network policies; it passes once a new join Job ran and the
                  Cluster is Ready with all its instances, within REBUILD_TIMEOUT s. On failure,
                  the join pods' logs (ADR-048 §7). Gate only: it deletes a volume.
  check-polaris-pg
                  compares the running `Cluster` polaris-pg with the values its helm release
                  was installed with, its shutdown timeout and synchronous replication among
                  them (ADR-056), and checks its instances run on different nodes (ADR-016).
                  When the values turn on failoverQuorum, its `FailoverQuorum` object must
                  report quorum over `number` standbys (ADR-058 §3).
  check-datahub-pg
                  compares the running `Cluster` datahub-pg with the values its helm release
                  was installed with, requires it asynchronous with every instance ready on
                  its own node, then switches WAL on its primary and waits until the segment
                  is archived through the plugin, then takes an on-demand `Backup` and waits
                  until it completes (ADR-066 §5).
  check-datahub-kafka
                  from a short-lived pod in Datahub's namespace with Kafka's client label (never
                  the check label) and the image datahub-kafka's helm release runs: creates a
                  topic, produces a 4.5 MiB message and consumes it back unchanged; deletes the
                  broker's pod, waits until a new one is ready, and reads the message again from
                  its volume; deletes the topic (ADR-068 §5). The check pod is deleted whatever
                  happened.
  check-datahub-opensearch
                  from a short-lived pod in Datahub's namespace with OpenSearch's client label
                  (never the check label) and the image datahub-opensearch's helm release runs:
                  over plain HTTP without credentials, creates an index with one shard and no
                  replica, waits for it to be green, writes one document and finds it by search;
                  deletes the node's pod, waits until a new one is ready, and finds the index
                  green and the document again on its volume; deletes the index (ADR-070 §5).
                  The check pod is deleted whatever happened.
  check-datahub   both system-update Jobs completed (ADR-071 §8); then gates/datahub/check.py in
                  a short-lived pod on the pinned Python image, in Datahub's namespace with GMS's
                  client label (never the check label), as the system client: GMS answers
                  /health, a dataset written is read back by its URN and found by search within
                  DATAHUB_SEARCH_SECONDS, then hard-deleted; then the same script from a pod in
                  the gate's ingress-class namespace, which it creates if missing (ADR-072 §4):
                  the frontend signs in `datahub` with the password in datahub-root-user and
                  refuses `datahub:datahub`. Each credential is read from its Secret and sent
                  on the pod's stdin, never printed or put on a command line.
  check-datahub-ingestion
                  one ingestion of a Polaris catalog into Datahub (ADR-076 §6), every pod in
                  Datahub's namespace, never with the check label: gates/datahub/check.py
                  `seed`, as root, creates gate_ingest_catalog on the S3 endpoint as the smoke
                  tests create theirs, its namespace `ingest` and two tables, one with a nested
                  struct column, keeping what is there; then a Job from the ingestion CronJob
                  (`kubectl create job --from`), waited on until it completes within
                  INGESTION_TIMEOUT s, its `catalog` lines and time printed, its log on failure;
                  then `ingested`, from a pod with GMS's client label as the system client:
                  both datasets by the URNs the source makes with the catalog as platform
                  instance, their fields, nested ones included, and their container; then
                  `refused`, from an unlabelled pod mounting datahub-polaris-principal:
                  datahub_ingest is refused a namespace create and a load with vended
                  credentials, and allowed the same load without them; then `clean`
                  hard-deletes the datasets and the container, whatever failed after the Job
                  was created. The catalog stays. Credentials are read from their Secrets and
                  sent on stdin, never printed or put on a command line.
  check-datahub-recovery
                  on the gate's own context only, since it deletes volumes (ADR-071 §8,
                  ADR-004): check.py's steps write a dataset and find it by search; then
                  datahub-opensearch-0's claim and pod are deleted, a new pod is ready on a new
                  claim without datasetindex_v2, Datahub's install step runs again, its
                  system-update recreating the index, a Job from the restore-indices template
                  completes and the dataset is found again; then datahub-kafka-0's claim and pod
                  are deleted and Datahub's topics are gone, the install step runs again and
                  they are back, GMS's pod is deleted and a new one is ready, the dataset reads
                  back by its URN and a second one is written and found. Both datasets are
                  hard-deleted and the check pods deleted whatever happened. When it ends,
                  passed, failed or stopped, it prints its timeline (ADR-077 §3): the seconds
                  after it started, and since the line before, of each phase's start and end
                  by its own clock, and each install step's Helm revision, system-update's
                  Jobs and the restore-indices Job by their own times, read right after each
                  step; a read or a timeline that fails is said, never a problem.
  check-polaris   compares the running Polaris (its Deployment, pods and ConfigMap) with the
                  values its helm release was installed with and with `.env`, checks its
                  replicas run on different nodes (ADR-016), then runs gates/check_polaris.sh
                  in a short-lived curl pod: ready, a token for root, and each pod accepting
                  it (ADR-026).
  console-image   builds the Polaris console's image (ADR-028): apache/polaris-tools fetched at
                  the commit in images/polaris-console/source.json and checked, its console/ as
                  the context with this repository's package-lock.json, built with
                  images/polaris-console/Dockerfile, then loaded into the kind cluster.
                  --build stops before the load and needs no cluster, so smoke runs it while
                  kind starts; --load loads the image it built.
  batch-image     builds the log batch's image from images/polaris-log-batch, tagged as
                  install/release.py names it, and loads it into the kind cluster (ADR-036).
  check-logs      follows traffic to the hand-off (ADR-024, ADR-036): gates/logs_traffic.sh in
                  a short-lived curl pod makes the catalog gate_logs on the S3 endpoint, then
                  sends each Polaris pod reads with root's token, requests without one, a 400,
                  a 404 and a table made, each with its own X-Request-ID; a Job from the
                  batch's CronJob runs with its clock past that hour, then again; a stand-in
                  Fluent Bit (pinned, the claim read-only) reads processed-logs/ and
                  aggregated-logs/. Every line and row it read must hold contract v2
                  (gates/contract.py) and every summary's books balance; each refused request
                  must be kept once from its pod, no read kept but all counted on the rows; the
                  policy's rules on Polaris's own lines: the 404s dropped with their
                  application lines (3'), the 400s and the catalog made kept with theirs (2b),
                  each commit counted on its table's row (2a); both pods' files read; the second
                  run must change no file.
  check-console   runs gates/console/check.py (on stdin) in a short-lived pod on the pinned
                  Python image: the console answers and points the browser at Polaris, and
                  gate_maintainer signs in as the browser does, from the console's origin,
                  lists the catalogs and is refused a write (ADR-028, ADR-026).
  check-iceberg   runs gates/iceberg/smoke.py in a short-lived pod on the pinned Python image,
                  mounted with its hashed requirements from the ConfigMap iceberg-smoke: PyIceberg
                  as gate_smoke, with the smoke tests' own S3 key (Secret smoke-s3), writes a
                  table into gate_catalog on the S3 endpoint and reads it back, and the policy
                  endpoint refuses what the gate's policy does not grant (ADR-026, ADR-031).
  check-restore   proves a restore (ADR-043 §4): an on-demand `Backup` of polaris-pg through
                  the Barman Cloud Plugin, waited for until completed; then a WAL switch on
                  polaris-pg's primary, waited for until archived, since a backup from a
                  standby is consistent only past it (#86); then the `Cluster`
                  polaris-pg-restore, one instance bootstrapped from the ObjectStore
                  polaris-backup, its base backup and archived WAL to the latest, waited for
                  until Ready, read with its full-recovery Job's pods every few seconds: each
                  failed pod's logs printed as soon as it fails, before CNPG deletes it, then
                  every pod seen, when it ran and how it ended (#86); then healthy on several
                  reads in a row, past CNPG's restart after the recovery; then
                  gates/check_restore.sh in a pod on
                  Polaris's PostgreSQL image reads from it, as Polaris, the realm's root
                  principal and gate_catalog.
                  polaris-pg-restore is deleted whatever happened.

  gate-policies   applies the gate's own allow policies in Polaris's namespace (gates/network.py,
                  ADR-048 §5): the check pods, labelled lakehouse-gate/check, reach every pod;
                  the PostgreSQL recorder reaches the CNPG instances on 5432. `install` runs
                  it before the first release it installs for real: the last release, or this
                  commit's when there is none to upgrade from.
  check-network   creates a namespace per caller class with the gate's label, one without any
                  label and Datahub's, then from a short-lived pod in each, from an
                  unlabelled pod in Polaris's namespace, and from a pod with CNPG's join label
                  in Polaris's namespace and in the unlabelled one, all at once, connects to
                  each target of gates/network.py's CASES: each allowed one must connect, each
                  refused one must time out or be reset (ADR-048 §6). One that should be
                  refused and connects means the cluster does not enforce NetworkPolicy.
                  polaris-pg's exporter on 9187 is reached through a Service the gate adds
                  (ADR-046 §4).
  check-metrics   asks the Prometheus stand-in, from a check pod, until every target is up:
                  `up` 1 for every running Polaris pod and polaris-pg instance, Polaris's
                  http_server_requests_seconds_count above 0, cnpg_collector_up 1 on every
                  instance (gates/metrics.py, ADR-046 §5). On a problem, every target with
                  its last error.
Every kubectl and helm call names the gate's context, and a namespace where the object has
one. Nothing here reaches any cluster but the kind cluster it creates (ADR-017).

Usage: gate.py tools BIN_DIR [NAME...] | up | secrets | standins | check-standins | check-traffic FILE... | install [--fresh] [--dry-run] | install --datahub-only | seed | check-migration | rebuild-replica | check-polaris-pg | check-datahub-pg | check-datahub-kafka | check-datahub-opensearch | check-datahub | check-datahub-ingestion | check-datahub-recovery | check-polaris | console-image [--build|--load] | batch-image | check-logs | check-console | check-iceberg | check-restore | gate-policies | check-network | check-metrics
"""

import base64
import concurrent.futures
import copy
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "install"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import contract  # noqa: E402
import init_env  # noqa: E402
import metrics  # noqa: E402
import network  # noqa: E402
import release  # noqa: E402
import release_tag  # noqa: E402
import seed  # noqa: E402
import standins  # noqa: E402
import traffic  # noqa: E402

CLUSTER = "lakehouse-gate"
KIND_CONFIG = os.path.join(HERE, "kind.yaml")
CONTEXT = f"kind-{CLUSTER}"
TOOLS = ("kind", "kubectl", "helm", "kubeconform")
# The binary inside each tool's archive; the others are plain binaries.
ARCHIVE_MEMBERS = {"helm": "linux-amd64/helm", "kubeconform": "kubeconform"}
# The release manifests' own names (gates/pins.json `manifests`, ADR-043 §3).
CERT_MANAGER_NAMESPACE = "cert-manager"
CERT_MANAGER_DEPLOYMENTS = ("cert-manager", "cert-manager-cainjector", "cert-manager-webhook")
PLUGIN_DEPLOYMENT = "barman-cloud"
# On the gate the S3 stand-in is the gate's own, so the gate makes up its credentials, Polaris's
# the smoke tests' and each PostgreSQL's backups'; on a test or target cluster they are another
# team's and typed in by hand (ADR-031, ADR-043, ADR-066).
GATE_ENV_GENERATED = ("POLARIS_S3_ACCESS_KEY_ID", "POLARIS_S3_SECRET_ACCESS_KEY",
                      "SMOKE_S3_ACCESS_KEY_ID", "SMOKE_S3_SECRET_ACCESS_KEY",
                      "POLARIS_BACKUP_S3_ACCESS_KEY_ID", "POLARIS_BACKUP_S3_SECRET_ACCESS_KEY",
                      "DATAHUB_BACKUP_S3_ACCESS_KEY_ID", "DATAHUB_BACKUP_S3_SECRET_ACCESS_KEY")
# On the gate, the endpoints of other teams' services are the stand-ins' (ADR-031).
STANDIN_REGION = "us-east-1"
# The stress scenario's tuning values (ADR-053, ADR-000 rule 11).
STRESS_CONFIG = os.path.join(HERE, "stress", "config.json")
# What an overlay on it may change (ADR-053 §1, §7): the rates, the step length, the hold, the
# stand-ins' CPU and k6's VUs; never the shares, the pass rules, the seed or k6's CPU.
STRESS_OVERLAY_KEYS = {"step_seconds": True,
                       "profiles": {"*": {"start": True, "step": True, "max_steps": True}},
                       "hold": {"fraction": True, "seconds": True, "sample_seconds": True},
                       "settle": {"seconds": True, "timeout_seconds": True},
                       "standin_cpu": {"*": True},
                       "k6": {"pre_vus": True, "max_vus": True}}
STANDIN_BUCKET = "polaris-warehouse"
STANDIN_BACKUP_BUCKET = "polaris-backup"
STANDIN_DATAHUB_BACKUP_BUCKET = "datahub-backup"
# Polaris's own S3 key, as the AWS SDK reads it: catalogs without STS are read and written with
# it (charts/polaris/values-lakehouse.yaml).
POLARIS_S3_ENV = {"AWS_ACCESS_KEY_ID": "accessKeyId", "AWS_SECRET_ACCESS_KEY": "secretAccessKey"}
# Polaris's pods and ports, as charts/polaris renders them.
POLARIS_POD_LABELS = "app.kubernetes.io/name=polaris,app.kubernetes.io/instance=polaris"
POLARIS_API_PORT, POLARIS_MGMT_PORT = 8181, 8182
# Every pod the gate starts in Polaris's namespace to check it carries this label, which the
# gate's own allow policy lets reach every pod there (ADR-048 §5).
CHECK_POD_LABEL = f"--labels={network.CHECK_LABEL}=true"
# The catalog the smoke tests create and the gate's policy names (gates/opa/policy.rego).
ICEBERG_CATALOG = "gate_catalog"
# The catalog Datahub ingests on the gate (ADR-076 §6), named in the gate's .env.
INGEST_CATALOG = "gate_ingest_catalog"
# ReadWriteMany on kind for the log claim (ADR-036): one runner directory mounted into every
# node (gates/kind.yaml), offered by kind's local-path-provisioner as the class `shared`
# through its sharedFileSystemPath. A stand-in, never released (ADR-031).
SHARED_HOST_DIR = "/tmp/lakehouse-gate-shared"
SHARED_NODE_DIR = "/var/local-path-shared"
SHARED_CLASS = "shared"
GATE_LOGS_SIZE = "1Gi"
# kind's own default, which the class `standard` keeps.
STANDARD_NODE_PATHS = [{"node": "DEFAULT_PATH_FOR_NON_LISTED_NODES",
                        "paths": ["/var/local-path-provisioner"]}]
# The Polaris console's image, built here from a pinned polaris-tools commit (ADR-028).
CONSOLE_IMAGE_DIR = os.path.join(ROOT, "images", "polaris-console")
CONSOLE_IMAGE_REPOSITORY = "lakehouse/polaris-console"
CONSOLE_PORT = 8080
# The log batch on the gate (ADR-036): requests sent to each Polaris pod, how long a run of the
# batch or of the stand-in Fluent Bit may take, and where the claim is mounted to be read.
LOGS_REQUESTS = 5
LOGS_CATALOG = "gate_logs"
LOGS_NAMESPACE = "logs"
LOGS_POLICY_KINDS = ("invalid", "missing", "commit")
APP_LINE_LOGGERS = ("org.apache.polaris.service.admin.PolarisServiceImpl",
                    "org.apache.polaris.service.exception.IcebergExceptionMapper")
LOGS_TIMEOUT = 300
LOGS_MOUNT = "/logs"
OUTPUT_DIRS = ("processed-logs", "aggregated-logs")
# The stand-in reads as a user that is neither root nor Polaris's and the batch's (10000), as
# the observability team's reader would (ADR-024).
READER_UID = 65534
# Run in a short-lived pod with the claim at $1, read-only: one line per output file, its
# SHA-256, its line count and its path under the claim.
LOGS_LISTING = """cd "$1" || exit 1
for f in processed-logs/*.jsonl aggregated-logs/*.jsonl; do
  [ -f "$f" ] || continue
  printf '%s %s %s\\n' "$(sha256sum < "$f" | cut -c1-64)" "$(($(wc -l < "$f")))" "$f"
done
"""


def load_pins(path=os.path.join(HERE, "pins.json")):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def download(url):
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 (pinned https)
        return response.read()


def fetch(url, sha256, get=download):
    data = get(url)
    actual = hashlib.sha256(data).hexdigest()
    if actual != sha256:
        raise ValueError(f"{url}: sha256 {actual}, pinned {sha256}")
    return data


def tools(pins, bin_dir, names=TOOLS, get=download):
    """Fetches and checks every named tool first, then writes them; a bad hash writes nothing."""
    for name in names:
        if name not in TOOLS:
            raise KeyError(f"{name}: not a gate tool ({', '.join(TOOLS)})")
    blobs = {name: fetch(pins[name]["url"], pins[name]["sha256"], get) for name in names}
    for name, member in ARCHIVE_MEMBERS.items():
        if name in blobs:
            with tarfile.open(fileobj=io.BytesIO(blobs[name]), mode="r:gz") as tar:
                blobs[name] = tar.extractfile(member).read()
    os.makedirs(bin_dir, exist_ok=True)
    for name, data in blobs.items():
        path = os.path.join(bin_dir, name)
        with open(path, "wb") as f:
            f.write(data)
        os.chmod(path, 0o755)


def shared_storage_class():
    return {"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass",
            "metadata": {"name": SHARED_CLASS}, "provisioner": "rancher.io/local-path",
            "reclaimPolicy": "Delete", "volumeBindingMode": "WaitForFirstConsumer"}


def plugin_manifest(text, namespace):
    """The plugin's release manifest, refused unless every namespace it names is `namespace`:
    it names its own, and the plugin must run in the operator's (ADR-043 §3)."""
    named = {line.split(":", 1)[1].strip() for line in text.splitlines()
             if line.strip().startswith("namespace:")}
    if not named:
        raise ValueError("the plugin's manifest names no namespace")
    if named != {namespace}:
        raise ValueError(f"the plugin's manifest names {', '.join(sorted(named))}, "
                         f"not the operator's namespace {namespace}")
    return text


def up_commands(pins, chart_path, namespace, manifests, kind_config=KIND_CONFIG):
    """(argv, stdin) in order: the cluster from `kind_config`, the control-plane taken off
    NoSchedule, the shared class next to kind's `standard` (the provisioner restarted to read
    it), CNPG, then cert-manager and the plugin from `manifests` (name to text), each waited
    for."""
    config = {"nodePathMap": [],
              "storageClassConfigs": {"standard": {"nodePathMap": STANDARD_NODE_PATHS},
                                      SHARED_CLASS: {"sharedFileSystemPath": SHARED_NODE_DIR}}}
    provisioner = ["kubectl", "--context", CONTEXT, "-n", "local-path-storage"]
    # Server-side: cert-manager's CRDs are too large for the last-applied annotation.
    apply = ["kubectl", "--context", CONTEXT, "apply", "--server-side", "--field-manager=gate",
             "-f", "-"]
    return [
        (["kind", "create", "cluster", "--name", CLUSTER,
          "--config", kind_config,
          "--image", pins["node_image"]["image"], "--wait", "180s"], None),
        (["kubectl", "--context", CONTEXT, "taint", "nodes", f"{CLUSTER}-control-plane",
          "node-role.kubernetes.io/control-plane:NoSchedule-"], None),
        (provisioner + ["patch", "configmap", "local-path-config", "--type", "merge", "-p",
                        json.dumps({"data": {"config.json": json.dumps(config)}})], None),
        (["kubectl", "--context", CONTEXT, "apply", "-f", "-"],
         json.dumps(shared_storage_class())),
        (provisioner + ["rollout", "restart", "deployment/local-path-provisioner"], None),
        (provisioner + ["rollout", "status", "deployment/local-path-provisioner",
                        "--timeout=3m"], None),
        (["helm", "--kube-context", CONTEXT, "install", "cnpg", chart_path,
          "--namespace", namespace, "--create-namespace", "--wait", "--timeout", "5m"], None),
        (apply, manifests["cert_manager"]),
        (["kubectl", "--context", CONTEXT, "-n", CERT_MANAGER_NAMESPACE, "rollout", "status",
          *[f"deployment/{d}" for d in CERT_MANAGER_DEPLOYMENTS], "--timeout=5m"], None),
        (apply, manifests["barman_cloud_plugin"]),
        (["kubectl", "--context", CONTEXT, "-n", namespace, "rollout", "status",
          f"deployment/{PLUGIN_DEPLOYMENT}", "--timeout=5m"], None),
    ]


def standin_endpoints(ns, direct=False):
    """The `.env` values for the gate: every connection Polaris makes goes through the
    recorder in namespace `ns`, which forwards it to the stand-in or to polaris-pg (ADR-034).
    With `direct` (a stress install, ADR-053 §4), Polaris reaches SeaweedFS, OPA and polaris-pg's
    read-write service itself."""
    recorder = f"recorder.{ns}.svc.cluster.local"
    s3, opa = ((f"seaweedfs.{ns}.svc.cluster.local", f"opa.{ns}.svc.cluster.local") if direct
               else (recorder, recorder))
    return {
        "S3_ENDPOINT": f"http://{s3}:{standins.S3_PORT}",
        "S3_REGION": STANDIN_REGION,
        "POLARIS_WAREHOUSE_BUCKET": STANDIN_BUCKET,
        "POLARIS_BACKUP_BUCKET": STANDIN_BACKUP_BUCKET,
        "DATAHUB_BACKUP_BUCKET": STANDIN_DATAHUB_BACKUP_BUCKET,
        "POLARIS_OPA_URL": f"http://{opa}:{standins.OPA_PORT}{standins.OPA_PACKAGE_PATH}",
        "POLARIS_DB_HOST": release.POLARIS_PG_SERVICE if direct else recorder,
    }


def _overlay(base, overlay, allowed, where):
    """`base` with `overlay`'s values, each of which `allowed` must name ("*" for any key base
    has) and base must hold, with the same type; anything else is a ValueError."""
    merged = dict(base)
    for key, value in overlay.items():
        if key.startswith("_"):
            continue
        rule = allowed.get(key, allowed.get("*")) if isinstance(allowed, dict) else None
        path = f"{where}{key}"
        if rule is None or key not in base:
            raise ValueError(f"{path}: not a value the overlay may change")
        if isinstance(rule, dict):
            if not isinstance(value, dict) or not isinstance(base[key], dict):
                raise ValueError(f"{path}: expected an object")
            merged[key] = _overlay(base[key], value, rule, f"{path}.")
        elif type(value) is not type(base[key]) and not (
                isinstance(value, (int, float)) and isinstance(base[key], (int, float))
                and not isinstance(value, bool)):
            raise ValueError(f"{path}: {value!r} is not a {type(base[key]).__name__}")
        else:
            merged[key] = value
    return merged


def stress_config(environ=None, path=STRESS_CONFIG):
    """gates/stress/config.json, with the overlay STRESS_OVERLAY names (from the repository's
    root, or absolute) on top: Tier D's, gates/stress/capacity.json (ADR-053 §7). An overlay
    changes only the rates, the step length, the hold, the stand-ins' CPU and k6's VUs, so
    both tiers run the same scenario (ADR-053 §1)."""
    environ = os.environ if environ is None else environ
    with open(path, encoding="utf-8") as f:
        config = json.load(f)
    name = environ.get("STRESS_OVERLAY")
    if not name:
        return config
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return _overlay(config, json.load(f), STRESS_OVERLAY_KEYS, "")


def stress_standin_cpu(path=STRESS_CONFIG, environ=None):
    """The CPU limit of each stand-in a stress install reaches directly (ADR-053 §4)."""
    limits = stress_config(environ, path)["standin_cpu"]
    return {name: cpu for name, cpu in limits.items() if not name.startswith("_")}


def console_settings(ns):
    """The `.env` values for the gate's console, in Polaris's namespace `ns`: enabled, opened
    at its Service, and reaching Polaris at its Service (ADR-028)."""
    return {
        "POLARIS_CONSOLE_ENABLED": "true",
        "POLARIS_CONSOLE_ORIGIN": f"http://{release.CONSOLE}.{ns}.svc.cluster.local:{CONSOLE_PORT}",
        "POLARIS_CONSOLE_API_URL": f"http://{release.POLARIS}.{ns}.svc.cluster.local:"
                                   f"{POLARIS_API_PORT}",
    }


def gate_env(template_text, gen=init_env.generate, direct=False):
    """`.env` for the gate: its context, the stand-ins' endpoints (`direct`: see
    standin_endpoints), the S3 stand-in's credentials, the console's settings, the log claim's
    shared class and a namespace label per caller class (ADR-048 §6); init_env fills the rest."""
    template = init_env.parse(template_text)
    fixed = {"KUBE_CONTEXT": CONTEXT, **standin_endpoints(template["NS_STANDINS"], direct),
             **console_settings(template["NS_CATALOG_POLARIS"]),
             "POLARIS_LOGS_STORAGE_CLASS": SHARED_CLASS, "POLARIS_LOGS_SIZE": GATE_LOGS_SIZE,
             "DATAHUB_INGEST_POLARIS_CATALOGS": INGEST_CATALOG,
             **network.GATE_CALLER_LABELS}
    lines = []
    for line in template_text.splitlines():
        name = line.split("=", 1)[0]
        if name in fixed:
            line = f"{name}={fixed[name]}"
        elif name in GATE_ENV_GENERATED:
            line = f"{name}={gen(name)}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def write_gate_env(path, template_text, direct=False):
    if os.path.exists(path):
        sys.exit(f"{path} exists: the gate writes its own .env and never overwrites one")
    init_env.write_env(path, gate_env(template_text, direct=direct))


def standins_commands(env, manifest, direct=False):
    """(argv, stdin) in order: apply the stand-ins' List `manifest`, wait until all four are
    ready (and the log stand-ins, unless `direct`: a stress install leaves them out), then create each backup bucket, Polaris's then Datahub's, with SeaweedFS's shell
    (`weed mini -bucket` makes only the warehouse's) and check that its filer lists it (ADR-043,
    ADR-066 §3)."""
    ns = ["kubectl", "--context", CONTEXT, "-n", env["NS_STANDINS"]]
    commands = [
        (["kubectl", "--context", CONTEXT, "apply", "--server-side", "--field-manager=gate",
          "-f", "-"], manifest),
        (ns + ["rollout", "status", "deployment/seaweedfs", "deployment/opa",
               "deployment/recorder", "deployment/prometheus",
               *([] if direct else [f"deployment/{standins.LOGS_OPENSEARCH}",
                                    f"deployment/{standins.MAIN_FLUENT_BIT}"]),
               "--timeout=5m"], None),
    ]
    for bucket in (env["POLARIS_BACKUP_BUCKET"], env["DATAHUB_BACKUP_BUCKET"]):
        commands += [
            (ns + ["exec", "-i", "deployment/seaweedfs", "--", "weed", "shell"],
             f"s3.bucket.create -name {bucket}\n"),
            (ns + ["exec", "deployment/seaweedfs", "--", "curl", "-sf", "-o", "/dev/null",
                   f"http://127.0.0.1:{standins.FILER_PORT}/buckets/{bucket}/"], None),
        ]
    return commands


def recorder_certificate(env, directory):
    """A self-signed certificate and key (PEM) for the recorder's PostgreSQL TLS, made here for
    this gate run only (ADR-017, ADR-034)."""
    cert, key = os.path.join(directory, "tls.crt"), os.path.join(directory, "tls.key")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                    "-subj", f"/CN=recorder.{env['NS_STANDINS']}.svc.cluster.local",
                    "-keyout", key, "-out", cert], check=True, capture_output=True)
    with open(cert, encoding="ascii") as c, open(key, encoding="ascii") as k:
        return c.read(), k.read()


def polaris_pg_values(path=os.path.join(ROOT, "charts", "polaris-postgres", "values.yaml")):
    """The chart's top-level `image` and `database`, read without a YAML library."""
    values = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            for key in ("image", "database"):
                if line.startswith(f"{key}:"):
                    values[key] = line.split(":", 1)[1].strip().strip("\"'")
    return values


def check_db_command(env, image, database, name):
    """The pod that runs check_db.sh (on stdin) as Polaris, through POLARIS_DB_HOST."""
    creds = [{"name": var, "valueFrom": {"secretKeyRef": {"name": "polaris-db", "key": key}}}
             for var, key in (("PGUSER", "username"), ("PGPASSWORD", "password"))]
    overrides = {"spec": {"containers": [{"name": name, "image": image, "env": creds,
                                          "stdin": True, "stdinOnce": True}]}}
    return ["kubectl", "--context", CONTEXT, "-n", env["NS_CATALOG_POLARIS"], "run", name,
            CHECK_POD_LABEL, f"--image={image}", "--restart=Never", "--rm", "-i", "--quiet",
            "--pod-running-timeout=3m", "--override-type=strategic",
            "--overrides", json.dumps(overrides),
            "--command", "--", "sh", "-s", "--", env["POLARIS_DB_HOST"], database]


def check_standins_command(env, cases, image, name):
    """The pod that runs check_standins.sh (on stdin) with Polaris's S3 credentials."""
    creds = [{"name": var, "valueFrom": {"secretKeyRef": {"name": "polaris-s3", "key": key}}}
             for var, key in (("AWS_ACCESS_KEY_ID", "accessKeyId"),
                              ("AWS_SECRET_ACCESS_KEY", "secretAccessKey"))]
    overrides = {"spec": {"containers": [{"name": name, "image": image, "env": creds,
                                          "stdin": True, "stdinOnce": True}]}}
    argv = ["kubectl", "--context", CONTEXT, "-n", env["NS_CATALOG_POLARIS"], "run", name,
            CHECK_POD_LABEL, f"--image={image}", "--restart=Never", "--rm", "-i", "--quiet",
            "--pod-running-timeout=3m", "--override-type=strategic",
            "--overrides", json.dumps(overrides),
            "--command", "--", "sh", "-s", "--", env["S3_ENDPOINT"],
            env["POLARIS_WAREHOUSE_BUCKET"], env["S3_REGION"], env["POLARIS_OPA_URL"]]
    for case in cases:
        argv += [case["name"], "true" if case["allow"] else "false",
                 json.dumps(case["input"], separators=(",", ":"))]
    return argv


def recorder_logs_command(env):
    return ["kubectl", "--context", CONTEXT, "-n", env["NS_STANDINS"], "logs",
            "deployment/recorder", "--tail=-1"]


# When this commit's install over the last release ended, for check-traffic's restart window.
INSTALL_RECORD = "/tmp/lakehouse-gate-install.json"


def restart_events_command(namespace):
    return ["kubectl", "--context", CONTEXT, "-n", namespace, "get", "events",
            "--field-selector",
            f"involvedObject.name={release.POLARIS_PG},reason=UpgradingInstance", "-o", "json"]


def restart_window(events, install_end):
    """polaris-pg's restart during the upgrade: from its first UpgradingInstance event to the
    end of this commit's install; None without both (Kade, 2026-10-12). CNPG emits no event
    when a restart ends, and step 1 waits for it, so the install's end bounds it."""
    if not install_end:
        return None
    times = [e.get("eventTime") or e.get("firstTimestamp") or e.get("lastTimestamp")
             for e in events.get("items", [])
             if e.get("involvedObject", {}).get("kind") == "Cluster"
             and e["involvedObject"].get("name") == release.POLARIS_PG
             and e.get("reason") == "UpgradingInstance"]
    times = [t for t in times if t and traffic.when(t) <= traffic.when(install_end)]
    return (min(times, key=traffic.when), install_end) if times else None


def check_traffic(log_text, expectation_lists, restart=None):
    """(problems, lines): a count of the records, the restart window if any, then one line per
    expectation."""
    recorded = traffic.records(log_text)
    by_proto = {p: sum(1 for r in recorded
                       if r.get("proto") == p and r.get("event") != "listening")
                for p in ("http", "pg")}
    lines = [f"Recorder: {by_proto['http'] + by_proto['pg']} records: {by_proto['http']} HTTP, "
             f"{by_proto['pg']} PostgreSQL."]
    if restart:
        lines.append(f"{release.POLARIS_PG} restarted during the upgrade from {restart[0]} to "
                     f"{restart[1]}: only there may a rule let through what it allows.")
    problems = []
    for expectations in expectation_lists:
        more_problems, more_lines = traffic.check(recorded, expectations, restart)
        problems += more_problems
        lines += more_lines
    return problems, lines


def install_plan(previous, previous_dir, fresh, dry_run=False):
    """The commands that install a release on the gate, and the line that says which. The
    release before this one runs from a worktree of its tag, with the gate's `.env`."""
    this = [["python3", os.path.join(ROOT, "install", "release.py")] + (
        ["--dry-run"] if dry_run else [])]
    # The gate's own allow policies (ADR-048 §5), before the first release that installs for
    # real: since platform-2026.10.14 the last release applies Polaris's policies too, and its
    # realm bootstrap reaches polaris-pg through the recorder (dry-run 38493145873). Never
    # before a dry-run alone, which changes nothing.
    policies = [["python3", os.path.join(HERE, "gate.py"), "gate-policies"]]
    then = " (server-side dry-run)" if dry_run else ""
    if fresh or not previous:
        commands = this if dry_run else policies + this
        if fresh:
            return commands, f"Fresh install of this commit{then}, as asked."
        return commands, f"No release to upgrade from: fresh install{then} (ADR-051)."
    return [
        *policies,
        ["git", "-C", ROOT, "worktree", "add", "--detach", previous_dir, previous],
        # release.py reads .env from its own checkout, and a tag's worktree has none.
        ["cp", "-p", os.path.join(ROOT, ".env"), os.path.join(previous_dir, ".env")],
        ["python3", os.path.join(previous_dir, "install", "release.py")],
        # ADR-051 §2: data written through the last release, for check-migration to read back.
        # The dry-run's upgrade changes no data, so it seeds nothing (§4).
        *([] if dry_run else [["python3", os.path.join(HERE, "gate.py"), "seed"]]),
        *this,
    ], f"Upgrade: {previous} installed from its tag, then this commit over it{then} (ADR-051)."


# ADR-077 §1: the datahub-recovery job's install, release.py's own functions in ADR-045's order:
# step 0, then Datahub's part of step 1. release.py gains no mode; only the gate composes it.
DATAHUB_ONLY_STEPS = ("check_preconditions", "datahub_network_policies", "datahub_postgres",
                      "datahub_kafka", "datahub_opensearch", "datahub")


def datahub_only(env, supported):
    """Step 0 and Datahub's part of step 1 of this commit's release.py, the first step with
    problems stopping the rest. Returns (problems, notes, timed): each step's (start, end,
    label), for the timeline."""
    notes, timed = [], []
    for name in DATAHUB_ONLY_STEPS:
        step = getattr(release, name)
        began = dt.datetime.now(dt.timezone.utc).isoformat()
        problems, more = (step(env, release.call, supported) if name == "check_preconditions"
                          else step(env, release.call))
        timed.append((began, dt.datetime.now(dt.timezone.utc).isoformat(), f"release.py {name}"))
        notes += more
        if problems:
            return problems, notes, timed
    return [], notes, timed


def pull_events_command():
    """The kubelet's Pulled events in every namespace: one per image pulled, or found already
    present, on a node (#125)."""
    return ["kubectl", "--context", CONTEXT, "get", "events", "--all-namespaces",
            "--field-selector", "reason=Pulled", "-o", "json"]


GO_UNITS = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 1e-3, "us": 1e-6, "µs": 1e-6, "ns": 1e-9}
GO_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|us|µs|ns|h|m|s)")


def go_seconds(text):
    """A Go duration as the kubelet prints it (`1m2.5s`, `512ms`) in seconds."""
    parts = GO_DURATION_RE.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text:
        raise ValueError(f"not a Go duration: {text!r}")
    return sum(float(n) * GO_UNITS[u] for n, u in parts)


PULLED_RE = re.compile(r'Successfully pulled image "([^"]+)" in (\S+) \((\S+) including waiting\)'
                       r"(?:\. Image size: (\d+) bytes)?")
PRESENT_RE = re.compile(r'Container image "([^"]+)" already present on machine')


def image_pulls(events, since):
    """(pulls, present, unknown) from Pulled events last seen at or after `since`: each pull
    with its node, seconds, seconds with waiting and size; the (image, node) pairs already
    present; how many Pulled messages matched neither form."""
    pulls, present, unknown = [], set(), 0
    for e in events.get("items", []):
        at = e.get("lastTimestamp") or e.get("eventTime") or e.get("firstTimestamp")
        if e.get("reason") != "Pulled" or not at or traffic.when(at) < traffic.when(since):
            continue
        node = (e.get("source") or {}).get("host") or e.get("reportingInstance") or "?"
        message = e.get("message", "")
        m = PULLED_RE.match(message)
        if m:
            pulls.append({"image": m[1], "node": node, "seconds": go_seconds(m[2]),
                          "waiting": go_seconds(m[3]), "bytes": int(m[4]) if m[4] else None})
        elif PRESENT_RE.match(message):
            present.add((PRESENT_RE.match(message)[1], node))
        else:
            unknown += 1
    return pulls, present, unknown


def _count(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def pull_lines(pulls, present, unknown, install_seconds):
    """A summary against the install's length, then one line per image, slowest pull first,
    then images only present, then each node's pull time. A kubelet pulls one image at a time
    by default, so the busiest node's sum bounds what pulls cost the install."""
    per_node = {}
    for p in pulls:
        per_node[p["node"]] = per_node.get(p["node"], 0.0) + p["seconds"]
    if per_node:
        busiest = max(per_node, key=per_node.get)
        lines = [f"Image pulls during the install: {_count(len(pulls), 'pull')} of "
                 f"{_count(len({p['image'] for p in pulls}), 'image')}; the busiest node, "
                 f"{busiest}, spent {per_node[busiest]:.1f} s pulling, of the install's "
                 f"{install_seconds:.0f} s."]
    else:
        lines = ["Image pulls during the install: none; every image was already present."]
    images = {}
    for p in pulls:
        images.setdefault(p["image"], []).append(p)
    for image, its in sorted(images.items(), key=lambda kv: -max(p["seconds"] for p in kv[1])):
        slowest = max(its, key=lambda p: p["seconds"])
        line = (f"- {image}: pulled on {_count(len(its), 'node')}, longest "
                f"{slowest['seconds']:.1f} s ({slowest['waiting']:.1f} s with waiting)")
        if slowest["bytes"] is not None:
            line += f", {slowest['bytes'] / 1e6:.1f} MB"
        nodes = sum(1 for i, _ in present if i == image)
        lines.append(line + (f"; already present on {_count(nodes, 'node')}" if nodes else ""))
    for image in sorted({i for i, _ in present} - set(images)):
        lines.append(f"- {image}: already present on "
                     f"{_count(sum(1 for i, _ in present if i == image), 'node')}")
    if per_node:
        lines.append("- per node: " + ", ".join(
            f"{n} {s:.1f} s" for n, s in sorted(per_node.items(), key=lambda kv: -kv[1])))
    if unknown:
        lines.append(f"- {unknown} Pulled events not understood")
    return lines


def measure_pulls(since, install_seconds):
    """pull_lines for the install that started at `since`; never stops the gate."""
    try:
        done = subprocess.run(pull_events_command(), capture_output=True, text=True,
                              check=False)
        if done.returncode != 0:
            raise RuntimeError(done.stderr.strip() or f"kubectl exited {done.returncode}")
        return pull_lines(*image_pulls(json.loads(done.stdout), since), install_seconds)
    except Exception as e:  # noqa: BLE001 - a measurement must never fail the install
        return [f"Image pulls: not measured ({e})"]


def _moment(text):
    """A Kubernetes or Helm time; Helm's carry nanoseconds, which Python does not read."""
    return traffic.when(re.sub(r"(\.\d{6})\d+", r"\1", text))


def command_label(command):
    """What one of install_plan's commands installs, for the timeline."""
    if command[1:2] and command[1].endswith(os.path.join("install", "release.py")):
        mine = command[1] == os.path.join(ROOT, "install", "release.py")
        label = f"release.py ({'this commit' if mine else 'the last release'})"
        return " ".join([label, *command[2:]])
    if command[1:2] and command[1].endswith("gate.py"):
        return " ".join(["gate.py", *command[2:3]])
    return " ".join([command[0], *command[3:5]] if command[1:2] == ["-C"] else command[:2])


# The kinds whose events go on the timeline: CNPG deletes its initdb and join Jobs once they
# end, so their events are what is left of them; the Cluster's say what CNPG is doing.
TIMELINE_EVENT_KINDS = ("Job", "Cluster")
# And between polaris-pg's Ready and Polaris's revision: release.py's endpoint check and
# bootstrap run in pods it names with these prefixes and deletes (--rm), then it makes the
# log claim (install/release.py, the last release's too).
TIMELINE_POD_PREFIXES = ("endpoint-check-", "polaris-bootstrap-")
TIMELINE_CLAIMS = ("polaris-logs",)


def timeline_event(obj):
    """Whether an event about `obj` (its involvedObject) goes on the timeline."""
    kind, name = obj.get("kind"), obj.get("name") or ""
    return (kind in TIMELINE_EVENT_KINDS
            or kind == "Pod" and name.startswith(TIMELINE_POD_PREFIXES)
            or kind == "PersistentVolumeClaim" and name in TIMELINE_CLAIMS)


def timeline_entries(since, histories, jobs, deployments, clusters, commands, events=None):
    """[(time, what happened)] at or after `since`, in order: each Helm revision (release.py
    runs Helm without --wait, so a step starts at its revision), each Job's start and end,
    each Deployment's finished rollout, each CNPG Cluster's Ready, the gate's own commands
    (start, end, label), and the events timeline_event keeps."""
    entries = []
    for e in (events or {}).get("items", []):
        obj = e.get("involvedObject", {})
        at = e.get("lastTimestamp") or e.get("eventTime") or e.get("firstTimestamp")
        if timeline_event(obj) and at:
            entries.append((at, f"{obj['kind'].lower()} {obj.get('namespace')}/{obj.get('name')}"
                                f": {e.get('reason')} ({e.get('message', '').strip()[:100]})"))
    for (namespace, name), revisions in histories.items():
        for r in revisions:
            if r.get("updated"):
                entries.append((r["updated"], f"helm {namespace}/{name} revision "
                                              f"{r['revision']} {r['status']}"))
    for job in jobs.get("items", []):
        where, status = "{namespace}/{name}".format(**job["metadata"]), job.get("status", {})
        if status.get("startTime"):
            entries.append((status["startTime"], f"job {where} started"))
        if status.get("completionTime"):
            entries.append((status["completionTime"], f"job {where} complete"))
        for c in status.get("conditions", []):
            if c.get("type") == "Failed" and c.get("status") == "True":
                entries.append((c["lastTransitionTime"], f"job {where} failed"))
    for d in deployments.get("items", []):
        where = "{namespace}/{name}".format(**d["metadata"])
        for c in d.get("status", {}).get("conditions", []):
            if c.get("type") == "Progressing" and c.get("reason") == "NewReplicaSetAvailable":
                entries.append((c["lastUpdateTime"], f"deployment {where} rolled out"))
    for cluster in clusters.get("items", []):
        where = "{namespace}/{name}".format(**cluster["metadata"])
        for c in cluster.get("status", {}).get("conditions", []):
            if c.get("type") == "Ready" and c.get("status") == "True":
                entries.append((c["lastTransitionTime"], f"cluster {where} Ready"))
    for start, end, label in commands:
        entries += [(start, f"{label} started"), (end, f"{label} ended")]
    start = _moment(since)
    timed = [(_moment(at), text) for at, text in entries]
    return sorted((e for e in timed if e[0] >= start), key=lambda e: e[0])


def timeline_lines(entries, since, title="Install", what="the install"):
    """Each entry with whole seconds after `since` and since the entry before."""
    if not entries:
        return [f"{title} timeline: nothing recorded after {what} started."]
    lines, before, start = [
        f"{title} timeline, seconds after {what} started (and since the line before):"], 0, \
        _moment(since)
    for at, text in entries:
        offset = int((at - start).total_seconds())
        lines.append(f"- {offset:3d} s (+{offset - before:2d})  {text}")
        before = offset
    return lines


def measure_timeline(since, commands):
    """timeline_lines for the install that started at `since`, read from Helm and the cluster;
    never stops the gate."""
    def read(argv):
        done = subprocess.run(argv, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            raise RuntimeError(done.stderr.strip() or f"{argv[0]} exited {done.returncode}")
        return json.loads(done.stdout)

    try:
        helm = ["helm", "--kube-context", CONTEXT]
        histories = {
            (r["namespace"], r["name"]): read(helm + ["-n", r["namespace"], "history",
                                                      r["name"], "-o", "json"])
            for r in read(helm + ["list", "--all-namespaces", "-o", "json"])}
        kubectl = ["kubectl", "--context", CONTEXT, "get", "--all-namespaces", "-o", "json"]
        jobs, deployments, clusters = (read(kubectl[:4] + [kind] + kubectl[4:])
                                       for kind in ("jobs", "deployments",
                                                    "clusters.postgresql.cnpg.io"))
        try:
            events, missing = read(kubectl[:4] + ["events"] + kubectl[4:]), []
        except Exception as e:  # noqa: BLE001 - the rest of the timeline still stands
            events, missing = None, [f"- events not read ({e})"]
        return timeline_lines(timeline_entries(since, histories, jobs, deployments, clusters,
                                               commands, events), since) + missing
    except Exception as e:  # noqa: BLE001 - a measurement must never fail the install
        return [f"Install timeline: not measured ({e})"]


def polaris_pg_commands(namespace):
    """The helm release's values, the Cluster, its instance pods (not Job pods) and its
    FailoverQuorum, as JSON; nothing for a FailoverQuorum CNPG does not keep."""
    kubectl = ["kubectl", "--context", CONTEXT, "-n", namespace, "get"]
    return [
        ["helm", "--kube-context", CONTEXT, "-n", namespace, "get", "values",
         release.POLARIS_PG_RELEASE, "--all", "-o", "json"],
        kubectl + [f"cluster/{release.POLARIS_PG}", "-o", "json"],
        kubectl + ["pods", "-l", f"cnpg.io/cluster={release.POLARIS_PG},cnpg.io/podRole=instance",
                   "-o", "json"],
        kubectl + [f"failoverquorum/{release.POLARIS_PG}", "--ignore-not-found", "-o", "json"],
    ]


def polaris_pg_problems(values, cluster, pods, quorum):
    """The running Cluster, and its FailoverQuorum (None without one), against the values it
    was installed with (ADR-016)."""
    spec, problems = cluster["spec"], []
    running_sync = spec.get("postgresql", {}).get("synchronous")
    wanted_quorum = (polaris_pg_sync(values["synchronous"], values["instances"]) is not None
                     and values["synchronous"]["failoverQuorum"])
    for field, running, wanted in (
            ("instances", spec["instances"], values["instances"]),
            ("image", spec["imageName"], values["image"]),
            ("podAntiAffinityType", spec["affinity"]["podAntiAffinityType"],
             values["podAntiAffinityType"]),
            ("smartShutdownTimeout", spec.get("smartShutdownTimeout"),
             values["smartShutdownTimeout"]),
            ("synchronous", polaris_pg_sync(running_sync),
             polaris_pg_sync(values["synchronous"], values["instances"])),
            ("failoverQuorum", bool((running_sync or {}).get("failoverQuorum")),
             wanted_quorum)):
        if running != wanted:
            problems.append(f"{field}: {running} running, {wanted} in the values")
    status = (quorum or {}).get("status", {})
    reported = (f"{str(status.get('method', '')).lower()} {status.get('standbyNumber')}"
                if status else None)
    if wanted_quorum and reported != f"any {values['synchronous']['number']}":
        problems.append(f"FailoverQuorum {release.POLARIS_PG}: {reported or 'not reported'}, "
                        f"any {values['synchronous']['number']} in the values")
    ready = cluster.get("status", {}).get("readyInstances", 0)
    if ready != values["instances"]:
        problems.append(f"{ready} instances ready of {values['instances']}")
    nodes = sorted({p["spec"].get("nodeName") for p in pods["items"]})
    if len(nodes) != len(pods["items"]) or len(nodes) != values["instances"]:
        problems.append(f"{len(pods['items'])} instances on {len(nodes)} nodes: "
                        + ", ".join(str(n) for n in nodes))
    anti = spec["affinity"]["podAntiAffinityType"]
    sync = polaris_pg_sync(spec.get("postgresql", {}).get("synchronous"))
    replication = "synchronous " + " ".join(str(v) for v in sync) if sync else "asynchronous"
    if reported:
        replication += f", failover quorum {reported}"
    return problems, [f"Cluster {release.POLARIS_PG}: {ready} instances ready on "
                      f"{len(nodes)} nodes ({', '.join(str(n) for n in nodes)}), image "
                      f"{spec['imageName']}, anti-affinity {anti}, replication {replication}."]


def polaris_pg_sync(block, instances=None):
    """(method, number, durability) of a `synchronous` block, or None for asynchronous. From the
    values (`instances` given), what the chart renders: only with 3 or more (ADR-056 §3)."""
    if block is None or (instances is not None and instances < 3):
        return None
    method = "any" if instances is not None else block.get("method")
    return (method, block.get("number"), block.get("dataDurability"))


def polaris_commands(namespace):
    """Polaris's helm values, its Deployment, its pods and its ConfigMap, as JSON."""
    kubectl = ["kubectl", "--context", CONTEXT, "-n", namespace, "get"]
    return [
        ["helm", "--kube-context", CONTEXT, "-n", namespace, "get", "values", release.POLARIS,
         "--all", "-o", "json"],
        kubectl + [f"deployment/{release.POLARIS}", "-o", "json"],
        kubectl + ["pods", "-l", POLARIS_POD_LABELS, "-o", "json"],
        kubectl + [f"configmap/{release.POLARIS}", "-o", "json"],
    ]


def _properties(configmap):
    text = configmap["data"]["application.properties"]
    pairs = (line.split("=", 1) for line in text.splitlines()
             if "=" in line and not line.startswith("#"))
    return {k.strip(): v.strip() for k, v in pairs}


def _running(pods):
    return [p for p in pods["items"] if p.get("status", {}).get("phase") == "Running"]


# Seconds a rebuilt replica has to join and the Cluster to get Ready again, as release.py's
# wait for the Cluster (ADR-048 §7).
REBUILD_TIMEOUT = 900
JOIN_SELECTOR = f"cnpg.io/cluster={release.POLARIS_PG},cnpg.io/jobRole=join"


def rebuild_target(cluster, pods):
    """The replica rebuild-replica deletes: the first instance by name that is not the primary;
    None without one."""
    primary = cluster.get("status", {}).get("currentPrimary")
    names = sorted(p["metadata"]["name"] for p in pods["items"]
                   if p["metadata"]["name"] != primary)
    return names[0] if names else None


def rebuild_commands(namespace, instance):
    """The instance's PVC, without waiting (its pod still holds it), then its pod. CNPG names
    both after the instance."""
    kubectl = ["kubectl", "--context", CONTEXT, "-n", namespace, "delete"]
    return [kubectl + ["pvc", instance, "--wait=false"], kubectl + ["pod", instance]]


def join_jobs_command(namespace):
    return ["kubectl", "--context", CONTEXT, "-n", namespace, "get", "jobs", "-l",
            JOIN_SELECTOR, "-o", "json"]


def rebuilt(cluster, joins):
    """A new join Job ran and the Cluster is Ready with every instance."""
    status = cluster.get("status", {})
    ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                for c in status.get("conditions", []))
    return bool(joins) and ready and status.get("readyInstances") == cluster["spec"]["instances"]


def rebuild_replica(env):
    """ADR-048 §7. Returns (problems, notes)."""
    ns = env["NS_CATALOG_POLARIS"]
    cluster_command, pods_command = polaris_pg_commands(ns)[1:3]
    instance = rebuild_target(json.loads(output(cluster_command)),
                              json.loads(output(pods_command)))
    if not instance:
        return [f"Cluster {release.POLARIS_PG} has no replica to rebuild"], []
    jobs_command = join_jobs_command(ns)
    # By uid: a Job made again under an old instance's name is new all the same.
    before = {j["metadata"]["uid"] for j in json.loads(output(jobs_command))["items"]}
    for command in rebuild_commands(ns, instance):
        run(command)
    # A poll sees a join Job while it runs, even if CNPG removes it once done.
    joins, end = set(), time.monotonic() + REBUILD_TIMEOUT
    while True:
        joins |= {j["metadata"]["name"] for j in json.loads(output(jobs_command))["items"]
                  if j["metadata"]["uid"] not in before}
        if rebuilt(json.loads(output(cluster_command)), joins):
            return [], [f"Replica {instance} deleted with its PVC; {', '.join(sorted(joins))} "
                        f"cloned the primary, and Cluster {release.POLARIS_PG} is Ready with "
                        f"every instance."]
        if time.monotonic() > end:
            break
        time.sleep(3)
    subprocess.run(["kubectl", "--context", CONTEXT, "-n", ns, "logs", "-l", JOIN_SELECTOR,
                    "--all-containers", "--prefix", "--tail=200"], check=False)
    joined = ", ".join(sorted(joins)) or "no join Job"
    return [f"Cluster {release.POLARIS_PG} not Ready with every instance {REBUILD_TIMEOUT} s "
            f"after {instance} was deleted with its PVC ({joined})"], []


def polaris_problems(values, deployment, pods, configmap, env):
    """The running Polaris against the values it was installed with (ADR-016), and those
    values against `.env`: the database host and the policy endpoint the gate names."""
    problems, wanted = [], values["replicaCount"]
    replicas = deployment["spec"]["replicas"]
    if replicas != wanted:
        problems.append(f"replicas: {replicas} running, {wanted} in the values")
    ready = deployment.get("status", {}).get("readyReplicas", 0)
    if ready != wanted:
        problems.append(f"{ready} replicas ready of {wanted}")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    image = container["image"]
    running_env = {e["name"]: e.get("valueFrom", {}).get("secretKeyRef")
                   for e in container.get("env", [])}
    for var, key in POLARIS_S3_ENV.items():
        want = {"name": "polaris-s3", "key": key}
        if running_env.get(var) != want:
            problems.append(f"{var}: {running_env.get(var)} running, {want} wanted")
    want_image = f"{values['image']['repository']}:{values['image']['tag']}"
    if image != want_image:
        problems.append(f"image: {image} running, {want_image} in the values")
    running = _running(pods)
    nodes = sorted({p["spec"].get("nodeName") for p in running})
    if len(nodes) != len(running) or len(nodes) != wanted:
        problems.append(f"{len(running)} replicas on {len(nodes)} nodes: "
                        + ", ".join(str(n) for n in nodes))
    advanced, props = values["advancedConfig"], _properties(configmap)
    for key, want in (
            ("polaris.persistence.type", values["persistence"]["type"]),
            ("polaris.authentication.token-broker.type",
             values["authentication"]["tokenBroker"]["type"]),
            ("polaris.authorization.type", advanced["polaris.authorization.type"]),
            ("polaris.authorization.opa.policy-uri",
             advanced["polaris.authorization.opa.policy-uri"]),
            ("quarkus.datasource.jdbc.url", advanced["quarkus.datasource.jdbc.url"]),
            *((k, v) for k, v in advanced.items()
              if k.startswith(release.JDBC_PROPERTIES_PREFIX))):
        if props.get(key) != want:
            problems.append(f"{key}: {props.get(key)} running, {want} in the values")
    for key, want in (("quarkus.datasource.jdbc.url", release.jdbc_url(env)),
                      ("polaris.authorization.opa.policy-uri", env["POLARIS_OPA_URL"])):
        if advanced[key] != want:
            problems.append(f"{key}: {advanced[key]} in the values, {want} from .env")
    prefix = release.JDBC_PROPERTIES_PREFIX
    driver = " ".join(f"{k[len(prefix):]}={v}" for k, v in sorted(props.items())
                      if k.startswith(prefix))
    return problems, [
        f"Deployment {release.POLARIS}: {ready} replicas ready on {len(nodes)} nodes "
        f"({', '.join(str(n) for n in nodes)}), image {image}.",
        f"Polaris settings: S3 key from Secret polaris-s3, persistence "
        f"{props.get('polaris.persistence.type')}, database "
        f"{props.get('quarkus.datasource.jdbc.url')} with driver {driver or 'defaults'}, tokens "
        f"{props.get('polaris.authentication.token-broker.type')}, authorization "
        f"{props.get('polaris.authorization.type')} at "
        f"{props.get('polaris.authorization.opa.policy-uri')}."]


def polaris_pod_urls(pods):
    """Each running Polaris pod's API, by its own address: one check per replica."""
    return [f"http://{p['status']['podIP']}:{POLARIS_API_PORT}" for p in _running(pods)]


def check_polaris_command(env, image, name, pod_urls):
    """The pod that runs check_polaris.sh (on stdin) with root's credentials."""
    creds = [{"name": var, "valueFrom": {"secretKeyRef": {"name": release.POLARIS_ROOT_SECRET,
                                                         "key": key}}}
             for var, key in (("ROOT_CLIENT_ID", "clientId"),
                              ("ROOT_CLIENT_SECRET", "clientSecret"))]
    overrides = {"spec": {"containers": [{"name": name, "image": image, "env": creds,
                                          "stdin": True, "stdinOnce": True}]}}
    return ["kubectl", "--context", CONTEXT, "-n", env["NS_CATALOG_POLARIS"], "run", name,
            CHECK_POD_LABEL, f"--image={image}", "--restart=Never", "--rm", "-i", "--quiet",
            "--pod-running-timeout=3m", "--override-type=strategic",
            "--overrides", json.dumps(overrides),
            "--command", "--", "sh", "-s", "--",
            f"http://{release.POLARIS}:{POLARIS_API_PORT}",
            f"http://{release.POLARIS}-mgmt:{POLARIS_MGMT_PORT}", release.POLARIS_REALM,
            *pod_urls]


def smoke_python_image(pins):
    """The smoke tests' Python: the pinned image the recorder already runs on."""
    return pins["standin_images"]["recorder"]["image"]


def iceberg_configmap(env, script, requirements):
    """gates/iceberg/smoke.py and its hashed requirements, for the smoke pod to mount."""
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": "iceberg-smoke", "namespace": env["NS_CATALOG_POLARIS"]},
            "data": {"smoke.py": script, "requirements.txt": requirements}}


def check_iceberg_commands(env, image, name):
    """Apply the ConfigMap (on stdin), then the pod that installs PyIceberg by hash and runs
    smoke.py as the smoke tests' principal, with root's credentials and the smoke S3 key."""
    ns = env["NS_CATALOG_POLARIS"]
    creds = [{"name": var, "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}}
             for var, secret, key in (
                 ("ROOT_CLIENT_ID", release.POLARIS_ROOT_SECRET, "clientId"),
                 ("ROOT_CLIENT_SECRET", release.POLARIS_ROOT_SECRET, "clientSecret"),
                 ("S3_ACCESS_KEY_ID", "smoke-s3", "accessKeyId"),
                 ("S3_SECRET_ACCESS_KEY", "smoke-s3", "secretAccessKey"))]
    settings = [{"name": var, "value": value} for var, value in (
        ("PYTHONUNBUFFERED", "1"), ("PIP_DISABLE_PIP_VERSION_CHECK", "1"),
        ("PIP_NO_CACHE_DIR", "1"), ("PIP_ROOT_USER_ACTION", "ignore"))]
    overrides = {"spec": {
        "containers": [{"name": name, "image": image, "env": creds + settings,
                        "volumeMounts": [{"name": "code", "mountPath": "/smoke",
                                          "readOnly": True}]}],
        "volumes": [{"name": "code", "configMap": {"name": "iceberg-smoke"}}]}}
    install = ("pip install --quiet --require-hashes --only-binary :all: "
               "-r /smoke/requirements.txt && exec python3 /smoke/smoke.py \"$@\"")
    return [
        ["kubectl", "--context", CONTEXT, "-n", ns, "apply", "--server-side",
         "--field-manager=gate", "-f", "-"],
        ["kubectl", "--context", CONTEXT, "-n", ns, "run", name, CHECK_POD_LABEL,
         f"--image={image}",
         "--restart=Never", "--rm", "--attach", "--quiet", "--pod-running-timeout=3m",
         "--override-type=strategic", "--overrides", json.dumps(overrides),
         "--command", "--", "sh", "-c", install, "smoke",
         f"http://{release.POLARIS}:{POLARIS_API_PORT}", release.POLARIS_REALM,
         ICEBERG_CATALOG, env["S3_ENDPOINT"], env["S3_REGION"],
         env["POLARIS_WAREHOUSE_BUCKET"]],
    ]


# What `seed` wrote and counted, kept on the runner for check-migration (ADR-051 §2): it holds
# the seeded principals' credentials, so it is never printed.
SEED_RECORD = "/tmp/lakehouse-gate-seed.json"


def seed_configmap(env, files):
    """gates/seed.py, iceberg/smoke.py, whose helpers it uses, and their hashed requirements."""
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": "migration-seed", "namespace": env["NS_CATALOG_POLARIS"]},
            "data": files}


def seed_pod_command(env, image, name, mode):
    """The pod that installs PyIceberg by hash and runs seed.py MODE with root's credentials
    and the smoke S3 key; `check` reads the record on stdin."""
    creds = [{"name": var, "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}}
             for var, secret, key in (
                 ("ROOT_CLIENT_ID", release.POLARIS_ROOT_SECRET, "clientId"),
                 ("ROOT_CLIENT_SECRET", release.POLARIS_ROOT_SECRET, "clientSecret"),
                 ("S3_ACCESS_KEY_ID", "smoke-s3", "accessKeyId"),
                 ("S3_SECRET_ACCESS_KEY", "smoke-s3", "secretAccessKey"))]
    settings = [{"name": var, "value": value} for var, value in (
        ("PYTHONUNBUFFERED", "1"), ("PIP_DISABLE_PIP_VERSION_CHECK", "1"),
        ("PIP_NO_CACHE_DIR", "1"), ("PIP_ROOT_USER_ACTION", "ignore"))]
    container = {"name": name, "image": image, "env": creds + settings,
                 "volumeMounts": [{"name": "code", "mountPath": "/seed", "readOnly": True}]}
    if mode == "check":
        container.update(stdin=True, stdinOnce=True)
    overrides = {"spec": {"containers": [container],
                          "volumes": [{"name": "code", "configMap": {"name": "migration-seed"}}]}}
    install = ("pip install --quiet --require-hashes --only-binary :all: "
               "-r /seed/requirements.txt && exec python3 /seed/seed.py \"$@\"")
    return ["kubectl", "--context", CONTEXT, "-n", env["NS_CATALOG_POLARIS"], "run", name,
            CHECK_POD_LABEL, f"--image={image}", "--restart=Never", "--rm",
            "-i" if mode == "check" else "--attach", "--quiet", "--pod-running-timeout=3m",
            "--override-type=strategic", "--overrides", json.dumps(overrides),
            "--command", "--", "sh", "-c", install, "seed", mode,
            f"http://{release.POLARIS}:{POLARIS_API_PORT}", release.POLARIS_REALM,
            env["S3_ENDPOINT"], env["S3_REGION"], env["POLARIS_WAREHOUSE_BUCKET"]]


def db_counts_command(env, image, database, name):
    """The pod that runs db_counts.sh (on stdin) as Polaris against polaris-pg directly."""
    creds = [{"name": var, "valueFrom": {"secretKeyRef": {"name": release.POLARIS_DB_SECRET,
                                                          "key": key}}}
             for var, key in (("PGUSER", "username"), ("PGPASSWORD", "password"))]
    overrides = {"spec": {"containers": [{"name": name, "image": image, "env": creds,
                                          "stdin": True, "stdinOnce": True}]}}
    return ["kubectl", "--context", CONTEXT, "-n", env["NS_CATALOG_POLARIS"], "run", name,
            CHECK_POD_LABEL, f"--image={image}", "--restart=Never", "--rm", "-i", "--quiet",
            "--pod-running-timeout=3m", "--override-type=strategic",
            "--overrides", json.dumps(overrides),
            "--command", "--", "sh", "-s", "--", f"{release.POLARIS_PG}-rw", database]


def parse_counts(text):
    """db_counts.sh's lines: each table's rows and the schema version."""
    counts, version = {}, None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == "table":
            counts[parts[1]] = int(parts[2])
        elif len(parts) == 2 and parts[0] == "version":
            version = int(parts[1])
    return {"counts": counts, "version": version}


def db_problems(before, after):
    """(problems, notes): a table of polaris_schema with fewer rows than recorded fails; the
    schema version is reported, a change not failed by itself (ADR-051 §3)."""
    problems = []
    for table, rows in before["counts"].items():
        now = after["counts"].get(table)
        if now is None:
            problems.append(f"polaris_schema.{table}: gone, {rows} rows recorded before the "
                            "upgrade")
        elif now < rows:
            problems.append(f"polaris_schema.{table}: {now} rows, {rows} recorded before the "
                            "upgrade")
    version = f"Schema version: {before['version']} before the upgrade, {after['version']} after"
    notes = [f"polaris_schema: {len(before['counts'])} tables, "
             f"{sum(before['counts'].values())} rows before the upgrade, "
             f"{sum(after['counts'].values())} after.",
             version + ("." if before["version"] == after["version"] else
                        ": a change is reported, not failed (ADR-051 §3).")]
    return problems, notes


def _seed_pod_files():
    files = {}
    for name, path in (("seed.py", os.path.join(HERE, "seed.py")),
                       ("smoke.py", os.path.join(HERE, "iceberg", "smoke.py")),
                       ("requirements.txt", os.path.join(HERE, "iceberg", "requirements.txt"))):
        with open(path, encoding="utf-8") as f:
            files[name] = f.read()
    return files


def _apply_seed_configmap(env):
    apply = ["kubectl", "--context", CONTEXT, "-n", env["NS_CATALOG_POLARIS"], "apply",
             "--server-side", "--field-manager=gate", "-f", "-"]
    print("+ " + " ".join(apply) + "  (the ConfigMap migration-seed on stdin)", flush=True)
    subprocess.run(apply, input=json.dumps(seed_configmap(env, _seed_pod_files())), text=True,
                   check=True)


def _db_counts(env, name):
    with open(os.path.join(HERE, "db_counts.sh"), encoding="utf-8") as f:
        script = f.read()
    pg = polaris_pg_values()
    return parse_counts(output(db_counts_command(env, pg["image"], pg["database"], name),
                               script))


def seed_gate(env, pins, path=SEED_RECORD):
    """`seed`: see the module's docstring. Returns (problems, notes)."""
    pid = os.getpid()
    _apply_seed_configmap(env)
    done = subprocess.run(seed_pod_command(env, smoke_python_image(pins), f"seed-{pid}", "seed"),
                          capture_output=True, text=True, check=False)
    for line in done.stdout.splitlines():
        if not line.startswith(seed.RECORD_PREFIX):
            print(line, flush=True)
    print(done.stderr, end="", file=sys.stderr, flush=True)
    if done.returncode:
        return ["the seed failed: see its lines above"], []
    record = seed.read_record(done.stdout)
    if record is None:
        return ["the seed wrote no record"], []
    db = _db_counts(env, f"db-counts-{pid}")
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w",
              encoding="utf-8") as f:
        json.dump({"api": record, "db": db}, f)
    return [], [f"Seeded through the last release: {len(record['objects'])} objects recorded, "
                f"{len(db['counts'])} tables of polaris_schema counted, schema version "
                f"{db['version']} (ADR-051 §2)."]


def check_migration(env, pins, path=SEED_RECORD):
    """`check-migration`: see the module's docstring. Returns (problems, notes)."""
    if not os.path.exists(path):
        return [], ["Nothing was seeded: a fresh install, nothing to check (ADR-051 §3)."]
    with open(path, encoding="utf-8") as f:
        kept = json.load(f)
    pid = os.getpid()
    _apply_seed_configmap(env)
    problems = []
    if subprocess.run(seed_pod_command(env, smoke_python_image(pins), f"check-migration-{pid}",
                                       "check"),
                      input=json.dumps(kept["api"]), text=True, check=False).returncode:
        problems.append("the seeded objects did not all read back: see the lines above")
    more_problems, notes = db_problems(kept["db"], _db_counts(env, f"db-counts-{pid}"))
    return problems + more_problems, notes


# The restore the smoke gate proves (ADR-043 §4): its Cluster, sized for the gate only.
RESTORE = "polaris-pg-restore"
RESTORE_SIZE = "1Gi"
BACKUP_TIMEOUT = 600
RESTORE_TIMEOUT = 900
# How often the wait for the restore reads it and its full-recovery pods (#86).
RESTORE_POLL = 3
# Seconds polaris-pg's primary has to archive the WAL segment it switched before the restore.
ARCHIVE_TIMEOUT = 120


def backup_manifest(env, name, namespace_var="NS_CATALOG_POLARIS", cluster=release.POLARIS_PG):
    """An on-demand Backup of `cluster` (polaris-pg unless told otherwise) through the Barman
    Cloud Plugin."""
    return {"apiVersion": "postgresql.cnpg.io/v1", "kind": "Backup",
            "metadata": {"name": name, "namespace": env[namespace_var]},
            "spec": {"cluster": {"name": cluster}, "method": "plugin",
                     "pluginConfiguration": {"name": release.PLUGIN_NAME}}}


def backup_outcome(backup):
    """None while the Backup runs; (phase, error) once it completed or failed."""
    status = backup.get("status", {})
    if status.get("phase") in ("completed", "failed"):
        return status["phase"], status.get("error", "")
    return None


def _primary_psql(namespace, primary, sql):
    return ["kubectl", "--context", CONTEXT, "-n", namespace, "exec", primary, "-c",
            "postgres", "--", "psql", "-XAtq", "-c", sql]


def wal_switch_command(namespace, primary):
    """Closes the primary's current WAL segment and prints its name (#86)."""
    return _primary_psql(namespace, primary, "SELECT pg_walfile_name(pg_switch_wal())")


def archived_wal_command(namespace, primary):
    return _primary_psql(namespace, primary,
                         "SELECT coalesce(last_archived_wal, '') FROM pg_stat_archiver")


def wal_archived(segment, last):
    """Whether the archiver reached `segment`: WAL file names sort by timeline, then
    position."""
    return bool(last) and last >= segment


def switch_wal(kubectl, namespace, cluster=release.POLARIS_PG, when="before the restore"):
    """(problem, note): switches WAL on `cluster`'s primary (polaris-pg unless told otherwise)
    and waits until the segment it closed is archived. A backup from a standby, CNPG's default
    target, is consistent only past the WAL archived when it ended; without this the restore
    failed until the primary's archive_timeout (#86, smoke 38515945485)."""
    status = json.loads(output(kubectl + ["get", f"cluster/{cluster}", "-o", "json"]))
    primary = status["status"]["currentPrimary"]
    segment = output(wal_switch_command(namespace, primary)).strip()
    for _ in range(ARCHIVE_TIMEOUT // RESTORE_POLL):
        if wal_archived(segment, output(archived_wal_command(namespace, primary)).strip()):
            return None, f"WAL switched on {primary}: {segment} archived {when}."
        time.sleep(RESTORE_POLL)
    return (f"WAL segment {segment} from {primary} was not archived within "
            f"{ARCHIVE_TIMEOUT} s"), None

def restore_cluster(env, image):
    """polaris-pg-restore: one instance bootstrapped from polaris-pg's ObjectStore, with no
    recovery target, so every archived WAL is replayed. It names no plugin of its own, so it
    never archives into polaris-pg's backups. The Polaris user keeps its password from
    polaris-db."""
    source = release.POLARIS_PG
    return {"apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster",
            "metadata": {"name": RESTORE, "namespace": env["NS_CATALOG_POLARIS"]},
            "spec": {"instances": 1, "imageName": image, "storage": {"size": RESTORE_SIZE},
                     "bootstrap": {"recovery": {
                         "source": source, "database": release.POLARIS_DATABASE,
                         "owner": "polaris", "secret": {"name": release.POLARIS_DB_SECRET}}},
                     "externalClusters": [{
                         "name": source,
                         "plugin": {"name": release.PLUGIN_NAME, "parameters": {
                             "barmanObjectName": release.POLARIS_BACKUP_STORE,
                             "serverName": source}}}]}}


def datahub_pg_commands(namespace):
    """datahub-pg's helm values, its Cluster and its instances, as JSON (ADR-066 §5)."""
    kubectl = ["kubectl", "--context", CONTEXT, "-n", namespace, "get"]
    return [
        ["helm", "--kube-context", CONTEXT, "-n", namespace, "get", "values",
         release.DATAHUB_PG_RELEASE, "--all", "-o", "json"],
        kubectl + [f"cluster/{release.DATAHUB_PG}", "-o", "json"],
        kubectl + ["pods", "-l", f"cnpg.io/cluster={release.DATAHUB_PG},cnpg.io/podRole=instance",
                   "-o", "json"],
    ]


def datahub_pg_problems(values, cluster, pods):
    """The running Cluster datahub-pg against the values it was installed with, and
    asynchronous whatever they say (ADR-066 §2, ADR-016)."""
    spec, problems = cluster["spec"], []
    for field, running, wanted in (
            ("instances", spec["instances"], values["instances"]),
            ("image", spec["imageName"], values["image"]),
            ("podAntiAffinityType", spec["affinity"]["podAntiAffinityType"],
             values["podAntiAffinityType"]),
            ("smartShutdownTimeout", spec.get("smartShutdownTimeout"),
             values["smartShutdownTimeout"])):
        if running != wanted:
            problems.append(f"{field}: {running} running, {wanted} in the values")
    if spec.get("postgresql", {}).get("synchronous"):
        problems.append("replication: synchronous running, asynchronous in ADR-066")
    ready = cluster.get("status", {}).get("readyInstances", 0)
    if ready != values["instances"]:
        problems.append(f"{ready} instances ready of {values['instances']}")
    nodes = sorted({p["spec"].get("nodeName") for p in pods["items"]})
    if len(nodes) != len(pods["items"]) or len(nodes) != values["instances"]:
        problems.append(f"{len(pods['items'])} instances on {len(nodes)} nodes: "
                        + ", ".join(str(n) for n in nodes))
    return problems, [f"Cluster {release.DATAHUB_PG}: {ready} instances ready on "
                      f"{len(nodes)} nodes ({', '.join(str(n) for n in nodes)}), image "
                      f"{spec['imageName']}, anti-affinity "
                      f"{spec['affinity']['podAntiAffinityType']}, replication asynchronous."]


# check-datahub-kafka (ADR-068 §5): the broker's pods, the time its pod has to come back ready
# after it is deleted, and the size of the message, above 4.5 MiB and below Datahub's 5 MiB.
KAFKA_POD_LABELS = f"app.kubernetes.io/name={release.DATAHUB_KAFKA}"
KAFKA_RESTART_TIMEOUT = 300
# Run in the check pod with `bash -s -- STEP BOOTSTRAP TOPIC`. `write` creates the topic, with
# nothing of its own, so the broker's message.max.bytes is what decides, and produces one line;
# `read` consumes the first message from the beginning and keeps the line that starts with the
# topic's name; each prints the sha256 and size of what it sent or received. `delete` deletes
# the topic. `topics` lists every topic, `topic NAME` a line each, TOPIC unused
# (check-datahub-recovery). KAFKA_BIN is only for the tests.
KAFKA_CHECK = r"""
set -euo pipefail
step=$1 bootstrap=$2 topic=$3
bin=${KAFKA_BIN:-/opt/kafka/bin}
cd "$(mktemp -d)"
case "$step" in
  write)
    "$bin/kafka-topics.sh" --bootstrap-server "$bootstrap" --create --topic "$topic" \
      --partitions 1 --replication-factor 1 >&2
    { printf '%s-' "$topic"; head -c 4718592 /dev/zero | tr '\000' x; echo; } > sent
    "$bin/kafka-console-producer.sh" --bootstrap-server "$bootstrap" --topic "$topic" \
      --producer-property max.request.size=5242880 --producer-property acks=all < sent >&2
    echo "sent $(sha256sum < sent | cut -d' ' -f1) $(wc -c < sent | tr -d ' ')"
    ;;
  read)
    "$bin/kafka-console-consumer.sh" --bootstrap-server "$bootstrap" --topic "$topic" \
      --from-beginning --max-messages 1 --timeout-ms 120000 \
      --consumer-property max.partition.fetch.bytes=5242880 > received 2> consumer.log \
      || tail -n 5 consumer.log >&2
    # The tool prints lines of its own on stdout too: only the message's line is compared.
    grep -v "^$topic-" received >&2 || true
    grep "^$topic-" received > message || true
    echo "received $(sha256sum < message | cut -d' ' -f1) $(wc -c < message | tr -d ' ')"
    ;;
  delete)
    "$bin/kafka-topics.sh" --bootstrap-server "$bootstrap" --delete --topic "$topic" >&2
    echo deleted
    ;;
  topics)
    "$bin/kafka-topics.sh" --bootstrap-server "$bootstrap" --list | sed 's/^/topic /'
    ;;
esac
"""


def kafka_bootstrap(namespace):
    """datahub-kafka's headless Service: the name resolves to the broker's pod."""
    return f"{release.DATAHUB_KAFKA}.{namespace}.svc.cluster.local:{network.KAFKA_PORT}"


def kafka_check_pod(namespace, image, name):
    """A pod that waits, with Kafka's client label and never the check label, so the rule of
    ADR-068 §3 is what lets it in, running the chart's own image and its tools."""
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": namespace,
                         "labels": {release.DATAHUB_KAFKA_CLIENT_LABEL: "true",
                                    "app.kubernetes.io/part-of": "gate"}},
            "spec": {"restartPolicy": "Never", "enableServiceLinks": False,
                     "containers": [{"name": "check", "image": image,
                                     "command": ["sleep", "1800"]}]}}


def kafka_exec(namespace, pod, step, topic):
    """KAFKA_CHECK's `step` in the check pod; the script goes on stdin."""
    return ["kubectl", "--context", CONTEXT, "-n", namespace, "exec", "-i", pod, "--", "bash",
            "-s", "--", step, kafka_bootstrap(namespace), topic]


def kafka_lines(text):
    """{sent|received: (sha256, size)} from KAFKA_CHECK's output."""
    found = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] in ("sent", "received"):
            found[parts[0]] = (parts[1], parts[2])
    return found


def kafka_topics(text):
    """The topics KAFKA_CHECK's `topics` listed."""
    return {line[6:].strip() for line in text.splitlines() if line.startswith("topic ")}


def new_ready_pod(old_uids):
    """For wait_for over a pod list: the name of a ready pod that is none of `old_uids`."""
    def done(pods):
        for pod in pods["items"]:
            ready = any(c["type"] == "Ready" and c["status"] == "True"
                        for c in pod.get("status", {}).get("conditions", []))
            if ready and pod["metadata"]["uid"] not in old_uids:
                return pod["metadata"]["name"]
        return None
    return done


def check_datahub_kafka(env):
    """ADR-068 §5: see the module's docstring. Returns (problems, notes)."""
    ns, pid = env["NS_CATALOG_DATAHUB"], os.getpid()
    kubectl = ["kubectl", "--context", CONTEXT, "-n", ns]
    values = json.loads(output(["helm", "--kube-context", CONTEXT, "-n", ns, "get", "values",
                                release.DATAHUB_KAFKA_RELEASE, "--all", "-o", "json"]))
    pod, topic = f"{release.DATAHUB_KAFKA}-check-{pid}", f"gate-check-{pid}"
    broker = f"{release.DATAHUB_KAFKA}-0"
    run(kubectl + ["apply", "-f", "-"], json.dumps(kafka_check_pod(ns, values["image"], pod)))
    try:
        output(kubectl + ["wait", "--for=condition=Ready", f"pod/{pod}", "--timeout=180s"])
        sent = kafka_lines(output(kafka_exec(ns, pod, "write", topic), KAFKA_CHECK)).get("sent")
        received = kafka_lines(output(kafka_exec(ns, pod, "read", topic),
                                      KAFKA_CHECK)).get("received")
        if not sent or received != sent:
            return [f"Topic {topic}: sent {sent}, consumed back {received} (sha256, bytes)"], []
        notes = [f"Topic {topic}: a message of {sent[1]} bytes produced and consumed back "
                 f"unchanged through {kafka_bootstrap(ns)}."]
        pods = kubectl + ["get", "pods", "-l", KAFKA_POD_LABELS, "-o", "json"]
        old = {p["metadata"]["uid"] for p in json.loads(output(pods))["items"]}
        output(kubectl + ["delete", f"pod/{broker}", "--wait=true"])
        back = wait_for(pods, new_ready_pod(old), f"{broker} back ready",
                        timeout=KAFKA_RESTART_TIMEOUT)
        received = kafka_lines(output(kafka_exec(ns, pod, "read", topic),
                                      KAFKA_CHECK)).get("received")
        if received != sent:
            return [f"Topic {topic}: sent {sent}, read {received} after the restart of "
                    f"{broker} (sha256, bytes)"], notes
        notes.append(f"Pod {broker} deleted and back ready as {back}; the message read back "
                     "unchanged from its volume.")
        output(kafka_exec(ns, pod, "delete", topic), KAFKA_CHECK)
        notes.append(f"Topic {topic} deleted.")
        return [], notes
    finally:
        subprocess.run(kubectl + ["delete", "pod", pod, "--ignore-not-found", "--wait=false"],
                       capture_output=True, text=True, check=False)


# check-datahub-opensearch (ADR-070 §5): the node's pods, by the labels upstream's chart gives
# release datahub-opensearch, and the time its pod has to come back ready after it is deleted:
# its startup probe allows 5 s plus 30 tries of 10 s, and the image may be pulled again.
OPENSEARCH_POD_LABELS = ("app.kubernetes.io/name=opensearch,"
                         f"app.kubernetes.io/instance={release.DATAHUB_OPENSEARCH_RELEASE}")
OPENSEARCH_RESTART_TIMEOUT = 600
# Run in the check pod with `bash -s -- STEP URL INDEX`, over plain HTTP with no credential: an
# answer that is not 2xx stops the step. `write` creates the index with one shard and no replica,
# so one node can make it green, waits for green and writes one document, refreshed so a search
# finds it at once; `read` waits for green and searches for the document. Each prints the health
# and the search's answer, one JSON line each. `delete` deletes the index. `exists` prints the
# HTTP status of INDEX, an index or an alias, whatever it is (check-datahub-recovery). CURL is
# only for the tests.
OPENSEARCH_CHECK = r"""
set -euo pipefail
step=$1 url=$2 index=$3
curl=${CURL:-curl}
call() { "$curl" -sS --fail-with-body --max-time 150 "$@"; }
health() {
  local answer
  answer=$(call "$url/_cluster/health/$index?wait_for_status=green&timeout=120s")
  echo "health $answer"
}
case "$step" in
  write)
    call -X PUT "$url/$index" -H 'Content-Type: application/json' \
      -d '{"settings":{"index":{"number_of_shards":1,"number_of_replicas":0}}}' >&2
    health
    call -X PUT "$url/$index/_doc/1?refresh=true" -H 'Content-Type: application/json' \
      -d "{\"check\":\"$index\"}" >&2
    ;;
  read)
    health
    answer=$(call "$url/$index/_search?q=check:$index")
    echo "search $answer"
    ;;
  delete)
    call -X DELETE "$url/$index" >&2
    echo deleted
    ;;
  exists)
    echo "exists $("$curl" -sS -o /dev/null -w '%{http_code}' --max-time 150 "$url/$index")"
    ;;
esac
"""


def opensearch_url(namespace):
    """datahub-opensearch's Service, plain HTTP (ADR-070 §2)."""
    return (f"http://{release.DATAHUB_OPENSEARCH}.{namespace}.svc.cluster.local:"
            f"{network.OPENSEARCH_PORT}")


def opensearch_image(values):
    """The image the release runs, from its values: upstream's chart writes repository:tag."""
    return f"{values['image']['repository']}:{values['image']['tag']}"


def opensearch_check_pod(namespace, image, name):
    """A pod that waits, with OpenSearch's client label and never the check label, so the rule
    of ADR-070 §2 is what lets it in, running the chart's own image and its curl."""
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": namespace,
                         "labels": {release.DATAHUB_OPENSEARCH_CLIENT_LABEL: "true",
                                    "app.kubernetes.io/part-of": "gate"}},
            "spec": {"restartPolicy": "Never", "enableServiceLinks": False,
                     "containers": [{"name": "check", "image": image,
                                     "command": ["sleep", "1800"]}]}}


def opensearch_exec(namespace, pod, step, index):
    """OPENSEARCH_CHECK's `step` in the check pod; the script goes on stdin."""
    return ["kubectl", "--context", CONTEXT, "-n", namespace, "exec", "-i", pod, "--", "bash",
            "-s", "--", step, opensearch_url(namespace), index]


def opensearch_lines(text):
    """{health|search|exists: answer} from OPENSEARCH_CHECK's output."""
    found = {}
    for line in text.splitlines():
        key, _, answer = line.partition(" ")
        if key in ("health", "search", "exists") and answer:
            found[key] = json.loads(answer)
    return found


def opensearch_found(found, index):
    """True when the index is green and the search finds the check's document, once."""
    hits = found.get("search", {}).get("hits", {})
    return (found.get("health", {}).get("status") == "green"
            and hits.get("total", {}).get("value") == 1
            and [h.get("_source", {}).get("check") for h in hits.get("hits", [])] == [index])


def check_datahub_opensearch(env):
    """ADR-070 §5: see the module's docstring. Returns (problems, notes)."""
    ns, pid = env["NS_CATALOG_DATAHUB"], os.getpid()
    kubectl = ["kubectl", "--context", CONTEXT, "-n", ns]
    values = json.loads(output(["helm", "--kube-context", CONTEXT, "-n", ns, "get", "values",
                                release.DATAHUB_OPENSEARCH_RELEASE, "--all", "-o", "json"]))
    pod, index = f"{release.DATAHUB_OPENSEARCH}-check-{pid}", f"gate-check-{pid}"
    node = f"{release.DATAHUB_OPENSEARCH}-0"
    run(kubectl + ["apply", "-f", "-"],
        json.dumps(opensearch_check_pod(ns, opensearch_image(values), pod)))
    try:
        # The image is about 1 GB, and the check pod's node may not have pulled it yet.
        output(kubectl + ["wait", "--for=condition=Ready", f"pod/{pod}", "--timeout=600s"])
        written = opensearch_lines(output(opensearch_exec(ns, pod, "write", index),
                                          OPENSEARCH_CHECK))
        found = opensearch_lines(output(opensearch_exec(ns, pod, "read", index),
                                        OPENSEARCH_CHECK))
        green = written.get("health", {}).get("status") == "green"
        if not green or not opensearch_found(found, index):
            return [f"Index {index}: created with health {written.get('health')}, then read "
                    f"back {found}; expected green and the document found once"], []
        notes = [f"Index {index}: created green with one shard and no replica, a document "
                 f"written and found by search over plain HTTP without credentials at "
                 f"{opensearch_url(ns)}."]
        pods = kubectl + ["get", "pods", "-l", OPENSEARCH_POD_LABELS, "-o", "json"]
        old = {p["metadata"]["uid"] for p in json.loads(output(pods))["items"]}
        output(kubectl + ["delete", f"pod/{node}", "--wait=true"])
        back = wait_for(pods, new_ready_pod(old), f"{node} back ready",
                        timeout=OPENSEARCH_RESTART_TIMEOUT)
        found = opensearch_lines(output(opensearch_exec(ns, pod, "read", index),
                                        OPENSEARCH_CHECK))
        if not opensearch_found(found, index):
            return [f"Index {index}: read back {found} after the restart of {node}; expected "
                    "green and the document found once"], notes
        notes.append(f"Pod {node} deleted and back ready as {back}; the index green and the "
                     "document found again on its volume.")
        output(opensearch_exec(ns, pod, "delete", index), OPENSEARCH_CHECK)
        notes.append(f"Index {index} deleted.")
        return [], notes
    finally:
        subprocess.run(kubectl + ["delete", "pod", pod, "--ignore-not-found", "--wait=false"],
                       capture_output=True, text=True, check=False)


# check-datahub (ADR-071 §8, ADR-072 §4): what helm ran before GMS and after it, the script both
# check pods run, and the time a written dataset has to be found by search.
DATAHUB_JOBS = (f"{release.DATAHUB_RELEASE}-system-update",
                f"{release.DATAHUB_RELEASE}-system-update-nonblk")
DATAHUB_CHECK = os.path.join(HERE, "datahub", "check.py")
DATAHUB_SEARCH_SECONDS = 180


def datahub_urls(namespace):
    """(GMS's, the frontend's) Services as upstream's chart names them for release datahub."""
    svc = f"{namespace}.svc.cluster.local"
    return (f"http://{release.DATAHUB_RELEASE}-datahub-gms.{svc}:{network.GMS_PORT}",
            f"http://{release.DATAHUB_RELEASE}-datahub-frontend.{svc}:{network.FRONTEND_PORT}")


def system_update_problems(jobs):
    """A problem for each system-update Job, of `kubectl get jobs -o json`, not Complete."""
    complete = {j["metadata"]["name"] for j in jobs["items"]
                if any(c.get("type") == "Complete" and c.get("status") == "True"
                       for c in j.get("status", {}).get("conditions") or [])}
    return [f"Job {name} in Datahub's namespace has not completed" for name in DATAHUB_JOBS
            if name not in complete]


def secret_value(secret, key):
    """One key of a Secret read as JSON, decoded; never printed."""
    return base64.b64decode(secret["data"][key]).decode("utf-8")


def root_password(user_props):
    """The password of user.props's line for the root user, `datahub:<password>`, or None."""
    user, sep, password = user_props.strip().partition(":")
    return password if user == "datahub" and sep and password else None


def datahub_check_command(namespace, image, name, args, labels=None, secret_env=None):
    """A short-lived pod in `namespace` running gates/datahub/check.py with `args`, its
    credential on stdin; with `labels` if given, never the check label; with `secret_env`,
    {variable: (Secret, key)}, those variables from the Secret mounted in the pod."""
    with open(DATAHUB_CHECK, encoding="utf-8") as f:
        script = f.read()
    flags = [f"--labels={','.join(f'{k}={v}' for k, v in labels.items())}"] if labels else []
    overrides = []
    if secret_env:
        container = {"name": name, "image": image, "stdin": True, "stdinOnce": True, "env": [
            {"name": var, "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}}
            for var, (secret, key) in secret_env.items()]}
        overrides = ["--override-type=strategic", "--overrides",
                     json.dumps({"spec": {"containers": [container]}})]
    return ["kubectl", "--context", CONTEXT, "-n", namespace, "run", name, *flags,
            f"--image={image}", "--restart=Never", "--rm", "-i", "--quiet",
            "--pod-running-timeout=3m", *overrides, "--command", "--", "python3", "-c", script,
            *args]


def datahub_lines(text):
    """(problems, notes) from check.py's `FAIL` and `ok` lines."""
    problems, notes = [], []
    for line in text.splitlines():
        if line.startswith("FAIL "):
            problems.append(line[5:].strip())
        elif line.startswith("ok "):
            notes.append(line[3:].strip())
    return problems, notes


def check_datahub(env, pins):
    """ADR-071 §8, ADR-072 §4: see the module's docstring. Returns (problems, notes)."""
    ns, pid = env["NS_CATALOG_DATAHUB"], os.getpid()
    kubectl = ["kubectl", "--context", CONTEXT, "-n", ns]
    problems = system_update_problems(json.loads(output(kubectl + ["get", "jobs", "-o", "json"])))
    if problems:
        return problems, []
    notes = [f"Jobs {', '.join(DATAHUB_JOBS)} completed: system-update ran before GMS and "
             "after it."]
    auth = json.loads(output(kubectl + ["get", "secret", "datahub-auth-secrets", "-o", "json"]))
    root = json.loads(output(kubectl + ["get", "secret", "datahub-root-user", "-o", "json"]))
    secret = secret_value(auth, "system_client_secret")
    password = root_password(secret_value(root, "user.props"))
    if not password:
        return ["Secret datahub-root-user holds no user.props line for datahub"], notes
    # The namespace check-network gives the ingress class, labelled as `.env` says.
    key, _, value = env["INGRESS_NAMESPACE_LABEL"].partition("=")
    ingress, labels = network.SOURCE_PREFIX + "ingress", {key: value}
    output(["kubectl", "--context", CONTEXT, "apply", "--server-side", "--field-manager=gate",
            "-f", "-"], json.dumps({"apiVersion": "v1", "kind": "Namespace",
                                    "metadata": {"name": ingress, "labels": labels}}))
    gms, frontend = datahub_urls(ns)
    image = smoke_python_image(pins)
    pods = [(ns, f"check-datahub-gms-{pid}",
             ["gms", gms, f"gate_check_{pid}", str(DATAHUB_SEARCH_SECONDS)],
             {release.DATAHUB_GMS_CLIENT_LABEL: "true"}, secret),
            (ingress, f"check-datahub-login-{pid}", ["login", frontend], None, password)]
    for namespace, name, args, pod_labels, credential in pods:
        found, ok = datahub_pod(namespace, image, name, args, pod_labels, credential,
                                (secret, password))
        problems += found
        notes += ok
    return problems, notes


def datahub_pod(namespace, image, name, args, labels, credential, masked, secret_env=None):
    """check.py with `args` in a pod of its own, `credential` on its stdin. Returns (problems,
    notes); a pod that failed without a line of its own is a problem, every value of `masked`
    redacted."""
    done = subprocess.run(datahub_check_command(namespace, image, name, args, labels,
                                                secret_env),
                          input=credential + "\n", capture_output=True, text=True, check=False)
    found, ok = datahub_lines(done.stdout)
    if done.returncode and not found:
        said = done.stderr.strip() or done.stdout.strip()
        for value in masked:
            said = said.replace(value, "<redacted>")
        found = [f"pod {name} in {namespace} exited {done.returncode}: {said}"]
    return found, ok


# check-datahub-ingestion (ADR-076 §6): the CronJob a Job is created from, and the time it has
# to complete, its image pull included.
INGESTION_CRONJOB = (f"{release.DATAHUB_RELEASE}-datahub-ingestion-cron-"
                     f"{release.INGESTION_CRON}")
INGESTION_TIMEOUT = 900
INGESTION_LOG_TAIL = 80


def _seconds_between(start, end):
    return int((_moment(end) - _moment(start)).total_seconds())


def run_ingestion_job(namespace, name, masked, sleep, clock):
    """A Job from the ingestion CronJob, waited on. Returns (problems, notes); the log's
    `catalog` lines on success, its tail on failure, every value of `masked` redacted."""
    kubectl = ["kubectl", "--context", CONTEXT, "-n", namespace]
    output(kubectl + ["create", "job", f"--from=cronjob/{INGESTION_CRONJOB}", name])
    end, state, job = clock() + INGESTION_TIMEOUT, None, {}
    while True:
        job = json.loads(output(kubectl + ["get", "job", name, "-o", "json"]))
        state = job_finished(job)
        if state or clock() >= end:
            break
        sleep(5)
    done = subprocess.run(kubectl + ["logs", f"job/{name}", "--tail=-1"], capture_output=True,
                          text=True, check=False)
    log = done.stdout if done.returncode == 0 else f"(no log: {done.stderr.strip()})"
    for value in masked:
        log = log.replace(value, "<redacted>")
    if state != "succeeded":
        what = state or f"not finished within {INGESTION_TIMEOUT} s"
        tail = "\n".join(log.strip().splitlines()[-INGESTION_LOG_TAIL:])
        return [f"Job {name} from cronjob/{INGESTION_CRONJOB} {what}; its log:\n{tail}"], []
    status = job.get("status", {})
    took = ""
    if status.get("startTime") and status.get("completionTime"):
        took = f" in {_seconds_between(status['startTime'], status['completionTime'])} s"
    notes = [f"Job {name} from cronjob/{INGESTION_CRONJOB} completed{took}."]
    notes += [line.strip() for line in log.splitlines()
              if line.startswith("catalog ") or "Pipeline finished" in line]
    return [], notes


def check_datahub_ingestion(env, pins, sleep=time.sleep, clock=time.monotonic):
    """ADR-076 §6: see the module's docstring. Returns (problems, notes)."""
    ns, polaris_ns, pid = env["NS_CATALOG_DATAHUB"], env["NS_CATALOG_POLARIS"], os.getpid()

    def secret(namespace, name):
        return json.loads(output(["kubectl", "--context", CONTEXT, "-n", namespace, "get",
                                  "secret", name, "-o", "json"]))
    root = secret(polaris_ns, release.POLARIS_ROOT_SECRET)
    root_id, root_secret = secret_value(root, "clientId"), secret_value(root, "clientSecret")
    system = secret_value(secret(ns, "datahub-auth-secrets"), "system_client_secret")
    principal = secret(ns, release.DATAHUB_PRINCIPAL_SECRET)
    masked = (root_id, root_secret, system, secret_value(principal, "clientId"),
              secret_value(principal, "clientSecret"))
    image = smoke_python_image(pins)
    polaris = (f"http://{release.POLARIS}.{polaris_ns}.svc.cluster.local:"
               f"{release.POLARIS_API_PORT}")
    gms, _ = datahub_urls(ns)
    gms_client = {release.DATAHUB_GMS_CLIENT_LABEL: "true"}
    realm = release.POLARIS_REALM

    problems, notes = datahub_pod(
        ns, image, f"check-ingest-seed-{pid}",
        ["seed", polaris, realm, INGEST_CATALOG, env["S3_ENDPOINT"], env["S3_REGION"],
         env["POLARIS_WAREHOUSE_BUCKET"]], None, f"{root_id}:{root_secret}", masked)
    if problems:
        return problems, notes
    try:
        found, ok = run_ingestion_job(ns, f"check-ingest-{pid}", masked, sleep, clock)
        problems += found
        notes += ok
        pods = [] if found else [(f"check-ingest-read-{pid}", ["ingested", gms, INGEST_CATALOG],
                                  gms_client, system, None)]
        pods.append((f"check-ingest-refused-{pid}", ["refused", polaris, realm, INGEST_CATALOG],
                     None, "", {"CLIENT_ID": (release.DATAHUB_PRINCIPAL_SECRET, "clientId"),
                                "CLIENT_SECRET": (release.DATAHUB_PRINCIPAL_SECRET,
                                                  "clientSecret")}))
        for name, args, labels, credential, secret_env in pods:
            found, ok = datahub_pod(ns, image, name, args, labels, credential, masked,
                                    secret_env)
            problems += found
            notes += ok
    finally:
        found, ok = datahub_pod(ns, image, f"check-ingest-clean-{pid}",
                                ["clean", gms, INGEST_CATALOG], gms_client, system, masked)
        problems += found
        notes += ok
    return problems, notes


# check-datahub-recovery (ADR-071 §8): the index a dataset's search reads, an index or an alias;
# two topics system-update creates, the one GMS waits on at start and the change log the
# indices are built from; the restore-indices template upstream's chart names for release
# datahub; GMS's pods, by the labels upstream's chart gives them, and the times a restore and
# GMS's start have (upstream's readiness probe starts at 120 s, and its image may be pulled).
DATAHUB_DATASET_INDEX = "datasetindex_v2"
DATAHUB_TOPICS = ("DataHubUpgradeHistory_v1", "MetadataChangeLog_Versioned_v1")
RESTORE_INDICES_TEMPLATE = f"{release.DATAHUB_RELEASE}-datahub-restore-indices-job-template"
RESTORE_INDICES_TIMEOUT = 900
GMS_POD_LABELS = ("app.kubernetes.io/name=datahub-gms,"
                  f"app.kubernetes.io/instance={release.DATAHUB_RELEASE}")
GMS_RESTART_TIMEOUT = 900


def claim_of(pod):
    """The one PersistentVolumeClaim among a pod's volumes, read as JSON, or None."""
    claims = [v["persistentVolumeClaim"]["claimName"] for v in pod["spec"].get("volumes", [])
              if "persistentVolumeClaim" in v]
    return claims[0] if len(claims) == 1 else None


def job_finished(job):
    """`succeeded` or `failed` by the Job's own condition, after its retries; None while it
    runs."""
    for condition in job.get("status", {}).get("conditions") or []:
        if condition.get("status") == "True":
            if condition.get("type") == "Complete":
                return "succeeded"
            if condition.get("type") == "Failed":
                return "failed"
    return None


def lose_volume(kubectl, name, labels, timeout, mark):
    """Deletes StatefulSet pod `name`'s claim, then the pod, and waits until a new pod is ready,
    each phase given to `mark`. Returns (the claim, the new pod's name, whether the claim it
    runs on is a new one)."""
    claim = claim_of(json.loads(output(kubectl + ["get", "pod", name, "-o", "json"])))
    if not claim:
        sys.exit(f"Pod {name} has no single PersistentVolumeClaim to lose")
    pvc = kubectl + ["get", "pvc", claim, "-o", "json"]
    before = json.loads(output(pvc))["metadata"]["uid"]
    pods = kubectl + ["get", "pods", "-l", labels, "-o", "json"]
    old = {p["metadata"]["uid"] for p in json.loads(output(pods))["items"]}
    # The claim waits for its pod to go; the StatefulSet makes a new claim for the new pod.
    mark(f"claim {claim} and pod {name} being deleted")
    output(kubectl + ["delete", "pvc", claim, "--wait=false"])
    output(kubectl + ["delete", f"pod/{name}", "--wait=true"])
    mark(f"claim {claim} and pod {name} deleted")
    back = wait_for(pods, new_ready_pod(old), f"{name} back ready on a new volume",
                    timeout=timeout)
    mark(f"{back} ready")
    return claim, back, json.loads(output(pvc))["metadata"]["uid"] != before


def revision_marks(history):
    """(time, line) for each revision of `helm history datahub -o json`."""
    return [(r["updated"], f"helm {release.DATAHUB_RELEASE} revision {r['revision']} "
                           f"{r['status']}") for r in history]


def job_marks(jobs, names):
    """(time, line) for the start, completion or failure of each Job of `jobs` (a list read as
    JSON) named in `names`: system-update's are hooks Helm deletes on the next revision, so the
    check reads them after each install step."""
    marks = []
    for job in jobs["items"]:
        name, status = job["metadata"]["name"], job.get("status", {})
        if name not in names:
            continue
        if status.get("startTime"):
            marks.append((status["startTime"], f"job {name} started"))
        if status.get("completionTime"):
            marks.append((status["completionTime"], f"job {name} complete"))
        for c in status.get("conditions") or []:
            if c.get("type") == "Failed" and c.get("status") == "True":
                marks.append((c["lastTransitionTime"], f"job {name} failed"))
    return marks


def recovery_timeline(marks, since):
    """ADR-077 §3: check-datahub-recovery's marks, (time, line), in order, with whole seconds
    after `since` and since the line before. One that cannot be read is said, never a
    problem."""
    try:
        return timeline_lines(sorted(((_moment(at), text) for at, text in marks),
                                     key=lambda e: e[0]),
                              since, "Recovery", "check-datahub-recovery")
    except Exception as e:  # noqa: BLE001 - a measurement must never fail the check
        return [f"Recovery timeline: not measured ({e})"]


def check_datahub_recovery(env, pins, timeline=None):
    """ADR-071 §8: see the module's docstring. Returns (problems, notes); each phase goes on
    `timeline` as (time, line) as it happens, so a check that stops has it so far (ADR-077
    §3)."""
    timeline = [] if timeline is None else timeline

    def mark(text, at=None):
        timeline.append((at or dt.datetime.now(dt.timezone.utc).isoformat(), text))

    def read_marks(argv, marks, what):
        """Marks read from Helm or the cluster; a read that fails is said, never a problem."""
        found, why = _read_json(argv)
        try:
            if found is None:
                raise ValueError(why)
            for at, text in marks(found):
                mark(text, at)
        except (KeyError, TypeError, ValueError) as e:
            mark(f"{what} not read ({e})")

    if env.get("KUBE_CONTEXT") != CONTEXT:
        return [f"KUBE_CONTEXT is {env.get('KUBE_CONTEXT')!r}, not the gate's {CONTEXT}: "
                "check-datahub-recovery deletes volumes, so it runs on the gate only "
                "(ADR-004)"], []
    ns, pid = env["NS_CATALOG_DATAHUB"], os.getpid()
    kubectl = ["kubectl", "--context", CONTEXT, "-n", ns]
    secret = secret_value(json.loads(output(kubectl + ["get", "secret", "datahub-auth-secrets",
                                                       "-o", "json"])), "system_client_secret")

    def values(name):
        return json.loads(output(["helm", "--kube-context", CONTEXT, "-n", ns, "get", "values",
                                  name, "--all", "-o", "json"]))
    kafka_pod = f"{release.DATAHUB_KAFKA}-recovery-{pid}"
    opensearch_pod = f"{release.DATAHUB_OPENSEARCH}-recovery-{pid}"
    gms, _ = datahub_urls(ns)
    image, labels = smoke_python_image(pins), {release.DATAHUB_GMS_CLIENT_LABEL: "true"}
    first, second = f"gate_recovery_a_{pid}", f"gate_recovery_b_{pid}"
    seconds = str(DATAHUB_SEARCH_SECONDS)
    problems, notes, written, pods = [], [], [], iter(range(1, 100))

    def step(*args):
        """check.py's step `args[0]` against GMS, from a pod of GMS's clients."""
        what = " ".join(a for a in args if not a.isdigit())
        mark(f"{what} started")
        found, ok = datahub_pod(ns, image, f"check-datahub-recovery-{next(pods)}-{pid}",
                                [args[0], gms, *args[1:]], labels, secret, (secret,))
        problems.extend(found)
        notes.extend(ok)
        mark(f"{what} {'failed' if found else 'passed'}")
        return not found

    def index_status():
        return opensearch_lines(output(opensearch_exec(ns, opensearch_pod, "exists",
                                                       DATAHUB_DATASET_INDEX),
                                       OPENSEARCH_CHECK)).get("exists")

    def topics():
        return kafka_topics(output(kafka_exec(ns, kafka_pod, "topics", "-"), KAFKA_CHECK))

    def lost(name, pod_labels, timeout):
        claim, back, new = lose_volume(kubectl, name, pod_labels, timeout, mark)
        if not new:
            problems.append(f"Pod {name} came back as {back} on its old claim {claim}: the "
                            "volume was not lost")
            return False
        notes.append(f"Claim {claim} and pod {name} deleted; {back} ready again on a new "
                     "volume.")
        return True

    def reinstall():
        """Datahub's install step again (ADR-071 §7): system-update runs as a pre-upgrade hook.
        Its revision and system-update's Jobs go on the timeline, read before the next
        revision deletes them."""
        mark("Datahub's install step started")
        found, more = release.datahub(env, release.call)
        mark("Datahub's install step ended")
        read_marks(["helm", "--kube-context", CONTEXT, "-n", ns, "history",
                    release.DATAHUB_RELEASE, "--max", "1", "-o", "json"], revision_marks,
                   "Datahub's Helm revision")
        read_marks(kubectl + ["get", "jobs", "-o", "json"],
                   lambda jobs: job_marks(jobs, DATAHUB_JOBS), "Datahub's system-update Jobs")
        problems.extend(found)
        notes.extend(more)
        return not found

    def restore_indices():
        job = f"datahub-restore-indices-{pid}"
        output(kubectl + ["create", "job", job, f"--from=cronjob/{RESTORE_INDICES_TEMPLATE}"])
        mark(f"job {job} created")
        outcome = wait_for(kubectl + ["get", "job", job, "-o", "json"], job_finished,
                           f"Job {job}", timeout=RESTORE_INDICES_TIMEOUT)
        read_marks(kubectl + ["get", "job", job, "-o", "json"],
                   lambda one: job_marks({"items": [one]}, (job,)), f"job {job}")
        mark(f"job {job} {outcome}")
        if outcome != "succeeded":
            # Kept for the state the smoke run prints on failure; its log said here.
            log = subprocess.run(kubectl + ["logs", f"job/{job}", "--tail=20"],
                                 capture_output=True, text=True, check=False)
            problems.append(f"Job {job} from {RESTORE_INDICES_TEMPLATE} {outcome}: "
                            f"{(log.stdout or log.stderr).strip()}")
            return False
        subprocess.run(kubectl + ["delete", "job", job, "--ignore-not-found", "--wait=false"],
                       capture_output=True, text=True, check=False)
        notes.append(f"Job {job} from {RESTORE_INDICES_TEMPLATE} (restore-indices) replayed "
                     "datahub-pg into the indices.")
        return True

    def index_is(want, when):
        status = index_status()
        mark(f"index {DATAHUB_DATASET_INDEX} answers {status}")
        if status != want:
            problems.append(f"Index {DATAHUB_DATASET_INDEX} answers {status} {when}; expected "
                            f"{want}")
        return status == want

    def topics_are(present, when):
        listed = topics()
        mark(f"topics {', '.join(DATAHUB_TOPICS)}: "
             f"{sum(t in listed for t in DATAHUB_TOPICS)} of {len(DATAHUB_TOPICS)} present")
        wrong = [t for t in DATAHUB_TOPICS if (t in listed) != present]
        if wrong:
            problems.append(f"Topics {', '.join(wrong)} {'missing' if present else 'still there'} "
                            f"{when}")
        return not wrong

    def recover():
        written.append(first)
        if not (step("write", first) and step("search", first, seconds)):
            return
        # OpenSearch's volume lost (ADR-070): system-update recreates the indices, and a
        # restore replays datahub-pg into them.
        if not (index_is(200, "before its volume is lost")
                and lost(f"{release.DATAHUB_OPENSEARCH}-0", OPENSEARCH_POD_LABELS,
                         OPENSEARCH_RESTART_TIMEOUT)
                and index_is(404, "on the new volume")):
            return
        notes.append(f"Index {DATAHUB_DATASET_INDEX} gone with the volume.")
        if not (reinstall() and index_is(200, "after system-update")):
            return
        notes.append(f"Index {DATAHUB_DATASET_INDEX} recreated by system-update.")
        if not (restore_indices() and step("search", first, seconds)):
            return
        # Kafka's volume lost (ADR-025, ADR-068 §5): system-update recreates the topics and its
        # message, which a new GMS waits for.
        if not (topics_are(True, "before the broker's volume is lost")
                and lost(f"{release.DATAHUB_KAFKA}-0", KAFKA_POD_LABELS, KAFKA_RESTART_TIMEOUT)
                and topics_are(False, "on the new volume")):
            return
        notes.append(f"Topics {', '.join(DATAHUB_TOPICS)} gone with the volume.")
        if not (reinstall() and topics_are(True, "after system-update")):
            return
        notes.append(f"Topics {', '.join(DATAHUB_TOPICS)} recreated by system-update.")
        gms_pods = kubectl + ["get", "pods", "-l", GMS_POD_LABELS, "-o", "json"]
        old = {p["metadata"]["uid"] for p in json.loads(output(gms_pods))["items"]}
        mark("GMS's pod being deleted")
        output(kubectl + ["delete", "pod", "-l", GMS_POD_LABELS, "--wait=true"])
        mark("GMS's pod deleted")
        back = wait_for(gms_pods, new_ready_pod(old), "GMS back ready",
                        timeout=GMS_RESTART_TIMEOUT)
        mark(f"GMS's pod {back} ready")
        notes.append(f"GMS's pod deleted; {back} started on the new topics and is ready.")
        if not step("read", first):
            return
        written.append(second)
        if step("write", second):
            step("search", second, seconds)

    # The stores' own images and tools, each pod with its store's client label only.
    for pod in (kafka_check_pod(ns, values(release.DATAHUB_KAFKA_RELEASE)["image"], kafka_pod),
                opensearch_check_pod(ns, opensearch_image(
                    values(release.DATAHUB_OPENSEARCH_RELEASE)), opensearch_pod)):
        run(kubectl + ["apply", "-f", "-"], json.dumps(pod))
    mark("check pods created")
    try:
        for name in (kafka_pod, opensearch_pod):
            output(kubectl + ["wait", "--for=condition=Ready", f"pod/{name}", "--timeout=600s"])
        mark("check pods ready")
        recover()
    finally:
        mark("clean-up started")
        if written:
            step("delete", *written)
        subprocess.run(kubectl + ["delete", "pod", kafka_pod, opensearch_pod,
                                  "--ignore-not-found", "--wait=false"],
                       capture_output=True, text=True, check=False)
        mark("clean-up ended")
    return problems, notes


def check_datahub_pg(env):
    """ADR-066 §5: datahub-pg as its values say; then a WAL switch on its primary, waited for
    until archived through the plugin; then an on-demand Backup, waited for until completed.
    Returns (problems, notes)."""
    ns = env["NS_CATALOG_DATAHUB"]
    kubectl = ["kubectl", "--context", CONTEXT, "-n", ns]
    values, cluster, pods = (json.loads(output(c)) for c in datahub_pg_commands(ns))
    problems, notes = datahub_pg_problems(values, cluster, pods)
    if problems:
        return problems, notes
    problem, note = switch_wal(kubectl, ns, release.DATAHUB_PG,
                               f"through the plugin to {release.DATAHUB_BACKUP_STORE}")
    if problem:
        return [problem], notes
    notes.append(note)
    name = f"{release.DATAHUB_PG}-gate-{os.getpid()}"
    run(kubectl + ["apply", "--server-side", "--field-manager=gate", "-f", "-"],
        json.dumps(backup_manifest(env, name, "NS_CATALOG_DATAHUB", release.DATAHUB_PG)))
    phase, error = wait_for(kubectl + ["get", f"backup/{name}", "-o", "json"], backup_outcome,
                            f"Backup {name}", timeout=BACKUP_TIMEOUT)
    if phase != "completed":
        return [f"Backup {name} of {release.DATAHUB_PG} failed: {error}"], notes
    notes.append(f"Backup {name} of {release.DATAHUB_PG} completed through the plugin.")
    return [], notes


def check_restore_command(env, image, database, name):
    """The pod that runs check_restore.sh (on stdin) as Polaris against the restore."""
    creds = [{"name": var, "valueFrom": {"secretKeyRef": {"name": release.POLARIS_DB_SECRET,
                                                          "key": key}}}
             for var, key in (("PGUSER", "username"), ("PGPASSWORD", "password"))]
    overrides = {"spec": {"containers": [{"name": name, "image": image, "env": creds,
                                          "stdin": True, "stdinOnce": True}]}}
    return ["kubectl", "--context", CONTEXT, "-n", env["NS_CATALOG_POLARIS"], "run", name,
            CHECK_POD_LABEL, f"--image={image}", "--restart=Never", "--rm", "-i", "--quiet",
            "--pod-running-timeout=3m", "--override-type=strategic",
            "--overrides", json.dumps(overrides),
            "--command", "--", "sh", "-s", "--", f"{RESTORE}-rw", database,
            release.POLARIS_REALM, ICEBERG_CATALOG]


def check_network(env, pins):
    """ADR-048 §6: the source namespaces, then from a pod in each a connection to each target
    of network.CASES. The probe pods all run at once. Returns (problems, lines)."""
    objects = network.namespaces_manifest(env)
    objects["items"].append(network.metrics_service(env))
    manifest = json.dumps(objects)
    apply = ["kubectl", "--context", CONTEXT, "apply", "--server-side", "--field-manager=gate",
             "-f", "-"]
    subprocess.run(apply, input=manifest, text=True, check=True, capture_output=True)
    addresses, sources = network.targets(env), network.source_namespaces(env)

    def probe(source):
        namespace, _ = sources[source]
        wanted = sorted({c.target for c in network.CASES if c.source == source})
        command = network.probe_command(namespace, smoke_python_image(pins),
                                        f"check-network-{source}-{os.getpid()}",
                                        [addresses[t] for t in wanted],
                                        labels=network.POD_LABELS.get(source))
        out = subprocess.run(command, input=network.PROBE, capture_output=True, text=True,
                             check=False)
        by_address = network.parse_probe(out.stdout)
        return {t: by_address.get(addresses[t]) for t in wanted}

    with concurrent.futures.ThreadPoolExecutor(len(sources)) as pool:
        results = dict(zip(sources, pool.map(probe, sources)))
    return network.report(network.CASES, results)


def check_metrics(env, pins):
    """ADR-046 §5: every running pod of each job of the contract, then the stand-in asked from
    a check pod until they are all up. Returns (problems, notes)."""
    kubectl = ["kubectl", "--context", CONTEXT, "-n", env["NS_CATALOG_POLARIS"]]
    pods = {job: [p["metadata"]["name"] for p in
                  _running(json.loads(output(kubectl + ["get", "pods", "-l", selector,
                                                        "-o", "json"])))]
            for job, selector in metrics.JOBS.items()}
    command = metrics.query_command(env, smoke_python_image(pins),
                                    f"check-metrics-{os.getpid()}",
                                    sum(len(names) for names in pods.values()))
    out = subprocess.run(command, input=metrics.QUERY, capture_output=True, text=True,
                         check=False)
    print(out.stderr, end="", flush=True)
    return metrics.problems(metrics.parse_answer(out.stdout), pods)


def recovery_pods_command(namespace):
    """The restore's full-recovery Job pods, failed ones included, as JSON (#86)."""
    return ["kubectl", "--context", CONTEXT, "-n", namespace, "get", "pods", "-l",
            f"cnpg.io/cluster={RESTORE},cnpg.io/jobRole=full-recovery", "-o", "json"]


def _container_end(status):
    state = status.get("state", {})
    if "terminated" in state:
        t = state["terminated"]
        text = f"terminated {t.get('reason', '?')} (exit {t.get('exitCode', '?')})"
    else:
        text = next(iter(state), "unknown")
    restarts = status.get("restartCount", 0)
    return f"{status['name']}: {text}" + (f", {restarts} restarts" if restarts else "")


def _clock(timestamp):
    return timestamp[11:19] if timestamp else "?"


def recovery_report(pods):
    """(lines, failed pod names): how many pods the full-recovery Job ran, when each ran and
    how it ended, oldest first. #86: it ran six on the gate before one completed."""
    items = sorted(pods.get("items", []), key=lambda p: p["metadata"]["creationTimestamp"])
    failed = [p["metadata"]["name"] for p in items if p["status"].get("phase") == "Failed"]
    lines = [f"Full recovery of {RESTORE}: {len(items)} pods, {len(failed)} failed."]
    for pod in items:
        statuses = (pod["status"].get("initContainerStatuses", [])
                    + pod["status"].get("containerStatuses", []))
        ends = [c["state"]["terminated"].get("finishedAt") for c in statuses
                if "terminated" in c.get("state", {})]
        when = f"created {_clock(pod['metadata']['creationTimestamp'])}"
        if any(ends):
            when += f", ended {_clock(max(e for e in ends if e))}"
        lines.append(f"  {pod['metadata']['name']}: {pod['status'].get('phase')}, {when}; "
                     + "; ".join(_container_end(c) for c in statuses))
    return lines, failed


def cluster_ready(cluster):
    return any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in cluster.get("status", {}).get("conditions", []))


def remember_pods(seen, pods):
    """Keeps each pod's latest state in `seen`, by name, after it is gone too; returns the
    pods that failed since the last read, oldest first, each named once."""
    newly = []
    for pod in sorted(pods.get("items", []), key=lambda p: p["metadata"]["creationTimestamp"]):
        name = pod["metadata"]["name"]
        before = seen.get(name, {}).get("status", {}).get("phase")
        seen[name] = pod
        if pod["status"].get("phase") == "Failed" and before != "Failed":
            newly.append(name)
    return newly


def _read_json(argv):
    """(the command's JSON, None) or (None, why it could not be read)."""
    done = subprocess.run(argv, capture_output=True, text=True, check=False)
    try:
        if done.returncode != 0:
            raise ValueError(done.stderr.strip() or f"exit {done.returncode}")
        return json.loads(done.stdout), None
    except ValueError as e:
        return None, str(e)


def watch_restore(kubectl, namespace):
    """Waits up to RESTORE_TIMEOUT s for the restore to get Ready, reading it and its
    full-recovery pods every RESTORE_POLL s. Each pod's logs are printed as soon as it is seen
    failed, before CNPG deletes it (#86). A read that fails is counted and the wait goes on.
    Returns (ready, recovery_report's lines for every pod seen)."""
    seen, unread, end = {}, [], time.monotonic() + RESTORE_TIMEOUT
    while True:
        cluster, _ = _read_json(kubectl + ["get", f"cluster/{RESTORE}", "-o", "json"])
        pods, why = _read_json(recovery_pods_command(namespace))
        if pods is None:
            unread.append(why)
        else:
            try:
                failed = remember_pods(seen, pods)
            except (KeyError, TypeError) as e:
                unread.append(f"unexpected pod list: {e}")
                failed = []
            for name in failed:
                print(f"--- logs of {name}, failed", flush=True)
                subprocess.run(kubectl + ["logs", name, "--all-containers", "--prefix",
                                          "--tail=100"], check=False)
        ready = cluster is not None and cluster_ready(cluster)
        if ready or time.monotonic() > end:
            break
        time.sleep(RESTORE_POLL)
    lines, _ = recovery_report({"items": list(seen.values())})
    if unread:
        lines.append(f"  {len(unread)} read{'' if len(unread) == 1 else 's'} of the pods "
                     f"failed: {unread[-1]}")
    return ready, lines


def check_restore(env):
    """Backs up polaris-pg, restores it into polaris-pg-restore, reads it, deletes it.
    Returns (problems, notes)."""
    ns = env["NS_CATALOG_POLARIS"]
    kubectl = ["kubectl", "--context", CONTEXT, "-n", ns]
    apply = kubectl + ["apply", "--server-side", "--field-manager=gate", "-f", "-"]
    pg = polaris_pg_values()
    name = f"{release.POLARIS_PG}-gate-{os.getpid()}"
    run(apply, json.dumps(backup_manifest(env, name)))
    phase, error = wait_for(kubectl + ["get", f"backup/{name}", "-o", "json"], backup_outcome,
                            f"Backup {name}", timeout=BACKUP_TIMEOUT)
    if phase != "completed":
        return [f"Backup {name} of {release.POLARIS_PG} failed: {error}"], []
    notes = [f"Backup {name} of {release.POLARIS_PG} completed through the plugin."]
    problem, note = switch_wal(kubectl, ns)
    if problem:
        return [problem], notes
    notes.append(note)
    try:
        run(apply, json.dumps(restore_cluster(env, pg["image"])))
        ready, lines = watch_restore(kubectl, ns)
        notes += lines
        if not ready:
            return [f"Cluster {RESTORE} did not get Ready from "
                    f"{release.POLARIS_BACKUP_STORE}"], notes
        # CNPG restarts the restored instance once after the recovery: Ready, then refusing
        # connections (smoke 38245393066), as polaris-pg after an upgrade.
        problem = release.settle(release.call, CONTEXT, ns, cluster=RESTORE)
        if problem:
            return [problem], notes
        notes.append(f"Cluster {RESTORE} restored from {release.POLARIS_BACKUP_STORE} is Ready, "
                     f"healthy on {release.SETTLE_READS} reads {release.SETTLE_INTERVAL} s apart.")
        with open(os.path.join(HERE, "check_restore.sh"), encoding="utf-8") as f:
            script = f.read()
        command = check_restore_command(env, pg["image"], pg["database"],
                                        f"check-restore-{os.getpid()}")
        if subprocess.run(command, input=script, text=True, check=False).returncode != 0:
            return [f"Cluster {RESTORE} lacks what polaris-pg held"], notes
        return [], notes
    finally:
        subprocess.run(kubectl + ["delete", "cluster", RESTORE, "--ignore-not-found",
                                  "--wait", "--timeout=5m"], check=False)


def console_source(path=os.path.join(CONSOLE_IMAGE_DIR, "source.json")):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def console_image(source):
    """The console image's name: built here, never pulled, tagged with its commit."""
    return f"{CONSOLE_IMAGE_REPOSITORY}:{source['commit'][:12]}"


def console_image_commands(source, context_dir):
    """Fetch the pinned commit into `context_dir`, build its console/ with our Dockerfile, load
    the image into kind. Our lockfile is copied in, and the commit checked, between the third
    and the fourth command."""
    return [
        ["git", "init", "--quiet", context_dir],
        ["git", "-C", context_dir, "fetch", "--quiet", "--depth", "1", source["repository"],
         source["commit"]],
        ["git", "-C", context_dir, "-c", "advice.detachedHead=false", "checkout", "--quiet",
         "FETCH_HEAD"],
        ["docker", "build", "--file", os.path.join(CONSOLE_IMAGE_DIR, "Dockerfile"),
         "--tag", console_image(source), os.path.join(context_dir, source["path"])],
        ["kind", "load", "docker-image", console_image(source), "--name", CLUSTER],
    ]


def check_commit(repo, commit):
    """Raises unless `repo` has exactly `commit` checked out."""
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if head != commit:
        raise ValueError(f"{repo}: {head or 'nothing'} checked out, pinned {commit}")


def check_console_command(env, image, name):
    """The pod that runs console/check.py (on stdin) with root's credentials, against the
    console's origin and the URL its browser reaches Polaris at."""
    if not release.console_enabled(env):
        sys.exit("the console is not enabled in .env (POLARIS_CONSOLE_ENABLED): nothing to check")
    creds = [{"name": var, "valueFrom": {"secretKeyRef": {"name": release.POLARIS_ROOT_SECRET,
                                                         "key": key}}}
             for var, key in (("ROOT_CLIENT_ID", "clientId"),
                              ("ROOT_CLIENT_SECRET", "clientSecret"))]
    overrides = {"spec": {"containers": [{"name": name, "image": image, "env": creds,
                                          "stdin": True, "stdinOnce": True}]}}
    return ["kubectl", "--context", CONTEXT, "-n", env["NS_CATALOG_POLARIS"], "run", name,
            CHECK_POD_LABEL, f"--image={image}", "--restart=Never", "--rm", "-i", "--quiet",
            "--pod-running-timeout=3m", "--override-type=strategic",
            "--overrides", json.dumps(overrides),
            "--command", "--", "python3", "-", env["POLARIS_CONSOLE_ORIGIN"],
            env["POLARIS_CONSOLE_API_URL"], release.POLARIS_REALM]


def batch_image_commands(directory=release.BATCH_IMAGE_DIR,
                         registry=release.DEFAULT_IMAGE_REGISTRY):
    """Build the log batch's image, tagged as release.py names it in that registry (ADR-042),
    and load it into kind, which runs it without pulling."""
    image = release.batch_image(directory, registry)
    return [["docker", "build", "--file", os.path.join(directory, "Dockerfile"),
             "--tag", image, directory],
            ["kind", "load", "docker-image", image, "--name", CLUSTER]]


def logs_request_ids(prefix, count, pod_names, failing=False):
    """The X-Request-ID each request carries, as logs_traffic.sh makes it -> (its pod, its kind),
    each kind for a rule of the audit policy (contract v2): `read`, root listing the catalogs,
    which it counts (6); `denied`, the same without a token, which it keeps (3); once per pod,
    `invalid`, a table made without a schema, 400, refused before anything is written, kept
    with its application line (2b); `missing`, a table that does not exist, 404, dropped with
    its application line (3'); `commit`, a table made, whose commit counts on its row (2a).
    `catalog`, through the Service, so on no pod known in advance: LOGS_CATALOG made, its
    application line kept (2b). With `failing` (Create Fixtures, ADR-040, logs_traffic.sh's
    LOGS_DOWN_ENDPOINT), also LOGS_CATALOG_down made (`catalog`) and once per pod `failed`, a
    table made there, 500, kept with its ERROR line and the line's `exception` (1)."""
    sent = {f"{prefix}-{pod}-{kind}-{n}": (pod, kind) for pod in pod_names
            for n in range(1, count + 1) for kind in ("read", "denied")}
    sent.update({f"{prefix}-{pod}-{kind}-1": (pod, kind) for pod in pod_names
                 for kind in LOGS_POLICY_KINDS})
    sent[f"{prefix}-setup-catalog"] = (None, "catalog")
    if failing:
        sent.update({f"{prefix}-{pod}-failed-1": (pod, "failed") for pod in pod_names})
        sent[f"{prefix}-setup-down-catalog"] = (None, "catalog")
    return sent


def logs_table(request_id):
    """The table a `commit` request makes, named for it as logs_traffic.sh names it."""
    return request_id.replace("-", "_")


def logs_traffic_command(env, image, name, prefix, count, pods, down=None):
    """The pod that runs logs_traffic.sh (on stdin) with root's credentials against each of
    `pods`, (name, URL) pairs, LOGS_CATALOG on the S3 endpoint in `env`; with `down`, its
    LOGS_DOWN_ENDPOINT (Create Fixtures, ADR-040)."""
    creds = [{"name": var, "valueFrom": {"secretKeyRef": {"name": release.POLARIS_ROOT_SECRET,
                                                         "key": key}}}
             for var, key in (("ROOT_CLIENT_ID", "clientId"),
                              ("ROOT_CLIENT_SECRET", "clientSecret"))]
    if down:
        creds.append({"name": "LOGS_DOWN_ENDPOINT", "value": down})
    overrides = {"spec": {"containers": [{"name": name, "image": image, "env": creds,
                                          "stdin": True, "stdinOnce": True}]}}
    return ["kubectl", "--context", CONTEXT, "-n", env["NS_CATALOG_POLARIS"], "run", name,
            CHECK_POD_LABEL, f"--image={image}", "--restart=Never", "--rm", "-i", "--quiet",
            "--pod-running-timeout=3m", "--override-type=strategic",
            "--overrides", json.dumps(overrides),
            "--command", "--", "sh", "-s", "--",
            f"http://{release.POLARIS}:{POLARIS_API_PORT}", release.POLARIS_REALM, str(count),
            prefix, LOGS_CATALOG, env["S3_ENDPOINT"], env["S3_REGION"],
            env["POLARIS_WAREHOUSE_BUCKET"], *[part for pod in pods for part in pod]]


def batch_now(t):
    """The clock the gate gives the batch: the hour after `t` began, plus the batch's 60 s of
    grace and one more, so the hour `t` is in is publishable (ADR-036)."""
    hour = t.astimezone(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    return (hour + dt.timedelta(hours=1, seconds=61)).strftime("%Y-%m-%dT%H:%M:%SZ")


def batch_job(cronjob, name, now):
    """A Job from the CronJob's own template, the batch told `--now`."""
    template = copy.deepcopy(cronjob["spec"]["jobTemplate"])
    container = template["spec"]["template"]["spec"]["containers"][0]
    container["args"] = container.get("args", []) + ["--now", now]
    metadata = {"name": name, "namespace": cronjob["metadata"]["namespace"]}
    if template.get("metadata", {}).get("labels"):
        metadata["labels"] = template["metadata"]["labels"]
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": metadata,
            "spec": template["spec"]}


def job_outcome(job):
    """`succeeded`, `failed`, or None while the Job runs."""
    status = job.get("status", {})
    if status.get("succeeded"):
        return "succeeded"
    if status.get("failed"):
        return "failed"
    return None


def json_lines(text):
    """The lines of `text` that are JSON objects; anything else (a log line, a traceback) is
    skipped."""
    out = []
    for line in text.splitlines():
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


batch_events = fluent_bit_records = json_lines


def _logs_volume(read_only):
    return ({"name": release.POLARIS_LOGS,
             "persistentVolumeClaim": {"claimName": release.POLARIS_LOGS,
                                       "readOnly": read_only}},
            {"name": release.POLARIS_LOGS, "mountPath": LOGS_MOUNT, "readOnly": read_only})


def logs_listing_command(env, image, name):
    """The pod that runs LOGS_LISTING (on stdin) with the claim mounted read-only."""
    volume, mount = _logs_volume(True)
    overrides = {"spec": {"containers": [{"name": name, "image": image, "stdin": True,
                                          "stdinOnce": True, "volumeMounts": [mount]}],
                          "volumes": [volume]}}
    return ["kubectl", "--context", CONTEXT, "-n", env["NS_CATALOG_POLARIS"], "run", name,
            CHECK_POD_LABEL, f"--image={image}", "--restart=Never", "--rm", "-i", "--quiet",
            "--pod-running-timeout=3m", "--override-type=strategic",
            "--overrides", json.dumps(overrides), "--command", "--", "sh", "-s", "--",
            LOGS_MOUNT]


def parse_listing(text):
    """LOGS_LISTING's lines -> {path: {"sha256": ..., "lines": ...}}."""
    out = {}
    for line in text.splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3:
            out[parts[2]] = {"sha256": parts[0], "lines": int(parts[1])}
    return out


def fluent_bit_pod(env, image, name):
    """The stand-in for the observability team's Fluent Bit (ADR-024, ADR-036): in Polaris's
    namespace, the claim mounted read-only, it tails both output directories from their start
    and prints every line as JSON with its path, then exits once it has read them all."""
    volume, mount = _logs_volume(True)
    paths = ",".join(f"{LOGS_MOUNT}/{d}/*.jsonl" for d in OUTPUT_DIRS)
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": env["NS_CATALOG_POLARIS"],
                         "labels": {"app.kubernetes.io/name": "fluent-bit-standin"}},
            "spec": {
                "restartPolicy": "Never",
                "securityContext": {"runAsNonRoot": True, "runAsUser": READER_UID,
                                    "runAsGroup": READER_UID,
                                    "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{
                    "name": "fluent-bit", "image": image,
                    "command": ["/fluent-bit/bin/fluent-bit"],
                    "args": ["-R", "/fluent-bit/etc/parsers.conf",
                             "-i", "tail", "-p", f"path={paths}", "-p", "read_from_head=true",
                             "-p", "exit_on_eof=true", "-p", "parser=json",
                             "-p", "path_key=source_file", "-p", "buffer_max_size=1M",
                             "-o", "stdout", "-p", "format=json_lines",
                             "-p", "json_date_key=false", "-f", "1"],
                    "securityContext": {"allowPrivilegeEscalation": False,
                                        "readOnlyRootFilesystem": True,
                                        "capabilities": {"drop": ["ALL"]}},
                    "volumeMounts": [mount]}],
                "volumes": [volume]}}


def logs_problems(sent, pods, events, before, after, records):
    """What the reader saw against what was sent (ADR-036, contract v2): every line and row
    holds the contract, every file was read whole, every summary's books balance, each kept
    request was read once from its pod, no counted one was kept but the rows count them all,
    the policy's rules 2a, 2b and 3' held on Polaris's own lines (logs_request_ids), every
    pod's file was read, and a second run changed nothing. Returns (problems, notes)."""
    problems = []
    first, second = events
    published = [e["hour"] for e in first if e.get("event") == "published"]
    if not published:
        why = "; ".join(e.get("reason", e.get("event", "")) for e in first) or "no output"
        problems.append(f"the first run published no hour: {why}")
    if any(e.get("event") == "published" for e in second):
        problems.append("the second run published again: "
                        + ", ".join(e["hour"] for e in second if e.get("event") == "published"))
    for path in sorted(set(before) | set(after)):
        if before.get(path) != after.get(path):
            problems.append(f"the second run changed {path}: {before.get(path)} -> "
                            f"{after.get(path)}")
    hours = {d: {p.split("/", 1)[1] for p in after if p.startswith(d + "/")}
             for d in OUTPUT_DIRS}
    for missing in sorted(hours["processed-logs"] ^ hours["aggregated-logs"]):
        where = "aggregated-logs" if missing in hours["processed-logs"] else "processed-logs"
        problems.append(f"{where}/{missing} is missing: an hour has both files")

    processed_schema = contract.load("processed")
    read, rows, seen, by_rid = {}, [], {}, {}
    for record in records:
        record = dict(record)
        path = record.pop("source_file", "")
        rel = path[len(LOGS_MOUNT) + 1:] if path.startswith(LOGS_MOUNT + "/") else path
        read[rel] = read.get(rel, 0) + 1
        kind = rel.split("/", 1)[0]
        if kind == "aggregated-logs":
            problems += [f"{rel}: {p}" for p in contract.row_problems(record)]
            rows.append((rel, record))
        elif kind == "processed-logs":
            problems += [f"{rel}: {p}" for p in contract.problems(record, processed_schema)]
            rid = record.get("mdc", {}).get("requestId")
            if rid in sent:
                seen.setdefault(rid, []).append(record.get("hostName"))
                by_rid.setdefault(rid, []).append(record)
        else:
            problems.append(f"a record from {path or 'no file'}, outside the hand-off")
    for path in sorted(set(after) | set(read)):
        want = after.get(path, {}).get("lines", 0)
        if read.get(path, 0) != want:
            problems.append(f"{path}: the reader read {read.get(path, 0)} lines of {want}")

    summaries = [(rel, r) for rel, r in rows if r.get("report_type") == "summary"]
    for rel, s in summaries:
        books = (("lines_in", s.get("processed", 0) + s.get("dropped", 0) + s.get("malformed", 0)),
                 ("dropped", s.get("access_counted", 0) + s.get("app_dropped_total", 0)
                  + s.get("app_dropped_404", 0)),
                 ("access_kept", s.get("access_seen", 0) - s.get("access_counted", 0)),
                 ("processed", after.get(f"processed-logs/{rel.split('/', 1)[1]}",
                                         {}).get("lines")))
        for field, want in books:
            if s.get(field) != want:
                problems.append(f"{rel}: {field} is {s.get(field)}, its books say {want}")

    kept = {rid: pod for rid, (pod, kind) in sent.items() if kind == "denied"}
    counted = {rid: pod for rid, (pod, kind) in sent.items() if kind == "read"}
    for rid, pod in sorted(kept.items()):
        hosts = seen.get(rid, [])
        if hosts != [pod]:
            problems.append(f"request {rid}, sent to {pod} without a token: read {len(hosts)} "
                            f"times" + (f", from {', '.join(map(str, hosts))}" if hosts else ""))
    for rid in sorted(set(counted) & set(seen)):
        problems.append(f"request {rid}, a read the policy counts, was kept {len(seen[rid])} "
                        f"times")

    def total(kind, key, value, field):
        return sum(r.get(field, 0) for _, r in rows
                   if r.get("report_type") == kind and r.get(key) == value)

    catalogs = "/api/management/v1/catalogs"
    for what, have, want in (
            (f"resource {catalogs} reads", total("resource", "resource", catalogs, "reads"),
             len(kept) + len(counted)),
            (f"resource {catalogs} auth_denied",
             total("resource", "resource", catalogs, "auth_denied"), len(kept)),
            ("principal root reads", total("principal", "user_principal_name", "root", "reads"),
             len(counted)),
            ("principal - auth_denied",
             total("principal", "user_principal_name", "-", "auth_denied"), len(kept))):
        if have < want:
            problems.append(f"{what}: {have}, fewer than the {want} sent")

    def of_kind(kind):
        return sorted(rid for rid, (_, k) in sent.items() if k == kind)

    missing, invalid, commits = of_kind("missing"), of_kind("invalid"), of_kind("commit")
    for rid in missing:
        if by_rid.get(rid):
            problems.append(f"request {rid}, a 404: {len(by_rid[rid])} of its lines kept "
                            f"({', '.join(r.get('loggerName', '') for r in by_rid[rid])})")
    for field in ("counted_404", "app_dropped_404"):
        have = sum(s.get(field, 0) for _, s in summaries)
        if have < len(missing):
            problems.append(f"the summaries' {field}: {have}, fewer than the {len(missing)} "
                            f"404s sent")
    mapper = APP_LINE_LOGGERS[1]
    for rid in invalid + of_kind("catalog"):
        pod, lines = sent[rid][0], by_rid.get(rid, [])
        access = [r for r in lines if "http_status" in r]
        app = [r for r in lines if r.get("loggerName") in
               ((mapper,) if pod else APP_LINE_LOGGERS)]
        hosts = sorted({r.get("hostName") for r in lines})
        if len(access) != 1 or not app or (pod and hosts != [pod]):
            problems.append(f"request {rid}{f', sent to {pod}' if pod else ''}: "
                            f"{len(access)} access line(s) and {len(app)} application line(s) "
                            f"kept, from {', '.join(map(str, hosts)) or 'no pod'}; want one "
                            f"access line and its application line, kept from its pod")
    for rid in commits:
        table = (f"/api/catalog/v1/{LOGS_CATALOG}/namespaces/{LOGS_NAMESPACE}/tables/"
                 f"{logs_table(rid)}")
        if total("resource", "resource", table, "commit_count") < 1:
            problems.append(f"resource {table}: no commit counted for request {rid}")
    failed = of_kind("failed")
    for rid in failed:
        pod, lines = sent[rid][0], by_rid.get(rid, [])
        access = [r for r in lines if r.get("http_status") == 500]
        errors = [r for r in lines if r.get("level") == "ERROR"
                  and isinstance(r.get("exception"), dict)]
        hosts = sorted({r.get("hostName") for r in lines})
        if len(access) != 1 or not errors or hosts != [pod]:
            problems.append(f"request {rid}, sent to {pod}: {len(access)} access line(s) of a "
                            f"500 and {len(errors)} ERROR line(s) with `exception` kept, from "
                            f"{', '.join(map(str, hosts)) or 'no pod'}; want one access line "
                            f"and its ERROR line, kept from its pod")

    logged = {p for _, s in summaries for p in s.get("pods", [])}
    files = {f["file"] for _, s in summaries for f in s.get("sources", [])}
    for pod in pods:
        if pod not in logged or f"polaris-{pod}.log" not in files:
            problems.append(f"pod {pod}: its file polaris-{pod}.log was not read")

    n_files = len(after)
    notes = [
        f"The batch published {len(published)} hour(s) ({', '.join(published)}); the second "
        f"run published nothing and left all {n_files} files as they were.",
        f"The stand-in Fluent Bit read {len(records)} lines from {len(read)} files, each "
        f"checked against contract v2, every summary's books balanced.",
        f"All {len(kept)} kept request IDs (no token, 401) read exactly once, each from the pod "
        f"it was sent to; none of the {len(counted)} counted (root's reads) kept, all counted "
        f"on the rows; pods {', '.join(pods)}, both files read.",
        f"Rules 2a, 2b and 3' on Polaris's own lines: {len(missing)} 404s dropped with their "
        f"application lines, {len(invalid)} 400s kept with theirs, {len(commits)} commits on "
        f"their tables' rows; {LOGS_CATALOG} made, its application line kept."
        + (f" Rule 1: {len(failed)} 500s kept with their ERROR line and its exception."
           if failed else "")]
    return problems, ([] if problems else notes)


def _git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=False)


def released_tags(repo=ROOT):
    """The release tags whose content HEAD contains.

    A tag sits on an `rc` merge commit that `main` never contains (ADR-033), so a tag counts
    when it is an ancestor of HEAD (a commit on `rc`) or when its second parent, the `main`
    commit the release gate tested, is (a commit on `main` or a gate branch).
    """
    tags = _git(repo, "tag", "--list", "platform-*").stdout.split()
    return [t for t in tags
            if _git(repo, "merge-base", "--is-ancestor", t, "HEAD").returncode == 0
            or _git(repo, "merge-base", "--is-ancestor", f"{t}^2", "HEAD").returncode == 0]


def run(argv, stdin=None):
    print("+ " + " ".join(argv), flush=True)
    subprocess.run(argv, input=stdin, text=True, check=True)


def output(argv, stdin=None):
    """A command's stdout; its failure stops the gate with what it said."""
    done = subprocess.run(argv, input=stdin, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        sys.exit(f"{' '.join(argv[:8])}... failed: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout


def wait_for(argv, done, what, timeout=LOGS_TIMEOUT):
    """Reads `argv` as JSON every 3 s until done(answer) gives a value; stops the gate after
    `timeout` seconds."""
    end = time.monotonic() + timeout
    while True:
        result = done(json.loads(output(argv)))
        if result:
            return result
        if time.monotonic() > end:
            sys.exit(f"{what}: not done after {timeout} s")
        time.sleep(3)


def check_logs(env, pins):
    """`check-logs`: see the module's docstring. Returns (problems, notes)."""
    ns, pid = env["NS_CATALOG_POLARIS"], os.getpid()
    kubectl = ["kubectl", "--context", CONTEXT, "-n", ns]
    pods = _running(json.loads(output(kubectl + ["get", "pods", "-l", POLARIS_POD_LABELS,
                                                 "-o", "json"])))
    if len(pods) < 2:
        return [f"{len(pods)} Polaris pod(s) running: the check needs both replicas"], []
    targets = [(p["metadata"]["name"], f"http://{p['status']['podIP']}:{POLARIS_API_PORT}")
               for p in pods]
    prefix = f"gate-logs-{pid}"
    sent = logs_request_ids(prefix, LOGS_REQUESTS, [name for name, _ in targets])
    with open(os.path.join(HERE, "logs_traffic.sh"), encoding="utf-8") as f:
        script = f.read()
    if subprocess.run(logs_traffic_command(env, release.images()["curl"], f"logs-traffic-{pid}",
                                           prefix, LOGS_REQUESTS, targets),
                      input=script, text=True, check=False).returncode:
        return ["the traffic to Polaris's pods failed: see its lines above"], []
    now = batch_now(dt.datetime.now(dt.timezone.utc))
    print(f"The batch runs twice with --now {now}.", flush=True)
    cronjob = json.loads(output(kubectl + ["get", f"cronjob/{release.BATCH}", "-o", "json"]))
    events, listings = [], []
    for n in (1, 2):
        name = f"{release.BATCH}-gate-{pid}-{n}"
        run(kubectl + ["create", "-f", "-"], json.dumps(batch_job(cronjob, name, now)))
        outcome = wait_for(kubectl + ["get", f"job/{name}", "-o", "json"], job_outcome,
                           f"job/{name}")
        log = output(kubectl + ["logs", f"job/{name}"])
        print(log, end="", flush=True)
        if outcome != "succeeded":
            return [f"job/{name} {outcome}: see its output above"], []
        events.append(batch_events(log))
        listings.append(parse_listing(output(
            logs_listing_command(env, release.images()["curl"], f"logs-listing-{pid}-{n}"),
            LOGS_LISTING)))
    reader = fluent_bit_pod(env, pins["standin_images"]["fluent_bit"]["image"],
                            f"fluent-bit-standin-{pid}")
    name = reader["metadata"]["name"]
    run(kubectl + ["apply", "-f", "-"], json.dumps(reader))
    phase = wait_for(kubectl + ["get", f"pod/{name}", "-o", "json"],
                     lambda p: p.get("status", {}).get("phase") in ("Succeeded", "Failed")
                     and p["status"]["phase"], f"pod/{name}")
    records = fluent_bit_records(output(kubectl + ["logs", f"pod/{name}"]))
    run(kubectl + ["delete", f"pod/{name}", "--wait=false"])
    if phase != "Succeeded":
        return [f"the stand-in Fluent Bit ended {phase}"], []
    return logs_problems(sent, [name for name, _ in targets], events, listings[0],
                         listings[1], records)


def up(pins, kind_config=KIND_CONFIG):
    """`up`: see the module's docstring. The failover driver passes its own `kind_config`
    (ADR-047 §3)."""
    namespace = os.environ.get("NS_CNPG_SYSTEM") or "cnpg-system"
    chart = pins["cnpg_chart"]
    with tempfile.TemporaryDirectory() as d:
        chart_path = os.path.join(d, os.path.basename(chart["url"]))
        with open(chart_path, "wb") as f:
            f.write(fetch(chart["url"], chart["sha256"]))
        manifests = {name: fetch(pin["url"], pin["sha256"]).decode("utf-8")
                     for name, pin in pins["manifests"].items()}
        plugin_manifest(manifests["barman_cloud_plugin"], namespace)
        os.makedirs(SHARED_HOST_DIR, exist_ok=True)
        os.chmod(SHARED_HOST_DIR, 0o777)
        for command, stdin in up_commands(pins, chart_path, namespace, manifests,
                                          kind_config):
            run(command, stdin)


def main(argv):
    pins = load_pins()
    if len(argv) >= 3 and argv[1] == "tools":
        tools(pins, argv[2], argv[3:] or TOOLS)
        return 0
    if len(argv) == 2 and argv[1] == "up":
        up(pins)
        return 0
    if argv[1:] in (["secrets"], ["secrets", "--direct"]):
        with open(os.path.join(ROOT, ".env.template"), encoding="utf-8") as f:
            write_gate_env(os.path.join(ROOT, ".env"), f.read(), direct="--direct" in argv)
        run(["python3", os.path.join(ROOT, "scripts", "init_env.py"), "--apply-secrets"])
        return 0
    if argv[1:] in (["standins"], ["standins", "--direct"]):
        direct = "--direct" in argv
        env = release.load_env(ROOT, os.environ)
        with open(os.path.join(HERE, "opa", "policy.rego"), encoding="utf-8") as f, \
                open(os.path.join(HERE, "recorder", "proxy.py"), encoding="utf-8") as code, \
                tempfile.TemporaryDirectory() as d:
            with open(metrics.CONTRACT, encoding="utf-8") as scrape:
                config = metrics.prometheus_config(scrape.read(), env)
            manifest = json.dumps(standins.manifests(
                env, pins, f.read(), code.read(), recorder_certificate(env, d), config,
                direct=direct, cpu_limits=stress_standin_cpu() if direct else None))
        (apply, _), *rest = standins_commands(env, manifest, direct=direct)
        print("+ " + " ".join(apply) + "  (the stand-ins' List on stdin)", flush=True)
        subprocess.run(apply, input=manifest, text=True, check=True)
        for command, stdin in rest:
            run(command, stdin)
        return 0
    if argv[1:] == ["check-standins"]:
        env = release.load_env(ROOT, os.environ)
        cases = standins.load_cases(os.path.join(HERE, "opa", "cases.json"))
        with open(os.path.join(HERE, "check_standins.sh"), encoding="utf-8") as f:
            script = f.read()
        command = check_standins_command(env, cases, release.images()["curl"],
                                         f"check-standins-{os.getpid()}")
        failed = subprocess.run(command, input=script, text=True, check=False).returncode
        with open(os.path.join(HERE, "check_db.sh"), encoding="utf-8") as f:
            script = f.read()
        pg = polaris_pg_values()
        command = check_db_command(env, pg["image"], pg["database"], f"check-db-{os.getpid()}")
        failed |= subprocess.run(command, input=script, text=True, check=False).returncode
        return 1 if failed else 0
    if argv[1:2] == ["check-traffic"] and len(argv) > 2:
        env = release.load_env(ROOT, os.environ)
        log = subprocess.run(recorder_logs_command(env), capture_output=True, text=True,
                             check=True).stdout
        install_end = None
        if os.path.exists(INSTALL_RECORD):
            with open(INSTALL_RECORD, encoding="utf-8") as f:
                install_end = json.load(f)["end"]
        events = json.loads(output(restart_events_command(env["NS_CATALOG_POLARIS"])))
        problems, lines = check_traffic(log, [traffic.load(path) for path in argv[2:]],
                                        restart_window(events, install_end))
        for line in lines:
            print(line)
        return 1 if problems else 0
    if argv[1:] == ["install", "--datahub-only"]:
        start = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        print("Datahub alone, fresh: step 0 and Datahub's part of step 1 of this commit's "
              "release.py (ADR-077).", flush=True)
        problems, notes, timed = datahub_only(release.load_env(ROOT, os.environ),
                                              release.supported_versions())
        for line in notes + problems:
            print(line, flush=True)
        end = dt.datetime.now(dt.timezone.utc)
        for line in measure_pulls(start.isoformat(), (end - start).total_seconds()):
            print(line, flush=True)
        for line in measure_timeline(start.isoformat(), timed):
            print(line, flush=True)
        return 1 if problems else 0
    if argv[1:2] == ["install"] and set(argv[2:]) <= {"--fresh", "--dry-run"} \
            and len(set(argv[2:])) == len(argv[2:]):
        start = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        with tempfile.TemporaryDirectory() as d:
            commands, summary = install_plan(release_tag.previous(released_tags()),
                                             os.path.join(d, "previous"),
                                             "--fresh" in argv, "--dry-run" in argv)
            print(summary, flush=True)
            timed = []
            for command in commands:
                began = dt.datetime.now(dt.timezone.utc).isoformat()
                run(command)
                timed.append((began, dt.datetime.now(dt.timezone.utc).isoformat(),
                              command_label(command)))
        end = dt.datetime.now(dt.timezone.utc)
        if len(commands) > 1 and "--dry-run" not in argv:
            with open(INSTALL_RECORD, "w", encoding="utf-8") as f:
                json.dump({"end": end.isoformat()}, f)
        for line in measure_pulls(start.isoformat(), (end - start).total_seconds()):
            print(line, flush=True)
        # A dry-run alone installs nothing, so it has no timeline.
        if len(commands) > 1 or "--dry-run" not in argv:
            for line in measure_timeline(start.isoformat(), timed):
                print(line, flush=True)
        return 0
    if argv[1:2] == ["console-image"] and argv[2:] in ([], ["--build"], ["--load"]):
        source = console_source()
        with tempfile.TemporaryDirectory() as d:
            commands = console_image_commands(source, d)
            if argv[2:] != ["--load"]:
                for command in commands[:3]:
                    run(command)
                check_commit(d, source["commit"])
                shutil.copyfile(os.path.join(CONSOLE_IMAGE_DIR, "package-lock.json"),
                                os.path.join(d, source["path"], "package-lock.json"))
                run(commands[3])
            if argv[2:] != ["--build"]:
                run(commands[4])
        return 0
    if argv[1:] == ["batch-image"]:
        env = release.load_env(ROOT, os.environ)
        for command in batch_image_commands(registry=release.image_registry(env)):
            run(command)
        return 0
    if argv[1:] == ["check-logs"]:
        problems, notes = check_logs(release.load_env(ROOT, os.environ), pins)
        for line in notes + problems:
            print(line, flush=True)
        return 1 if problems else 0
    if argv[1:] == ["check-console"]:
        env = release.load_env(ROOT, os.environ)
        with open(os.path.join(HERE, "console", "check.py"), encoding="utf-8") as f:
            script = f.read()
        command = check_console_command(env, smoke_python_image(pins),
                                        f"check-console-{os.getpid()}")
        return subprocess.run(command, input=script, text=True, check=False).returncode
    if argv[1:] == ["rebuild-replica"]:
        problems, notes = rebuild_replica(release.load_env(ROOT, os.environ))
        for line in notes + problems:
            print(line, flush=True)
        return 1 if problems else 0
    if argv[1:] == ["check-polaris-pg"]:
        namespace = os.environ.get("NS_CATALOG_POLARIS") or "catalog-polaris"
        outputs = [subprocess.run(c, capture_output=True, text=True, check=True).stdout
                   for c in polaris_pg_commands(namespace)]
        problems, notes = polaris_pg_problems(*(json.loads(o) if o.strip() else None
                                                for o in outputs))
        for line in notes + problems:
            print(line)
        return 1 if problems else 0
    if argv[1:] == ["check-datahub-pg"]:
        problems, notes = check_datahub_pg(release.load_env(ROOT, os.environ))
        for line in notes + problems:
            print(line, flush=True)
        return 1 if problems else 0
    if argv[1:] == ["check-datahub-kafka"]:
        problems, notes = check_datahub_kafka(release.load_env(ROOT, os.environ))
        for line in notes + problems:
            print(line, flush=True)
        return 1 if problems else 0
    if argv[1:] == ["check-datahub-opensearch"]:
        problems, notes = check_datahub_opensearch(release.load_env(ROOT, os.environ))
        for line in notes + problems:
            print(line, flush=True)
        return 1 if problems else 0
    if argv[1:] == ["check-datahub"]:
        problems, notes = check_datahub(release.load_env(ROOT, os.environ), pins)
        for line in notes + problems:
            print(line, flush=True)
        return 1 if problems else 0
    if argv[1:] == ["check-datahub-ingestion"]:
        problems, notes = check_datahub_ingestion(release.load_env(ROOT, os.environ), pins)
        for line in notes + problems:
            print(line, flush=True)
        return 1 if problems else 0
    if argv[1:] == ["check-datahub-recovery"]:
        start, timeline = dt.datetime.now(dt.timezone.utc).isoformat(), []
        try:
            problems, notes = check_datahub_recovery(release.load_env(ROOT, os.environ), pins,
                                                     timeline)
            for line in notes + problems:
                print(line, flush=True)
        finally:
            # ADR-077 §3: passed, failed or stopped by the gate.
            for line in recovery_timeline(timeline, start):
                print(line, flush=True)
        return 1 if problems else 0
    if argv[1:] == ["check-polaris"]:
        env = release.load_env(ROOT, os.environ)
        answers = [json.loads(subprocess.run(c, capture_output=True, text=True,
                                             check=True).stdout)
                   for c in polaris_commands(env["NS_CATALOG_POLARIS"])]
        problems, notes = polaris_problems(*answers, env)
        for line in notes + problems:
            print(line, flush=True)
        with open(os.path.join(HERE, "check_polaris.sh"), encoding="utf-8") as f:
            script = f.read()
        command = check_polaris_command(env, release.images()["curl"],
                                        f"check-polaris-{os.getpid()}",
                                        polaris_pod_urls(answers[2]))
        failed = subprocess.run(command, input=script, text=True, check=False).returncode
        return 1 if problems or failed else 0
    if argv[1:] == ["check-iceberg"]:
        env = release.load_env(ROOT, os.environ)
        with open(os.path.join(HERE, "iceberg", "smoke.py"), encoding="utf-8") as f, \
                open(os.path.join(HERE, "iceberg", "requirements.txt"), encoding="utf-8") as r:
            manifest = json.dumps(iceberg_configmap(env, f.read(), r.read()))
        apply, pod = check_iceberg_commands(env, smoke_python_image(pins),
                                            f"check-iceberg-{os.getpid()}")
        print("+ " + " ".join(apply) + "  (the ConfigMap iceberg-smoke on stdin)", flush=True)
        subprocess.run(apply, input=manifest, text=True, check=True)
        return subprocess.run(pod, check=False).returncode
    if argv[1:] == ["gate-policies"]:
        env = release.load_env(ROOT, os.environ)
        manifest = json.dumps(network.gate_policies(env))
        apply = ["kubectl", "--context", CONTEXT, "apply", "--server-side",
                 "--field-manager=gate", "-f", "-"]
        print("+ " + " ".join(apply) + "  (the gate's allow policies on stdin)", flush=True)
        subprocess.run(apply, input=manifest, text=True, check=True)
        return 0
    if argv[1:] == ["check-network"]:
        problems, lines = check_network(release.load_env(ROOT, os.environ), pins)
        for line in lines + problems:
            print(line, flush=True)
        return 1 if problems else 0
    if argv[1:] == ["check-metrics"]:
        problems, notes = check_metrics(release.load_env(ROOT, os.environ), pins)
        for line in notes + problems:
            print(line, flush=True)
        return 1 if problems else 0
    if argv[1:] in (["seed"], ["check-migration"]):
        step = seed_gate if argv[1] == "seed" else check_migration
        problems, notes = step(release.load_env(ROOT, os.environ), pins)
        for line in notes + problems:
            print(line, flush=True)
        return 1 if problems else 0
    if argv[1:] == ["check-restore"]:
        problems, notes = check_restore(release.load_env(ROOT, os.environ))
        for line in notes + problems:
            print(line, flush=True)
        return 1 if problems else 0
    print(__doc__.strip().splitlines()[-1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

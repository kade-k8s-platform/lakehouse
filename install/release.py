"""Installs a release of this repository in the order of ADR-029.

0. Preconditions, checked, never installed: the CloudNativePG `Cluster` CRD is present and the
   operator in `NS_CNPG_SYSTEM` runs a version this release supports (install/supported.json);
   the Barman Cloud Plugin's `ObjectStore` CRD is present and the plugin runs in
   `NS_CNPG_SYSTEM`: its Service, labelled as CNPG finds plugins, has a ready pod (ADR-043).
   On the target clusters the operator and the plugin are the operator team's; on the gate,
   gates/gate.py installs them first.
1. Polaris, in `NS_CATALOG_POLARIS`, then Datahub, in `NS_CATALOG_DATAHUB` (ADR-045). Their
   Secrets come first (scripts/init_env.py --apply-secrets, ADR-006). The gates run this same
   script (ADR-038).
   a. Its PostgreSQL: the `Cluster` polaris-pg from charts/polaris-postgres (ADR-008,
      ADR-010), installed with helm and waited on until Ready, then until it reads healthy
      several times in a row, since a change can restart its instances; once, when the
      upgrade left its spec unchanged (its generation the same). Its backups go to
      `POLARIS_BACKUP_BUCKET` on `S3_ENDPOINT` with the key in `polaris-backup-s3` (ADR-043).
   b. The S3 endpoint and the policy endpoint from `.env` must answer, asked from Polaris's
      namespace by one short-lived curl pod (install/images.json): any HTTP status is an
      answer, no connection is not (ADR-029, ADR-031). It runs with --dry-run too.
   c. The bootstrap (ADR-026): Polaris's admin tool, in a short-lived pod, creates the schema,
      the realm and its root principal from the `polaris-root` Secret. A realm already
      bootstrapped is kept.
   d. The claim polaris-logs (ADR-036): ReadWriteMany, its storage class (which must exist)
      and size from `.env`, applied outside any Helm release so an uninstall keeps the logs.
   e. The log sidecar's configuration (ADR-081, ADR-082): charts/polaris-log-sidecar, the
      ConfigMap polaris-log-sidecar, with the output host and port from
      `POLARIS_LOG_OUTPUT_HOST` and `POLARIS_LOG_OUTPUT_PORT`. Polaris's pods mount it, so a
      failure stops the install here. An empty host is said, not a problem: the chart's
      placeholder resolves nowhere.
   f. Polaris 1.6.0 from charts/polaris (ADR-009), with values-lakehouse.yaml, the sidecar's
      polaris-values.yaml and the values from `.env` (the database host, the policy endpoint,
      the console's origin, allowed by CORS only while the console is enabled, and a
      checksum of the sidecar chart as a pod annotation), waited on until rolled out. Its
      pods write their log files to the claim and forward them through the sidecar.
   g. Datahub's network policies (ADR-048 §4, ADR-066 §4, ADR-071 §5):
      charts/datahub-network-policies, default deny in its namespace, each store and GMS open
      to their labelled clients, datahub-pg to the operator, GMS to the emitters' namespaces and
      the frontend to the ingress controller's, from `.env`.
   h. Datahub's PostgreSQL: the `Cluster` datahub-pg from charts/datahub-postgres (ADR-066),
      asynchronous, waited on as polaris-pg is. Its backups go to `DATAHUB_BACKUP_BUCKET` on
      `S3_ENDPOINT` with the key in `datahub-backup-s3`.
   i. Datahub's Kafka: the StatefulSet datahub-kafka from charts/datahub-kafka (ADR-068), one
      KRaft broker, waited on until rolled out and ready.
   j. Datahub's OpenSearch: the StatefulSet datahub-opensearch from upstream's chart in
      charts/datahub-opensearch with its values-lakehouse.yaml (ADR-070), one node, waited on
      until rolled out and ready.
   k. Datahub (ADR-071 §7): its four Secrets from init-env read for their key names, then
      upstream's chart in charts/datahub with its values-lakehouse.yaml, through helm's --wait:
      the blocking system-update Job completes before GMS and the frontend start, both are
      ready, and the non-blocking Job completes. An empty DATAHUB_EMITTER_NAMESPACE_LABEL is
      said, not a problem. Its ingestion CronJob (ADR-076 §2) gets Polaris's URI from
      `NS_CATALOG_POLARIS` and the catalogs of DATAHUB_INGEST_POLARIS_CATALOGS, each name
      checked; with none it is suspended, and the install says so.
   l. Datahub's Polaris principal (ADR-076 §3): `datahub-polaris-principal` from init-env read
      for its key names, then a short-lived pod in Datahub's namespace on the ingestion image,
      the Secret mounted and root's credential on stdin, gets a token as root, creates
      `datahub_ingest` (one already there is kept), resets its credentials to the Secret's,
      and gets a token as it. No grant: the policy endpoint decides what it may do.
2. After every service of step 1 (ADR-029, ADR-037):
   a. The log batch (ADR-036): charts/polaris-log-batch with the image built from this
      checkout (batch_image), pulled from `IMAGE_REGISTRY` or, when it is empty, the GitHub
      Container Registry the release workflow pushes it to (ADR-042); then the CronJob read
      back to check it runs that image. Its failure stops the install. It stays until the
      sidecar replaces it (L7).
   b. The console, when `POLARIS_CONSOLE_ENABLED=true` (ADR-028): charts/polaris-console with
      its values-lakehouse.yaml and the URL at which the browser reaches Polaris, waited on
      until rolled out. A maintainer tool, never part of how Polaris operates: its problems
      are printed and the install still passes.

With --dry-run, the helm upgrades are server-side dry-runs, the bootstrap and Datahub's
principal do not run and nothing waits: the dry-run gate (ADR-038).

Settings come from `.env` at the repository root, then the environment, which wins. Every
kubectl and helm call names `$KUBE_CONTEXT` (no default, ADR-004) and its namespace (ADR-007).
Needs kubectl and helm.

`batch-image` prints the batch's image on the GitHub Container Registry, whatever `.env` says:
the image the release workflow pushes (ADR-042).

Usage: python3 install/release.py [--dry-run] | batch-image
       Exits 0 when every step passed, 1 with the problems, 2 on an unknown option.
"""

import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from collections import namedtuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from init_env import SECRETS, parse  # noqa: E402

CNPG_CRD = "clusters.postgresql.cnpg.io"
OPERATOR_LABEL = "app.kubernetes.io/name=cloudnative-pg"
# The Barman Cloud Plugin (ADR-043): its CRD, and the label on the Service by which CNPG finds
# it in the operator's namespace.
PLUGIN_CRD = "objectstores.barmancloud.cnpg.io"
PLUGIN_NAME = "barman-cloud.cloudnative-pg.io"
PLUGIN_LABEL = f"cnpg.io/pluginName={PLUGIN_NAME}"
# Polaris's network policies (ADR-048), and the label a pod needs to reach polaris-pg.
POLARIS_NETPOL_RELEASE = "polaris-network-policies"
POLARIS_NETPOL_CHART = os.path.join(ROOT, "charts", POLARIS_NETPOL_RELEASE)
PG_CLIENT_LABEL = "lakehouse/polaris-pg-client"
# Datahub's network policies and the labels of datahub-pg's, datahub-kafka's and
# datahub-opensearch's clients (ADR-048, ADR-066 §4, ADR-068 §3, ADR-070 §2).
DATAHUB_NETPOL_RELEASE = "datahub-network-policies"
DATAHUB_NETPOL_CHART = os.path.join(ROOT, "charts", DATAHUB_NETPOL_RELEASE)
DATAHUB_PG_CLIENT_LABEL = "lakehouse/datahub-pg-client"
DATAHUB_KAFKA_CLIENT_LABEL = "lakehouse/datahub-kafka-client"
DATAHUB_OPENSEARCH_CLIENT_LABEL = "lakehouse/datahub-opensearch-client"
DATAHUB_GMS_CLIENT_LABEL = "lakehouse/datahub-gms-client"
# Datahub's caller classes in other teams' namespaces (ADR-071 §5): the ingress controller's,
# shared with Polaris, and the lineage emitters'.
DATAHUB_CALLER_VARS = {"ingress": "INGRESS_NAMESPACE_LABEL",
                       "emitter": "DATAHUB_EMITTER_NAMESPACE_LABEL"}
# Polaris's pods, by the labels upstream's chart sets with release name POLARIS.
POLARIS_POD_SELECTOR = "app.kubernetes.io/name=polaris,app.kubernetes.io/instance=polaris"
# One namespace label per caller class in other teams' namespaces (ADR-048 §2).
CALLER_VARS = {"gateway": "POLARIS_GATEWAY_NAMESPACE_LABEL",
               "query": "POLARIS_QUERY_NAMESPACE_LABEL",
               "metrics": "METRICS_NAMESPACE_LABEL",
               "ingress": "INGRESS_NAMESPACE_LABEL"}
POLARIS_PG = "polaris-pg"
POLARIS_PG_RELEASE = "polaris-postgres"
POLARIS_PG_CHART = os.path.join(ROOT, "charts", "polaris-postgres")
POLARIS_DB_SECRET = "polaris-db"
# polaris-pg's backups (ADR-043): the chart's ObjectStore and the Secret with their key.
POLARIS_BACKUP_STORE = "polaris-backup"
POLARIS_BACKUP_SECRET = "polaris-backup-s3"
# Datahub's PostgreSQL and its backups (ADR-066): its own chart, Secrets and ObjectStore.
DATAHUB_PG = "datahub-pg"
DATAHUB_PG_RELEASE = "datahub-postgres"
DATAHUB_PG_CHART = os.path.join(ROOT, "charts", DATAHUB_PG_RELEASE)
DATAHUB_DB_SECRET = "datahub-db"
DATAHUB_BACKUP_STORE = "datahub-backup"
DATAHUB_BACKUP_SECRET = "datahub-backup-s3"
# Datahub's Kafka (ADR-068): its chart and its StatefulSet, of the same name.
DATAHUB_KAFKA = "datahub-kafka"
DATAHUB_KAFKA_RELEASE = "datahub-kafka"
DATAHUB_KAFKA_CHART = os.path.join(ROOT, "charts", DATAHUB_KAFKA_RELEASE)
# Datahub's OpenSearch (ADR-070): upstream's chart with our values; its StatefulSet and Service
# take the release's name.
DATAHUB_OPENSEARCH = "datahub-opensearch"
DATAHUB_OPENSEARCH_RELEASE = "datahub-opensearch"
DATAHUB_OPENSEARCH_CHART = os.path.join(ROOT, "charts", DATAHUB_OPENSEARCH_RELEASE)
DATAHUB_OPENSEARCH_VALUES = os.path.join(DATAHUB_OPENSEARCH_CHART, "values-lakehouse.yaml")
DATAHUB_RELEASE = "datahub"
DATAHUB_CHART = os.path.join(ROOT, "charts", DATAHUB_RELEASE)
DATAHUB_VALUES = os.path.join(DATAHUB_CHART, "values-lakehouse.yaml")
# The Secrets init-env creates for Datahub's chart, its own turned off (ADR-071 §6).
DATAHUB_SECRETS = ("datahub-auth-secrets", "datahub-encryption-secrets",
                   "datahub-frontend-secret", "datahub-root-user")
# helm's limit for each hook Job and for the wait: the three images are pulled first, and GMS's
# readiness probe starts at 120 s.
DATAHUB_TIMEOUT = "1200s"
# The ingestion CronJob's entry in the chart's values, and what a catalog's name may be
# (ADR-076 §1, §2).
INGESTION_CRON = "polaris"
CATALOG_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
POLARIS_API_PORT = 8181
# Datahub's own Polaris principal and its Secret from init-env (ADR-076 §3); the pod that sets
# it runs the ingestion CronJob's image, read from the values file, the registry upstream's
# chart puts before its repository.
DATAHUB_PRINCIPAL = "datahub_ingest"
DATAHUB_PRINCIPAL_SECRET = "datahub-polaris-principal"
INGESTION_REPOSITORY = "docker.io/acryldata/datahub-ingestion"
INGESTION_TAG_RE = re.compile(r'^datahub-ingestion-cron:\n(?:(?:  .*)?\n)*?    tag: "([^"]+)"$',
                              re.MULTILINE)
PRINCIPAL_TIMEOUT = "10m"
# Step 1 reads the Secrets it needs for their key names only, one line per Secret, never a value.
SECRET_KEYS_TEMPLATE = ('go-template={{range .items}}{{.metadata.name}}:'
                        '{{range $key, $value := .data}} {{$key}}{{end}}{{println}}{{end}}')
# The same for one Secret, read by its name alone.
SECRET_ONE_KEYS_TEMPLATE = 'go-template={{range $key, $value := .data}}{{$key}} {{end}}'
# Run in the principal's pod with Python (ADR-076 §3): POLARIS_URL REALM PRINCIPAL; root's
# client ID and secret on stdin, a line each; the principal's in CLIENT_ID and CLIENT_SECRET
# from its Secret. Prints one line per call, `ok` or `FAIL` with the HTTP status and Polaris's
# message, never a credential or a token.
PRINCIPAL_SCRIPT = r"""import json, os, sys, urllib.error, urllib.parse, urllib.request
polaris, realm, name = sys.argv[1:4]
root_id, root_secret = sys.stdin.readline().strip(), sys.stdin.readline().strip()


def call(path, token=None, body=None, form=None):
    headers = {"Polaris-Realm": realm}
    if token:
        headers["Authorization"] = "Bearer " + token
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    else:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(polaris + path, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def answer(status, raw, what, ok):
    if status in ok:
        return json.loads(raw or b"{}")
    try:
        error = json.loads(raw).get("error")
    except ValueError:
        error = None
    message = error.get("message") if isinstance(error, dict) else None
    print(f"FAIL {what}: HTTP {status}" + (f", {message}" if message else ""), flush=True)
    sys.exit(1)


def token(client_id, client_secret, who):
    status, raw = call("/api/catalog/v1/oauth/tokens", form={
        "grant_type": "client_credentials", "client_id": client_id,
        "client_secret": client_secret, "scope": "PRINCIPAL_ROLE:ALL"})
    found = answer(status, raw, f"a token as {who}", (200,))
    print(f"ok   a token as {who}", flush=True)
    return found["access_token"]


root = token(root_id, root_secret, "root")
status, raw = call("/api/management/v1/principals", root, {"principal": {"name": name}})
answer(status, raw, f"creating principal {name}", (201, 409))
print(f"ok   principal {name} " + ("created" if status == 201 else "already there"), flush=True)
status, raw = call(f"/api/management/v1/principals/{name}/reset", root, {
    "clientId": os.environ["CLIENT_ID"], "clientSecret": os.environ["CLIENT_SECRET"]})
answer(status, raw, f"resetting {name}'s credentials", (200,))
print(f"ok   {name}'s credentials reset to datahub-polaris-principal's", flush=True)
token(os.environ["CLIENT_ID"], os.environ["CLIENT_SECRET"], name)
"""
# The key the backups' region comes from; init_env.py writes it from S3_REGION (ADR-060).
POLARIS_BACKUP_REGION_KEY = "region"
POLARIS = "polaris"
POLARIS_REALM = "POLARIS"
POLARIS_DATABASE = "polaris"
POLARIS_PG_SERVICE = f"{POLARIS_PG}-rw"
POLARIS_ROOT_SECRET = "polaris-root"
# The Secrets the chart reads (charts/polaris/values-lakehouse.yaml), from init_env.py.
POLARIS_SECRETS = (POLARIS_DB_SECRET, "polaris-s3", "polaris-token-signing")
# Every `.env` variable that holds a credential: what goes into a Secret.
CREDENTIAL_VARS = sorted({var for s in SECRETS for var in s.from_env.values()})
# The admin tool image's entrypoint. The bootstrap pod runs it only once stdin ends, that is
# once kubectl is attached, so none of its output is lost; all of it goes to stderr, which a
# failed command returns here (call) with its stdout.
ADMIN_TOOL_ENTRYPOINT = "/opt/jboss/container/java/run/run-java.sh"
BOOTSTRAP_WRAPPER = f'cat >/dev/null; exec {ADMIN_TOOL_ENTRYPOINT} "$@" 1>&2'
# What Polaris 1.6.0's admin tool says, exit 3, for a realm that has its root principal
# (polaris-core AuthBootstrapUtil.java); the schema script it ran first changes nothing.
ALREADY_BOOTSTRAPPED = "It appears this metastore manager has already been bootstrapped"
BOOTSTRAP_TAIL = 20
# The claim Polaris writes its log files to and the batch reads (ADR-036); its name is also
# in charts/polaris/values-lakehouse.yaml.
POLARIS_LOGS = "polaris-logs"
# The log batch (ADR-014, ADR-036): its chart, and its image built from this repository,
# tagged by a hash of the files that go into it.
BATCH = "polaris-log-batch"
BATCH_CHART = os.path.join(ROOT, "charts", BATCH)
BATCH_IMAGE_DIR = os.path.join(ROOT, "images", BATCH)
BATCH_IMAGE_FILES = ("Dockerfile", "polaris_log_batch.py")
# Where the release workflow pushes this repository's images, and where a cluster pulls them
# from while `IMAGE_REGISTRY` is empty (ADR-042, ADR-074).
DEFAULT_IMAGE_REGISTRY = "ghcr.io/kade-k8s-platform/lakehouse"
POLARIS_CHART = os.path.join(ROOT, "charts", "polaris")
# The log sidecar (ADR-081, ADR-082): its chart, and the values fragment that adds it to Polaris.
SIDECAR = "polaris-log-sidecar"
SIDECAR_CHART = os.path.join(ROOT, "charts", SIDECAR)
SIDECAR_FRAGMENT = os.path.join(SIDECAR_CHART, "polaris-values.yaml")
POLARIS_VALUES = os.path.join(POLARIS_CHART, "values-lakehouse.yaml")
CONSOLE = "polaris-console"
CONSOLE_CHART = os.path.join(ROOT, "charts", "polaris-console")
CONSOLE_VALUES = os.path.join(CONSOLE_CHART, "values-lakehouse.yaml")
CONSOLE_TIMEOUT = "300s"
READY_TIMEOUT = "900s"
# After the helm upgrade of polaris-pg: CNPG sets no observedGeneration on the Cluster's Ready
# condition, so `kubectl wait` can pass before the operator acts on a new spec; on the smoke
# gate it began restarting the instances 2 s after Ready (adding the plugin, ADR-043). The
# Cluster must read healthy, every instance ready, this many times in a row, this far apart.
# An upgrade that leaves its generation unchanged gives CNPG nothing to roll out, so one
# healthy read is enough then (#125: the three reads cost at least 20 s).
SETTLE_READS = 3
SETTLE_INTERVAL = 10
SETTLE_TIMEOUT = 900
HEALTHY_PHASE = "Cluster in healthy state"
# The endpoint check's pod reads this script on stdin (`sh -s`), so it starts only once kubectl
# is attached and no output is lost. Each URL in its arguments gets one request; any HTTP
# status is an answer. Exits 1 when any URL gave none.
ENDPOINT_CHECK = """fail=0
for url in "$@"; do
  if code=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 15 "$url"); then
    echo "$url answered HTTP $code"
  else
    echo "$url did not answer (curl exit $?)"
    fail=1
  fi
done
exit $fail
"""
# The operator image's last path segment, then a version tag; an optional digest after it.
OPERATOR_IMAGE_RE = re.compile(r"(?:^|/)cloudnative-pg:(\d+\.\d+\.\d+)(?:@sha256:[0-9a-f]+)?$")


def load_env(root, environ):
    env = {}
    path = os.path.join(root, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            env.update(parse(f.read()))
    env.update(environ)
    return env


def supported_versions(path=os.path.join(HERE, "supported.json")):
    with open(path, encoding="utf-8") as f:
        return json.load(f)["cnpg_operator"]


def images(path=os.path.join(HERE, "images.json")):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def operator_versions(deployments_json):
    """Versions of the CNPG operator images in a `kubectl get deployments -o json` answer."""
    versions = []
    for item in json.loads(deployments_json).get("items", []):
        for container in item["spec"]["template"]["spec"].get("containers", []):
            match = OPERATOR_IMAGE_RE.search(container.get("image", ""))
            if match:
                versions.append(match.group(1))
    return versions


def call(argv, stdin=None):
    """Runs a command, with stdin if given; (exit code, stdout or, on failure, stderr and then
    stdout), so a failed pod's own report survives whichever stream kubectl relays it on (#37)."""
    result = subprocess.run(argv, input=stdin, capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return result.returncode, result.stdout
    return result.returncode, "\n".join(part.strip() for part in (result.stderr, result.stdout)
                                         if part.strip())


def check_preconditions(env, run, supported):
    """Step 0. Returns (problems, notes); no call is made without a context."""
    context = env.get("KUBE_CONTEXT", "")
    namespace = env.get("NS_CNPG_SYSTEM", "")
    if not context:
        return ["KUBE_CONTEXT is empty: set the cluster's context in .env (ADR-004)"], []
    if not namespace:
        return ["NS_CNPG_SYSTEM is empty: set the operator's namespace in .env (ADR-007)"], []
    base = ["kubectl", "--context", context]
    code, out = run(base + ["get", "crd", CNPG_CRD, "-o", "name"])
    if code != 0:
        return [f"CRD {CNPG_CRD} not found: the CNPG operator is missing (ADR-029)"], []
    code, out = run(base + ["-n", namespace, "get", "deployments", "-l", OPERATOR_LABEL,
                            "-o", "json"])
    if code != 0:
        return [f"cannot list the operator's deployments in {namespace}: {out.strip()}"], []
    versions = operator_versions(out)
    if not versions:
        return [f"no CNPG operator with a version tag in {namespace} ({OPERATOR_LABEL})"], []
    problems = [f"CNPG operator {v} in {namespace} is not supported; this release supports "
                + ", ".join(supported) for v in versions
                if ".".join(v.split(".")[:2]) not in supported]
    notes = [f"Step 0: CNPG operator {', '.join(versions)} in {namespace}, supported."]
    plugin_problems, plugin_notes = check_plugin(base, namespace, run)
    return problems + plugin_problems, notes + plugin_notes


def _ready(pod):
    return any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in pod.get("status", {}).get("conditions", []))


def check_plugin(base, namespace, run):
    """Step 0's part for the Barman Cloud Plugin (ADR-043 §3): its CRD, then its Service in the
    operator's namespace, then a ready pod behind that Service."""
    code, _ = run(base + ["get", "crd", PLUGIN_CRD, "-o", "name"])
    if code != 0:
        return [f"CRD {PLUGIN_CRD} not found: the Barman Cloud Plugin is missing (ADR-043)"], []
    code, out = run(base + ["-n", namespace, "get", "services", "-l", PLUGIN_LABEL,
                            "-o", "json"])
    if code != 0:
        return [f"cannot list the plugin's services in {namespace}: {out.strip()}"], []
    services = json.loads(out).get("items", [])
    if not services:
        return [f"no Barman Cloud Plugin in {namespace}: no Service labelled {PLUGIN_LABEL} "
                "(ADR-043)"], []
    service = services[0]
    selector = ",".join(f"{k}={v}" for k, v in sorted(service["spec"]["selector"].items()))
    code, out = run(base + ["-n", namespace, "get", "pods", "-l", selector, "-o", "json"])
    if code != 0:
        return [f"cannot list the plugin's pods in {namespace}: {out.strip()}"], []
    if not any(_ready(pod) for pod in json.loads(out).get("items", [])):
        return [f"the Barman Cloud Plugin in {namespace} is not running: no ready pod behind "
                f"Service {service['metadata']['name']} (ADR-043)"], []
    return [], [f"Step 0: Barman Cloud Plugin running in {namespace} "
                f"(Service {service['metadata']['name']})."]


def caller_labels(env, caller_vars):
    """{class: {key, value}} from `.env`'s namespace label per caller class (ADR-048 §2).
    Returns (callers, problems, empty): a caller class whose variable is empty is left out, and
    named in `empty`; anything but `key=value` is a problem. A chart's schema checks the rest."""
    callers, problems, empty = {}, [], []
    for name, var in caller_vars.items():
        text = env.get(var, "").strip()
        if not text:
            empty.append(var)
            continue
        key, sep, value = text.partition("=")
        if not sep or not key:
            problems.append(f"{var} must be one namespace label, key=value (ADR-048)")
            continue
        callers[name] = {"key": key, "value": value}
    return callers, problems, empty


def network_policy_values(env):
    """charts/polaris-network-policies's values from `.env` (ADR-048 §2). Returns (values,
    problems, empty), as caller_labels does."""
    callers, problems, empty = caller_labels(env, CALLER_VARS)
    problems += [f"{var} is empty: the network policies select that namespace by name "
                 "(ADR-048)" for var in ("NS_CATALOG_DATAHUB", "NS_CNPG_SYSTEM")
                 if not env.get(var)]
    values = {"callers": callers,
              "namespaces": {"datahub": env.get("NS_CATALOG_DATAHUB", ""),
                             "operator": env.get("NS_CNPG_SYSTEM", "")},
              "console": {"enabled": console_enabled(env)}}
    return values, problems, empty


def network_policies(env, run, dry_run=False):
    """Step 1, first: Polaris's network policies (ADR-048, ADR-045). Polaris's running pods
    get the client label before, so on the first upgrade to a release with policies they keep
    reaching polaris-pg until their own rollout. Returns (problems, notes)."""
    namespace = env.get("NS_CATALOG_POLARIS", "")
    if not namespace:
        return ["NS_CATALOG_POLARIS is empty: set Polaris's namespace in .env (ADR-007)"], []
    values, problems, empty = network_policy_values(env)
    if problems:
        return problems, []
    context, notes = env["KUBE_CONTEXT"], []
    if not dry_run:
        kubectl = ["kubectl", "--context", context, "-n", namespace]
        code, out = run(kubectl + ["get", "pods", "-l", POLARIS_POD_SELECTOR, "-o", "name"])
        if code != 0:
            return [f"cannot list Polaris's pods in {namespace}: {out.strip()}"], []
        pods = out.split()
        if pods:
            code, out = run(kubectl + ["label", "--overwrite", *pods, f"{PG_CLIENT_LABEL}=true"])
            if code != 0:
                return [f"cannot label Polaris's pods in {namespace}: {out.strip()}"], []
            notes.append(f"Step 1: {len(pods)} running Polaris pods labelled "
                         f"{PG_CLIENT_LABEL}=true before the network policies (ADR-048).")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(values, f)
    try:
        helm = ["helm", "--kube-context", context, "-n", namespace, "upgrade", "--install",
                POLARIS_NETPOL_RELEASE, POLARIS_NETPOL_CHART, "-f", f.name]
        code, out = run(helm + (["--dry-run=server"] if dry_run else []))
    finally:
        os.unlink(f.name)
    if code != 0:
        return [f"helm upgrade {POLARIS_NETPOL_RELEASE} in {namespace} failed: "
                f"{out.strip()}"], []
    refused = (f"; no namespace label in {', '.join(empty)}, so those callers are refused"
               if empty else "")
    done = "server-side dry-run passed" if dry_run else "installed"
    notes.append(f"Step 1: network policies in {namespace}, {done}{refused} (ADR-048).")
    return [], notes


def postgres_values(env):
    """charts/polaris-postgres's values from `.env`: where the backups go (ADR-043)."""
    return {"backup": {"endpointURL": env["S3_ENDPOINT"],
                       "bucket": env["POLARIS_BACKUP_BUCKET"]}}


def datahub_postgres_values(env):
    """charts/datahub-postgres's values from `.env`: where Datahub's backups go (ADR-066 §3)."""
    return {"backup": {"endpointURL": env["S3_ENDPOINT"],
                       "bucket": env["DATAHUB_BACKUP_BUCKET"]}}


# One service's PostgreSQL for step 1: its Cluster, chart and Secrets, the .env variables naming
# its namespace and backup bucket, its values from .env, and the ADRs its messages cite.
Postgres = namedtuple("Postgres", "service namespace_var bucket_var cluster release chart "
                                  "db_secret backup_secret values backup_adr region_adr")
POLARIS_POSTGRES = Postgres("Polaris", "NS_CATALOG_POLARIS", "POLARIS_BACKUP_BUCKET", POLARIS_PG,
                            POLARIS_PG_RELEASE, POLARIS_PG_CHART, POLARIS_DB_SECRET,
                            POLARIS_BACKUP_SECRET, postgres_values, "ADR-043", "ADR-060")
DATAHUB_POSTGRES = Postgres("Datahub", "NS_CATALOG_DATAHUB", "DATAHUB_BACKUP_BUCKET", DATAHUB_PG,
                            DATAHUB_PG_RELEASE, DATAHUB_PG_CHART, DATAHUB_DB_SECRET,
                            DATAHUB_BACKUP_SECRET, datahub_postgres_values, "ADR-066", "ADR-066")


def polaris_postgres(env, run, dry_run=False):
    """Step 1, Polaris's PostgreSQL (ADR-043, ADR-060)."""
    return postgres(env, run, POLARIS_POSTGRES, dry_run)


def datahub_postgres(env, run, dry_run=False):
    """Step 1, Datahub's PostgreSQL, after its network policies (ADR-066 §4)."""
    return postgres(env, run, DATAHUB_POSTGRES, dry_run)


def datahub_network_policies(env, run, dry_run=False):
    """Step 1, Datahub's part, first: its network policies (ADR-048 §4, ADR-066 §4, ADR-071 §5),
    in NS_CATALOG_DATAHUB, admitting the operator's namespace to datahub-pg, and the ingress
    controller's and the emitters' namespaces by their labels in `.env`. Returns (problems,
    notes)."""
    callers, problems, empty = caller_labels(env, DATAHUB_CALLER_VARS)
    problems += [f"{var} is empty: Datahub's network policies need it in .env (ADR-066)"
                 for var in ("NS_CATALOG_DATAHUB", "NS_CNPG_SYSTEM") if not env.get(var)]
    if problems:
        return problems, []
    namespace, context = env["NS_CATALOG_DATAHUB"], env["KUBE_CONTEXT"]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"callers": callers, "namespaces": {"operator": env["NS_CNPG_SYSTEM"]}}, f)
    try:
        helm = ["helm", "--kube-context", context, "-n", namespace, "upgrade", "--install",
                DATAHUB_NETPOL_RELEASE, DATAHUB_NETPOL_CHART, "-f", f.name]
        code, out = run(helm + (["--dry-run=server"] if dry_run else []))
    finally:
        os.unlink(f.name)
    if code != 0:
        return [f"helm upgrade {DATAHUB_NETPOL_RELEASE} in {namespace} failed: "
                f"{out.strip()}"], []
    done = "server-side dry-run passed" if dry_run else "installed"
    refused = (f"; no namespace label in {', '.join(empty)}, so those callers are refused"
               if empty else "")
    return [], [f"Step 1: Datahub's network policies in {namespace}, {done}{refused} (ADR-048, "
                "ADR-066, ADR-071)."]


def datahub_kafka(env, run, dry_run=False):
    """Step 1, Datahub's Kafka, after datahub-pg (ADR-068 §4): its chart, with nothing from
    `.env` but the namespace, then its StatefulSet waited on until rolled out and ready. Returns
    (problems, notes)."""
    namespace = env.get("NS_CATALOG_DATAHUB", "")
    if not namespace:
        return ["NS_CATALOG_DATAHUB is empty: set Datahub's namespace in .env (ADR-007)"], []
    context = env["KUBE_CONTEXT"]
    helm = ["helm", "--kube-context", context, "-n", namespace, "upgrade", "--install",
            DATAHUB_KAFKA_RELEASE, DATAHUB_KAFKA_CHART]
    code, out = run(helm + (["--dry-run=server"] if dry_run else []))
    if code != 0:
        return [f"helm upgrade {DATAHUB_KAFKA_RELEASE} in {namespace} failed: {out.strip()}"], []
    if dry_run:
        return [], [f"Step 1: StatefulSet {DATAHUB_KAFKA} in {namespace}, server-side dry-run "
                    "passed."]
    code, out = run(["kubectl", "--context", context, "-n", namespace, "rollout", "status",
                     f"statefulset/{DATAHUB_KAFKA}", f"--timeout={READY_TIMEOUT}"])
    if code != 0:
        return [f"StatefulSet {DATAHUB_KAFKA} in {namespace} is not ready: {out.strip()}"], []
    return [], [f"Step 1: StatefulSet {DATAHUB_KAFKA} in {namespace} is rolled out and ready "
                "(ADR-068)."]


def datahub_opensearch(env, run, dry_run=False):
    """Step 1, Datahub's OpenSearch, after datahub-kafka (ADR-070 §4): upstream's chart with our
    values, nothing from `.env` but the namespace, then its StatefulSet waited on until rolled
    out and ready. Returns (problems, notes)."""
    namespace = env.get("NS_CATALOG_DATAHUB", "")
    if not namespace:
        return ["NS_CATALOG_DATAHUB is empty: set Datahub's namespace in .env (ADR-007)"], []
    context = env["KUBE_CONTEXT"]
    helm = ["helm", "--kube-context", context, "-n", namespace, "upgrade", "--install",
            DATAHUB_OPENSEARCH_RELEASE, DATAHUB_OPENSEARCH_CHART, "-f", DATAHUB_OPENSEARCH_VALUES]
    code, out = run(helm + (["--dry-run=server"] if dry_run else []))
    if code != 0:
        return [f"helm upgrade {DATAHUB_OPENSEARCH_RELEASE} in {namespace} failed: "
                f"{out.strip()}"], []
    if dry_run:
        return [], [f"Step 1: StatefulSet {DATAHUB_OPENSEARCH} in {namespace}, server-side "
                    "dry-run passed."]
    code, out = run(["kubectl", "--context", context, "-n", namespace, "rollout", "status",
                     f"statefulset/{DATAHUB_OPENSEARCH}", f"--timeout={READY_TIMEOUT}"])
    if code != 0:
        return [f"StatefulSet {DATAHUB_OPENSEARCH} in {namespace} is not ready: "
                f"{out.strip()}"], []
    return [], [f"Step 1: StatefulSet {DATAHUB_OPENSEARCH} in {namespace} is rolled out and "
                "ready (ADR-070)."]


def ingest_catalogs(env):
    """The catalogs of DATAHUB_INGEST_POLARIS_CATALOGS, spaces around each name dropped, or
    None when a name is not ^[A-Za-z0-9_-]+$ (ADR-076 §2)."""
    value = env.get("DATAHUB_INGEST_POLARIS_CATALOGS", "").strip()
    if not value:
        return []
    names = [name.strip() for name in value.split(",")]
    return names if all(CATALOG_NAME_RE.match(name) for name in names) else None


def datahub_values(env):
    """The values the install adds to charts/datahub (ADR-076 §2): the ingestion CronJob's
    Polaris URI, in `NS_CATALOG_POLARIS` from `.env` so no name is in a chart, and its
    catalogs; suspended while there are none."""
    catalogs = ingest_catalogs(env) or []
    uri = (f"http://{POLARIS}.{env['NS_CATALOG_POLARIS']}.svc.cluster.local:{POLARIS_API_PORT}"
           "/api/catalog")
    return {"datahub-ingestion-cron": {"crons": {INGESTION_CRON: {
        "env": {"POLARIS_URI": uri, "DATAHUB_INGEST_POLARIS_CATALOGS": ",".join(catalogs)},
        "suspend": not catalogs}}}}


def datahub(env, run, dry_run=False):
    """Step 1, Datahub itself, after datahub-opensearch (ADR-071 §7): stops unless init-env's
    four Secrets exist with their keys, read by name only, and every catalog to ingest is a
    valid name; says so when the emitters' label or the catalogs are empty; then upstream's
    chart with our values and the ingestion's (datahub_values), and helm's --wait, under which
    the hooks run in order. Returns (problems, notes)."""
    namespace = env.get("NS_CATALOG_DATAHUB", "")
    if not namespace:
        return ["NS_CATALOG_DATAHUB is empty: set Datahub's namespace in .env (ADR-007)"], []
    if not env.get("NS_CATALOG_POLARIS", ""):
        return ["NS_CATALOG_POLARIS is empty: Datahub's ingestion reaches Polaris in it "
                "(ADR-007, ADR-076)"], []
    catalogs = ingest_catalogs(env)
    if catalogs is None:
        return ["DATAHUB_INGEST_POLARIS_CATALOGS must be Polaris catalog names matching "
                "^[A-Za-z0-9_-]+$, comma-separated (ADR-076)"], []
    context = env["KUBE_CONTEXT"]
    code, out = run(["kubectl", "--context", context, "-n", namespace, "get", "secret",
                     *DATAHUB_SECRETS, "-o", SECRET_KEYS_TEMPLATE])
    if code != 0:
        return [f"Secrets {', '.join(DATAHUB_SECRETS)} must exist in {namespace}: run "
                "scripts/init_env.py --apply-secrets first (ADR-006, ADR-071)"], []
    found = {name.strip(): set(rest.split())
             for name, _, rest in (line.partition(":") for line in out.splitlines()) if name}
    problems = []
    for secret in SECRETS:
        if secret.name not in DATAHUB_SECRETS:
            continue
        lacking = sorted({*secret.from_env, *secret.fixed} - found.get(secret.name, set()))
        if lacking:
            problems.append(f"Secret {secret.name} in {namespace} lacks {', '.join(lacking)}: "
                            "run scripts/init_env.py --apply-secrets with this release first "
                            "(ADR-071)")
    if problems:
        return problems, []
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(datahub_values(env), f)
    try:
        helm = ["helm", "--kube-context", context, "-n", namespace, "upgrade", "--install",
                DATAHUB_RELEASE, DATAHUB_CHART, "-f", DATAHUB_VALUES, "-f", f.name]
        code, out = run(helm + (["--dry-run=server"] if dry_run
                                else ["--wait", f"--timeout={DATAHUB_TIMEOUT}"]))
    finally:
        os.unlink(f.name)
    if code != 0:
        return [f"helm upgrade {DATAHUB_RELEASE} in {namespace} failed: {out.strip()}"], []
    said = ("" if env.get("DATAHUB_EMITTER_NAMESPACE_LABEL", "").strip() else
            "; DATAHUB_EMITTER_NAMESPACE_LABEL is empty, so no lineage emitter's namespace "
            "may reach GMS")
    said += (f"; the ingestion CronJob runs daily for {', '.join(catalogs)}" if catalogs else
             "; DATAHUB_INGEST_POLARIS_CATALOGS is empty, so the ingestion CronJob is suspended")
    if dry_run:
        return [], [f"Step 1: Datahub in {namespace}, server-side dry-run passed{said} "
                    "(ADR-071, ADR-076)."]
    return [], [f"Step 1: Datahub in {namespace} is installed: system-update's Jobs completed, "
                f"GMS and the frontend ready{said} (ADR-071, ADR-076)."]


def postgres(env, run, pg, dry_run=False):
    """Step 1, one service's PostgreSQL `pg`. Returns (problems, notes); no call without a
    namespace or the backups' endpoint and bucket."""
    namespace = env.get(pg.namespace_var, "")
    if not namespace:
        return [f"{pg.namespace_var} is empty: set {pg.service}'s namespace in .env (ADR-007)"], []
    empty = [var for var in ("S3_ENDPOINT", pg.bucket_var) if not env.get(var)]
    if empty:
        return [f"{var} is empty: {pg.cluster}'s backups need it in .env ({pg.backup_adr})"
                for var in empty], []
    context = env["KUBE_CONTEXT"]
    secrets_needed = (pg.db_secret, pg.backup_secret)
    generation = None
    code, out = run(["kubectl", "--context", context, "-n", namespace, "get", "secret",
                     *secrets_needed, "-o", SECRET_KEYS_TEMPLATE])
    if code != 0:
        return [f"Secrets {', '.join(secrets_needed)} must exist in {namespace}: run "
                "scripts/init_env.py --apply-secrets first (ADR-006)"], []
    keys = {name.strip(): set(rest.split())
            for name, _, rest in (line.partition(":") for line in out.splitlines()) if name}
    if POLARIS_BACKUP_REGION_KEY not in keys.get(pg.backup_secret, set()):
        return [f"Secret {pg.backup_secret} in {namespace} has no key "
                f"{POLARIS_BACKUP_REGION_KEY}: run scripts/init_env.py --apply-secrets with this "
                f"release first ({pg.region_adr})"], []
    if not dry_run:
        generation = cluster_generation(run, context, namespace, pg.cluster)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(pg.values(env), f)
    try:
        helm = ["helm", "--kube-context", context, "-n", namespace, "upgrade", "--install",
                pg.release, pg.chart, "-f", f.name]
        code, out = run(helm + (["--dry-run=server"] if dry_run else []))
    finally:
        os.unlink(f.name)
    if code != 0:
        return [f"helm upgrade {pg.release} in {namespace} failed: {out.strip()}"], []
    if dry_run:
        return [], [f"Step 1: Cluster {pg.cluster} in {namespace}, server-side dry-run passed."]
    code, out = run(["kubectl", "--context", context, "-n", namespace, "wait",
                     "--for=condition=Ready", f"cluster/{pg.cluster}",
                     f"--timeout={READY_TIMEOUT}"])
    if code != 0:
        return [f"Cluster {pg.cluster} in {namespace} is not Ready: {out.strip()}"], []
    unchanged = generation is not None and         cluster_generation(run, context, namespace, pg.cluster) == generation
    problem = settle(run, context, namespace, pg.cluster,
                     needed=1 if unchanged else SETTLE_READS)
    if problem:
        return [problem], []
    if unchanged:
        return [], [f"Step 1: Cluster {pg.cluster} in {namespace} is Ready, healthy on one "
                    f"read; its spec is unchanged by the upgrade (generation {generation})."]
    return [], [f"Step 1: Cluster {pg.cluster} in {namespace} is Ready, healthy on "
                f"{SETTLE_READS} reads {SETTLE_INTERVAL} s apart."]


def cluster_generation(run, context, namespace, cluster=POLARIS_PG):
    """The Cluster's metadata.generation; None when there is none yet or it cannot be read."""
    code, out = run(["kubectl", "--context", context, "-n", namespace, "get",
                     f"cluster/{cluster}", "-o", "jsonpath={.metadata.generation}"])
    return int(out) if code == 0 and out.strip().isdigit() else None


def settle(run, context, namespace, cluster=POLARIS_PG, needed=SETTLE_READS):
    """None once `cluster` reads healthy with every instance ready `needed` times in a row;
    a problem naming its last phase after SETTLE_TIMEOUT seconds."""
    end, healthy, phase = time.monotonic() + SETTLE_TIMEOUT, 0, ""
    while True:
        code, out = run(["kubectl", "--context", context, "-n", namespace, "get",
                         f"cluster/{cluster}", "-o", "json"])
        if code != 0:
            return f"cannot read Cluster {cluster} in {namespace}: {out.strip()}"
        read = json.loads(out)
        status = read.get("status", {})
        phase = status.get("phase", "")
        ok = (phase == HEALTHY_PHASE
              and status.get("readyInstances") == read["spec"]["instances"])
        healthy = healthy + 1 if ok else 0
        if healthy >= needed:
            return None
        if time.monotonic() > end:
            return (f"Cluster {cluster} in {namespace} did not stay healthy for "
                    f"{needed} reads in {SETTLE_TIMEOUT} s; last phase: {phase}")
        time.sleep(SETTLE_INTERVAL)


def jdbc_url(env):
    """Polaris's database: POLARIS_DB_HOST, or the Cluster's read-write service when empty.
    TLS is required and the server's certificate is not verified (Kade, 2026-10-11), so it
    passes the gate's recorder (ADR-035)."""
    host = env.get("POLARIS_DB_HOST") or POLARIS_PG_SERVICE
    return f"jdbc:postgresql://{host}:5432/{POLARIS_DATABASE}?sslmode=require"


# The Quarkus keys whose values Polaris passes to pgjdbc on every connection (ADR-047 §8).
JDBC_PROPERTIES_PREFIX = "quarkus.datasource.jdbc.additional-jdbc-properties."
_JDBC_PROPERTY = re.compile(r'^\s+' + re.escape(JDBC_PROPERTIES_PREFIX) + r'(\w+):\s*"([^"]*)"\s*$')


def polaris_jdbc_properties(path=POLARIS_VALUES):
    """pgjdbc's properties from values-lakehouse.yaml's advancedConfig, read line by line
    without a YAML parser, for the failover harness's probe to connect as Polaris does."""
    with open(path) as f:
        return dict(m.groups() for m in map(_JDBC_PROPERTY.match, f) if m)


def console_enabled(env):
    return env.get("POLARIS_CONSOLE_ENABLED", "") == "true"


def sidecar_port(env):
    """POLARIS_LOG_OUTPUT_PORT as an int, None while empty; ValueError outside 1 to 65535."""
    raw = env.get("POLARIS_LOG_OUTPUT_PORT", "").strip()
    if not raw:
        return None
    if not (raw.isascii() and raw.isdigit()) or not 1 <= int(raw) <= 65535:
        raise ValueError(f"POLARIS_LOG_OUTPUT_PORT is {raw!r}: set a port from 1 to 65535 "
                         "in .env, or leave it empty")
    return int(raw)


def sidecar_values(env):
    """The sidecar chart's values from `.env`: {} while both variables are empty (the chart's
    placeholder host and port stand), else only the keys that are set."""
    output = {}
    host = env.get("POLARIS_LOG_OUTPUT_HOST", "").strip()
    if host:
        output["host"] = host
    port = sidecar_port(env)
    if port is not None:
        output["port"] = port
    return {"output": output} if output else {}


def sidecar_checksum(env, directory=SIDECAR_CHART):
    """A SHA-256 over the names and contents of every file of the sidecar chart but its values
    fragment (which changes the pod spec and rolls the pods itself), then the chart's values
    from `.env`: Fluent Bit reads its ConfigMap at start, so a change must roll Polaris's pods
    (ADR-081). Of the chart's inputs, not of the rendered ConfigMap: no YAML library here."""
    digest = hashlib.sha256()
    names = sorted(os.path.relpath(os.path.join(top, n), directory)
                   for top, _, files in os.walk(directory) for n in files)
    for name in names:
        if name == os.path.basename(SIDECAR_FRAGMENT):
            continue
        with open(os.path.join(directory, name), "rb") as f:
            data = f.read()
        digest.update(f"{name}\0{len(data)}\0".encode() + data)
    digest.update(json.dumps(sidecar_values(env), sort_keys=True).encode())
    return digest.hexdigest()


def polaris_values(env):
    """The chart values that come from `.env`, on top of charts/polaris/values-lakehouse.yaml
    and the sidecar's fragment. Polaris allows the console's origin only while the console is
    enabled (ADR-028); the pods carry a checksum of the sidecar chart (ADR-081)."""
    values = {"advancedConfig": {"quarkus.datasource.jdbc.url": jdbc_url(env),
                                 "polaris.authorization.opa.policy-uri": env["POLARIS_OPA_URL"]},
              "podAnnotations": {"checksum/log-sidecar": sidecar_checksum(env)}}
    if console_enabled(env) and env.get("POLARIS_CONSOLE_ORIGIN"):
        values["cors"] = {"enabled": True, "allowedOrigins": [env["POLARIS_CONSOLE_ORIGIN"]]}
    return values


def console_values(env):
    """The console's values from `.env`: where the browser reaches Polaris and its realm."""
    api = env["POLARIS_CONSOLE_API_URL"].rstrip("/")
    return {"config": {"api": {"polarisApiUrl": api,
                               "oauthTokenUrl": f"{api}/api/catalog/v1/oauth/tokens",
                               "polarisRealm": POLARIS_REALM}}}


def image_registry(env):
    """The registry and path the cluster pulls this repository's images from (ADR-042):
    `IMAGE_REGISTRY`, or the GitHub Container Registry while it is empty."""
    return env.get("IMAGE_REGISTRY", "").rstrip("/") or DEFAULT_IMAGE_REGISTRY


def batch_image(directory=BATCH_IMAGE_DIR, registry=DEFAULT_IMAGE_REGISTRY):
    """The batch's image: the registry, its name, and the first 12 hex digits of a SHA-256
    over the names and contents of the files that go into it."""
    digest = hashlib.sha256()
    for name in BATCH_IMAGE_FILES:
        with open(os.path.join(directory, name), "rb") as f:
            data = f.read()
        digest.update(f"{name}\0{len(data)}\0".encode() + data)
    return f"{registry}/{BATCH}:{digest.hexdigest()[:12]}"


def batch_values(env):
    """The batch chart's values from this checkout and `.env`: the image built from it, in
    the registry the cluster pulls from."""
    repository, tag = batch_image(registry=image_registry(env)).rsplit(":", 1)
    return {"image": {"repository": repository, "tag": tag}}


def ingestion_image(path=DATAHUB_VALUES):
    """The ingestion CronJob's image, by the tag and digest in charts/datahub's values."""
    with open(path, encoding="utf-8") as f:
        m = INGESTION_TAG_RE.search(f.read())
    if not m:
        raise ValueError(f"{path}: no datahub-ingestion-cron image tag")
    return f"{INGESTION_REPOSITORY}:{m.group(1)}"


def datahub_principal(env, run, image=None, name=None, dry_run=False):
    """Step 1, last, after Datahub (ADR-076 §3): datahub_ingest, created by root and given the
    credential in datahub-polaris-principal, read by key names only first. Root's credential
    reaches the pod on stdin, never a command line or a Secret of Datahub's namespace. Returns
    (problems, notes); a dry-run makes no call."""
    if dry_run:
        return [], [f"Step 1: Polaris principal {DATAHUB_PRINCIPAL} is not set in a dry-run "
                    "(ADR-076)."]
    namespace, context = env["NS_CATALOG_DATAHUB"], env["KUBE_CONTEXT"]
    code, out = run(["kubectl", "--context", context, "-n", namespace, "get", "secret",
                     DATAHUB_PRINCIPAL_SECRET, "-o", SECRET_ONE_KEYS_TEMPLATE])
    if code != 0:
        return [f"Secret {DATAHUB_PRINCIPAL_SECRET} must exist in {namespace}: run "
                "scripts/init_env.py --apply-secrets first (ADR-006, ADR-076)"], []
    (secret,) = [s for s in SECRETS if s.name == DATAHUB_PRINCIPAL_SECRET]
    lacking = sorted(set(secret.from_env) - set(out.split()))
    if lacking:
        return [f"Secret {DATAHUB_PRINCIPAL_SECRET} in {namespace} lacks {', '.join(lacking)}: "
                "run scripts/init_env.py --apply-secrets with this release first (ADR-076)"], []
    image = image or ingestion_image()
    name = name or f"datahub-principal-{secrets.token_hex(4)}"
    container = {"name": name, "image": image, "stdin": True, "stdinOnce": True, "env": [
        {"name": "CLIENT_ID", **secret_ref(DATAHUB_PRINCIPAL_SECRET, "clientId")},
        {"name": "CLIENT_SECRET", **secret_ref(DATAHUB_PRINCIPAL_SECRET, "clientSecret")},
    ]}
    polaris = (f"http://{POLARIS}.{env['NS_CATALOG_POLARIS']}.svc.cluster.local:"
               f"{POLARIS_API_PORT}")
    code, out = run(["kubectl", "--context", context, "-n", namespace, "run", name,
                     f"--image={image}", "--restart=Never", "--rm", "-i", "--quiet",
                     f"--pod-running-timeout={PRINCIPAL_TIMEOUT}", "--override-type=strategic",
                     "--overrides=" + json.dumps({"spec": {"containers": [container]}}),
                     "--command", "--", "python3", "-c", PRINCIPAL_SCRIPT, polaris,
                     POLARIS_REALM, DATAHUB_PRINCIPAL],
                    f"{env['POLARIS_ROOT_CLIENT_ID']}\n{env['POLARIS_ROOT_CLIENT_SECRET']}\n")
    if code != 0:
        return [f"Polaris principal {DATAHUB_PRINCIPAL} in {namespace} was not set (exit "
                f"{code}):\n{masked(out, env).strip()}"], []
    return [], [f"Step 1: Polaris principal {DATAHUB_PRINCIPAL} has {DATAHUB_PRINCIPAL_SECRET}'s "
                "credential and gets a token with it (ADR-076)."]


def endpoints_answer(env, run, image=None, name=None):
    """Before Polaris: the S3 and policy endpoints answer from Polaris's namespace (ADR-029).

    Returns (problems, notes); no call is made while an endpoint is empty.
    """
    endpoints = {"S3_ENDPOINT": "the S3 endpoint", "POLARIS_OPA_URL": "the policy endpoint"}
    empty = [f"{var} is empty: set {what} in .env (ADR-031)"
             for var, what in endpoints.items() if not env.get(var)]
    if empty:
        return empty, []
    namespace = env["NS_CATALOG_POLARIS"]
    image = image or images()["curl"]
    name = name or f"endpoint-check-{secrets.token_hex(4)}"
    code, out = run(["kubectl", "--context", env["KUBE_CONTEXT"], "-n", namespace, "run", name,
                     f"--image={image}", "--restart=Never", "--rm", "-i", "--quiet",
                     "--pod-running-timeout=3m", "--command", "--", "sh", "-s", "--",
                     env["S3_ENDPOINT"], env["POLARIS_OPA_URL"]], ENDPOINT_CHECK)
    if code != 0:
        return [f"before Polaris, from {namespace}: {out.strip()} (ADR-029)"], []
    return [], [f"Step 1: the S3 endpoint and the policy endpoint answer from {namespace}."]


def masked(text, env):
    """`text` with every credential from `.env` replaced, before it is printed (ADR-002)."""
    for var in CREDENTIAL_VARS:
        if env.get(var):
            text = text.replace(env[var], "<redacted>")
    return text


def secret_ref(secret, key):
    return {"valueFrom": {"secretKeyRef": {"name": secret, "key": key}}}


def bootstrap(env, run, image=None, name=None, dry_run=False):
    """Step 1, after the endpoints: the realm and its root principal (ADR-026), created by
    Polaris's admin tool in the database Polaris uses. A realm already bootstrapped is kept.

    Returns (problems, notes); a dry-run makes no call, since the tool has no dry-run.
    """
    namespace, context = env["NS_CATALOG_POLARIS"], env["KUBE_CONTEXT"]
    if dry_run:
        return [], [f"Step 1: the bootstrap of realm {POLARIS_REALM} does not run in a dry-run."]
    code, _ = run(["kubectl", "--context", context, "-n", namespace, "get", "secret",
                   POLARIS_ROOT_SECRET, "-o", "name"])
    if code != 0:
        return [f"Secret {POLARIS_ROOT_SECRET} not found in {namespace}: run "
                "scripts/init_env.py --apply-secrets first (ADR-006)"], []
    image = image or images()["polaris_admin_tool"]
    name = name or f"polaris-bootstrap-{secrets.token_hex(4)}"
    container = {"name": name, "image": image, "stdin": True, "stdinOnce": True, "env": [
        {"name": "QUARKUS_DATASOURCE_JDBC_URL", "value": jdbc_url(env)},
        {"name": "QUARKUS_DATASOURCE_USERNAME", **secret_ref(POLARIS_DB_SECRET, "username")},
        {"name": "QUARKUS_DATASOURCE_PASSWORD", **secret_ref(POLARIS_DB_SECRET, "password")},
        {"name": "POLARIS_ROOT_CLIENT_ID", **secret_ref(POLARIS_ROOT_SECRET, "clientId")},
        {"name": "POLARIS_ROOT_CLIENT_SECRET", **secret_ref(POLARIS_ROOT_SECRET, "clientSecret")},
    ]}
    # Kubernetes expands $(VAR) from the container's env, so no credential is on this command
    # line or in the pod's spec.
    credential = f"{POLARIS_REALM},$(POLARIS_ROOT_CLIENT_ID),$(POLARIS_ROOT_CLIENT_SECRET)"
    code, out = run(["kubectl", "--context", context, "-n", namespace, "run", name,
                     f"--image={image}", "--restart=Never", "--rm", "-i", "--quiet",
                     f"--labels={PG_CLIENT_LABEL}=true",
                     "--pod-running-timeout=3m", "--override-type=strategic",
                     "--overrides=" + json.dumps({"spec": {"containers": [container]}}),
                     "--command", "--", "sh", "-c", BOOTSTRAP_WRAPPER, "admin-tool",
                     "bootstrap", "-r", POLARIS_REALM, "-c", credential], "\n")
    out = masked(out, env)
    if code == 0:
        return [], [f"Step 1: realm {POLARIS_REALM} bootstrapped in {namespace}: its schema "
                    "and root principal."]
    if ALREADY_BOOTSTRAPPED in out:
        return [], [f"Step 1: realm {POLARIS_REALM} in {namespace} was already bootstrapped; "
                    "kept as it is."]
    tail = "\n".join(out.strip().splitlines()[-BOOTSTRAP_TAIL:])
    return [f"the bootstrap of realm {POLARIS_REALM} in {namespace} failed (exit {code}):\n"
            f"{tail}"], []


def logs_claim_manifest(env):
    """The claim polaris-logs: ReadWriteMany, its class and size from `.env`, outside any Helm
    release so an uninstall keeps the logs (ADR-036)."""
    return {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"name": POLARIS_LOGS, "namespace": env["NS_CATALOG_POLARIS"],
                         "labels": {"app.kubernetes.io/name": "polaris",
                                    "app.kubernetes.io/component": "logs"}},
            "spec": {"accessModes": ["ReadWriteMany"], "volumeMode": "Filesystem",
                     "storageClassName": env["POLARIS_LOGS_STORAGE_CLASS"],
                     "resources": {"requests": {"storage": env["POLARIS_LOGS_SIZE"]}}}}


def logs_claim(env, run, dry_run=False):
    """Step 1, before Polaris: the claim Polaris and the log batch share (ADR-036). Its storage
    class must exist; the claim binds once Polaris's pods mount it. Returns (problems, notes);
    no call is made while the class or the size is empty."""
    empty = [f"{var} is empty: set the claim's {what} in .env (ADR-036)"
             for var, what in (("POLARIS_LOGS_STORAGE_CLASS", "ReadWriteMany storage class"),
                               ("POLARIS_LOGS_SIZE", "size")) if not env.get(var)]
    if empty:
        return empty, []
    namespace, context = env["NS_CATALOG_POLARIS"], env["KUBE_CONTEXT"]
    cls = env["POLARIS_LOGS_STORAGE_CLASS"]
    code, out = run(["kubectl", "--context", context, "-n", namespace, "get", "storageclass",
                     cls, "-o", "name"])
    if code != 0:
        return [f"the storage class {cls} does not exist: the log claim needs a ReadWriteMany "
                f"class (ADR-036): {out.strip()}"], []
    code, out = run(["kubectl", "--context", context, "-n", namespace, "apply", "-f", "-"]
                    + (["--dry-run=server"] if dry_run else []),
                    json.dumps(logs_claim_manifest(env)))
    if code != 0:
        return [f"the claim {POLARIS_LOGS} in {namespace} was refused: {out.strip()}"], []
    what = (f"the claim {POLARIS_LOGS} in {namespace}: ReadWriteMany, class {cls}, "
            f"{env['POLARIS_LOGS_SIZE']}")
    return [], [f"Step 1: {what}, server-side dry-run passed." if dry_run
                else f"Step 1: {what}."]


def log_sidecar(env, run, dry_run=False):
    """Step 1, after the log claim and before Polaris: the sidecar's ConfigMap from
    charts/polaris-log-sidecar, the output from `.env` (ADR-081, ADR-082). Polaris's pods mount
    it, so a failure stops the install. An empty host is a note, not a problem. Returns
    (problems, notes); a bad port makes no call."""
    try:
        values = sidecar_values(env)
    except ValueError as e:
        return [str(e)], []
    namespace, context = env["NS_CATALOG_POLARIS"], env["KUBE_CONTEXT"]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(values, f)
    try:
        helm = ["helm", "--kube-context", context, "-n", namespace, "upgrade", "--install",
                SIDECAR, SIDECAR_CHART, "-f", f.name]
        code, out = run(helm + (["--dry-run=server"] if dry_run else []))
    finally:
        os.unlink(f.name)
    if code != 0:
        return [f"helm upgrade {SIDECAR} in {namespace} failed: {out.strip()}"], []
    output = values.get("output", {})
    if "host" in output:
        where = f"{output['host']}:{output.get('port', 'the default port')}"
    else:
        where = (f"the chart's placeholder main-fluent-bit-standin, port "
                 f"{output.get('port', 'by default')}, which resolves nowhere on a target "
                 f"cluster: POLARIS_LOG_OUTPUT_HOST is empty")
    what = (f"the log sidecar's configuration (ConfigMap {SIDECAR}) in {namespace}, "
            f"forwarding to {where}")
    return [], [f"Step 1: {what}, server-side dry-run passed." if dry_run
                else f"Step 1: {what}."]


def polaris(env, run, dry_run=False):
    """Step 1, last: Polaris from charts/polaris with values-lakehouse.yaml, the sidecar's
    fragment and the values from `.env`, waited on until its rollout is done. Returns (problems, notes)."""
    namespace, context = env["NS_CATALOG_POLARIS"], env["KUBE_CONTEXT"]
    code, out = run(["kubectl", "--context", context, "-n", namespace, "get", "secret",
                     *POLARIS_SECRETS, "-o", "name"])
    if code != 0:
        return [f"Secrets {', '.join(POLARIS_SECRETS)} must exist in {namespace}: run "
                f"scripts/init_env.py --apply-secrets first (ADR-006): {out.strip()}"], []
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(polaris_values(env), f)
    try:
        helm = ["helm", "--kube-context", context, "-n", namespace, "upgrade", "--install",
                POLARIS, POLARIS_CHART, "-f", POLARIS_VALUES, "-f", SIDECAR_FRAGMENT,
                "-f", f.name]
        code, out = run(helm + (["--dry-run=server"] if dry_run else []))
    finally:
        os.unlink(f.name)
    if code != 0:
        return [f"helm upgrade {POLARIS} in {namespace} failed: {out.strip()}"], []
    if dry_run:
        return [], [f"Step 1: Polaris in {namespace}, server-side dry-run passed."]
    code, out = run(["kubectl", "--context", context, "-n", namespace, "rollout", "status",
                     f"deployment/{POLARIS}", f"--timeout={READY_TIMEOUT}"])
    if code != 0:
        return [f"deployment/{POLARIS} in {namespace} did not finish its rollout: "
                f"{out.strip()}"], []
    return [], [f"Step 1: deployment/{POLARIS} in {namespace} rolled out."]


def log_batch(env, run, dry_run=False):
    """Step 2, first: the log batch's CronJob from charts/polaris-log-batch with the image built
    from this checkout, in the registry from `.env` (ADR-042), then read back to check it runs
    that image (ADR-036). A failure stops the install (ADR-037). Returns (problems, notes)."""
    namespace, context = env["NS_CATALOG_POLARIS"], env["KUBE_CONTEXT"]
    expected = batch_image(registry=image_registry(env))
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(batch_values(env), f)
    try:
        helm = ["helm", "--kube-context", context, "-n", namespace, "upgrade", "--install",
                BATCH, BATCH_CHART, "-f", f.name]
        code, out = run(helm + (["--dry-run=server"] if dry_run else []))
    finally:
        os.unlink(f.name)
    if code != 0:
        return [f"helm upgrade {BATCH} in {namespace} failed: {out.strip()}"], []
    if dry_run:
        return [], [f"Step 2: the log batch in {namespace}, server-side dry-run passed."]
    code, out = run(["kubectl", "--context", context, "-n", namespace, "get",
                     f"cronjob/{BATCH}", "-o",
                     "jsonpath={.spec.jobTemplate.spec.template.spec.containers[0].image}"])
    if code != 0:
        return [f"cronjob/{BATCH} in {namespace} cannot be read: {out.strip()}"], []
    image = out.strip()
    if image != expected:
        return [f"cronjob/{BATCH} in {namespace} runs {image or 'no image'}, "
                f"expected {expected}"], []
    return [], [f"Step 2: cronjob/{BATCH} in {namespace} runs {image}."]


def console(env, run, dry_run=False):
    """Step 2: the console, when enabled (ADR-028). Returns (problems, notes); no call is made
    while it is off or its settings are incomplete."""
    enabled = env.get("POLARIS_CONSOLE_ENABLED", "")
    if enabled not in ("true", "false", ""):
        return [f"POLARIS_CONSOLE_ENABLED is {enabled!r}: set true or false in .env"], []
    if not console_enabled(env):
        return [], ["Step 2: the console is not enabled (ADR-028)."]
    empty = [f"{var} is empty: the console needs it (ADR-028)"
             for var in ("POLARIS_CONSOLE_ORIGIN", "POLARIS_CONSOLE_API_URL") if not env.get(var)]
    if empty:
        return empty, []
    namespace, context = env["NS_CATALOG_POLARIS"], env["KUBE_CONTEXT"]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(console_values(env), f)
    try:
        helm = ["helm", "--kube-context", context, "-n", namespace, "upgrade", "--install",
                CONSOLE, CONSOLE_CHART, "-f", CONSOLE_VALUES, "-f", f.name]
        code, out = run(helm + (["--dry-run=server"] if dry_run else []))
    finally:
        os.unlink(f.name)
    if code != 0:
        return [f"helm upgrade {CONSOLE} in {namespace} failed: {out.strip()}"], []
    if dry_run:
        return [], [f"Step 2: the console in {namespace}, server-side dry-run passed."]
    code, out = run(["kubectl", "--context", context, "-n", namespace, "rollout", "status",
                     f"deployment/{CONSOLE}", f"--timeout={CONSOLE_TIMEOUT}"])
    if code != 0:
        return [f"deployment/{CONSOLE} in {namespace} did not finish its rollout: "
                f"{out.strip()}"], []
    return [], [f"Step 2: deployment/{CONSOLE} in {namespace} rolled out."]


def install(env, run, supported, dry_run):
    """Every step in order; the first step with problems stops the rest. The console's
    problems are notes: it never fails a release (ADR-028); the batch's and the sidecar's stop it
    (ADR-037, ADR-082)."""
    notes = []
    for step in (lambda: check_preconditions(env, run, supported),
                 lambda: network_policies(env, run, dry_run),
                 lambda: polaris_postgres(env, run, dry_run),
                 lambda: endpoints_answer(env, run),
                 lambda: bootstrap(env, run, dry_run=dry_run),
                 lambda: logs_claim(env, run, dry_run),
                 lambda: log_sidecar(env, run, dry_run),
                 lambda: polaris(env, run, dry_run),
                 lambda: datahub_network_policies(env, run, dry_run),
                 lambda: datahub_postgres(env, run, dry_run),
                 lambda: datahub_kafka(env, run, dry_run),
                 lambda: datahub_opensearch(env, run, dry_run),
                 lambda: datahub(env, run, dry_run),
                 lambda: datahub_principal(env, run, dry_run=dry_run),
                 lambda: log_batch(env, run, dry_run)):
        problems, more = step()
        notes += more
        if problems:
            return problems, notes
    problems, more = console(env, run, dry_run)
    notes += more + [f"Step 2: the console (ADR-028, not blocking): {p}" for p in problems]
    return [], notes


def main(argv):
    if argv[1:] == ["batch-image"]:
        print(batch_image())
        return 0
    if argv[1:] not in ([], ["--dry-run"]):
        print(__doc__.strip().splitlines()[-2].strip(), file=sys.stderr)
        return 2
    env = load_env(ROOT, os.environ)
    problems, notes = install(env, call, supported_versions(), dry_run=len(argv) == 2)
    for line in notes + problems:
        print(line)
    return 1 if problems else 0

if __name__ == "__main__":
    sys.exit(main(sys.argv))

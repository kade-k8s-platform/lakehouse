"""Tests for charts/polaris (ADR-009, ADR-016, ADR-026): the upstream 1.6.0 chart rendered with
values-lakehouse.yaml and the values release.py takes from `.env`, each setting that matters
compared with the chart's own default.

The rendered kinds are core Kubernetes kinds, for which kubeconform has no schema here; the
dry-run gate validates them server-side (ADR-016).

Run: python3 -m unittest discover -s charts
Needs helm on PATH (`python3 gates/gate.py tools BIN_DIR helm kubeconform`) and PyYAML
(`pip install --require-hashes -r charts/requirements.txt`).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "install"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import init_env  # noqa: E402
import release  # noqa: E402

NAMESPACE = "catalog-polaris"
GATE_ENV = {"POLARIS_DB_HOST": "recorder.standins.svc.cluster.local",
            "POLARIS_OPA_URL": "http://recorder.standins.svc.cluster.local:8181"
                               "/v1/data/polaris/authz"}
IMAGE_RE = re.compile(r"^apache/polaris:(\d+\.\d+\.\d+)@sha256:[0-9a-f]{64}$")
# Every JVM option Polaris runs with (ADR-061, ADR-062).
JAVA_OPTS = "-XX:MaxRAMPercentage=50.0 -XX:+UseZGC -XX:+ZGenerational -XX:+ExitOnOutOfMemoryError"
ADMIN_IMAGE_RE = re.compile(r"^apache/polaris-admin-tool:(\d+\.\d+\.\d+)@sha256:[0-9a-f]{64}$")


def setUpModule():
    if not shutil.which("helm"):
        raise RuntimeError("helm not on PATH: run "
                           "`python3 gates/gate.py tools BIN_DIR helm kubeconform` first")


def chart_defaults():
    with open(os.path.join(release.POLARIS_CHART, "values.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def chart_version():
    with open(os.path.join(release.POLARIS_CHART, "Chart.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)["appVersion"]


def render(env=GATE_ENV, *sets, api_versions=()):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        json.dump(release.polaris_values(env), f)
    try:
        argv = ["helm", "template", release.POLARIS, release.POLARIS_CHART,
                "--namespace", NAMESPACE, "-f", release.POLARIS_VALUES, "-f", f.name]
        for s in sets:
            argv += ["--set", s]
        for api in api_versions:
            argv += ["--api-versions", api]
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
    finally:
        os.unlink(f.name)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def one(docs, kind, name=release.POLARIS):
    (doc,) = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    return doc


def secret_env(container):
    """The container's variables that come from a Secret: name -> secretKeyRef."""
    return {v["name"]: v["valueFrom"]["secretKeyRef"] for v in container["env"]
            if "secretKeyRef" in v.get("valueFrom", {})}


def properties(docs):
    text = one(docs, "ConfigMap")["data"]["application.properties"]
    pairs = (line.split("=", 1) for line in text.splitlines()
             if "=" in line and not line.startswith("#"))
    return {k.strip(): v.strip() for k, v in pairs}


def secret_keys(name):
    secret = next(s for s in init_env.SECRETS if s.name == name)
    return set(secret.from_env) | set(secret.fixed)


class Rendered(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = render()
        cls.deployment = one(cls.docs, "Deployment")
        cls.pod = cls.deployment["spec"]["template"]["spec"]
        (cls.container,) = cls.pod["containers"]
        cls.props = properties(cls.docs)
        cls.defaults = chart_defaults()

    def test_the_kinds_and_names_the_install_waits_for(self):
        self.assertEqual(sorted((d["kind"], d["metadata"]["name"]) for d in self.docs), [
            ("ConfigMap", "polaris"), ("Deployment", "polaris"),
            ("PodDisruptionBudget", "polaris"), ("Service", "polaris"),
            ("Service", "polaris-mgmt"), ("ServiceAccount", "polaris")])

    def test_no_credential_is_rendered(self):
        self.assertNotIn("Secret", [d["kind"] for d in self.docs])
        plain = {v["name"]: v["value"] for v in self.container.get("env", []) if "value" in v}
        self.assertEqual(set(plain), {"TZ", "JAVA_OPTS"})
        for var in self.container.get("env", []):
            if var["name"] not in plain:
                self.assertIn("valueFrom", var, var["name"])

    def test_two_replicas_where_the_chart_defaults_to_one(self):
        self.assertEqual(self.deployment["spec"]["replicas"], 2)
        self.assertNotEqual(self.defaults["replicaCount"], 2)

    def test_the_server_image_is_the_charts_version_pinned_by_digest(self):
        match = IMAGE_RE.match(self.container["image"])
        self.assertIsNotNone(match, self.container["image"])
        self.assertEqual(match.group(1), chart_version())

    def test_the_admin_tool_is_the_same_version_pinned_by_digest(self):
        match = ADMIN_IMAGE_RE.match(release.images()["polaris_admin_tool"])
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), chart_version())

    def test_the_pods_carry_the_label_polaris_pgs_policy_allows(self):
        # ADR-048 §3; the chart sets no pod label by default.
        self.assertEqual(self.defaults["podLabels"], {})
        labels = self.deployment["spec"]["template"]["metadata"]["labels"]
        self.assertEqual(labels[release.PG_CLIENT_LABEL], "true")

    def test_the_pods_match_the_selector_the_install_relabels_them_by(self):
        labels = self.deployment["spec"]["template"]["metadata"]["labels"]
        for pair in release.POLARIS_POD_SELECTOR.split(","):
            key, value = pair.split("=")
            self.assertEqual(labels[key], value)

    def test_replicas_never_share_a_node(self):
        self.assertEqual(self.defaults["affinity"], {})
        (term,) = self.pod["affinity"]["podAntiAffinity"][
            "requiredDuringSchedulingIgnoredDuringExecution"]
        self.assertEqual(term["topologyKey"], "kubernetes.io/hostname")
        self.assertEqual(term["labelSelector"]["matchLabels"],
                         self.deployment["spec"]["selector"]["matchLabels"])

    def test_one_replica_stays_up_through_a_voluntary_disruption(self):
        self.assertIs(self.defaults["podDisruptionBudget"]["enabled"], False)
        spec = one(self.docs, "PodDisruptionBudget")["spec"]
        self.assertEqual(spec["minAvailable"], 1)
        self.assertNotIn("maxUnavailable", spec)
        self.assertEqual(spec["selector"]["matchLabels"],
                         self.deployment["spec"]["selector"]["matchLabels"])

    def test_persistence_is_postgresql_where_the_chart_defaults_to_memory(self):
        self.assertEqual(self.defaults["persistence"]["type"], "in-memory")
        self.assertEqual(self.props["polaris.persistence.type"], "relational-jdbc")

    def test_the_database_login_comes_from_the_secret_init_env_creates(self):
        env = secret_env(self.container)
        for var, key in (("quarkus.datasource.username", "username"),
                         ("quarkus.datasource.password", "password")):
            self.assertEqual(env[var], {"name": release.POLARIS_DB_SECRET, "key": key})
            self.assertIn(key, secret_keys(release.POLARIS_DB_SECRET))
        self.assertNotIn("quarkus.datasource.jdbc.url", env)

    def test_pgjdbc_gives_up_on_a_lost_node_within_seconds(self):
        # ADR-047 §8: connect 2 s and login 3 s as C1 measured, socket 10 s (Kade, 2026-10-15).
        prefix = release.JDBC_PROPERTIES_PREFIX
        self.assertEqual({k[len(prefix):]: v for k, v in self.props.items()
                          if k.startswith(prefix)},
                         {"connectTimeout": "2", "loginTimeout": "3", "socketTimeout": "10",
                          "tcpKeepAlive": "true"})

    def test_release_reads_the_same_driver_properties_as_helm(self):
        with open(release.POLARIS_VALUES) as f:
            advanced = yaml.safe_load(f)["advancedConfig"]
        prefix = release.JDBC_PROPERTIES_PREFIX
        self.assertEqual(release.polaris_jdbc_properties(),
                         {k[len(prefix):]: str(v) for k, v in advanced.items()
                          if k.startswith(prefix)})

    def test_the_jdbc_url_names_the_host_from_env_with_tls_required(self):
        self.assertEqual(self.props["quarkus.datasource.jdbc.url"],
                         "jdbc:postgresql://recorder.standins.svc.cluster.local:5432/polaris"
                         "?sslmode=require")

    def test_an_empty_db_host_means_the_clusters_read_write_service(self):
        props = properties(render({**GATE_ENV, "POLARIS_DB_HOST": ""}))
        self.assertEqual(props["quarkus.datasource.jdbc.url"],
                         "jdbc:postgresql://polaris-pg-rw:5432/polaris?sslmode=require")

    def test_the_s3_credentials_come_from_their_secret(self):
        env = secret_env(self.container)
        for var, key in (("polaris.storage.aws.access-key", "accessKeyId"),
                         ("polaris.storage.aws.secret-key", "secretAccessKey")):
            self.assertEqual(env[var], {"name": "polaris-s3", "key": key})
            self.assertIn(key, secret_keys("polaris-s3"))

    def test_polaris_writes_to_s3_with_its_own_key_where_the_chart_sets_none(self):
        # With stsUnavailable, Polaris 1.6.0 reads and writes table metadata with the AWS SDK's
        # default credentials; `polaris.storage.aws.*` is only used for STS.
        self.assertEqual(self.defaults["extraEnv"], [])
        env = secret_env(self.container)
        for var, key in (("AWS_ACCESS_KEY_ID", "accessKeyId"),
                         ("AWS_SECRET_ACCESS_KEY", "secretAccessKey")):
            self.assertEqual(env[var], {"name": "polaris-s3", "key": key})
            self.assertIn(key, secret_keys("polaris-s3"))

    def test_tokens_are_signed_with_the_shared_key_init_env_creates(self):
        self.assertEqual(self.defaults["authentication"]["tokenBroker"]["type"], "rsa-key-pair")
        self.assertEqual(self.props["polaris.authentication.type"], "internal")
        self.assertEqual(self.props["polaris.authentication.token-broker.type"], "symmetric-key")
        path = self.props["polaris.authentication.token-broker.symmetric-key.file"]
        mounts = {m["name"]: m["mountPath"] for m in self.container["volumeMounts"]}
        ((volume, secret),) = [(v, src["secret"]) for v in self.pod["volumes"]
                               for src in v.get("projected", {}).get("sources", [])
                               if src.get("secret", {}).get("name") == "polaris-token-signing"]
        (item,) = secret["items"]
        self.assertEqual(item["key"], "signingKey")
        self.assertIn("signingKey", secret_keys("polaris-token-signing"))
        self.assertEqual(path, os.path.join(mounts[volume["name"]], item["path"]))

    def test_authorization_asks_the_policy_endpoint_from_env(self):
        self.assertNotIn("polaris.authorization.type", self.defaults["advancedConfig"])
        self.assertEqual(self.props["polaris.authorization.type"], "opa")
        self.assertEqual(self.props["polaris.authorization.opa.policy-uri"],
                         GATE_ENV["POLARIS_OPA_URL"])

    def test_polaris_writes_no_log_file(self):
        # ADR-081, ADR-082: the logs leave through the sidecar; no file appender.
        self.assertIs(self.defaults["logging"]["file"]["enabled"], False)
        self.assertEqual({k: v for k, v in self.props.items() if k.startswith("quarkus.log.file.")},
                         {"quarkus.log.file.enabled": "false"})

    def test_the_chart_creates_no_log_claim_of_its_own(self):
        self.assertNotIn("PersistentVolumeClaim", [d["kind"] for d in self.docs])

    def test_the_pod_mounts_no_log_claim(self):
        self.assertEqual([v for v in self.pod["volumes"] if "persistentVolumeClaim" in v], [])
        self.assertNotIn("/deployments/logs",
                         [m["mountPath"] for m in self.container["volumeMounts"]])

    def test_polaris_runs_in_utc_so_its_rolls_are_named_in_utc(self):
        self.assertEqual({v["name"]: v.get("value") for v in self.container["env"]}["TZ"], "UTC")

    def test_the_jvm_runs_generational_zgc_where_the_image_picks_parallel(self):
        # JAVA_OPTS replaces the image's run-java.sh defaults whole (ADR-061 §1, §2).
        env = {v["name"]: v.get("value") for v in self.container["env"]}
        self.assertEqual(env["JAVA_OPTS"], JAVA_OPTS)
        self.assertEqual(self.defaults["extraEnv"], [])

    def test_one_realm_named_as_the_bootstrap_creates_it(self):
        self.assertEqual(self.props["polaris.realm-context.realms"], release.POLARIS_REALM)

    def test_cors_is_off_while_the_console_is_not_enabled(self):
        self.assertIs(self.defaults["cors"]["enabled"], False)
        self.assertNotIn("quarkus.http.cors.enabled", self.props)
        self.assertNotIn("quarkus.http.cors.origins", self.props)

    def test_cors_allows_only_the_consoles_origin_while_it_is_enabled(self):
        props = properties(render({**GATE_ENV, "POLARIS_CONSOLE_ENABLED": "true",
                                   "POLARIS_CONSOLE_ORIGIN": "http://console.example:8080",
                                   "POLARIS_CONSOLE_API_URL": "http://polaris.example:8181"}))
        self.assertEqual(props["quarkus.http.cors.enabled"], "true")
        self.assertEqual(props["quarkus.http.cors.origins"], "http://console.example:8080")

    def test_the_management_port_is_8182_named_explicitly(self):
        self.assertEqual(self.props["quarkus.management.port"], "8182")
        mgmt = one(self.docs, "Service", "polaris-mgmt")["spec"]["ports"]
        self.assertEqual([p["port"] for p in mgmt], [8182])

    def test_metrics_stay_on_as_the_chart_defaults(self):
        # ADR-046 §1: /q/metrics on the management port, with no extra tags.
        self.assertIs(self.defaults["metrics"]["enabled"], True)
        self.assertNotIn("quarkus.micrometer.enabled", self.props)
        self.assertFalse([k for k in self.props if k.startswith("polaris.metrics.")])

    def test_no_service_monitor_even_where_the_prometheus_operator_runs(self):
        # ADR-046 §2: the chart renders one wherever monitoring.coreos.com/v1 is served.
        self.assertIs(self.defaults["serviceMonitor"]["enabled"], True)
        docs = render(GATE_ENV, api_versions=["monitoring.coreos.com/v1"])
        self.assertNotIn("ServiceMonitor", [d["kind"] for d in docs])


class ValuesSchema(unittest.TestCase):
    def test_upstreams_schema_refuses_an_unknown_authentication_type(self):
        with self.assertRaises(AssertionError) as raised:
            render(GATE_ENV, "authentication.type=sso")
        self.assertIn("schema", str(raised.exception).lower())


if __name__ == "__main__":
    unittest.main()

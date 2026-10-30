"""Tests for standins.py. Run: python3 -m unittest discover -s gates"""

import base64
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import gate  # noqa: E402
import seed  # noqa: E402
import standins  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = {"NS_STANDINS": "standins", "NS_CATALOG_POLARIS": "catalog-polaris",
       "NS_CATALOG_DATAHUB": "catalog-datahub",
       "POLARIS_S3_ACCESS_KEY_ID": "gatekey",
       "POLARIS_S3_SECRET_ACCESS_KEY": "gatesecret",
       "SMOKE_S3_ACCESS_KEY_ID": "smokekey",
       "SMOKE_S3_SECRET_ACCESS_KEY": "smokesecret",
       "POLARIS_BACKUP_S3_ACCESS_KEY_ID": "backupkey",
       "POLARIS_BACKUP_S3_SECRET_ACCESS_KEY": "backupsecret",
       "POLARIS_WAREHOUSE_BUCKET": "polaris-warehouse",
       "POLARIS_BACKUP_BUCKET": "polaris-backup",
       "DATAHUB_BACKUP_S3_ACCESS_KEY_ID": "datahubbackupkey",
       "DATAHUB_BACKUP_S3_SECRET_ACCESS_KEY": "datahubbackupsecret",
       "DATAHUB_BACKUP_BUCKET": "datahub-backup",
       "METRICS_NAMESPACE_LABEL": "lakehouse-gate/caller=metrics"}
PINS = gate.load_pins(os.path.join(HERE, "pins.json"))


TLS = ("certificate-pem\n", "key-pem\n")
PROMETHEUS_CONFIG = "global:\n  scrape_interval: 10s\nscrape_configs: []\n"


def items(env=ENV, **kwargs):
    return standins.manifests(env, PINS, "package polaris.authz\n", "# proxy.py\n", TLS,
                              PROMETHEUS_CONFIG, **kwargs)["items"]


def objects(env=ENV, **kwargs):
    return {(o["kind"], o["metadata"]["name"]): o for o in items(env, **kwargs)}


def container(deployment):
    (c,) = deployment["spec"]["template"]["spec"]["containers"]
    return c


class Pins(unittest.TestCase):
    def test_each_stand_in_image_is_pinned_by_digest(self):
        for name in ("seaweedfs", "opa", "recorder"):
            with self.subTest(name=name):
                self.assertRegex(PINS["standin_images"][name]["image"],
                                 r"^([a-z]+/)?[a-z]+:\d+\.\d+(\.\d+)?(-slim)?@sha256:[0-9a-f]{64}$")


class Manifests(unittest.TestCase):
    def test_the_namespace_comes_first_and_holds_every_other_object(self):
        # But the Prometheus stand-in's Role and RoleBinding, in the namespace it discovers.
        listed = items()
        self.assertEqual((listed[0]["kind"], listed[0]["metadata"]["name"]),
                         ("Namespace", "standins"))
        for o in listed[1:]:
            with self.subTest(object=o["metadata"]["name"]):
                expected = ("catalog-polaris" if o["kind"] in ("Role", "RoleBinding")
                            else "standins")
                self.assertEqual(o["metadata"]["namespace"], expected)

    def test_the_namespace_name_comes_from_env(self):
        self.assertIn(("Namespace", "other-ns"), objects({**ENV, "NS_STANDINS": "other-ns"}))

    def test_seaweedfs_runs_mini_with_the_bucket_and_without_telemetry(self):
        c = container(objects()[("Deployment", "seaweedfs")])
        self.assertEqual(c["image"], PINS["standin_images"]["seaweedfs"]["image"])
        self.assertEqual(c["args"][0], "mini")
        for arg in ("-s3.port=8333", "-bucket=polaris-warehouse", "-master.telemetry=false"):
            self.assertIn(arg, c["args"])

    def test_seaweedfs_reads_its_identities_from_the_secret(self):
        o = objects()
        c = container(o[("Deployment", "seaweedfs")])
        self.assertIn("-s3.config=/etc/seaweedfs/s3.json", c["args"])
        self.assertNotIn("env", c)
        self.assertIn({"name": "s3-config", "mountPath": "/etc/seaweedfs", "readOnly": True},
                      c["volumeMounts"])
        volumes = o[("Deployment", "seaweedfs")]["spec"]["template"]["spec"]["volumes"]
        self.assertIn({"name": "s3-config", "secret": {"secretName": "seaweedfs-s3"}}, volumes)

    def test_polaris_key_may_do_everything_and_the_smoke_key_only_the_bucket(self):
        config = json.loads(base64.b64decode(
            objects()[("Secret", "seaweedfs-s3")]["data"]["s3.json"]))
        by_name = {i["name"]: i for i in config["identities"]}
        self.assertEqual(sorted(by_name), ["backup", "datahub-backup", "polaris", "smoke"])
        self.assertEqual(by_name["polaris"]["credentials"],
                         [{"accessKey": "gatekey", "secretKey": "gatesecret"}])
        self.assertIn("Admin", by_name["polaris"]["actions"])
        self.assertEqual(by_name["smoke"]["credentials"],
                         [{"accessKey": "smokekey", "secretKey": "smokesecret"}])
        self.assertEqual(sorted(by_name["smoke"]["actions"]),
                         ["List:polaris-warehouse", "Read:polaris-warehouse",
                          "Write:polaris-warehouse"])

    def test_the_backup_key_may_read_write_and_list_its_own_bucket_only(self):
        # ADR-043 §2: the backups' own key, never Polaris's warehouse key.
        config = json.loads(base64.b64decode(
            objects()[("Secret", "seaweedfs-s3")]["data"]["s3.json"]))
        backup = next(i for i in config["identities"] if i["name"] == "backup")
        self.assertEqual(backup["credentials"],
                         [{"accessKey": "backupkey", "secretKey": "backupsecret"}])
        self.assertEqual(sorted(backup["actions"]),
                         ["List:polaris-backup", "Read:polaris-backup", "Write:polaris-backup"])

    def test_datahubs_backup_key_may_read_write_and_list_its_own_bucket_only(self):
        # ADR-066 §3: Datahub's backups have their own key and bucket, never Polaris's.
        config = json.loads(base64.b64decode(
            objects()[("Secret", "seaweedfs-s3")]["data"]["s3.json"]))
        backup = next(i for i in config["identities"] if i["name"] == "datahub-backup")
        self.assertEqual(backup["credentials"],
                         [{"accessKey": "datahubbackupkey", "secretKey": "datahubbackupsecret"}])
        self.assertEqual(sorted(backup["actions"]),
                         ["List:datahub-backup", "Read:datahub-backup", "Write:datahub-backup"])

    def test_the_credentials_appear_only_in_the_secret(self):
        o = objects()
        del o[("Secret", "seaweedfs-s3")]
        text = json.dumps(list(o.values()))
        for value in ("gatekey", "gatesecret", "smokekey", "smokesecret", "backupkey",
                      "backupsecret"):
            self.assertNotIn(value, text)
            self.assertNotIn(base64.b64encode(value.encode()).decode(), text)

    def test_seaweedfs_is_ready_only_once_its_bucket_exists(self):
        probe = container(objects()[("Deployment", "seaweedfs")])["readinessProbe"]
        self.assertIn("/buckets/polaris-warehouse/", " ".join(probe["exec"]["command"]))

    def test_opa_serves_the_policy_from_its_configmap_on_all_addresses(self):
        o = objects()
        c = container(o[("Deployment", "opa")])
        self.assertEqual(c["image"], PINS["standin_images"]["opa"]["image"])
        self.assertEqual(c["args"][:2], ["run", "--server"])
        self.assertIn("--addr=0.0.0.0:8181", c["args"])
        self.assertIn("--ignore=.*", c["args"])
        self.assertEqual(c["readinessProbe"]["httpGet"]["path"], "/health")
        self.assertEqual(o[("ConfigMap", "opa-policy")]["data"]["policy.rego"],
                         "package polaris.authz\n")

    def test_the_recorder_listens_for_each_connection_and_forwards_to_its_service(self):
        c = container(objects()[("Deployment", "recorder")])
        self.assertEqual(c["image"], PINS["standin_images"]["recorder"]["image"])
        self.assertEqual(c["command"][:2], ["python3", "/recorder/proxy.py"])
        args = c["command"][2:]
        pairs = list(zip(args[0::2], args[1::2]))
        self.assertIn(("--http", "opa=8181:opa.standins.svc.cluster.local:8181"), pairs)
        self.assertIn(("--http", "s3=8333:seaweedfs.standins.svc.cluster.local:8333"), pairs)
        self.assertIn(("--pg", "pg=5432:polaris-pg-rw.catalog-polaris.svc.cluster.local:5432"),
                      pairs)
        self.assertIn(("--tls-cert", "/tls/tls.crt"), pairs)
        self.assertIn(("--tls-key", "/tls/tls.key"), pairs)

    def test_the_recorders_readiness_opens_no_connection_it_would_relay(self):
        c = container(objects()[("Deployment", "recorder")])
        probe = c["readinessProbe"]
        self.assertNotIn("tcpSocket", probe)
        self.assertNotIn("httpGet", probe)
        args = c["command"][2:]
        ready = dict(zip(args[0::2], args[1::2]))["--ready-file"]
        self.assertEqual(probe["exec"]["command"], ["test", "-f", ready])

    def test_the_recorder_runs_its_code_from_a_configmap_and_its_certificate_from_a_secret(self):
        o = objects()
        self.assertEqual(o[("ConfigMap", "recorder-code")]["data"]["proxy.py"], "# proxy.py\n")
        tls = o[("Secret", "recorder-tls")]
        self.assertEqual(tls["type"], "kubernetes.io/tls")
        self.assertEqual(base64.b64decode(tls["data"]["tls.crt"]).decode(), TLS[0])
        mounts = {m["mountPath"] for m in container(o[("Deployment", "recorder")])["volumeMounts"]}
        self.assertEqual(mounts, {"/recorder", "/tls"})

    def test_the_services_expose_s3_on_8333_and_opa_on_8181(self):
        o = objects()
        for name, port in (("seaweedfs", 8333), ("opa", 8181)):
            with self.subTest(name=name):
                svc = o[("Service", name)]["spec"]
                self.assertEqual(svc["ports"][0]["port"], port)
                self.assertEqual(svc["selector"], {"app.kubernetes.io/name": name})
        ports = {p["port"] for p in o[("Service", "recorder")]["spec"]["ports"]}
        self.assertEqual(ports, {8181, 8333, 5432})

    def test_the_committed_policy_is_the_package_polaris_reads(self):
        with open(os.path.join(HERE, "opa", "policy.rego"), encoding="utf-8") as f:
            self.assertRegex(f.read(), r"(?m)^package polaris\.authz$")


class NetworkPolicies(unittest.TestCase):
    """ADR-048 §3, NS_STANDINS's rows restated from ADR-034."""

    def setUp(self):
        self.policies = {name: o["spec"] for (kind, name), o in objects().items()
                         if kind == "NetworkPolicy"}

    def test_every_pod_is_denied_ingress_by_default(self):
        self.assertEqual(self.policies["default-deny-ingress"],
                         {"podSelector": {}, "policyTypes": ["Ingress"]})

    def test_the_recorder_from_polaris_namespace_on_its_three_ports_datahubs_on_s3_only(self):
        # ADR-067 §1: datahub-pg's backups reach S3 through the recorder, on its S3 port only.
        spec = self.policies["recorder"]
        self.assertEqual(spec["podSelector"],
                         {"matchLabels": {"app.kubernetes.io/name": "recorder"}})
        self.assertEqual(spec["ingress"], [
            {"ports": [{"protocol": "TCP", "port": p} for p in (8181, 8333, 5432)],
             "from": [{"namespaceSelector": {"matchLabels": {
                 "kubernetes.io/metadata.name": "catalog-polaris"}}}]},
            {"ports": [{"protocol": "TCP", "port": 8333}],
             "from": [{"namespaceSelector": {"matchLabels": {
                 "kubernetes.io/metadata.name": "catalog-datahub"}}}]}])

    def test_seaweedfs_and_opa_from_the_recorder_and_the_gates_check_pods(self):
        for name, port in (("seaweedfs", 8333), ("opa", 8181)):
            with self.subTest(name=name):
                spec = self.policies[name]
                self.assertEqual(spec["podSelector"],
                                 {"matchLabels": {"app.kubernetes.io/name": name}})
                self.assertEqual(spec["ingress"], [{
                    "ports": [{"protocol": "TCP", "port": port}],
                    "from": [
                        {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "recorder"}}},
                        {"namespaceSelector": {"matchLabels": {
                            "kubernetes.io/metadata.name": "catalog-polaris"}},
                         "podSelector": {"matchLabels": {"lakehouse-gate/check": "true"}}}]}])

    def test_prometheus_from_the_gates_check_pods_only(self):
        # ADR-046 §4.
        spec = self.policies["prometheus"]
        self.assertEqual(spec["podSelector"],
                         {"matchLabels": {"app.kubernetes.io/name": "prometheus"}})
        self.assertEqual(spec["ingress"], [{
            "ports": [{"protocol": "TCP", "port": 9090}],
            "from": [{"namespaceSelector": {"matchLabels": {
                          "kubernetes.io/metadata.name": "catalog-polaris"}},
                      "podSelector": {"matchLabels": {"lakehouse-gate/check": "true"}}}]}])

    def test_every_policy_restricts_ingress_only(self):
        self.assertEqual(sorted(self.policies),
                         ["default-deny-ingress", "logs-opensearch", "main-fluent-bit-standin",
                          "opa", "prometheus", "recorder", "seaweedfs"])
        for name, spec in self.policies.items():
            self.assertEqual(spec["policyTypes"], ["Ingress"], name)


class Prometheus(unittest.TestCase):
    """ADR-046 §5: the stand-in scrapes Polaris's namespace as the observability team's would."""

    def setUp(self):
        self.o = objects()

    def test_the_namespace_carries_the_gates_metrics_label(self):
        # So the stand-in crosses the release's own policies as the real scraper would.
        ns = self.o[("Namespace", "standins")]
        self.assertEqual(ns["metadata"]["labels"], {"lakehouse-gate/caller": "metrics"})

    def test_an_empty_metrics_label_leaves_the_namespace_unlabelled(self):
        ns = objects({**ENV, "METRICS_NAMESPACE_LABEL": ""})[("Namespace", "standins")]
        self.assertNotIn("labels", ns["metadata"])

    def test_it_runs_the_pinned_image_with_the_config_given(self):
        deployment = self.o[("Deployment", "prometheus")]
        c = container(deployment)
        self.assertEqual(c["image"], PINS["standin_images"]["prometheus"]["image"])
        self.assertIn("--config.file=/etc/prometheus/prometheus.yml", c["args"])
        self.assertIn("--web.listen-address=:9090", c["args"])
        self.assertEqual(c["readinessProbe"]["httpGet"], {"path": "/-/ready", "port": 9090})
        self.assertEqual(self.o[("ConfigMap", "prometheus-config")]["data"],
                         {"prometheus.yml": PROMETHEUS_CONFIG})
        pod = deployment["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], "prometheus")
        self.assertIn({"name": "config", "configMap": {"name": "prometheus-config"}},
                      pod["volumes"])

    def test_it_may_only_read_pods_in_polaris_namespace(self):
        role = self.o[("Role", "prometheus-discovery")]
        self.assertEqual(role["rules"], [{"apiGroups": [""], "resources": ["pods"],
                                          "verbs": ["get", "list", "watch"]}])
        binding = self.o[("RoleBinding", "prometheus-discovery")]
        self.assertEqual(binding["roleRef"], {"apiGroup": "rbac.authorization.k8s.io",
                                              "kind": "Role", "name": "prometheus-discovery"})
        self.assertEqual(binding["subjects"], [{"kind": "ServiceAccount", "name": "prometheus",
                                                "namespace": "standins"}])
        self.assertIn(("ServiceAccount", "prometheus"), self.o)

    def test_its_service_answers_on_9090(self):
        svc = self.o[("Service", "prometheus")]["spec"]
        self.assertEqual(svc["ports"][0]["port"], 9090)
        self.assertEqual(svc["selector"], {"app.kubernetes.io/name": "prometheus"})


class Direct(unittest.TestCase):
    """ADR-053 §4: a stress install points Polaris at SeaweedFS and OPA directly, never through
    the recorder, and both run under a CPU limit so the run can tell when they are the limit."""

    LIMITS = {"seaweedfs": "1", "opa": "500m"}

    def test_the_default_list_is_the_one_smoke_has_always_applied(self):
        self.assertEqual(items(), items(direct=False))
        for name in ("seaweedfs", "opa"):
            with self.subTest(name=name):
                self.assertNotIn("resources", container(objects()[("Deployment", name)]))

    def test_seaweedfs_and_opa_also_admit_polaris_namespace_on_their_own_port(self):
        policies = {name: o["spec"] for (kind, name), o in
                    objects(direct=True, cpu_limits=self.LIMITS).items()
                    if kind == "NetworkPolicy"}
        default = {name: o["spec"] for (kind, name), o in objects().items()
                   if kind == "NetworkPolicy"}
        named = [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": ns}}}
                 for ns in ("catalog-polaris", "catalog-datahub")]
        # ADR-067 §2: in a stress install SeaweedFS admits Datahub's namespace too; OPA does not.
        for name, port, extra in (("seaweedfs", 8333, named), ("opa", 8181, named[:1])):
            with self.subTest(name=name):
                (rule,) = policies[name]["ingress"]
                self.assertEqual(rule["ports"], [{"protocol": "TCP", "port": port}])
                self.assertEqual(rule["from"], default[name]["ingress"][0]["from"] + extra)
        for name in ("default-deny-ingress", "recorder", "prometheus"):
            with self.subTest(name=name):
                self.assertEqual(policies[name], default[name])

    def test_seaweedfs_and_opa_run_under_the_cpu_limit_given(self):
        found = objects(direct=True, cpu_limits=self.LIMITS)
        for name, cpu in self.LIMITS.items():
            with self.subTest(name=name):
                self.assertEqual(container(found[("Deployment", name)])["resources"],
                                 {"limits": {"cpu": cpu}})

    def test_direct_needs_a_limit_for_both(self):
        with self.assertRaises(ValueError):
            items(direct=True, cpu_limits={"seaweedfs": "1"})
        with self.assertRaises(ValueError):
            items(direct=True)

    def test_only_the_policies_and_the_two_containers_differ(self):
        default = objects()
        direct = objects(direct=True, cpu_limits=self.LIMITS)
        # A stress install leaves the log stand-ins out (ADR-053 §4); test_standins_logs pins it.
        log_standins = {key for key in default
                        if key[1] in ("logs-opensearch", "main-fluent-bit-standin",
                                      "main-fluent-bit-config")}
        self.assertEqual(sorted(set(default) - set(direct)), sorted(log_standins))
        self.assertEqual(set(direct) - set(default), set())
        default = {k: v for k, v in default.items() if k not in log_standins}
        changed = sorted(key for key in default if default[key] != direct[key])
        self.assertEqual(changed, [("Deployment", "opa"), ("Deployment", "seaweedfs"),
                                   ("NetworkPolicy", "opa"), ("NetworkPolicy", "seaweedfs")])


class Cases(unittest.TestCase):
    def test_each_case_has_a_name_a_decision_and_polaris_input(self):
        cases = standins.load_cases(os.path.join(HERE, "opa", "cases.json"))
        self.assertTrue(any(c["allow"] for c in cases))
        self.assertTrue(any(not c["allow"] for c in cases))
        for c in cases:
            with self.subTest(case=c["name"]):
                self.assertEqual(set(c["input"]), {"actor", "action", "resource", "context"})
                self.assertIsInstance(c["allow"], bool)

    def test_the_stress_principal_is_allowed_in_its_catalog_and_refused_outside_it(self):
        # ADR-053: gate_stress may do what the stress scenario needs, in gate_stress_catalog only.
        cases = [c for c in standins.load_cases(os.path.join(HERE, "opa", "cases.json"))
                 if c["input"]["actor"]["principal"] == "gate_stress"]
        allowed = {c["input"]["action"] for c in cases if c["allow"]}
        self.assertEqual(allowed, set(standins.STRESS_ACTIONS))
        refused = [c for c in cases if not c["allow"]]
        self.assertTrue(any(c["input"]["action"] in standins.STRESS_ACTIONS for c in refused),
                        "an action it may do, refused in another catalog")
        self.assertTrue(any(c["input"]["action"] not in standins.STRESS_ACTIONS
                            for c in refused), "an action it may not do, in its own catalog")

    def test_the_policy_names_every_stress_action(self):
        with open(os.path.join(HERE, "opa", "policy.rego"), encoding="utf-8") as f:
            rego = f.read()
        block = rego[rego.index('input.actor.principal == "gate_stress"'):]
        block = block[:block.index("}\n}")]
        for action in standins.STRESS_ACTIONS:
            with self.subTest(action=action):
                self.assertIn(f'"{action}"', block)
        self.assertIn('in_catalog(target, "gate_stress_catalog")', block)

    def test_the_seed_principal_is_allowed_in_its_catalog_and_refused_outside_it(self):
        # ADR-051 §2: gate_seed writes the seed's tables and view, in gate_seed_catalog only.
        cases = [c for c in standins.load_cases(os.path.join(HERE, "opa", "cases.json"))
                 if c["input"]["actor"]["principal"] == seed.WRITER]
        allowed = {c["input"]["action"] for c in cases if c["allow"]}
        self.assertEqual(allowed, set(standins.SEED_ACTIONS))
        refused = [c for c in cases if not c["allow"]]
        self.assertTrue(any(c["input"]["action"] in standins.SEED_ACTIONS for c in refused),
                        "an action it may do, refused in another catalog")
        self.assertTrue(any(c["input"]["action"] not in standins.SEED_ACTIONS
                            for c in refused), "an action it may not do, in its own catalog")

    def test_the_policy_names_every_seed_action_in_the_seed_catalog(self):
        with open(os.path.join(HERE, "opa", "policy.rego"), encoding="utf-8") as f:
            rego = f.read()
        block = rego[rego.index(f'input.actor.principal == "{seed.WRITER}"'):]
        block = block[:block.index("}\n}")]
        for action in standins.SEED_ACTIONS:
            with self.subTest(action=action):
                self.assertIn(f'"{action}"', block)
        self.assertIn(f'in_catalog(target, "{seed.CATALOG}")', block)

    def test_the_ingestion_principal_may_read_in_any_catalog_and_nothing_else(self):
        # ADR-076 §4: the four actions Datahub's Iceberg source causes, in any catalog, with a
        # target; no write, no delegation, no view, no catalog list, no management call.
        cases = [c for c in standins.load_cases(os.path.join(HERE, "opa", "cases.json"))
                 if c["input"]["actor"]["principal"] == "datahub_ingest"]
        allowed = [c for c in cases if c["allow"]]
        self.assertEqual({c["input"]["action"] for c in allowed}, set(standins.INGEST_ACTIONS))
        catalogs = {p["name"] for c in allowed for t in c["input"]["resource"]["targets"]
                    for p in [t] + t["parents"] if p["type"] == "CATALOG"}
        self.assertGreater(len(catalogs), 1, "allowed in more than one catalog")
        refused = {c["input"]["action"] for c in cases if not c["allow"]}
        self.assertLessEqual({"CREATE_NAMESPACE", "LOAD_TABLE_WITH_WRITE_DELEGATION",
                              "LOAD_TABLE_WITH_READ_DELEGATION", "LIST_VIEWS", "LIST_CATALOGS",
                              "CREATE_PRINCIPAL", "LOAD_TABLE"}, refused)
        self.assertTrue(any(not c["input"]["resource"].get("targets") and not c["allow"]
                            for c in cases), "an action it may do, with no target, refused")

    def test_the_policy_names_every_ingestion_action_and_no_other(self):
        with open(os.path.join(HERE, "opa", "policy.rego"), encoding="utf-8") as f:
            rego = f.read()
        block = rego[rego.index('input.actor.principal == "datahub_ingest"'):]
        block = block[:block.index("}\n}")]
        named = set(re.findall(r'"([A-Z_]+)"', block))
        self.assertEqual(named, set(standins.INGEST_ACTIONS))
        self.assertIn("count(input.resource.targets) > 0", block)
        self.assertIn("in_a_catalog(target)", block)


if __name__ == "__main__":
    unittest.main()

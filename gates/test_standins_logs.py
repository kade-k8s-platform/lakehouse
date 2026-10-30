"""Tests for the log stand-ins of standins.py (ADR-081, ADR-031): OpenSearch and a main Fluent Bit.
Run: python3 -m unittest discover -s gates"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import gate  # noqa: E402
import standins  # noqa: E402
from test_standins import ENV, PINS, container, objects  # noqa: E402

LIMITS = {"seaweedfs": "1", "opa": "500m"}

NEW = [("Deployment", "logs-opensearch"), ("Service", "logs-opensearch"),
       ("Deployment", "main-fluent-bit-standin"), ("Service", "main-fluent-bit-standin"),
       ("ConfigMap", "main-fluent-bit-config"),
       ("NetworkPolicy", "logs-opensearch"), ("NetworkPolicy", "main-fluent-bit-standin")]


class Objects(unittest.TestCase):
    def setUp(self):
        self.found = objects()

    def test_the_new_objects_are_in_the_stand_ins_namespace(self):
        for key in NEW:
            with self.subTest(key=key):
                self.assertIn(key, self.found)
                self.assertEqual(self.found[key]["metadata"]["namespace"], "standins")

    def test_opensearch(self):
        c = container(self.found[("Deployment", "logs-opensearch")])
        self.assertEqual(c["image"], PINS["standin_images"]["opensearch"]["image"])
        env = {e["name"]: e["value"] for e in c["env"]}
        self.assertEqual(env["discovery.type"], "single-node")
        self.assertEqual(env["DISABLE_SECURITY_PLUGIN"], "true")
        self.assertEqual(env["DISABLE_INSTALL_DEMO_CONFIG"], "true")
        self.assertEqual(env["OPENSEARCH_JAVA_OPTS"], "-Xms512m -Xmx512m")
        self.assertEqual(c["ports"][0]["containerPort"], 9200)
        self.assertIn("/_cluster/health", c["readinessProbe"]["httpGet"]["path"])
        self.assertEqual(c["resources"]["limits"]["memory"], "1536Mi")
        pod = self.found[("Deployment", "logs-opensearch")]["spec"]["template"]["spec"]
        self.assertNotIn("securityContext", pod)
        self.assertNotIn("securityContext", c)

    def test_fluent_bit(self):
        deployment = self.found[("Deployment", "main-fluent-bit-standin")]
        c = container(deployment)
        self.assertEqual(c["image"], PINS["standin_images"]["fluent_bit"]["image"])
        (volume,) = deployment["spec"]["template"]["spec"]["volumes"]
        self.assertEqual(volume["configMap"]["name"], "main-fluent-bit-config")
        (mount,) = c["volumeMounts"]
        self.assertTrue(mount["readOnly"])
        self.assertEqual(c["readinessProbe"]["tcpSocket"]["port"], 24224)
        config = self.found[("ConfigMap", "main-fluent-bit-config")]["data"]["fluent-bit.conf"]
        self.assertEqual(config, standins.main_fluent_bit_config(ENV))

    def test_the_config(self):
        config = standins.main_fluent_bit_config(ENV)
        self.assertEqual(len(re.findall(r"^\[INPUT\]", config, re.M)), 1)
        self.assertRegex(config, r"(?m)^\s*Name\s+forward$")
        self.assertRegex(config, r"(?m)^\s*Rule\s+\$index_class\s+\^candidate\$\s+polaris\.candidate\s+false")
        self.assertRegex(config, r"(?m)^\s*Rule\s+\$index_class\s+\^error\$\s+polaris\.error\s+false")
        self.assertEqual(len(re.findall(r"(?m)^\s*Name\s+opensearch$", config)), 2)
        for kind in ("candidate", "error"):
            self.assertRegex(config, rf"(?m)^\s*Logstash_Prefix\s+polaris-logs-{kind}$")
            self.assertRegex(config, rf"(?m)^\s*Match\s+polaris\.{kind}$")
        self.assertEqual(config.count("Id_Key event_id"), 2)
        self.assertEqual(config.count("Host logs-opensearch"), 2)
        self.assertNotRegex(config, r"(?mi)^\s*storage")
        self.assertNotIn("Path", config)


class Policies(unittest.TestCase):
    def setUp(self):
        self.found = objects()

    def spec(self, name):
        return self.found[("NetworkPolicy", name)]["spec"]

    def test_opensearch_takes_the_forwarder_and_the_check_pods_on_9200(self):
        spec = self.spec("logs-opensearch")
        self.assertEqual(spec["policyTypes"], ["Ingress"])
        self.assertEqual(spec["ingress"], [{
            "ports": [{"protocol": "TCP", "port": 9200}],
            "from": [{"podSelector": {"matchLabels": {
                          "app.kubernetes.io/name": "main-fluent-bit-standin"}}},
                     {"namespaceSelector": {"matchLabels": {
                          "kubernetes.io/metadata.name": "catalog-polaris"}},
                      "podSelector": {"matchLabels": {"lakehouse-gate/check": "true"}}}]}])

    def test_fluent_bit_takes_the_check_pods_on_24224(self):
        spec = self.spec("main-fluent-bit-standin")
        self.assertEqual(spec["policyTypes"], ["Ingress"])
        self.assertEqual(spec["ingress"], [{
            "ports": [{"protocol": "TCP", "port": 24224}],
            "from": [{"namespaceSelector": {"matchLabels": {
                          "kubernetes.io/metadata.name": "catalog-polaris"}},
                      "podSelector": {"matchLabels": {"lakehouse-gate/check": "true"}}}]}])

    def test_the_names(self):
        self.assertEqual(sorted(k[1] for k in self.found if k[0] == "NetworkPolicy"),
                         ["default-deny-ingress", "logs-opensearch", "main-fluent-bit-standin",
                          "opa", "prometheus", "recorder", "seaweedfs"])


class Direct(unittest.TestCase):
    def test_a_stress_install_leaves_them_out(self):
        direct = objects(direct=True, cpu_limits=LIMITS)
        for key in NEW:
            with self.subTest(key=key):
                self.assertNotIn(key, direct)
        default = objects()
        self.assertEqual(sorted(set(default) - set(direct)), sorted(NEW))


class Commands(unittest.TestCase):
    def rollout(self, **kwargs):
        commands = gate.standins_commands(ENV, "{}", **kwargs)
        (argv,) = [c for c, _ in commands if "rollout" in c]
        return argv

    def test_the_rollout_waits_for_both(self):
        argv = self.rollout()
        self.assertIn("deployment/logs-opensearch", argv)
        self.assertIn("deployment/main-fluent-bit-standin", argv)

    def test_direct_waits_for_neither(self):
        argv = self.rollout(direct=True)
        self.assertNotIn("deployment/logs-opensearch", argv)
        self.assertNotIn("deployment/main-fluent-bit-standin", argv)


if __name__ == "__main__":
    unittest.main()

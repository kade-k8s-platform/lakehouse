"""Tests for network.py (ADR-048 §5, §6). Run: python3 -m unittest discover -s gates"""

import errno
import json
import os
import socket
import subprocess
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import gate  # noqa: E402
import metrics  # noqa: E402
import network  # noqa: E402

ENV = {"NS_CATALOG_POLARIS": "catalog-polaris", "NS_STANDINS": "standins",
       "NS_CATALOG_DATAHUB": "catalog-datahub",
       "POLARIS_GATEWAY_NAMESPACE_LABEL": "lakehouse-gate/caller=gateway",
       "POLARIS_QUERY_NAMESPACE_LABEL": "lakehouse-gate/caller=query",
       "METRICS_NAMESPACE_LABEL": "lakehouse-gate/caller=metrics",
       "INGRESS_NAMESPACE_LABEL": "lakehouse-gate/caller=ingress",
       "DATAHUB_EMITTER_NAMESPACE_LABEL": "lakehouse-gate/caller=emitter"}
PYTHON = "python:3.12.14-slim@sha256:" + "f" * 64


class GatePolicies(unittest.TestCase):
    """§5: two allow policies in Polaris's namespace, on the gate only."""

    def setUp(self):
        self.items = {p["metadata"]["name"]: p for p in network.gate_policies(ENV)["items"]}

    def test_both_in_polaris_namespace_and_named_as_the_gates(self):
        self.assertEqual(sorted(self.items), ["gate-checks", "gate-recorder-postgres"])
        for policy in self.items.values():
            self.assertEqual(policy["kind"], "NetworkPolicy")
            self.assertEqual(policy["metadata"]["namespace"], "catalog-polaris")
            self.assertEqual(policy["spec"]["policyTypes"], ["Ingress"])

    def test_check_pods_reach_every_port_of_every_pod(self):
        spec = self.items["gate-checks"]["spec"]
        self.assertEqual(spec["podSelector"], {})
        self.assertEqual(spec["ingress"], [{"from": [
            {"podSelector": {"matchLabels": {network.CHECK_LABEL: "true"}}}]}])

    def test_the_postgresql_recorder_reaches_the_instances_on_5432_only(self):
        spec = self.items["gate-recorder-postgres"]["spec"]
        self.assertEqual(spec["podSelector"], {"matchLabels": {"cnpg.io/podRole": "instance"}})
        self.assertEqual(spec["ingress"], [{
            "ports": [{"protocol": "TCP", "port": 5432}],
            # One element: the recorder's pods AND the stand-ins' namespace.
            "from": [{"namespaceSelector": {"matchLabels": {
                          "kubernetes.io/metadata.name": "standins"}},
                      "podSelector": {"matchLabels": {"app.kubernetes.io/name": "recorder"}}}]}])


class CheckPods(unittest.TestCase):
    """§5: every pod the gate starts in Polaris's namespace to check it carries the label."""

    def test_every_kubectl_run_of_the_gate_carries_the_check_label(self):
        env = {**ENV, "S3_ENDPOINT": "http://recorder:8333", "S3_REGION": "us-east-1",
               "POLARIS_WAREHOUSE_BUCKET": "w", "POLARIS_OPA_URL": "http://recorder:8181/x",
               "POLARIS_DB_HOST": "recorder", "POLARIS_CONSOLE_ENABLED": "true",
               "POLARIS_CONSOLE_ORIGIN": "http://c:8080",
               "POLARIS_CONSOLE_API_URL": "http://p:8181"}
        commands = {
            "check_db": gate.check_db_command(env, PYTHON, "polaris", "n"),
            "check_standins": gate.check_standins_command(env, [], PYTHON, "n"),
            "check_polaris": gate.check_polaris_command(env, PYTHON, "n", []),
            "check_iceberg": gate.check_iceberg_commands(env, PYTHON, "n")[1],
            "check_restore": gate.check_restore_command(env, PYTHON, "polaris", "n"),
            "check_console": gate.check_console_command(env, PYTHON, "n"),
            "logs_traffic": gate.logs_traffic_command(env, PYTHON, "n", "p", 1, ["pod-a"]),
            "logs_listing": gate.logs_listing_command(env, PYTHON, "n"),
            "check_metrics": metrics.query_command(env, PYTHON, "n", 5),
        }
        for name, argv in commands.items():
            with self.subTest(command=name):
                self.assertEqual(argv[5:7], ["run", "n"])
                self.assertIn(f"--labels={network.CHECK_LABEL}=true", argv)


class Cases(unittest.TestCase):
    """§6: one allowed and one refused connection per rule."""

    def test_every_rule_is_allowed_from_its_class_and_refused_from_elsewhere(self):
        expected = {(c.source, c.target): c.allowed for c in network.CASES}
        for source, target in (("gateway", "polaris-api"), ("query", "polaris-api"),
                               ("datahub", "polaris-api"), ("ingress", "polaris-api"),
                               ("metrics", "polaris-management"), ("ingress", "console"),
                               ("polaris", "recorder"), ("metrics", "polaris-pg-metrics")):
            with self.subTest(source=source, target=target):
                self.assertIs(expected[(source, target)], True)
        for target in ("polaris-api", "polaris-management", "console", "polaris-pg",
                       "polaris-pg-metrics", "recorder", "seaweedfs", "opa"):
            with self.subTest(source="none", target=target):
                self.assertIs(expected[("none", target)], False)

    def test_a_class_is_refused_a_port_it_is_not_allowed(self):
        expected = {(c.source, c.target): c.allowed for c in network.CASES}
        self.assertIs(expected[("gateway", "polaris-management")], False)
        self.assertIs(expected[("metrics", "polaris-api")], False)
        self.assertIs(expected[("query", "console")], False)
        self.assertIs(expected[("gateway", "polaris-pg-metrics")], False)

    def test_the_sidecars_path_is_allowed_from_polaris_pods_and_refused_from_other_pods(self):
        # ADR-081: one allowed and one refused connection to the main Fluent Bit's input.
        expected = {(c.source, c.target): c.allowed for c in network.CASES}
        self.assertIs(expected[("polaris-pod", "main-fluent-bit")], True)
        self.assertIs(expected[("polaris", "main-fluent-bit")], False)
        self.assertEqual({c.target: c.allowed for c in network.CASES
                          if c.source == "polaris-pod"}, {"main-fluent-bit": True})
        found = [c.allowed for c in network.CASES if c.target == "main-fluent-bit"]
        self.assertEqual(sorted(found), [False, True])

    def test_an_unlabelled_pod_in_polaris_namespace_reaches_only_the_recorder(self):
        expected = {c.target: c.allowed for c in network.CASES if c.source == "polaris"}
        self.assertEqual(expected, {"polaris-api": False, "polaris-pg": False,
                                    "seaweedfs": False, "opa": False, "recorder": True,
                                    "datahub-pg": False, "datahub-kafka": False,
                                    "datahub-gms": False, "main-fluent-bit": False})

    def test_datahub_pg_from_its_clients_and_join_pods_in_datahubs_namespace_only(self):
        # ADR-066 §4: datahub-pg's rules as polaris-pg's, for Datahub's own label.
        expected = {(c.source, c.target): c.allowed for c in network.CASES}
        self.assertIs(expected[("datahub-client", "datahub-pg")], True)
        self.assertIs(expected[("datahub-join", "datahub-pg")], True)
        for source in ("datahub", "none", "none-join", "polaris"):
            with self.subTest(source=source):
                self.assertIs(expected[(source, "datahub-pg")], False)

    def test_datahub_kafka_from_its_clients_in_datahubs_namespace_only(self):
        # ADR-068 §3: 9092 from pods with Kafka's own client label; datahub-pg's does not do.
        expected = {(c.source, c.target): c.allowed for c in network.CASES}
        self.assertIs(expected[("datahub-kafka-client", "datahub-kafka")], True)
        for source in ("datahub", "datahub-client", "none", "polaris"):
            with self.subTest(source=source):
                self.assertIs(expected[(source, "datahub-kafka")], False)
        # Kafka's label opens no other door.
        self.assertIs(expected[("datahub-kafka-client", "datahub-pg")], False)

    def test_datahub_opensearch_on_9200_from_its_clients_in_datahubs_namespace_only(self):
        # ADR-070 §5: 9200 from pods with OpenSearch's own client label; 9300 from none.
        expected = {(c.source, c.target): c.allowed for c in network.CASES}
        self.assertIs(expected[("datahub-opensearch-client", "datahub-opensearch")], True)
        for source in ("datahub", "none"):
            with self.subTest(source=source):
                self.assertIs(expected[(source, "datahub-opensearch")], False)
        self.assertIs(expected[("datahub-opensearch-client", "datahub-opensearch-transport")],
                      False)

    def test_gms_from_its_clients_and_the_emitters_namespace_only(self):
        # ADR-071 §5, §8: 8080 from pods with GMS's client label and the emitters' class.
        expected = {(c.source, c.target): c.allowed for c in network.CASES}
        self.assertIs(expected[("datahub-gms-client", "datahub-gms")], True)
        self.assertIs(expected[("emitter", "datahub-gms")], True)
        for source in ("datahub", "none", "polaris"):
            with self.subTest(source=source):
                self.assertIs(expected[(source, "datahub-gms")], False)

    def test_the_frontend_from_the_ingress_namespace_only(self):
        expected = {(c.source, c.target): c.allowed for c in network.CASES}
        self.assertIs(expected[("ingress", "datahub-frontend")], True)
        for source in ("none", "emitter", "datahub"):
            with self.subTest(source=source):
                self.assertIs(expected[(source, "datahub-frontend")], False)

    def test_the_emitters_class_opens_no_door_to_polaris(self):
        expected = {(c.source, c.target): c.allowed for c in network.CASES}
        self.assertIs(expected[("emitter", "polaris-api")], False)

    def test_datahubs_namespace_reaches_the_recorders_s3_port_only(self):
        # ADR-067 §4: its backups go through the recorder; OPA and the PostgreSQL port stay shut.
        expected = {(c.source, c.target): c.allowed for c in network.CASES}
        self.assertIs(expected[("datahub", "recorder")], True)
        self.assertIs(expected[("datahub", "recorder-pg")], False)
        self.assertIs(expected[("datahub", "opa")], False)

    def test_a_join_pod_reaches_polaris_pg_only_from_polaris_namespace(self):
        # ADR-048 §6: CNPG's join pods clone the primary; the label admits no other namespace.
        expected = {(c.source, c.target): c.allowed for c in network.CASES}
        self.assertIs(expected[("polaris-join", "polaris-pg")], True)
        self.assertIs(expected[("none-join", "polaris-pg")], False)

    def test_every_target_has_an_address(self):
        addresses = network.targets(ENV)
        self.assertEqual(addresses["polaris-api"], "polaris.catalog-polaris.svc.cluster.local:8181")
        self.assertEqual(addresses["polaris-management"],
                         "polaris-mgmt.catalog-polaris.svc.cluster.local:8182")
        self.assertEqual(addresses["console"],
                         "polaris-console.catalog-polaris.svc.cluster.local:8080")
        self.assertEqual(addresses["polaris-pg"],
                         "polaris-pg-rw.catalog-polaris.svc.cluster.local:5432")
        # ADR-046 §4: polaris-pg's Services expose 5432 only; the gate adds one for 9187.
        self.assertEqual(addresses["polaris-pg-metrics"],
                         "gate-polaris-pg-metrics.catalog-polaris.svc.cluster.local:9187")
        self.assertEqual(addresses["recorder"], "recorder.standins.svc.cluster.local:8333")
        self.assertEqual(addresses["recorder-pg"], "recorder.standins.svc.cluster.local:5432")
        self.assertEqual(addresses["seaweedfs"], "seaweedfs.standins.svc.cluster.local:8333")
        self.assertEqual(addresses["opa"], "opa.standins.svc.cluster.local:8181")
        self.assertEqual(addresses["main-fluent-bit"],
                         "main-fluent-bit-standin.standins.svc.cluster.local:24224")
        self.assertEqual(addresses["datahub-pg"],
                         "datahub-pg-rw.catalog-datahub.svc.cluster.local:5432")
        self.assertEqual(addresses["datahub-kafka"],
                         "datahub-kafka.catalog-datahub.svc.cluster.local:9092")
        self.assertEqual(addresses["datahub-opensearch"],
                         "datahub-opensearch.catalog-datahub.svc.cluster.local:9200")
        self.assertEqual(addresses["datahub-opensearch-transport"],
                         "datahub-opensearch.catalog-datahub.svc.cluster.local:9300")
        self.assertEqual(addresses["datahub-gms"],
                         "datahub-datahub-gms.catalog-datahub.svc.cluster.local:8080")
        self.assertEqual(addresses["datahub-frontend"],
                         "datahub-datahub-frontend.catalog-datahub.svc.cluster.local:9002")
        self.assertEqual({c.target for c in network.CASES}, set(addresses))


class Sources(unittest.TestCase):
    def test_a_namespace_per_caller_label_one_without_and_datahubs(self):
        namespaces = network.source_namespaces(ENV)
        self.assertEqual(namespaces, {
            "gateway": ("gate-net-gateway", {"lakehouse-gate/caller": "gateway"}),
            "query": ("gate-net-query", {"lakehouse-gate/caller": "query"}),
            "metrics": ("gate-net-metrics", {"lakehouse-gate/caller": "metrics"}),
            "ingress": ("gate-net-ingress", {"lakehouse-gate/caller": "ingress"}),
            "emitter": ("gate-net-emitter", {"lakehouse-gate/caller": "emitter"}),
            "none": ("gate-net-none", {}),
            "datahub": ("catalog-datahub", {}),
            "polaris": ("catalog-polaris", None),
            "polaris-join": ("catalog-polaris", None),
            "polaris-pod": ("catalog-polaris", None),
            "none-join": ("gate-net-none", None),
            "datahub-client": ("catalog-datahub", None),
            "datahub-join": ("catalog-datahub", None),
            "datahub-kafka-client": ("catalog-datahub", None),
            "datahub-opensearch-client": ("catalog-datahub", None),
            "datahub-gms-client": ("catalog-datahub", None)})

    def test_the_join_sources_carry_cnpgs_join_label_and_datahubs_client_its_own(self):
        self.assertEqual(network.POD_LABELS, {
            "polaris-join": {"cnpg.io/jobRole": "join"},
            "polaris-pod": {"app.kubernetes.io/name": "polaris",
                            "app.kubernetes.io/instance": "polaris"},
            "none-join": {"cnpg.io/jobRole": "join"},
            "datahub-join": {"cnpg.io/jobRole": "join"},
            "datahub-client": {"lakehouse/datahub-pg-client": "true"},
            "datahub-kafka-client": {"lakehouse/datahub-kafka-client": "true"},
            "datahub-opensearch-client": {"lakehouse/datahub-opensearch-client": "true"},
            "datahub-gms-client": {"lakehouse/datahub-gms-client": "true"}})

    def test_the_namespaces_manifest_leaves_polaris_alone(self):
        manifest = network.namespaces_manifest(ENV)
        names = [n["metadata"]["name"] for n in manifest["items"]]
        self.assertNotIn("catalog-polaris", names)
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("catalog-datahub", names)
        labelled = {n["metadata"]["name"]: n["metadata"].get("labels", {})
                    for n in manifest["items"]}
        self.assertEqual(labelled["gate-net-metrics"], {"lakehouse-gate/caller": "metrics"})
        self.assertEqual(labelled["gate-net-none"], {})

    def test_a_gate_service_for_the_instances_exporter(self):
        svc = network.metrics_service(ENV)
        self.assertEqual(svc["kind"], "Service")
        self.assertEqual(svc["metadata"]["name"], "gate-polaris-pg-metrics")
        self.assertEqual(svc["metadata"]["namespace"], "catalog-polaris")
        self.assertEqual(svc["spec"]["selector"],
                         {"cnpg.io/cluster": "polaris-pg", "cnpg.io/podRole": "instance"})
        self.assertEqual(svc["spec"]["ports"], [{"name": "metrics", "port": 9187,
                                                 "targetPort": 9187, "protocol": "TCP"}])

    def test_the_gates_env_labels_are_the_ones_the_namespaces_get(self):
        for var, value in network.GATE_CALLER_LABELS.items():
            with self.subTest(var=var):
                self.assertEqual(ENV[var], value)


class Probe(unittest.TestCase):
    def test_one_short_lived_unlabelled_pod_in_the_source_namespace(self):
        argv = network.probe_command("gate-net-none", PYTHON, "probe-1",
                                     ["polaris.catalog-polaris.svc.cluster.local:8181"])
        self.assertEqual(argv[:7], ["kubectl", "--context", gate.CONTEXT, "-n",
                                    "gate-net-none", "run", "probe-1"])
        for flag in (f"--image={PYTHON}", "--restart=Never", "--rm", "-i"):
            self.assertIn(flag, argv)
        self.assertFalse(any(a.startswith("--labels") for a in argv))
        self.assertEqual(argv[-1], "polaris.catalog-polaris.svc.cluster.local:8181")

    def test_a_join_probe_carries_cnpgs_join_label_never_the_check_label(self):
        # ADR-048 §6: §5's gate-checks policy would admit a pod with the check label.
        argv = network.probe_command("catalog-polaris", PYTHON, "probe-1", ["a:5432"],
                                     labels={"cnpg.io/jobRole": "join"})
        self.assertEqual([a for a in argv if a.startswith("--labels")],
                         ["--labels=cnpg.io/jobRole=join"])

    def test_a_probe_with_polaris_labels_is_never_ready_so_no_service_selects_it(self):
        # Polaris's Services select by these labels; a Ready probe pod would be an endpoint that
        # resets the connections sent to it (smoke run 184: ingress -> polaris-api refused).
        argv = network.probe_command("catalog-polaris", PYTHON, "probe-1", ["a:24224"],
                                     labels=network.POD_LABELS["polaris-pod"], ready=False)
        overrides = json.loads(argv[argv.index("--overrides") + 1])
        self.assertIn("--override-type=strategic", argv)
        (c,) = overrides["spec"]["containers"]
        self.assertEqual((c["name"], c["image"]), ("probe-1", PYTHON))
        self.assertEqual(c["readinessProbe"]["exec"]["command"], ["false"])
        plain = network.probe_command("catalog-polaris", PYTHON, "probe-1", ["a:5432"])
        self.assertNotIn("--overrides", plain)

    def test_check_network_probes_only_the_polaris_pod_source_unready(self):
        commands = []

        def subprocess_run(argv, **kwargs):
            commands.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch.object(gate.subprocess, "run", side_effect=subprocess_run):
            gate.check_network(ENV, {"standin_images": {"recorder": {"image": PYTHON}}})
        runs = {argv[argv.index("run") + 1].split("-" + str(os.getpid()))[0]: argv
                for argv in commands if "run" in argv}
        self.assertIn("--overrides", runs["check-network-polaris-pod"])
        for name, argv in runs.items():
            if name != "check-network-polaris-pod":
                self.assertNotIn("--overrides", argv, name)

    def test_check_network_probes_each_join_source_with_its_label(self):
        commands = []

        def subprocess_run(argv, **kwargs):
            commands.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch.object(gate.subprocess, "run", side_effect=subprocess_run):
            gate.check_network(ENV, {"standin_images": {"recorder": {"image": PYTHON}}})
        by_name = {argv[argv.index("run") + 1].split("-" + str(os.getpid()))[0]: argv
                   for argv in commands if "run" in argv}
        self.assertEqual([a for a in by_name["check-network-polaris-join"]
                          if a.startswith("--labels")], ["--labels=cnpg.io/jobRole=join"])
        self.assertEqual([a for a in by_name["check-network-none-join"]
                          if a.startswith("--labels")], ["--labels=cnpg.io/jobRole=join"])
        self.assertEqual(by_name["check-network-none-join"][4], "gate-net-none")
        pod = by_name["check-network-polaris-pod"]
        self.assertEqual([a for a in pod if a.startswith("--labels")],
                         ["--labels=app.kubernetes.io/name=polaris,"
                          "app.kubernetes.io/instance=polaris"])
        self.assertEqual(pod[4], "catalog-polaris")
        self.assertFalse(any("lakehouse-gate/check" in a for a in pod))
        for name in ("check-network-polaris", "check-network-none"):
            self.assertFalse(any(a.startswith("--labels") for a in by_name[name]), name)

    def test_check_network_runs_every_probe_pod_at_once(self):
        # Each probe waits until all of them have started: run one after another, the first
        # one's wait breaks and its source has no results.
        started = threading.Barrier(len(network.source_namespaces(ENV)), timeout=5)

        def subprocess_run(argv, **kwargs):
            if "run" not in argv:
                return subprocess.CompletedProcess(argv, 0, "", "")
            try:
                started.wait()
            except threading.BrokenBarrierError:
                return subprocess.CompletedProcess(argv, 1, "", "")
            out = "".join(f"{a} blocked\n" for a in argv[argv.index("python3") + 3:])
            return subprocess.CompletedProcess(argv, 0, out, "")

        with mock.patch.object(gate.subprocess, "run", side_effect=subprocess_run):
            problems, lines = gate.check_network(
                ENV, {"standin_images": {"recorder": {"image": PYTHON}}})
        self.assertEqual([p for p in problems if "no result" in p], [])
        self.assertEqual(len(lines) + len(problems), len(network.CASES))

    def test_the_script_reports_reached_and_refused_from_real_sockets(self):
        # A listening port and a closed one, on this machine.
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        closed_port = closed.getsockname()[1]
        closed.close()
        targets = [f"127.0.0.1:{listener.getsockname()[1]}", f"127.0.0.1:{closed_port}"]
        result = subprocess.run([sys.executable, "-", "1", *targets], input=network.PROBE,
                                capture_output=True, text=True, check=True, timeout=30)
        listener.close()
        self.assertEqual(network.parse_probe(result.stdout),
                         {targets[0]: "reached", targets[1]: "refused"})

    def test_a_timeout_or_an_unreachable_host_is_blocked_and_a_bad_name_an_error(self):
        # What a dropping policy gives depends on the network, so it is simulated here.
        probe = {"__name__": "probe"}
        exec(network.PROBE, probe)  # noqa: S102 (this repository's own script)
        for raised, expected in ((TimeoutError(), "blocked"),
                                 (OSError(errno.EHOSTUNREACH, "unreachable"), "blocked"),
                                 (socket.gaierror(-2, "unknown name"), "error"),
                                 (OSError(errno.EINVAL, "invalid"), "error")):
            with self.subTest(raised=raised), \
                    mock.patch.object(socket, "create_connection", side_effect=raised):
                self.assertEqual(probe["probe"]("polaris.example:8181", 1), expected)


class Outcome(unittest.TestCase):
    def case(self, allowed):
        return network.Case("gateway", "polaris-api", allowed)

    def test_an_allowed_connection_must_reach(self):
        self.assertTrue(network.holds(self.case(True), "reached"))
        for result in ("blocked", "refused", "error", None):
            self.assertFalse(network.holds(self.case(True), result))

    def test_a_refused_connection_must_be_blocked_or_reset(self):
        self.assertTrue(network.holds(self.case(False), "blocked"))
        self.assertTrue(network.holds(self.case(False), "refused"))
        self.assertFalse(network.holds(self.case(False), "reached"))
        self.assertFalse(network.holds(self.case(False), "error"))

    def test_a_refused_connection_that_reached_says_the_cni_does_not_enforce(self):
        problems, lines = network.report([self.case(False)], {"gateway": {
            "polaris-api": "reached"}})
        self.assertEqual(len(problems), 2)
        self.assertIn("does not enforce NetworkPolicy", problems[-1])
        self.assertIn("ADR-048", problems[-1])

    def test_a_refused_fluent_bit_connection_that_reached_says_the_cni_does_not_enforce(self):
        problems, _ = network.report([network.Case("polaris", "main-fluent-bit", False)],
                                     {"polaris": {"main-fluent-bit": "reached"}})
        self.assertIn("FAIL polaris -> main-fluent-bit: reached (refused)", problems[0])
        self.assertIn("does not enforce NetworkPolicy", problems[-1])

    def test_every_case_is_named_with_its_result(self):
        problems, lines = network.report(
            [self.case(True), network.Case("none", "polaris-api", False)],
            {"gateway": {"polaris-api": "reached"}, "none": {"polaris-api": "blocked"}})
        self.assertEqual(problems, [])
        self.assertEqual(lines, ["ok   gateway -> polaris-api: reached (allowed)",
                                 "ok   none -> polaris-api: blocked (refused)"])

    def test_a_missing_result_is_a_problem(self):
        problems, _ = network.report([self.case(True)], {"gateway": {}})
        self.assertEqual(len(problems), 1)
        self.assertIn("gateway -> polaris-api", problems[0])


if __name__ == "__main__":
    unittest.main()

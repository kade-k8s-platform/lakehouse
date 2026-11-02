"""Tests for gate.py. Run: python3 -m unittest discover -s gates"""

import contextlib
import copy
import hashlib
import io
import json
import re
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import gate  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def tarball(member, binary):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        info = tarfile.TarInfo(member)
        info.size = len(binary)
        tar.addfile(info, io.BytesIO(binary))
    return out.getvalue()


HELM_TGZ = tarball("linux-amd64/helm", b"helm-binary")
KUBECONFORM_TGZ = tarball("kubeconform", b"kubeconform-binary")
BLOBS = {"https://k/kind": b"kind-binary", "https://k/kubectl": b"kubectl-binary",
         "https://k/helm.tgz": HELM_TGZ, "https://k/kubeconform.tgz": KUBECONFORM_TGZ}
TOOL_PINS = {"kind": {"url": "https://k/kind", "sha256": sha(b"kind-binary")},
             "kubectl": {"url": "https://k/kubectl", "sha256": sha(b"kubectl-binary")},
             "helm": {"url": "https://k/helm.tgz", "sha256": sha(HELM_TGZ)},
             "kubeconform": {"url": "https://k/kubeconform.tgz", "sha256": sha(KUBECONFORM_TGZ)}}


class Pins(unittest.TestCase):
    def test_the_committed_pins_are_complete(self):
        pins = gate.load_pins(os.path.join(HERE, "pins.json"))
        for tool in ("kind", "kubectl", "helm", "kubeconform", "cnpg_chart"):
            with self.subTest(tool=tool):
                self.assertRegex(pins[tool]["sha256"], r"^[0-9a-f]{64}$")
                self.assertTrue(pins[tool]["url"].startswith("https://"))
        self.assertRegex(pins["node_image"]["image"],
                         r"^kindest/node:v1\.\d+\.\d+@sha256:[0-9a-f]{64}$")

    def test_cert_manager_and_the_plugin_are_pinned_release_manifests(self):
        # ADR-043 §3: the gate installs both, each pinned with its digest.
        manifests = gate.load_pins(os.path.join(HERE, "pins.json"))["manifests"]
        self.assertEqual(sorted(manifests), ["barman_cloud_plugin", "cert_manager"])
        for name, pin in manifests.items():
            with self.subTest(manifest=name):
                self.assertRegex(pin["sha256"], r"^[0-9a-f]{64}$")
                self.assertRegex(pin["version"], r"^v\d+\.\d+\.\d+$")
                self.assertTrue(pin["url"].startswith("https://github.com/"))
                self.assertIn(f"/download/{pin['version']}/", pin["url"])
                self.assertTrue(pin["url"].endswith(".yaml"))
                self.assertTrue(pin["source"])

    def test_the_network_policy_schema_is_the_gates_kubernetes_version_at_a_commit(self):
        # ADR-048 §4: kubeconform validates the policies against it in the static gate.
        pins = gate.load_pins(os.path.join(HERE, "pins.json"))
        pin = pins["kubernetes_schemas"]["networkpolicy"]
        version = pins["node_image"]["image"].split(":")[1].split("@")[0]
        self.assertRegex(pin["sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(pin["url"], r"^https://raw\.githubusercontent\.com/yannh/"
                         r"kubernetes-json-schema/[0-9a-f]{40}/")
        self.assertTrue(pin["url"].endswith(
            f"/{version}-standalone-strict/networkpolicy-networking-v1.json"))
        self.assertTrue(pin["source"])

    def test_the_workload_schemas_are_pinned_beside_the_network_policys(self):
        # ADR-068 §1, ADR-070 §1, ADR-071 §1, ADR-016: kubeconform validates
        # charts/datahub-kafka's, charts/datahub-opensearch's and charts/datahub's kinds in the
        # static gate, from the same commit and Kubernetes version.
        schemas = gate.load_pins(os.path.join(HERE, "pins.json"))["kubernetes_schemas"]
        commit_dir = schemas["networkpolicy"]["url"].rsplit("/", 1)[0]
        for name, file in (("statefulset", "statefulset-apps-v1.json"),
                           ("service", "service-v1.json"),
                           ("configmap", "configmap-v1.json"),
                           ("deployment", "deployment-apps-v1.json"),
                           ("serviceaccount", "serviceaccount-v1.json"),
                           ("role", "role-rbac-v1.json"),
                           ("rolebinding", "rolebinding-rbac-v1.json"),
                           ("job", "job-batch-v1.json"),
                           ("cronjob", "cronjob-batch-v1.json")):
            with self.subTest(schema=name):
                pin = schemas[name]
                self.assertEqual(pin["url"], f"{commit_dir}/{file}")
                self.assertRegex(pin["sha256"], r"^[0-9a-f]{64}$")
                self.assertTrue(pin["source"])

    def test_datahubs_chart_is_upstreams_release_archive_by_its_digest(self):
        # ADR-072 §1: the chart test compares charts/datahub with this archive, file by file.
        pin = gate.load_pins(os.path.join(HERE, "pins.json"))["datahub_chart"]
        self.assertRegex(pin["version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(pin["url"], "https://github.com/acryldata/datahub-helm/releases/download/"
                         f"datahub-{pin['version']}/datahub-{pin['version']}.tgz")
        self.assertRegex(pin["sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(pin["source"])

    def test_the_operator_pin_is_supported_by_the_install_script(self):
        pins = gate.load_pins(os.path.join(HERE, "pins.json"))
        with open(os.path.join(HERE, "..", "install", "supported.json"), encoding="utf-8") as f:
            supported = json.load(f)["cnpg_operator"]
        minor = ".".join(pins["cnpg_chart"]["operator_version"].split(".")[:2])
        self.assertIn(minor, supported)


class Fetch(unittest.TestCase):
    def test_a_matching_hash_returns_the_bytes(self):
        data = b"payload"
        self.assertEqual(gate.fetch("https://x.invalid/a", sha(data), get=lambda u: data), data)

    def test_a_wrong_hash_raises_and_names_the_url(self):
        with self.assertRaises(ValueError) as caught:
            gate.fetch("https://x.invalid/a", "0" * 64, get=lambda u: b"payload")
        self.assertIn("https://x.invalid/a", str(caught.exception))


class Tools(unittest.TestCase):
    def test_tools_are_written_executable_after_their_hashes_match(self):
        with tempfile.TemporaryDirectory() as d:
            gate.tools(TOOL_PINS, d, get=BLOBS.__getitem__)
            for name, content in (("kind", b"kind-binary"), ("kubectl", b"kubectl-binary"),
                                  ("helm", b"helm-binary"),
                                  ("kubeconform", b"kubeconform-binary")):
                with self.subTest(tool=name):
                    path = os.path.join(d, name)
                    with open(path, "rb") as f:
                        self.assertEqual(f.read(), content)
                    self.assertTrue(os.access(path, os.X_OK))

    def test_only_the_named_tools_are_fetched(self):
        fetched = []

        def get(url):
            fetched.append(url)
            return BLOBS[url]
        with tempfile.TemporaryDirectory() as d:
            gate.tools(TOOL_PINS, d, ["helm", "kubeconform"], get=get)
            self.assertEqual(sorted(os.listdir(d)), ["helm", "kubeconform"])
        self.assertEqual(fetched, ["https://k/helm.tgz", "https://k/kubeconform.tgz"])

    def test_an_unknown_tool_is_refused(self):
        with self.assertRaises(KeyError):
            gate.tools(TOOL_PINS, "/nonexistent", ["kubeconfrom"], get=BLOBS.__getitem__)

    def test_a_bad_hash_writes_nothing(self):
        pins = {name: {"url": pin["url"], "sha256": "0" * 64} for name, pin in TOOL_PINS.items()}
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                gate.tools(pins, d, get=lambda u: b"x")
            self.assertEqual(os.listdir(d), [])


class Commands(unittest.TestCase):
    PINS = {"node_image": {"image": "kindest/node:v1.36.4@sha256:" + "a" * 64}}
    MANIFESTS = {"cert_manager": "cert-manager manifest",
                 "barman_cloud_plugin": "plugin manifest"}

    def commands(self):
        return gate.up_commands(self.PINS, "/tmp/chart.tgz", "cnpg-system", self.MANIFESTS)

    def helm_index(self, steps):
        return next(i for i, (argv, _) in enumerate(steps) if argv[0] == "helm")

    def test_up_creates_the_three_node_cluster_first(self):
        (create, stdin) = self.commands()[0]
        self.assertIsNone(stdin)
        self.assertEqual(create[:4], ["kind", "create", "cluster", "--name"])
        self.assertEqual(create[create.index("--name") + 1], gate.CLUSTER)
        self.assertEqual(create[create.index("--image") + 1], self.PINS["node_image"]["image"])
        self.assertTrue(create[create.index("--config") + 1].endswith("gates/kind.yaml"))

    def test_up_takes_another_kind_config_for_the_failover_measurement(self):
        steps = gate.up_commands(self.PINS, "/tmp/chart.tgz", "cnpg-system", self.MANIFESTS,
                                 kind_config="/tmp/failover-kind.yaml")
        create = steps[0][0]
        self.assertEqual(create[create.index("--config") + 1], "/tmp/failover-kind.yaml")
        self.assertEqual(steps[1:], self.commands()[1:])

    def test_up_installs_cnpg_from_the_chart_archive_and_waits(self):
        steps = self.commands()
        helm, stdin = steps[self.helm_index(steps)]
        self.assertIsNone(stdin)
        self.assertEqual(helm[:3], ["helm", "--kube-context", gate.CONTEXT])
        self.assertIn("/tmp/chart.tgz", helm)
        self.assertEqual(helm[helm.index("--namespace") + 1], "cnpg-system")
        self.assertIn("--wait", helm)

    def test_after_cnpg_cert_manager_then_the_plugin_are_applied_and_waited_for(self):
        # ADR-043 §3: cert-manager in its own namespace, then the plugin's manifest, which
        # needs cert-manager's webhook for its Issuer and Certificates, in the operator's.
        steps = self.commands()
        after = steps[self.helm_index(steps) + 1:]
        self.assertEqual(len(after), 4)
        (cm_apply, cm_text), (cm_wait, w1), (pl_apply, pl_text), (pl_wait, w2) = after
        for apply in (cm_apply, pl_apply):
            self.assertEqual(apply, ["kubectl", "--context", gate.CONTEXT, "apply",
                                     "--server-side", "--field-manager=gate", "-f", "-"])
        self.assertEqual((cm_text, pl_text), ("cert-manager manifest", "plugin manifest"))
        self.assertEqual((w1, w2), (None, None))
        self.assertEqual(cm_wait, ["kubectl", "--context", gate.CONTEXT, "-n",
                                   gate.CERT_MANAGER_NAMESPACE, "rollout", "status",
                                   "deployment/cert-manager",
                                   "deployment/cert-manager-cainjector",
                                   "deployment/cert-manager-webhook", "--timeout=5m"])
        self.assertEqual(pl_wait, ["kubectl", "--context", gate.CONTEXT, "-n", "cnpg-system",
                                   "rollout", "status", f"deployment/{gate.PLUGIN_DEPLOYMENT}",
                                   "--timeout=5m"])

    def test_up_lets_the_control_plane_take_pods_before_cnpg(self):
        taint, _ = self.commands()[1]
        self.assertEqual(taint, ["kubectl", "--context", gate.CONTEXT, "taint", "nodes",
                                 f"{gate.CLUSTER}-control-plane",
                                 "node-role.kubernetes.io/control-plane:NoSchedule-"])

    def test_up_gives_kinds_provisioner_a_shared_class_and_keeps_standard(self):
        # ADR-036: ReadWriteMany on kind, from local-path-provisioner's sharedFileSystemPath.
        (patch, _), = [c for c in self.commands() if "patch" in c[0]]
        self.assertEqual(patch[patch.index("-n") + 1], "local-path-storage")
        self.assertEqual(patch[patch.index("patch"):patch.index("patch") + 3],
                         ["patch", "configmap", "local-path-config"])
        data = json.loads(patch[patch.index("-p") + 1])["data"]
        self.assertEqual(list(data), ["config.json"])
        config = json.loads(data["config.json"])
        self.assertEqual(config["nodePathMap"], [])
        self.assertEqual(config["storageClassConfigs"], {
            "standard": {"nodePathMap": [{"node": "DEFAULT_PATH_FOR_NON_LISTED_NODES",
                                          "paths": ["/var/local-path-provisioner"]}]},
            gate.SHARED_CLASS: {"sharedFileSystemPath": gate.SHARED_NODE_DIR}})

    def test_the_shared_class_is_applied_and_the_provisioner_restarted_before_cnpg(self):
        steps = self.commands()
        helm = self.helm_index(steps)
        (apply, stdin), = [c for c in steps[:helm] if "apply" in c[0]]
        storage_class = json.loads(stdin)
        self.assertEqual((storage_class["kind"], storage_class["metadata"]["name"]),
                         ("StorageClass", gate.SHARED_CLASS))
        self.assertEqual(storage_class["provisioner"], "rancher.io/local-path")
        self.assertEqual(storage_class["reclaimPolicy"], "Delete")
        names = [" ".join(argv) for argv, _ in steps]
        restart = next(i for i, n in enumerate(names) if "rollout restart" in n)
        status = next(i for i, n in enumerate(names) if "rollout status" in n)
        self.assertIn("deployment/local-path-provisioner", names[restart])
        self.assertLess(restart, status)
        self.assertLess(status, helm)
        for argv, _ in steps[1:]:
            if argv[0] == "kubectl":
                self.assertEqual(argv[1:3], ["--context", gate.CONTEXT])

    def test_the_plugin_manifest_must_name_only_the_operators_namespace(self):
        # The release manifest names its namespace itself; applied elsewhere, the plugin would
        # not run next to the operator (ADR-043 §3).
        text = "kind: Deployment\nmetadata:\n  name: barman-cloud\n  namespace: cnpg-system\n"
        self.assertEqual(gate.plugin_manifest(text, "cnpg-system"), text)
        with self.assertRaisesRegex(ValueError, "cnpg-system.*operators"):
            gate.plugin_manifest(text, "operators")
        with self.assertRaisesRegex(ValueError, "no namespace"):
            gate.plugin_manifest("kind: Deployment\n", "cnpg-system")

    def test_the_kind_config_has_one_control_plane_and_two_workers(self):
        with open(os.path.join(HERE, "kind.yaml"), encoding="utf-8") as f:
            roles = [line.split(":", 1)[1].strip() for line in f if "role:" in line]
        self.assertEqual(roles, ["control-plane", "worker", "worker"])

    def test_every_node_mounts_the_same_host_directory_for_the_shared_class(self):
        with open(os.path.join(HERE, "kind.yaml"), encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text.count(f"hostPath: {gate.SHARED_HOST_DIR}"), 3)
        self.assertEqual(text.count(f"containerPath: {gate.SHARED_NODE_DIR}"), 3)


class Install(unittest.TestCase):
    ROOT = os.path.dirname(HERE)
    THIS = ["python3", os.path.join(os.path.dirname(HERE), "install", "release.py")]
    # ADR-048 §5: the gate-only allow policies, before the first release that installs for
    # real: the last release has network policies of its own since platform-2026.10.14.
    GATE_POLICIES = ["python3", os.path.join(HERE, "gate.py"), "gate-policies"]
    # ADR-051 §2: the seed, between the last release's install and this commit's.
    SEED = ["python3", os.path.join(HERE, "gate.py"), "seed"]

    def test_with_a_release_installs_it_from_its_tag_seeds_it_then_this_commit(self):
        commands, summary = gate.install_plan("platform-2026.10.10", "/tmp/prev", fresh=False)
        self.assertEqual(commands, [
            self.GATE_POLICIES,
            ["git", "-C", self.ROOT, "worktree", "add", "--detach", "/tmp/prev",
             "platform-2026.10.10"],
            ["cp", "-p", os.path.join(self.ROOT, ".env"), "/tmp/prev/.env"],
            ["python3", "/tmp/prev/install/release.py"],
            self.SEED,
            self.THIS,
        ])
        self.assertIn("platform-2026.10.10", summary)
        self.assertIn("ADR-051", summary)

    def test_the_release_reads_the_gates_env_in_its_worktree(self):
        # release.py reads .env from its own checkout; a tag's worktree has none, so a
        # release that reads more than step 0 would find every endpoint empty.
        commands, _ = gate.install_plan("platform-2026.10.12", "/tmp/prev", fresh=False)
        worktree, copy, previous = commands[1], commands[2], commands[3]
        self.assertEqual(worktree[3:5], ["worktree", "add"])
        self.assertEqual(copy, ["cp", "-p", os.path.join(self.ROOT, ".env"), "/tmp/prev/.env"])
        self.assertEqual(previous, ["python3", "/tmp/prev/install/release.py"])

    def test_without_a_release_installs_this_commit_and_says_so(self):
        commands, summary = gate.install_plan(None, "/tmp/prev", fresh=False)
        self.assertEqual(commands, [self.GATE_POLICIES, self.THIS])
        self.assertEqual(summary, "No release to upgrade from: fresh install (ADR-051).")

    def test_fresh_ignores_the_release(self):
        commands, summary = gate.install_plan("platform-2026.10.10", "/tmp/prev", fresh=True)
        self.assertEqual(commands, [self.GATE_POLICIES, self.THIS])
        self.assertIn("Fresh install", summary)

    def test_only_an_upgrade_that_installs_for_real_seeds(self):
        # ADR-051 §4: the dry-run seeds nothing; a fresh install has nothing to upgrade.
        for previous, fresh, dry_run in ((None, False, False), ("platform-2026.10.10", True,
                                                                False),
                                         ("platform-2026.10.10", False, True),
                                         (None, False, True)):
            with self.subTest(previous=previous, fresh=fresh, dry_run=dry_run):
                commands, _ = gate.install_plan(previous, "/tmp/prev", fresh, dry_run)
                self.assertNotIn(self.SEED, commands)

    def test_dry_run_installs_the_release_then_dry_runs_this_commit_over_it(self):
        commands, summary = gate.install_plan("platform-2026.10.10", "/tmp/prev", fresh=False,
                                              dry_run=True)
        # The last release installs for real, so the gate's policies come first; dry-run 38493145873
        # failed without them: the recorder could not reach polaris-pg for the realm bootstrap.
        self.assertEqual(commands[0], self.GATE_POLICIES)
        self.assertEqual(commands[3], ["python3", "/tmp/prev/install/release.py"])
        # The dry-run itself changes nothing.
        self.assertEqual(commands[4:], [self.THIS + ["--dry-run"]])
        self.assertIn("server-side dry-run", summary)

    def test_dry_run_without_a_release_dry_runs_this_commit(self):
        commands, summary = gate.install_plan(None, "/tmp/prev", fresh=False, dry_run=True)
        self.assertEqual(commands, [self.THIS + ["--dry-run"]])
        self.assertIn("No release to upgrade from", summary)

    def test_an_unknown_install_option_exits_2(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(gate.main(["gate.py", "install", "--upgrade"]), 2)


class DatahubOnly(unittest.TestCase):
    """ADR-077 §1: `install --datahub-only` runs release.py's own functions, step 0 then
    Datahub's part of step 1, in ADR-045's order; the first step with a problem stops the
    rest."""

    DATAHUB = ("check_preconditions", "datahub_network_policies", "datahub_postgres",
               "datahub_kafka", "datahub_opensearch", "datahub")
    # Everything else release.py installs: none of it runs.
    OTHERS = ("network_policies", "polaris_postgres", "endpoints_answer", "bootstrap",
              "polaris", "console", "install")
    ENV = {"KUBE_CONTEXT": "kind-lakehouse-gate", "NS_CATALOG_DATAHUB": "catalog-datahub"}
    SUPPORTED = ["1.27"]

    def steps(self, broken=None):
        """The patches over release.py's steps, and the list of the steps called."""
        called = []

        def step(name):
            def call(env, run, *rest):
                self.assertEqual(env, self.ENV)
                self.assertIs(run, gate.release.call)
                self.assertEqual(rest, (self.SUPPORTED,) if name == "check_preconditions"
                                 else ())
                called.append(name)
                return ([f"{name} failed"] if name == broken else []), [f"{name} done"]
            return call
        stack = contextlib.ExitStack()
        for name in self.DATAHUB + self.OTHERS:
            stack.enter_context(mock.patch.object(gate.release, name, side_effect=step(name)))
        return stack, called

    def test_step_0_then_datahubs_part_of_step_1_in_order(self):
        stack, called = self.steps()
        with stack:
            problems, notes, timed = gate.datahub_only(self.ENV, self.SUPPORTED)
        self.assertEqual(problems, [])
        self.assertEqual(called, list(self.DATAHUB))
        self.assertEqual(notes, [f"{name} done" for name in self.DATAHUB])
        # Each step timed under its own name, for the install's timeline.
        self.assertEqual([label for _, _, label in timed],
                         [f"release.py {name}" for name in self.DATAHUB])
        for start, end, _ in timed:
            self.assertLessEqual(gate._moment(start), gate._moment(end))

    def test_the_first_step_with_a_problem_stops_the_rest(self):
        for broken in self.DATAHUB:
            with self.subTest(broken=broken):
                stack, called = self.steps(broken)
                with stack:
                    problems, notes, timed = gate.datahub_only(self.ENV, self.SUPPORTED)
                upto = list(self.DATAHUB[:self.DATAHUB.index(broken) + 1])
                self.assertEqual(problems, [f"{broken} failed"])
                self.assertEqual(called, upto)
                self.assertEqual(notes, [f"{name} done" for name in upto])
                self.assertEqual(len(timed), len(upto))

    def main(self, broken=None, argv=("install", "--datahub-only")):
        stack, called = self.steps(broken)
        with stack, mock.patch.object(gate.release, "load_env", return_value=self.ENV), \
                mock.patch.object(gate.release, "supported_versions",
                                  return_value=self.SUPPORTED), \
                mock.patch.object(gate, "run") as run, \
                mock.patch.object(gate, "released_tags") as tags, \
                mock.patch.object(gate, "measure_pulls",
                                  return_value=["Image pulls: p"]) as pulls, \
                mock.patch.object(gate, "measure_timeline",
                                  return_value=["Install timeline: t"]) as timeline, \
                mock.patch.object(gate, "INSTALL_RECORD",
                                  os.path.join(tempfile.mkdtemp(), "install.json")) as record, \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
            code = gate.main(["gate.py", *argv])
        return code, out.getvalue(), called, run, tags, pulls, timeline, record

    def test_the_command_prints_its_notes_then_the_pulls_and_the_timeline(self):
        code, out, called, run, tags, pulls, timeline, record = self.main()
        self.assertEqual(code, 0)
        self.assertEqual(called, list(self.DATAHUB))
        # Fresh, from this commit: no last release, seed, gate-policies or other command.
        run.assert_not_called()
        tags.assert_not_called()
        self.assertFalse(os.path.exists(record))
        self.assertIn("ADR-077", out.splitlines()[0])
        self.assertLess(out.index("datahub done"), out.index("Image pulls: p"))
        self.assertLess(out.index("Image pulls: p"), out.index("Install timeline: t"))
        since, seconds = pulls.call_args.args
        self.assertGreaterEqual(seconds, 0)
        self.assertEqual(timeline.call_args.args[0], since)
        self.assertEqual([label for _, _, label in timeline.call_args.args[1]],
                         [f"release.py {name}" for name in self.DATAHUB])

    def test_a_problem_fails_the_command_after_its_pulls_and_timeline(self):
        code, out, called, *_ = self.main(broken="datahub_kafka")
        self.assertEqual(code, 1)
        self.assertEqual(called[-1], "datahub_kafka")
        self.assertLess(out.index("datahub_kafka failed"), out.index("Install timeline: t"))

    def test_it_takes_no_other_option(self):
        for argv in (("install", "--datahub-only", "--fresh"),
                     ("install", "--datahub-only", "--dry-run"),
                     ("install", "--fresh", "--datahub-only")):
            with self.subTest(argv=argv):
                code, _, called, *_ = self.main(argv=argv)
                self.assertEqual((code, called), (2, []))

    def test_it_is_a_command_of_the_gate(self):
        self.assertIn("| install [--fresh] [--dry-run] | install --datahub-only |",
                      gate.__doc__.strip().splitlines()[-1])


def pulled(message, node="lakehouse-gate-worker", at="2026-10-14T01:10:00Z", reason="Pulled"):
    return {"reason": reason, "message": message, "source": {"host": node},
            "firstTimestamp": at, "lastTimestamp": at}


class ImagePulls(unittest.TestCase):
    """#125: how long the install spent pulling images, from the kubelet's Pulled events."""
    SINCE = "2026-10-14T01:05:00+00:00"

    def test_go_durations_in_seconds(self):
        for text, seconds in (("3.162s", 3.162), ("1m2.5s", 62.5), ("512ms", 0.512),
                              ("1h0m1s", 3601.0), ("850µs", 0.00085), ("0s", 0.0)):
            self.assertAlmostEqual(gate.go_seconds(text), seconds, msg=text)
        with self.assertRaises(ValueError):
            gate.go_seconds("soon")

    def test_the_events_are_read_in_every_namespace(self):
        self.assertEqual(gate.pull_events_command(), [
            "kubectl", "--context", gate.CONTEXT, "get", "events", "--all-namespaces",
            "--field-selector", "reason=Pulled", "-o", "json"])

    def test_pulls_and_images_already_present_since_the_install_started(self):
        events = {"items": [
            pulled('Successfully pulled image "apache/polaris:1.6.0" in 1m2.5s '
                   '(1m10s including waiting). Image size: 250000000 bytes.'),
            pulled('Successfully pulled image "apache/polaris:1.6.0" in 40s '
                   '(40s including waiting). Image size: 250000000 bytes.',
                   node="lakehouse-gate-worker2"),
            # Kubelets before 1.30 print no size.
            pulled('Successfully pulled image "curlimages/curl:8" in 2.1s '
                   '(2.1s including waiting)'),
            pulled('Container image "ghcr.io/cloudnative-pg/postgresql:17" already present '
                   'on machine', node="lakehouse-gate-control-plane"),
            # Before the install (the stand-ins' own), and another reason: not counted.
            pulled('Successfully pulled image "openpolicyagent/opa:1" in 9s '
                   '(9s including waiting)', at="2026-10-14T01:04:59Z"),
            pulled('Pulling image "apache/polaris:1.6.0"', reason="Pulling"),
            pulled("something new"),
        ]}
        pulls, present, unknown = gate.image_pulls(events, self.SINCE)
        self.assertEqual(pulls, [
            {"image": "apache/polaris:1.6.0", "node": "lakehouse-gate-worker",
             "seconds": 62.5, "waiting": 70.0, "bytes": 250000000},
            {"image": "apache/polaris:1.6.0", "node": "lakehouse-gate-worker2",
             "seconds": 40.0, "waiting": 40.0, "bytes": 250000000},
            {"image": "curlimages/curl:8", "node": "lakehouse-gate-worker",
             "seconds": 2.1, "waiting": 2.1, "bytes": None},
        ])
        self.assertEqual(present, {("ghcr.io/cloudnative-pg/postgresql:17",
                                    "lakehouse-gate-control-plane")})
        self.assertEqual(unknown, 1)

    def test_the_node_comes_from_the_reporting_instance_without_a_source(self):
        event = pulled('Successfully pulled image "a:1" in 1s (1s including waiting)')
        event["source"] = {}
        event["reportingInstance"] = "lakehouse-gate-worker2"
        pulls, _, _ = gate.image_pulls({"items": [event]}, self.SINCE)
        self.assertEqual(pulls[0]["node"], "lakehouse-gate-worker2")

    def test_lines_put_the_slowest_image_first_and_the_busiest_node_against_the_install(self):
        pulls = [
            {"image": "curlimages/curl:8", "node": "w1", "seconds": 2.1, "waiting": 2.1,
             "bytes": None},
            {"image": "apache/polaris:1.6.0", "node": "w1", "seconds": 62.5, "waiting": 70.0,
             "bytes": 250000000},
            {"image": "apache/polaris:1.6.0", "node": "w2", "seconds": 40.0, "waiting": 40.0,
             "bytes": 250000000},
        ]
        present = {("postgresql:17", "cp"), ("postgresql:17", "w1"), ("curlimages/curl:8", "w2")}
        lines = gate.pull_lines(pulls, present, 0, install_seconds=300)
        self.assertEqual(lines, [
            "Image pulls during the install: 3 pulls of 2 images; the busiest node, w1, "
            "spent 64.6 s pulling, of the install's 300 s.",
            "- apache/polaris:1.6.0: pulled on 2 nodes, longest 62.5 s (70.0 s with waiting), "
            "250.0 MB",
            "- curlimages/curl:8: pulled on 1 node, longest 2.1 s (2.1 s with waiting); "
            "already present on 1 node",
            "- postgresql:17: already present on 2 nodes",
            "- per node: w1 64.6 s, w2 40.0 s",
        ])

    def test_lines_say_when_nothing_was_pulled_and_count_what_was_not_understood(self):
        self.assertEqual(gate.pull_lines([], set(), 2, install_seconds=12), [
            "Image pulls during the install: none; every image was already present.",
            "- 2 Pulled events not understood",
        ])

    def test_install_measures_pulls_after_its_commands(self):
        events = json.dumps({"items": [pulled(
            'Successfully pulled image "a:1" in 1s (1s including waiting)',
            at="2999-01-01T00:00:00Z")]})
        done = subprocess.CompletedProcess([], 0, stdout=events, stderr="")
        with mock.patch.object(gate, "released_tags", return_value=[]), \
                mock.patch.object(gate, "run") as run, \
                mock.patch.object(gate.subprocess, "run", return_value=done) as kubectl, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(gate.main(["gate.py", "install", "--dry-run"]), 0)
        run.assert_called_once()
        kubectl.assert_called_once_with(gate.pull_events_command(), capture_output=True,
                                        text=True, check=False)
        self.assertIn("Image pulls during the install: 1 pull of 1 image;", out.getvalue())

    def test_a_measurement_that_fails_never_fails_the_install(self):
        refused = subprocess.CompletedProcess([], 1, stdout="", stderr="connection refused")
        for done in (refused, subprocess.CompletedProcess([], 0, stdout="{", stderr="")):
            with mock.patch.object(gate, "released_tags", return_value=[]), \
                    mock.patch.object(gate, "run"), \
                    mock.patch.object(gate.subprocess, "run", return_value=done), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(gate.main(["gate.py", "install", "--dry-run"]), 0)
            self.assertIn("Image pulls: not measured (", out.getvalue())


class InstallTimeline(unittest.TestCase):
    """#125: where the install's time goes, read back from the cluster after it."""
    SINCE = "2026-10-14T06:44:00+00:00"
    HISTORIES = {("catalog-polaris", "polaris-postgres"): [
        # Before the install: not on the timeline.
        {"revision": 1, "updated": "2026-10-13T03:00:00.5+00:00", "status": "superseded"},
        {"revision": 2, "updated": "2026-10-14T06:44:05.811736291Z", "status": "deployed"}],
        ("catalog-polaris", "polaris"): [
        {"revision": 1, "updated": "2026-10-14T06:45:30.1Z", "status": "deployed"}]}
    JOBS = {"items": [{"metadata": {"namespace": "catalog-polaris", "name": "polaris-bootstrap"},
                       "status": {"startTime": "2026-10-14T06:45:00Z",
                                  "completionTime": "2026-10-14T06:45:20Z"}}]}
    DEPLOYMENTS = {"items": [
        {"metadata": {"namespace": "catalog-polaris", "name": "polaris"},
         "status": {"conditions": [
             {"type": "Available", "status": "True",
              "lastTransitionTime": "2026-10-14T06:46:00Z"},
             {"type": "Progressing", "status": "True", "reason": "NewReplicaSetAvailable",
              "lastUpdateTime": "2026-10-14T06:46:10Z"}]}},
        # Still rolling out: no end to show.
        {"metadata": {"namespace": "catalog-polaris", "name": "polaris-console"},
         "status": {"conditions": [
             {"type": "Progressing", "status": "True", "reason": "ReplicaSetUpdated",
              "lastUpdateTime": "2026-10-14T06:46:20Z"}]}}]}
    CLUSTERS = {"items": [{"metadata": {"namespace": "catalog-polaris", "name": "polaris-pg"},
                           "status": {"conditions": [
                               {"type": "Ready", "status": "True",
                                "lastTransitionTime": "2026-10-14T06:44:50Z"}]}}]}
    COMMANDS = [("2026-10-14T06:44:01+00:00", "2026-10-14T06:46:12+00:00",
                 "release.py (the last release)")]

    def entries(self):
        return gate.timeline_entries(self.SINCE, self.HISTORIES, self.JOBS, self.DEPLOYMENTS,
                                     self.CLUSTERS, self.COMMANDS)

    def test_what_happened_since_the_start_in_order(self):
        self.assertEqual([text for _, text in self.entries()], [
            "release.py (the last release) started",
            "helm catalog-polaris/polaris-postgres revision 2 deployed",
            "cluster catalog-polaris/polaris-pg Ready",
            "job catalog-polaris/polaris-bootstrap started",
            "job catalog-polaris/polaris-bootstrap complete",
            "helm catalog-polaris/polaris revision 1 deployed",
            "deployment catalog-polaris/polaris rolled out",
            "release.py (the last release) ended",
        ])

    def test_helms_nanosecond_times_are_read(self):
        at = dict((text, when) for when, text in self.entries())
        self.assertEqual(at["helm catalog-polaris/polaris-postgres revision 2 deployed"],
                         gate.dt.datetime(2026, 10, 14, 6, 44, 5, 811736,
                                          tzinfo=gate.dt.timezone.utc))

    EVENTS = {"items": [
        {"involvedObject": {"kind": "Job", "namespace": "catalog-polaris",
                            "name": "polaris-pg-1-initdb"},
         "reason": "SuccessfulCreate", "message": "Created pod: polaris-pg-1-initdb-x",
         "lastTimestamp": "2026-10-14T06:44:10Z"},
        {"involvedObject": {"kind": "Job", "namespace": "catalog-polaris",
                            "name": "polaris-pg-1-initdb"},
         "reason": "Completed", "message": "Job completed", "eventTime": None,
         "lastTimestamp": "2026-10-14T06:44:30Z"},
        {"involvedObject": {"kind": "Cluster", "namespace": "catalog-polaris",
                            "name": "polaris-pg"},
         "reason": "CreatingInstance", "message": "Primary instance (initdb)",
         "eventTime": "2026-10-14T06:44:08.123456Z", "lastTimestamp": None},
        # Not a Job or a Cluster, and one from before the install: left out.
        {"involvedObject": {"kind": "Pod", "namespace": "catalog-polaris", "name": "p"},
         "reason": "Pulled", "message": "x", "lastTimestamp": "2026-10-14T06:44:20Z"},
        {"involvedObject": {"kind": "Job", "namespace": "catalog-polaris", "name": "old"},
         "reason": "Completed", "message": "Job completed",
         "lastTimestamp": "2026-10-13T06:44:20Z"}]}

    def test_job_and_cluster_events_split_what_cnpg_does(self):
        # CNPG deletes its initdb and join Jobs once they end; their events stay.
        entries = gate.timeline_entries(self.SINCE, {}, {}, {}, {}, [], events=self.EVENTS)
        self.assertEqual([text for _, text in entries], [
            "cluster catalog-polaris/polaris-pg: CreatingInstance (Primary instance (initdb))",
            "job catalog-polaris/polaris-pg-1-initdb: SuccessfulCreate "
            "(Created pod: polaris-pg-1-initdb-x)",
            "job catalog-polaris/polaris-pg-1-initdb: Completed (Job completed)",
        ])

    def test_the_endpoint_bootstrap_and_claim_events_split_the_step_before_polaris(self):
        # release.py runs the endpoint check and the bootstrap in pods it deletes (--rm);
        # their events stay, as do the log claim's. Other pods and claims are left out.
        def event(kind, name, reason, at, message="m"):
            return {"involvedObject": {"kind": kind, "namespace": "catalog-polaris",
                                       "name": name},
                    "reason": reason, "message": message, "lastTimestamp": at}

        entries = gate.timeline_entries(self.SINCE, {}, {}, {}, {}, [], events={"items": [
            event("Pod", "endpoint-check-1a2b3c4d", "Started", "2026-10-14T06:44:21Z"),
            event("Pod", "polaris-bootstrap-5e6f7a8b", "Pulled", "2026-10-14T06:44:25Z",
                  "Container image already present on machine"),
            event("PersistentVolumeClaim", "polaris-logs", "ProvisioningSucceeded",
                  "2026-10-14T06:44:40Z"),
            event("Pod", "polaris-pg-1", "Started", "2026-10-14T06:44:22Z"),
            event("PersistentVolumeClaim", "polaris-pg-1", "ProvisioningSucceeded",
                  "2026-10-14T06:44:23Z"),
            event("Pod", "polaris-bootstrap", "Started", "2026-10-14T06:44:24Z")]})
        self.assertEqual([text for _, text in entries], [
            "pod catalog-polaris/endpoint-check-1a2b3c4d: Started (m)",
            "pod catalog-polaris/polaris-bootstrap-5e6f7a8b: Pulled "
            "(Container image already present on machine)",
            "persistentvolumeclaim catalog-polaris/polaris-logs: ProvisioningSucceeded (m)",
        ])

    def test_events_that_cannot_be_read_leave_the_rest_of_the_timeline(self):
        def subprocess_run(argv, **kwargs):
            if "events" in argv:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="forbidden")
            out = "[]" if "list" in argv else json.dumps({"items": []})
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

        with mock.patch.object(gate.subprocess, "run", side_effect=subprocess_run):
            lines = gate.measure_timeline(self.SINCE, self.COMMANDS)
        self.assertIn("release.py (the last release) started", "\n".join(lines))
        self.assertEqual(lines[-1], "- events not read (forbidden)")

    def test_a_revision_without_a_time_is_left_out(self):
        # Helm 4 omits a zero `updated` from history's JSON.
        entries = gate.timeline_entries(self.SINCE, {("catalog-polaris", "polaris"): [
            {"revision": 1, "status": "pending-install"}]}, {}, {}, {}, [])
        self.assertEqual(entries, [])

    def test_lines_give_seconds_from_the_start_and_since_the_line_before(self):
        lines = gate.timeline_lines(self.entries()[:3], self.SINCE)
        self.assertEqual(lines, [
            "Install timeline, seconds after the install started (and since the line before):",
            "-   1 s (+ 1)  release.py (the last release) started",
            "-   5 s (+ 4)  helm catalog-polaris/polaris-postgres revision 2 deployed",
            "-  50 s (+45)  cluster catalog-polaris/polaris-pg Ready",
        ])

    def test_nothing_after_the_start_is_said(self):
        self.assertEqual(gate.timeline_lines([], self.SINCE),
                         ["Install timeline: nothing recorded after the install started."])

    def test_each_command_is_named_for_what_it_installs(self):
        previous = os.path.join(tempfile.gettempdir(), "previous", "install", "release.py")
        this = os.path.join(gate.ROOT, "install", "release.py")
        for command, label in (
                (["python3", previous], "release.py (the last release)"),
                (["python3", this], "release.py (this commit)"),
                (["python3", this, "--dry-run"], "release.py (this commit) --dry-run"),
                (["python3", os.path.join(gate.HERE, "gate.py"), "gate-policies"],
                 "gate.py gate-policies"),
                (["python3", os.path.join(gate.HERE, "gate.py"), "seed"], "gate.py seed"),
                (["git", "-C", gate.ROOT, "worktree", "add"], "git worktree add")):
            with self.subTest(label=label):
                self.assertEqual(gate.command_label(command), label)

    def test_a_measurement_that_fails_never_fails_the_install(self):
        refused = subprocess.CompletedProcess([], 1, stdout="", stderr="connection refused")
        with mock.patch.object(gate.subprocess, "run", return_value=refused):
            lines = gate.measure_timeline(self.SINCE, [])
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("Install timeline: not measured ("), lines[0])

    def test_reads_every_helm_release_then_jobs_deployments_and_clusters(self):
        answers = {
            "list": json.dumps([{"name": "polaris", "namespace": "catalog-polaris"}]),
            "history": json.dumps(self.HISTORIES[("catalog-polaris", "polaris")]),
            "jobs": json.dumps(self.JOBS), "deployments": json.dumps(self.DEPLOYMENTS),
            "clusters.postgresql.cnpg.io": json.dumps(self.CLUSTERS),
            "events": json.dumps(self.EVENTS)}
        calls = []

        def subprocess_run(argv, **kwargs):
            calls.append(argv)
            key = next(k for k in answers if k in argv)
            return subprocess.CompletedProcess(argv, 0, stdout=answers[key], stderr="")

        with mock.patch.object(gate.subprocess, "run", side_effect=subprocess_run):
            lines = gate.measure_timeline(self.SINCE, self.COMMANDS)
        # Helm 4, as pinned, lists every release without --all, and has no such flag.
        self.assertEqual(calls[0], ["helm", "--kube-context", gate.CONTEXT, "list",
                                    "--all-namespaces", "-o", "json"])
        self.assertIn(["helm", "--kube-context", gate.CONTEXT, "-n", "catalog-polaris",
                       "history", "polaris", "-o", "json"], calls)
        self.assertIn("helm catalog-polaris/polaris revision 1 deployed", "\n".join(lines))
        self.assertIn("deployment catalog-polaris/polaris rolled out", "\n".join(lines))
        self.assertIn(["kubectl", "--context", gate.CONTEXT, "get", "events",
                       "--all-namespaces", "-o", "json"], calls)
        self.assertIn("job catalog-polaris/polaris-pg-1-initdb: Completed", "\n".join(lines))

    def test_a_dry_run_that_installs_nothing_measures_nothing(self):
        with mock.patch.object(gate, "released_tags", return_value=[]), \
                mock.patch.object(gate, "run"), \
                mock.patch.object(gate, "measure_pulls", return_value=[]), \
                mock.patch.object(gate, "measure_timeline") as timeline, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gate.main(["gate.py", "install", "--dry-run"]), 0)
        timeline.assert_not_called()

    def test_an_install_prints_its_timeline_with_each_command_timed(self):
        with mock.patch.object(gate, "released_tags", return_value=[]), \
                mock.patch.object(gate, "run"), \
                mock.patch.object(gate, "measure_pulls", return_value=[]), \
                mock.patch.object(gate, "measure_timeline",
                                  return_value=["Install timeline: x"]) as timeline, \
                mock.patch.object(gate, "INSTALL_RECORD",
                                  os.path.join(tempfile.mkdtemp(), "install.json")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(gate.main(["gate.py", "install"]), 0)
        since, commands = timeline.call_args.args
        self.assertEqual([label for _, _, label in commands],
                         ["gate.py gate-policies", "release.py (this commit)"])
        self.assertIn("Install timeline: x", out.getvalue())


TEMPLATE = """KUBE_CONTEXT=
NS_CATALOG_POLARIS=catalog-polaris
NS_STANDINS=standins
S3_ENDPOINT=
S3_REGION=
POLARIS_WAREHOUSE_BUCKET=
POLARIS_OPA_URL=
POLARIS_DB_HOST=
POLARIS_GATEWAY_NAMESPACE_LABEL=
POLARIS_QUERY_NAMESPACE_LABEL=
METRICS_NAMESPACE_LABEL=
INGRESS_NAMESPACE_LABEL=
DATAHUB_EMITTER_NAMESPACE_LABEL=
POLARIS_CONSOLE_ENABLED=false
POLARIS_CONSOLE_ORIGIN=
POLARIS_CONSOLE_API_URL=
POLARIS_LOGS_STORAGE_CLASS=
POLARIS_LOGS_SIZE=
POLARIS_BACKUP_BUCKET=
DATAHUB_BACKUP_BUCKET=
POLARIS_S3_ACCESS_KEY_ID=
POLARIS_S3_SECRET_ACCESS_KEY=
SMOKE_S3_ACCESS_KEY_ID=
SMOKE_S3_SECRET_ACCESS_KEY=
POLARIS_BACKUP_S3_ACCESS_KEY_ID=
POLARIS_BACKUP_S3_SECRET_ACCESS_KEY=
DATAHUB_BACKUP_S3_ACCESS_KEY_ID=
DATAHUB_BACKUP_S3_SECRET_ACCESS_KEY=
POLARIS_DB_PASSWORD=
"""


class NoImageRegistry(unittest.TestCase):
    def test_the_gates_env_from_the_template_has_no_image_registry(self):
        with open(os.path.join(gate.ROOT, ".env.template"), encoding="utf-8") as f:
            env = gate.init_env.parse(gate.gate_env(f.read(), gen=lambda name: "x"))
        self.assertNotIn("IMAGE_REGISTRY", env)


class GateEnv(unittest.TestCase):
    def test_the_gate_env_sets_the_two_output_variables(self):
        # L4 put both in .env.template, so gate_env's fixed replacement applies.
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env.template")
        with open(path, encoding="utf-8") as f:
            template = f.read()
        env = gate.init_env.parse(gate.gate_env(template, gen=lambda name: "x"))
        self.assertEqual(env["POLARIS_LOG_OUTPUT_HOST"],
                         "main-fluent-bit-standin.standins.svc.cluster.local")
        self.assertEqual(env["POLARIS_LOG_OUTPUT_PORT"], "24224")

    def test_datahub_ingests_the_gates_ingestion_catalog(self):
        # ADR-076 §6: check-datahub-ingestion seeds it and runs the CronJob over it.
        env = gate.init_env.parse(gate.gate_env(TEMPLATE + "DATAHUB_INGEST_POLARIS_CATALOGS=\n",
                                                gen=lambda name: "x"))
        self.assertEqual(env["DATAHUB_INGEST_POLARIS_CATALOGS"], "gate_ingest_catalog")
        self.assertEqual(gate.INGEST_CATALOG, "gate_ingest_catalog")

    def test_the_gate_context_and_the_stand_in_credentials_are_set(self):
        env = gate.init_env.parse(gate.gate_env(TEMPLATE, gen=lambda name: f"gen-{name}"))
        self.assertEqual(env["KUBE_CONTEXT"], gate.CONTEXT)
        self.assertEqual(env["POLARIS_S3_ACCESS_KEY_ID"], "gen-POLARIS_S3_ACCESS_KEY_ID")
        self.assertEqual(env["POLARIS_S3_SECRET_ACCESS_KEY"],
                         "gen-POLARIS_S3_SECRET_ACCESS_KEY")
        self.assertEqual(env["SMOKE_S3_ACCESS_KEY_ID"], "gen-SMOKE_S3_ACCESS_KEY_ID")
        self.assertEqual(env["SMOKE_S3_SECRET_ACCESS_KEY"], "gen-SMOKE_S3_SECRET_ACCESS_KEY")
        self.assertEqual(env["POLARIS_BACKUP_S3_ACCESS_KEY_ID"],
                         "gen-POLARIS_BACKUP_S3_ACCESS_KEY_ID")
        self.assertEqual(env["POLARIS_BACKUP_S3_SECRET_ACCESS_KEY"],
                         "gen-POLARIS_BACKUP_S3_SECRET_ACCESS_KEY")
        # ADR-066 §3: Datahub's backups' own key, the gate's own on its stand-in.
        self.assertEqual(env["DATAHUB_BACKUP_S3_ACCESS_KEY_ID"],
                         "gen-DATAHUB_BACKUP_S3_ACCESS_KEY_ID")
        self.assertEqual(env["DATAHUB_BACKUP_S3_SECRET_ACCESS_KEY"],
                         "gen-DATAHUB_BACKUP_S3_SECRET_ACCESS_KEY")

    def test_the_endpoints_name_the_recorder_in_the_stand_ins_namespace(self):
        env = gate.init_env.parse(gate.gate_env(TEMPLATE.replace("NS_STANDINS=standins",
                                                                 "NS_STANDINS=si")))
        self.assertEqual(env["S3_ENDPOINT"], "http://recorder.si.svc.cluster.local:8333")
        self.assertEqual(env["S3_REGION"], "us-east-1")
        self.assertEqual(env["POLARIS_WAREHOUSE_BUCKET"], "polaris-warehouse")
        self.assertEqual(env["POLARIS_BACKUP_BUCKET"], "polaris-backup")
        self.assertEqual(env["DATAHUB_BACKUP_BUCKET"], "datahub-backup")
        self.assertEqual(env["POLARIS_OPA_URL"],
                         "http://recorder.si.svc.cluster.local:8181/v1/data/polaris/authz")
        self.assertEqual(env["POLARIS_DB_HOST"], "recorder.si.svc.cluster.local")

    def test_direct_endpoints_name_the_stand_ins_and_polaris_pg_never_the_recorder(self):
        # ADR-053 §4: a stress install measures Polaris, not the recorder.
        template = TEMPLATE.replace("NS_STANDINS=standins", "NS_STANDINS=si")
        env = gate.init_env.parse(gate.gate_env(template, gen=lambda name: "x", direct=True))
        self.assertEqual(env["S3_ENDPOINT"], "http://seaweedfs.si.svc.cluster.local:8333")
        self.assertEqual(env["POLARIS_OPA_URL"],
                         "http://opa.si.svc.cluster.local:8181/v1/data/polaris/authz")
        self.assertEqual(env["POLARIS_DB_HOST"], "polaris-pg-rw")
        self.assertNotIn("recorder", "".join(env.values()))

    def test_direct_changes_only_the_three_endpoints(self):
        recorded = gate.init_env.parse(gate.gate_env(TEMPLATE, gen=lambda name: "x"))
        direct = gate.init_env.parse(gate.gate_env(TEMPLATE, gen=lambda name: "x", direct=True))
        self.assertEqual(sorted(k for k in recorded if recorded[k] != direct[k]),
                         ["POLARIS_DB_HOST", "POLARIS_OPA_URL", "S3_ENDPOINT"])

    def test_each_caller_class_gets_the_gates_namespace_label(self):
        # ADR-048 §6: check-network gives these labels to its source namespaces.
        env = gate.init_env.parse(gate.gate_env(TEMPLATE, gen=lambda name: "x"))
        self.assertEqual({var: env[var] for var in gate.release.CALLER_VARS.values()}, {
            "POLARIS_GATEWAY_NAMESPACE_LABEL": "lakehouse-gate/caller=gateway",
            "POLARIS_QUERY_NAMESPACE_LABEL": "lakehouse-gate/caller=query",
            "METRICS_NAMESPACE_LABEL": "lakehouse-gate/caller=metrics",
            "INGRESS_NAMESPACE_LABEL": "lakehouse-gate/caller=ingress"})

    def test_the_emitters_class_gets_the_gates_namespace_label(self):
        # ADR-071 §5: check-network's emitters' namespace (ADR-048 §6).
        env = gate.init_env.parse(gate.gate_env(TEMPLATE, gen=lambda name: "x"))
        self.assertEqual(env["DATAHUB_EMITTER_NAMESPACE_LABEL"], "lakehouse-gate/caller=emitter")

    def test_the_console_is_enabled_at_its_service_and_reaches_polaris_at_its_service(self):
        env = gate.init_env.parse(gate.gate_env(TEMPLATE, gen=lambda name: "x"))
        self.assertEqual(env["POLARIS_CONSOLE_ENABLED"], "true")
        self.assertEqual(env["POLARIS_CONSOLE_ORIGIN"],
                         "http://polaris-console.catalog-polaris.svc.cluster.local:8080")
        self.assertEqual(env["POLARIS_CONSOLE_API_URL"],
                         "http://polaris.catalog-polaris.svc.cluster.local:8181")

    def test_the_last_releases_claim_variables_are_supplied_without_the_template(self):
        """The upgrade job runs the last release's release.py over this .env; it stops
        without these two (L7g removes them with the shared class)."""
        legacy = ("POLARIS_LOGS_STORAGE_CLASS", "POLARIS_LOGS_SIZE")
        without = "\n".join(line for line in TEMPLATE.splitlines()
                            if line.split("=", 1)[0] not in legacy) + "\n"
        for template in (without, TEMPLATE):
            with self.subTest(has_them=template is TEMPLATE):
                text = gate.gate_env(template, gen=lambda name: "x")
                env = gate.init_env.parse(text)
                self.assertEqual(env["POLARIS_LOGS_STORAGE_CLASS"], gate.SHARED_CLASS)
                self.assertEqual(env["POLARIS_LOGS_SIZE"], "1Gi")
                for name in legacy:
                    self.assertEqual(sum(line.startswith(name + "=")
                                         for line in text.splitlines()), 1, name)

    def test_the_committed_template_has_neither_old_variable(self):
        with open(os.path.join(HERE, "..", ".env.template"), encoding="utf-8") as f:
            names = gate.init_env.parse(f.read())
        self.assertNotIn("POLARIS_LOGS_STORAGE_CLASS", names)
        self.assertNotIn("POLARIS_LOGS_SIZE", names)

    def test_the_gate_env_of_the_committed_template_still_has_both(self):
        with open(os.path.join(HERE, "..", ".env.template"), encoding="utf-8") as f:
            env = gate.init_env.parse(gate.gate_env(f.read()))
        self.assertTrue(env["POLARIS_LOGS_STORAGE_CLASS"])
        self.assertTrue(env["POLARIS_LOGS_SIZE"])

    def test_everything_else_is_left_to_init_env(self):
        env = gate.init_env.parse(gate.gate_env(TEMPLATE, gen=lambda name: "x"))
        self.assertEqual(env["POLARIS_DB_PASSWORD"], "")
        self.assertEqual(env["NS_CATALOG_POLARIS"], "catalog-polaris")

    def test_the_committed_template_has_every_variable_the_gate_sets(self):
        with open(os.path.join(HERE, "..", ".env.template"), encoding="utf-8") as f:
            env = gate.init_env.parse(gate.gate_env(f.read()))
        for name in gate.GATE_ENV_GENERATED + ("KUBE_CONTEXT", "S3_ENDPOINT", "S3_REGION",
                                               "POLARIS_WAREHOUSE_BUCKET", "POLARIS_OPA_URL",
                                               "POLARIS_BACKUP_BUCKET", "DATAHUB_BACKUP_BUCKET",
                                               "POLARIS_DB_HOST", "POLARIS_CONSOLE_ORIGIN",
                                               "POLARIS_CONSOLE_API_URL"):
            with self.subTest(name=name):
                self.assertTrue(env[name])

    def test_an_existing_env_file_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".env")
            with open(path, "w", encoding="utf-8") as f:
                f.write("KUBE_CONTEXT=someone-elses\n")
            with self.assertRaises(SystemExit):
                gate.write_gate_env(path, TEMPLATE)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "KUBE_CONTEXT=someone-elses\n")

    def test_a_direct_env_file_names_the_stand_ins(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".env")
            gate.write_gate_env(path, TEMPLATE, direct=True)
            with open(path, encoding="utf-8") as f:
                env = gate.init_env.parse(f.read())
        self.assertEqual(env["S3_ENDPOINT"], "http://seaweedfs.standins.svc.cluster.local:8333")

    def test_the_env_file_is_written_for_its_owner_only(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".env")
            gate.write_gate_env(path, TEMPLATE)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


IMAGE = "ghcr.io/cloudnative-pg/postgresql:16.15-1-minimal-trixie@sha256:" + "a" * 64
SYNC = {"number": 1, "dataDurability": "required", "failoverQuorum": True}
VALUES = {"instances": 3, "image": IMAGE, "podAntiAffinityType": "required",
          "smartShutdownTimeout": 0, "synchronous": SYNC}
# What CNPG keeps of the chart's block, with a field it fills in itself.
RUNNING_SYNC = {"method": "any", **SYNC, "maxStandbyNamesFromCluster": 2}
# The FailoverQuorum CNPG keeps for the Cluster, as the primary's instance manager reports it.
QUORUM = {"status": {"method": "ANY", "standbyNumber": 1, "primary": "polaris-pg-1",
                     "standbyNames": ["polaris-pg-2", "polaris-pg-3"]}}


def pg_problems(values, running, instances, quorum=QUORUM):
    return gate.polaris_pg_problems(values, running, instances, quorum)


def cluster(instances=3, image=IMAGE, ready=3, anti="required", shutdown=0, sync=RUNNING_SYNC):
    spec = {"instances": instances, "imageName": image, "smartShutdownTimeout": shutdown,
            "affinity": {"podAntiAffinityType": anti}}
    if sync is not None:
        spec["postgresql"] = {"synchronous": sync}
    return {"spec": spec, "status": {"readyInstances": ready}}


def pods(*nodes):
    return {"items": [{"metadata": {"name": f"polaris-pg-{i + 1}"}, "spec": {"nodeName": n}}
                      for i, n in enumerate(nodes)]}


class PolarisPg(unittest.TestCase):
    """The running Cluster against the chart's values (ADR-016)."""

    def test_as_installed_passes_and_names_the_nodes(self):
        problems, notes = pg_problems(VALUES, cluster(), pods("a", "b", "c"))
        self.assertEqual(problems, [])
        self.assertIn("3 nodes", "\n".join(notes))
        self.assertIn("synchronous any 1 required", "\n".join(notes))

    def test_a_different_instance_count_is_found(self):
        problems, _ = pg_problems(VALUES, cluster(instances=1, ready=1), pods("a"))
        self.assertIn("instances", "\n".join(problems))

    def test_a_different_image_is_found(self):
        problems, _ = pg_problems(VALUES, cluster(image="postgres:16"),
                                               pods("a", "b", "c"))
        self.assertIn("image", "\n".join(problems))

    def test_instances_not_ready_are_found(self):
        problems, _ = pg_problems(VALUES, cluster(ready=2), pods("a", "b", "c"))
        self.assertIn("ready", "\n".join(problems))

    def test_a_different_anti_affinity_is_found(self):
        problems, _ = pg_problems(VALUES, cluster(anti="preferred"),
                                               pods("a", "b", "c"))
        self.assertIn("podAntiAffinityType", "\n".join(problems))

    def test_a_different_shutdown_timeout_is_found(self):
        problems, _ = pg_problems(VALUES, cluster(shutdown=180), pods("a", "b", "c"))
        self.assertIn("smartShutdownTimeout", "\n".join(problems))

    def test_asynchronous_replication_with_three_instances_is_found(self):
        # ADR-056 §1.
        problems, _ = pg_problems(VALUES, cluster(sync=None), pods("a", "b", "c"))
        self.assertIn("synchronous", "\n".join(problems))

    def test_a_different_durability_is_found(self):
        running = {**RUNNING_SYNC, "dataDurability": "preferred"}
        problems, _ = pg_problems(VALUES, cluster(sync=running), pods("a", "b", "c"))
        self.assertIn("synchronous", "\n".join(problems))

    def test_one_instance_runs_asynchronously(self):
        # ADR-056 §3.
        values = {**VALUES, "instances": 1}
        problems, notes = pg_problems(values, cluster(instances=1, ready=1, sync=None),
                                                   pods("a"))
        self.assertEqual(problems, [])
        self.assertIn("asynchronous", "\n".join(notes))
        problems, _ = pg_problems(values, cluster(instances=1, ready=1), pods("a"))
        self.assertIn("synchronous", "\n".join(problems))

    def test_two_instances_on_one_node_are_found(self):
        problems, _ = pg_problems(VALUES, cluster(), pods("a", "a", "c"))
        self.assertIn("nodes", "\n".join(problems))

    def test_a_cluster_without_the_failover_quorum_is_found(self):
        # ADR-058 §3.
        running = {k: v for k, v in RUNNING_SYNC.items() if k != "failoverQuorum"}
        problems, _ = pg_problems(VALUES, cluster(sync=running), pods("a", "b", "c"))
        self.assertIn("failoverQuorum", "\n".join(problems))

    def test_the_failover_quorum_object_is_named_in_the_notes(self):
        _, notes = pg_problems(VALUES, cluster(), pods("a", "b", "c"))
        self.assertIn("failover quorum any 1", "\n".join(notes))

    def test_a_missing_failover_quorum_object_is_found(self):
        for quorum in (None, {}, {"status": {}}):
            problems, _ = pg_problems(VALUES, cluster(), pods("a", "b", "c"), quorum)
            self.assertIn("FailoverQuorum", "\n".join(problems), quorum)

    def test_a_failover_quorum_over_another_count_is_found(self):
        quorum = {"status": {**QUORUM["status"], "standbyNumber": 2}}
        problems, _ = pg_problems(VALUES, cluster(), pods("a", "b", "c"), quorum)
        self.assertIn("FailoverQuorum", "\n".join(problems))

    def test_values_without_the_failover_quorum_need_no_object(self):
        values = {**VALUES, "synchronous": {**SYNC, "failoverQuorum": False}}
        running = {**RUNNING_SYNC, "failoverQuorum": False}
        problems, _ = pg_problems(values, cluster(sync=running), pods("a", "b", "c"), None)
        self.assertEqual(problems, [])

    def test_the_check_reads_the_release_and_the_cluster_in_its_namespace(self):
        commands = gate.polaris_pg_commands("catalog-polaris")
        self.assertEqual([c[0] for c in commands], ["helm", "kubectl", "kubectl", "kubectl"])
        self.assertIn("failoverquorum/polaris-pg", commands[3])
        self.assertIn("--ignore-not-found", commands[3])
        for argv in commands:
            self.assertEqual(argv[argv.index("-n") + 1], "catalog-polaris")
            self.assertIn(gate.CONTEXT, argv)
        self.assertIn("--all", commands[0])
        self.assertIn("cluster/polaris-pg", commands[1])
        self.assertIn("cnpg.io/cluster=polaris-pg,cnpg.io/podRole=instance", commands[2])


def rebuilding(primary="polaris-pg-1", ready=3, condition="True"):
    return {"spec": {"instances": 3},
            "status": {"currentPrimary": primary, "readyInstances": ready,
                       "conditions": [{"type": "Ready", "status": condition}]}}


def jobs(*names, uid="1"):
    return {"items": [{"metadata": {"name": n, "uid": f"{n}-{uid}"}} for n in names]}


class RebuildReplica(unittest.TestCase):
    """ADR-048 §7: a replica's PVC and pod deleted; CNPG rebuilds it with a join pod."""

    def test_it_picks_a_replica_never_the_primary(self):
        self.assertEqual(gate.rebuild_target(rebuilding("polaris-pg-1"), pods("a", "b", "c")),
                         "polaris-pg-2")
        self.assertEqual(gate.rebuild_target(rebuilding("polaris-pg-2"), pods("a", "b", "c")),
                         "polaris-pg-1")

    def test_with_no_replica_there_is_nothing_to_rebuild(self):
        self.assertIsNone(gate.rebuild_target(rebuilding("polaris-pg-1"), pods("a")))

    def test_it_deletes_the_pvc_without_waiting_then_the_pod_in_polaris_namespace(self):
        pvc, pod = gate.rebuild_commands("catalog-polaris", "polaris-pg-2")
        for argv in (pvc, pod):
            self.assertEqual(argv[:5], ["kubectl", "--context", gate.CONTEXT, "-n",
                                        "catalog-polaris"])
        self.assertEqual(pvc[5:], ["delete", "pvc", "polaris-pg-2", "--wait=false"])
        self.assertEqual(pod[5:], ["delete", "pod", "polaris-pg-2"])

    def test_it_reads_polaris_pgs_join_jobs(self):
        argv = gate.join_jobs_command("catalog-polaris")
        self.assertEqual(argv[argv.index("-n") + 1], "catalog-polaris")
        self.assertIn("cnpg.io/cluster=polaris-pg,cnpg.io/jobRole=join", argv)

    def test_rebuilt_needs_a_new_join_and_the_cluster_ready_with_every_instance(self):
        self.assertTrue(gate.rebuilt(rebuilding(), {"polaris-pg-4-join"}))
        self.assertFalse(gate.rebuilt(rebuilding(), set()))
        self.assertFalse(gate.rebuilt(rebuilding(ready=2), {"polaris-pg-4-join"}))
        self.assertFalse(gate.rebuilt(rebuilding(condition="False"), {"polaris-pg-4-join"}))

    def answers(self, after):
        """gate.output's answers: the Cluster, its instances and the join Jobs before the
        deletion, then (join Jobs, Cluster) for each poll in `after`."""
        before = [json.dumps(rebuilding()), json.dumps(pods("a", "b", "c")),
                  json.dumps(jobs("polaris-pg-2-join", "polaris-pg-3-join"))]
        return before + [json.dumps(x) for pair in after for x in pair]

    def run_rebuild(self, after, timeout=900):
        ran, other = [], []
        clock = iter(range(0, 100000, 3))
        with mock.patch.object(gate, "output", side_effect=self.answers(after)), \
                mock.patch.object(gate, "run", side_effect=ran.append), \
                mock.patch.object(gate.subprocess, "run",
                                  side_effect=lambda argv, **kw: other.append(argv)), \
                mock.patch.object(gate.time, "monotonic", side_effect=lambda: next(clock)), \
                mock.patch.object(gate.time, "sleep"), \
                mock.patch.object(gate, "REBUILD_TIMEOUT", timeout):
            problems, notes = gate.rebuild_replica({"NS_CATALOG_POLARIS": "catalog-polaris"})
        return problems, notes, ran, other

    def test_it_passes_once_a_new_join_ran_and_the_cluster_is_ready_again(self):
        old = jobs("polaris-pg-2-join", "polaris-pg-3-join")
        new = jobs("polaris-pg-2-join", "polaris-pg-3-join", "polaris-pg-4-join")
        problems, notes, ran, other = self.run_rebuild([
            (old, rebuilding()),            # the operator has not acted yet
            (new, rebuilding(ready=2)),     # the join runs
            (old, rebuilding()),            # CNPG removed the finished Job: seen all the same
        ])
        self.assertEqual(problems, [])
        self.assertEqual(ran, gate.rebuild_commands("catalog-polaris", "polaris-pg-2"))
        self.assertIn("polaris-pg-4-join", "\n".join(notes))
        self.assertEqual(other, [])

    def test_a_ready_cluster_without_a_new_join_fails_and_prints_the_join_pods_log(self):
        old = jobs("polaris-pg-2-join", "polaris-pg-3-join")
        problems, _, _, other = self.run_rebuild([(old, rebuilding())] * 3, timeout=6)
        self.assertEqual(len(problems), 1)
        self.assertIn("polaris-pg", problems[0])
        (logs,) = other
        self.assertIn("logs", logs)
        self.assertIn("cnpg.io/cluster=polaris-pg,cnpg.io/jobRole=join", logs)

    def test_a_join_job_made_again_under_an_old_name_counts_as_new(self):
        problems, notes, _, _ = self.run_rebuild([
            (jobs("polaris-pg-2-join", "polaris-pg-3-join", uid="2"), rebuilding())])
        self.assertEqual(problems, [])

    def test_a_join_that_never_gets_the_cluster_ready_fails(self):
        new = jobs("polaris-pg-2-join", "polaris-pg-3-join", "polaris-pg-4-join")
        problems, _, _, _ = self.run_rebuild([(new, rebuilding(ready=2))] * 3, timeout=6)
        self.assertEqual(len(problems), 1)

    def test_with_no_replica_it_deletes_nothing(self):
        with mock.patch.object(gate, "output", side_effect=[
                    json.dumps(rebuilding()), json.dumps(pods("a"))]), \
                mock.patch.object(gate, "run") as ran:
            problems, _ = gate.rebuild_replica({"NS_CATALOG_POLARIS": "catalog-polaris"})
        self.assertEqual(len(problems), 1)
        ran.assert_not_called()

    def test_main_runs_it_and_the_usage_names_it(self):
        self.assertIn("| rebuild-replica |", gate.__doc__.strip().splitlines()[-1])
        with mock.patch.object(gate, "rebuild_replica", return_value=([], ["ok"])) as rebuild, \
                mock.patch.object(gate.release, "load_env", return_value={}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gate.main(["gate.py", "rebuild-replica"]), 0)
        rebuild.assert_called_once()


POLARIS_ENV = {"NS_CATALOG_POLARIS": "catalog-polaris", "POLARIS_DB_HOST": "recorder.standins.svc",
               "POLARIS_OPA_URL": "http://recorder.standins.svc:8181/v1/data/polaris/authz"}
POLARIS_IMAGE = "apache/polaris:1.6.0@sha256:" + "d" * 64
POLARIS_VALUES = {
    "replicaCount": 2,
    "image": {"repository": "apache/polaris", "tag": POLARIS_IMAGE.split(":", 1)[1]},
    "persistence": {"type": "relational-jdbc"},
    "authentication": {"tokenBroker": {"type": "symmetric-key"}},
    "advancedConfig": {
        "polaris.authorization.type": "opa",
        "polaris.authorization.opa.policy-uri": POLARIS_ENV["POLARIS_OPA_URL"],
        "quarkus.datasource.jdbc.url":
            "jdbc:postgresql://recorder.standins.svc:5432/polaris?sslmode=require",
        "quarkus.datasource.jdbc.additional-jdbc-properties.socketTimeout": "10"},
}


def polaris_properties(**changes):
    props = {"polaris.persistence.type": "relational-jdbc",
             "polaris.authorization.type": "opa",
             "polaris.authorization.opa.policy-uri": POLARIS_ENV["POLARIS_OPA_URL"],
             "quarkus.datasource.jdbc.url":
                 "jdbc:postgresql://recorder.standins.svc:5432/polaris?sslmode=require",
             "polaris.authentication.token-broker.type": "symmetric-key",
             "quarkus.datasource.jdbc.additional-jdbc-properties.socketTimeout": "10", **changes}
    text = "\n".join(f"{k}={v}" for k, v in props.items())
    return {"data": {"application.properties": text}}


POLARIS_S3_ENV = [{"name": var, "valueFrom": {"secretKeyRef": {"name": "polaris-s3", "key": key}}}
                  for var, key in (("AWS_ACCESS_KEY_ID", "accessKeyId"),
                                   ("AWS_SECRET_ACCESS_KEY", "secretAccessKey"))]


def polaris_deployment(replicas=2, ready=2, image=POLARIS_IMAGE, env=POLARIS_S3_ENV):
    container = {"name": "polaris", "image": image, "env": env}
    return {"spec": {"replicas": replicas,
                     "template": {"spec": {"containers": [container]}}},
            "status": {"readyReplicas": ready}}


def polaris_pods(*nodes):
    return {"items": [{"metadata": {"name": f"polaris-{i}"},
                       "spec": {"nodeName": n},
                       "status": {"phase": "Running", "podIP": f"10.0.0.{i + 1}"}}
                      for i, n in enumerate(nodes)]}


class Polaris(unittest.TestCase):
    """The running Polaris against the values it was installed with (ADR-016)."""

    def problems(self, values=POLARIS_VALUES, deployment=None, pods=None, configmap=None,
                 env=POLARIS_ENV):
        return gate.polaris_problems(values, deployment or polaris_deployment(),
                                     pods or polaris_pods("a", "b"),
                                     configmap or polaris_properties(), env)

    def test_as_installed_passes_and_names_the_nodes_and_settings(self):
        problems, notes = self.problems()
        self.assertEqual(problems, [])
        text = "\n".join(notes)
        for part in ("2 replicas ready on 2 nodes (a, b)", POLARIS_IMAGE, "relational-jdbc",
                     "driver socketTimeout=10",
                     "symmetric-key", "opa", POLARIS_ENV["POLARIS_OPA_URL"],
                     "S3 key from Secret polaris-s3"):
            self.assertIn(part, text)

    def test_a_different_replica_count_is_found(self):
        problems, _ = self.problems(deployment=polaris_deployment(replicas=1, ready=1),
                                    pods=polaris_pods("a"))
        self.assertIn("replicas", "\n".join(problems))

    def test_replicas_not_ready_are_found(self):
        problems, _ = self.problems(deployment=polaris_deployment(ready=1))
        self.assertIn("ready", "\n".join(problems))

    def test_a_different_image_is_found(self):
        problems, _ = self.problems(deployment=polaris_deployment(image="apache/polaris:1.6.0"))
        self.assertIn("image", "\n".join(problems))

    def test_two_replicas_on_one_node_are_found(self):
        problems, _ = self.problems(pods=polaris_pods("a", "a"))
        self.assertIn("nodes", "\n".join(problems))

    def test_each_running_setting_is_compared_with_the_values(self):
        for key, other in (("polaris.persistence.type", "in-memory"),
                           ("polaris.authorization.type", "internal"),
                           ("polaris.authorization.opa.policy-uri", "http://elsewhere/v1"),
                           ("quarkus.datasource.jdbc.url", "jdbc:postgresql://x:5432/polaris"),
                           ("polaris.authentication.token-broker.type", "rsa-key-pair"),
                           ("quarkus.datasource.jdbc.additional-jdbc-properties.socketTimeout",
                            "0")):
            with self.subTest(key=key):
                problems, _ = self.problems(configmap=polaris_properties(**{key: other}))
                self.assertIn(key, "\n".join(problems))

    def test_polaris_without_its_s3_key_for_the_aws_sdk_is_found(self):
        for env in ([], POLARIS_S3_ENV[:1],
                    [{"name": "AWS_ACCESS_KEY_ID", "valueFrom": {"secretKeyRef": {
                        "name": "smoke-s3", "key": "accessKeyId"}}}, POLARIS_S3_ENV[1]]):
            with self.subTest(env=env):
                problems, _ = self.problems(deployment=polaris_deployment(env=env))
                self.assertIn("AWS_", "\n".join(problems))

    def test_values_that_do_not_name_the_endpoints_in_env_are_found(self):
        env = {**POLARIS_ENV, "POLARIS_DB_HOST": "polaris-pg-rw",
               "POLARIS_OPA_URL": "http://opa.standins.svc:8181/v1/data/polaris/authz"}
        problems, _ = self.problems(env=env)
        text = "\n".join(problems)
        self.assertIn("quarkus.datasource.jdbc.url", text)
        self.assertIn("polaris.authorization.opa.policy-uri", text)

    def test_each_running_pod_is_asked_at_its_own_address(self):
        pods = polaris_pods("a", "b")
        pods["items"].append({"metadata": {"name": "polaris-old"}, "spec": {"nodeName": "c"},
                              "status": {"phase": "Succeeded", "podIP": "10.0.0.9"}})
        self.assertEqual(gate.polaris_pod_urls(pods),
                         ["http://10.0.0.1:8181", "http://10.0.0.2:8181"])

    def test_the_check_reads_the_release_and_its_objects_in_its_namespace(self):
        commands = gate.polaris_commands("catalog-polaris")
        self.assertEqual([c[0] for c in commands], ["helm", "kubectl", "kubectl", "kubectl"])
        for argv in commands:
            self.assertEqual(argv[argv.index("-n") + 1], "catalog-polaris")
            self.assertIn(gate.CONTEXT, argv)
        self.assertEqual(commands[0][commands[0].index("values") + 1], "polaris")
        self.assertIn("--all", commands[0])
        self.assertIn("deployment/polaris", commands[1])
        self.assertIn("app.kubernetes.io/name=polaris,app.kubernetes.io/instance=polaris",
                      commands[2])
        self.assertIn("configmap/polaris", commands[3])

    def test_the_check_pod_gets_roots_credentials_from_their_secret(self):
        argv = gate.check_polaris_command(POLARIS_ENV, "curl:1@sha256:" + "e" * 64, "check-p",
                                          ["http://10.0.0.1:8181", "http://10.0.0.2:8181"])
        self.assertEqual(argv[:7], ["kubectl", "--context", gate.CONTEXT, "-n", "catalog-polaris",
                                    "run", "check-p"])
        overrides = json.loads(argv[argv.index("--overrides") + 1])
        (container,) = overrides["spec"]["containers"]
        env = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in container["env"]}
        self.assertEqual(env, {"ROOT_CLIENT_ID": {"name": "polaris-root", "key": "clientId"},
                               "ROOT_CLIENT_SECRET": {"name": "polaris-root",
                                                      "key": "clientSecret"}})
        tail = argv[argv.index("--") + 1:]
        self.assertEqual(tail, ["sh", "-s", "--", "http://polaris:8181", "http://polaris-mgmt:8182",
                                "POLARIS", "http://10.0.0.1:8181", "http://10.0.0.2:8181"])


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


class ReleasedTags(unittest.TestCase):
    """main -> rc -> tag (ADR-033): a tag sits on an rc merge commit that main never contains."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = r = self.tmp.name
        git(r, "init", "-q", "-b", "main")
        git(r, "commit", "-q", "--allow-empty", "-m", "base")
        git(r, "branch", "rc")
        git(r, "commit", "-q", "--allow-empty", "-m", "tested on main")
        git(r, "checkout", "-q", "rc")
        git(r, "merge", "-q", "--no-ff", "-m", "candidate", "main")
        git(r, "tag", "platform-2026.10.10")
        git(r, "checkout", "-q", "main")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_later_main_commit_includes_the_release_it_was_tested_as(self):
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "later on main")
        self.assertEqual(gate.released_tags(self.repo), ["platform-2026.10.10"])

    def test_a_commit_on_rc_includes_the_tag_itself(self):
        git(self.repo, "checkout", "-q", "rc")
        self.assertEqual(gate.released_tags(self.repo), ["platform-2026.10.10"])

    def test_a_commit_older_than_the_released_one_does_not(self):
        git(self.repo, "checkout", "-q", "main~1")
        self.assertEqual(gate.released_tags(self.repo), [])

    def test_no_tags_gives_none(self):
        git(self.repo, "tag", "-d", "platform-2026.10.10")
        self.assertEqual(gate.released_tags(self.repo), [])


STANDIN_ENV = {"NS_STANDINS": "standins", "NS_CATALOG_POLARIS": "catalog-polaris",
               "S3_ENDPOINT": "http://seaweedfs.standins.svc.cluster.local:8333",
               "S3_REGION": "us-east-1", "POLARIS_WAREHOUSE_BUCKET": "polaris-warehouse",
               "POLARIS_BACKUP_BUCKET": "polaris-backup",
               "DATAHUB_BACKUP_BUCKET": "datahub-backup",
               "POLARIS_OPA_URL": "http://opa.standins.svc.cluster.local:8181/v1/data/polaris/authz",
               "POLARIS_DB_HOST": "recorder.standins.svc.cluster.local"}
CURL = "curlimages/curl:8.22.0@sha256:" + "c" * 64


PYTHON = "python:3.12.14-slim@sha256:" + "f" * 64


class CheckIceberg(unittest.TestCase):
    def commands(self):
        return gate.check_iceberg_commands(STANDIN_ENV, PYTHON, name="iceberg-test")

    def test_the_configmap_holds_the_script_and_its_requirements_in_polaris_namespace(self):
        manifest = gate.iceberg_configmap(STANDIN_ENV, "# smoke.py\n", "pyiceberg==0.12.0\n")
        self.assertEqual(manifest["kind"], "ConfigMap")
        self.assertEqual(manifest["metadata"]["name"], "iceberg-smoke")
        self.assertEqual(manifest["metadata"]["namespace"], "catalog-polaris")
        self.assertEqual(manifest["data"], {"smoke.py": "# smoke.py\n",
                                            "requirements.txt": "pyiceberg==0.12.0\n"})
        apply, _ = self.commands()
        self.assertEqual(apply, ["kubectl", "--context", gate.CONTEXT, "-n", "catalog-polaris",
                                 "apply", "--server-side", "--field-manager=gate", "-f", "-"])

    def test_a_short_lived_pod_on_the_pinned_python_runs_in_polaris_namespace(self):
        _, argv = self.commands()
        self.assertEqual(argv[:7], ["kubectl", "--context", gate.CONTEXT, "-n",
                                    "catalog-polaris", "run", "iceberg-test"])
        for flag in ("--rm", "--attach", "--restart=Never", f"--image={PYTHON}",
                     "--override-type=strategic"):
            self.assertIn(flag, argv)

    def test_credentials_come_from_their_secrets_never_the_command_line(self):
        _, argv = self.commands()
        overrides = json.loads(argv[argv.index("--overrides") + 1])
        (container,) = overrides["spec"]["containers"]
        self.assertEqual(container["name"], "iceberg-test")
        refs = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in container["env"]
                if "valueFrom" in e}
        self.assertEqual(refs, {
            "ROOT_CLIENT_ID": {"name": "polaris-root", "key": "clientId"},
            "ROOT_CLIENT_SECRET": {"name": "polaris-root", "key": "clientSecret"},
            "S3_ACCESS_KEY_ID": {"name": "smoke-s3", "key": "accessKeyId"},
            "S3_SECRET_ACCESS_KEY": {"name": "smoke-s3", "key": "secretAccessKey"}})
        self.assertIn({"name": "code", "mountPath": "/smoke", "readOnly": True},
                      container["volumeMounts"])
        self.assertEqual(overrides["spec"]["volumes"],
                         [{"name": "code", "configMap": {"name": "iceberg-smoke"}}])

    def test_pip_installs_only_hashed_wheels_then_the_script_runs_against_the_gate(self):
        _, argv = self.commands()
        tail = argv[argv.index("--command") + 2:]
        self.assertEqual(tail[:2], ["sh", "-c"])
        for part in ("pip install", "--require-hashes", "--only-binary :all:",
                     "-r /smoke/requirements.txt", "exec python3 /smoke/smoke.py"):
            self.assertIn(part, tail[2])
        self.assertEqual(tail[3:], ["smoke", "http://polaris:8181", "POLARIS", "gate_catalog",
                                    STANDIN_ENV["S3_ENDPOINT"], "us-east-1",
                                    "polaris-warehouse"])

    def test_the_committed_requirements_pin_every_package_by_hash(self):
        with open(os.path.join(HERE, "iceberg", "requirements.txt"), encoding="utf-8") as f:
            text = f.read()
        packages = re.findall(r"^([a-z0-9-]+)==([^ ]+) \\$", text, re.MULTILINE)
        self.assertIn(("pyiceberg", "0.12.0"), packages)
        self.assertIn(("pyarrow", "25.0.1"), packages)
        blocks = re.split(r"^(?=[a-z0-9-]+==)", text, flags=re.MULTILINE)[1:]
        self.assertEqual(len(blocks), len(packages))
        for block in blocks:
            self.assertRegex(block, r"--hash=sha256:[0-9a-f]{64}")

    def test_the_python_image_is_the_pinned_one_the_recorder_runs_on(self):
        self.assertEqual(gate.smoke_python_image(gate.load_pins()),
                         gate.load_pins()["standin_images"]["recorder"]["image"])


class Migration(unittest.TestCase):
    """ADR-051 §2, §3: the seed before the upgrade and check-migration after it."""

    def pod(self, mode):
        return gate.seed_pod_command(STANDIN_ENV, PYTHON, f"seed-{mode}-1", mode)

    def test_the_configmap_holds_the_seed_smoke_py_and_the_requirements(self):
        files = {"seed.py": "# seed\n", "smoke.py": "# smoke\n", "requirements.txt": "x\n"}
        manifest = gate.seed_configmap(STANDIN_ENV, files)
        self.assertEqual((manifest["kind"], manifest["metadata"]),
                         ("ConfigMap", {"name": "migration-seed", "namespace": "catalog-polaris"}))
        self.assertEqual(manifest["data"], files)

    def test_both_pods_run_seed_py_on_the_pinned_python_with_credentials_from_secrets(self):
        for mode in ("seed", "check"):
            with self.subTest(mode=mode):
                argv = self.pod(mode)
                self.assertEqual(argv[:7], ["kubectl", "--context", gate.CONTEXT, "-n",
                                            "catalog-polaris", "run", f"seed-{mode}-1"])
                for flag in ("--rm", "--restart=Never", f"--image={PYTHON}", gate.CHECK_POD_LABEL):
                    self.assertIn(flag, argv)
                overrides = json.loads(argv[argv.index("--overrides") + 1])
                (container,) = overrides["spec"]["containers"]
                refs = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in container["env"]
                        if "valueFrom" in e}
                self.assertEqual(refs, {
                    "ROOT_CLIENT_ID": {"name": "polaris-root", "key": "clientId"},
                    "ROOT_CLIENT_SECRET": {"name": "polaris-root", "key": "clientSecret"},
                    "S3_ACCESS_KEY_ID": {"name": "smoke-s3", "key": "accessKeyId"},
                    "S3_SECRET_ACCESS_KEY": {"name": "smoke-s3", "key": "secretAccessKey"}})
                self.assertEqual(overrides["spec"]["volumes"],
                                 [{"name": "code", "configMap": {"name": "migration-seed"}}])
                tail = argv[argv.index("--command") + 2:]
                for part in ("--require-hashes", "--only-binary :all:",
                             "-r /seed/requirements.txt", "exec python3 /seed/seed.py"):
                    self.assertIn(part, tail[2])
                self.assertEqual(tail[3:], ["seed", mode, "http://polaris:8181", "POLARIS",
                                            STANDIN_ENV["S3_ENDPOINT"], "us-east-1",
                                            "polaris-warehouse"])

    def test_only_the_check_reads_the_record_on_stdin(self):
        seed_pod, check_pod = self.pod("seed"), self.pod("check")
        self.assertIn("--attach", seed_pod)
        self.assertNotIn("-i", seed_pod)
        self.assertIn("-i", check_pod)
        (container,) = json.loads(check_pod[check_pod.index("--overrides") + 1])["spec"][
            "containers"]
        self.assertEqual((container["stdin"], container["stdinOnce"]), (True, True))

    def test_the_counts_are_read_as_polaris_from_polaris_pg_directly(self):
        command = gate.db_counts_command(STANDIN_ENV, PG_IMAGE, "polaris", "db-counts-1")
        self.assertEqual(command[:7], ["kubectl", "--context", gate.CONTEXT, "-n",
                                       "catalog-polaris", "run", "db-counts-1"])
        self.assertIn(gate.CHECK_POD_LABEL, command)
        # Not through the recorder: the counts are the gate's, not Polaris's traffic.
        self.assertEqual(command[command.index("--") + 1:],
                         ["sh", "-s", "--", "polaris-pg-rw", "polaris"])
        (c,) = json.loads(command[command.index("--overrides") + 1])["spec"]["containers"]
        self.assertEqual([e["valueFrom"]["secretKeyRef"] for e in c["env"]],
                         [{"name": "polaris-db", "key": "username"},
                          {"name": "polaris-db", "key": "password"}])

    def test_the_counts_script_reads_every_table_of_polaris_schema_and_the_version(self):
        with open(os.path.join(HERE, "db_counts.sh"), encoding="utf-8") as f:
            script = f.read()
        self.assertIn("table_schema = 'polaris_schema'", script)
        self.assertIn("polaris_schema.version", script)
        self.assertIn("sslmode=require", script)

    def test_counts_and_the_version_parse_from_the_script_s_lines(self):
        text = "table entities 12\ntable grant_records 30\nversion 4\n"
        self.assertEqual(gate.parse_counts(text),
                         {"counts": {"entities": 12, "grant_records": 30}, "version": 4})
        self.assertEqual(gate.parse_counts("table entities 1\n"),
                         {"counts": {"entities": 1}, "version": None})

    def test_fewer_rows_or_a_table_gone_fails_and_the_version_is_reported(self):
        before = {"counts": {"entities": 12, "grant_records": 30, "events": 2}, "version": 4}
        after = {"counts": {"entities": 13, "grant_records": 29}, "version": 5}
        problems, notes = gate.db_problems(before, after)
        self.assertEqual(problems, [
            "polaris_schema.grant_records: 29 rows, 30 recorded before the upgrade",
            "polaris_schema.events: gone, 2 rows recorded before the upgrade"])
        self.assertIn("Schema version: 4 before the upgrade, 5 after: a change is reported, "
                      "not failed (ADR-051 §3).", notes)

    def test_the_same_or_more_rows_pass(self):
        before = {"counts": {"entities": 12}, "version": 4}
        problems, notes = gate.db_problems(before, {"counts": {"entities": 15, "new": 1},
                                                    "version": 4})
        self.assertEqual(problems, [])
        self.assertIn("Schema version: 4 before the upgrade, 4 after.", notes)

    def test_check_migration_without_a_seed_says_so_and_passes(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(gate.subprocess, "run") as run:
            problems, notes = gate.check_migration(STANDIN_ENV, gate.load_pins(),
                                                   os.path.join(d, "none.json"))
        self.assertEqual(problems, [])
        self.assertEqual(notes, ["Nothing was seeded: a fresh install, nothing to check "
                                 "(ADR-051 §3)."])
        run.assert_not_called()

    def test_the_seed_keeps_its_record_and_the_counts_on_the_runner_and_prints_neither(self):
        record = {"objects": {}, "rows": {}, "principals": {"gate_seed": "id:Secret0123456789"}}
        answers = {"seed": subprocess.CompletedProcess(
            [], 0, stdout=f"ok   a\nSEED-RECORD {json.dumps(record)}\n", stderr="")}
        counts = "table entities 12\nversion 4\n"
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(gate.subprocess, "run",
                                  side_effect=lambda argv, **kw: answers.get(
                                      argv[6].split("-")[0] if argv[5:6] == ["run"] else "",
                                      subprocess.CompletedProcess([], 0, "", ""))), \
                mock.patch.object(gate, "output", return_value=counts), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            path = os.path.join(d, "seed.json")
            problems, _ = gate.seed_gate(STANDIN_ENV, gate.load_pins(), path)
            with open(path, encoding="utf-8") as f:
                kept = json.load(f)
        self.assertEqual(problems, [])
        self.assertEqual(kept, {"api": record, "db": {"counts": {"entities": 12},
                                                      "version": 4}})
        self.assertIn("ok   a", out.getvalue())
        self.assertNotIn("Secret0123456789", out.getvalue())

    def test_a_seed_that_fails_or_writes_no_record_fails(self):
        for answer in (subprocess.CompletedProcess([], 1, stdout="FAIL x\n", stderr=""),
                       subprocess.CompletedProcess([], 0, stdout="ok   x\n", stderr="")):
            with self.subTest(code=answer.returncode), tempfile.TemporaryDirectory() as d, \
                    mock.patch.object(gate.subprocess, "run", return_value=answer), \
                    contextlib.redirect_stdout(io.StringIO()):
                path = os.path.join(d, "seed.json")
                problems, _ = gate.seed_gate(STANDIN_ENV, gate.load_pins(), path)
                self.assertTrue(problems)
                self.assertFalse(os.path.exists(path))

    def test_check_migration_hands_the_record_to_the_pod_and_compares_the_counts(self):
        record = {"objects": {"/x": {}}, "rows": {}, "principals": {}}
        kept = {"api": record, "db": {"counts": {"entities": 12}, "version": 4}}
        calls = []

        def fake_run(argv, **kw):
            calls.append((argv, kw))
            return subprocess.CompletedProcess([], 0, "", "")

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(gate.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(gate, "output", return_value="table entities 11\nversion 4\n"), \
                contextlib.redirect_stdout(io.StringIO()):
            path = os.path.join(d, "seed.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(kept, f)
            problems, notes = gate.check_migration(STANDIN_ENV, gate.load_pins(), path)
        pod = next(kw for argv, kw in calls if "--overrides" in argv and "-i" in argv)
        self.assertEqual(json.loads(pod["input"]), record)
        self.assertEqual(problems,
                         ["polaris_schema.entities: 11 rows, 12 recorded before the upgrade"])


CONSOLE_ENV = {**STANDIN_ENV, "POLARIS_CONSOLE_ENABLED": "true",
               "POLARIS_CONSOLE_ORIGIN": "http://polaris-console.catalog-polaris.svc.cluster.local:8080",
               "POLARIS_CONSOLE_API_URL": "http://polaris.catalog-polaris.svc.cluster.local:8181"}


class CheckConsole(unittest.TestCase):
    def command(self):
        return gate.check_console_command(CONSOLE_ENV, PYTHON, name="console-test")

    def test_a_short_lived_pod_on_the_pinned_python_reads_the_check_on_stdin(self):
        argv = self.command()
        self.assertEqual(argv[:7], ["kubectl", "--context", gate.CONTEXT, "-n",
                                    "catalog-polaris", "run", "console-test"])
        for flag in ("--rm", "-i", "--restart=Never", f"--image={PYTHON}",
                     "--override-type=strategic"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--command") + 1:], [
            "--", "python3", "-", CONSOLE_ENV["POLARIS_CONSOLE_ORIGIN"],
            CONSOLE_ENV["POLARIS_CONSOLE_API_URL"], "POLARIS"])

    def test_roots_credentials_come_from_their_secret_never_the_command_line(self):
        argv = self.command()
        overrides = json.loads(argv[argv.index("--overrides") + 1])
        (container,) = overrides["spec"]["containers"]
        self.assertEqual(container["name"], "console-test")
        self.assertIs(container["stdin"], True)
        refs = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in container["env"]
                if "valueFrom" in e}
        self.assertEqual(refs, {
            "ROOT_CLIENT_ID": {"name": "polaris-root", "key": "clientId"},
            "ROOT_CLIENT_SECRET": {"name": "polaris-root", "key": "clientSecret"}})

    def test_a_console_that_is_not_enabled_is_not_checked(self):
        with self.assertRaises(SystemExit):
            gate.check_console_command({**CONSOLE_ENV, "POLARIS_CONSOLE_ENABLED": "false"},
                                       PYTHON, name="console-test")


RESTORE_ENV = {"NS_CATALOG_POLARIS": "catalog-polaris"}
PG_IMAGE = "ghcr.io/cloudnative-pg/postgresql:16.15-1-minimal-trixie@sha256:" + "a" * 64


def recovery_pod(suffix, phase, created, containers, init=()):
    ended = created.replace(":00Z", ":20Z")

    def status(name, state, restarts=0):
        if "terminated" in state:
            state = {"terminated": {**state["terminated"], "finishedAt": ended}}
        return {"name": name, "state": state, "restartCount": restarts}
    return {"metadata": {"name": f"polaris-pg-restore-1-full-recovery-{suffix}",
                         "creationTimestamp": created},
            "status": {"phase": phase,
                       "initContainerStatuses": [status(*c) for c in init],
                       "containerStatuses": [status(*c) for c in containers]}}


ARCHIVED_BEFORE = "000000010000000000000007"
SWITCHED = "000000010000000000000008"
DONE = {"terminated": {"reason": "Completed", "exitCode": 0}}
ERROR = {"terminated": {"reason": "Error", "exitCode": 1}}
# Listed out of order: the report sorts them by creation.
RECOVERY_PODS = {"items": [
    recovery_pod("ccccc", "Succeeded", "2026-10-14T00:03:00Z", [("full-recovery", DONE)]),
    recovery_pod("aaaaa", "Failed", "2026-10-14T00:01:00Z", [("full-recovery", ERROR)],
                 init=[("plugin-barman-cloud", DONE)]),
    recovery_pod("bbbbb", "Failed", "2026-10-14T00:02:00Z", [("full-recovery", ERROR, 2)],
                 init=[("plugin-barman-cloud", {"running": {}})]),
]}


class CheckRestore(unittest.TestCase):
    """ADR-043 §4: a backup of polaris-pg, restored into polaris-pg-restore, read, deleted."""

    def test_an_on_demand_backup_of_polaris_pg_through_the_plugin(self):
        backup = gate.backup_manifest(RESTORE_ENV, "polaris-pg-gate-1")
        self.assertEqual((backup["apiVersion"], backup["kind"]),
                         ("postgresql.cnpg.io/v1", "Backup"))
        self.assertEqual(backup["metadata"], {"name": "polaris-pg-gate-1",
                                              "namespace": "catalog-polaris"})
        self.assertEqual(backup["spec"], {
            "cluster": {"name": "polaris-pg"}, "method": "plugin",
            "pluginConfiguration": {"name": "barman-cloud.cloudnative-pg.io"}})

    def test_the_restore_bootstraps_from_the_object_store_to_the_latest_wal(self):
        restore = gate.restore_cluster(RESTORE_ENV, PG_IMAGE)
        self.assertEqual((restore["kind"], restore["metadata"]["name"]),
                         ("Cluster", gate.RESTORE))
        self.assertEqual(gate.RESTORE, "polaris-pg-restore")
        self.assertEqual(restore["metadata"]["namespace"], "catalog-polaris")
        spec = restore["spec"]
        self.assertEqual(spec["instances"], 1)
        self.assertEqual(spec["imageName"], PG_IMAGE)
        # No recovery target: everything archived is replayed.
        self.assertEqual(spec["bootstrap"], {"recovery": {
            "source": "polaris-pg", "database": "polaris", "owner": "polaris",
            "secret": {"name": "polaris-db"}}})
        self.assertEqual(spec["externalClusters"], [{
            "name": "polaris-pg",
            "plugin": {"name": "barman-cloud.cloudnative-pg.io",
                       "parameters": {"barmanObjectName": "polaris-backup",
                                      "serverName": "polaris-pg"}}}])

    def test_the_restore_never_archives_into_polaris_pgs_backups(self):
        self.assertNotIn("plugins", gate.restore_cluster(RESTORE_ENV, PG_IMAGE)["spec"])

    def test_a_backup_is_done_once_completed_or_failed(self):
        self.assertIsNone(gate.backup_outcome({}))
        self.assertIsNone(gate.backup_outcome({"status": {"phase": "running"}}))
        self.assertEqual(gate.backup_outcome({"status": {"phase": "completed"}}),
                         ("completed", ""))
        self.assertEqual(gate.backup_outcome({"status": {"phase": "failed",
                                                         "error": "no bucket"}}),
                         ("failed", "no bucket"))

    def test_the_check_pod_reads_the_restore_as_polaris_with_its_secret(self):
        command = gate.check_restore_command(RESTORE_ENV, PG_IMAGE, "polaris", "check-restore-1")
        self.assertEqual(command[:5], ["kubectl", "--context", gate.CONTEXT, "-n",
                                       "catalog-polaris"])
        self.assertIn("--rm", command)
        self.assertEqual(command[command.index("--") + 1:],
                         ["sh", "-s", "--", "polaris-pg-restore-rw", "polaris", "POLARIS",
                          gate.ICEBERG_CATALOG])
        overrides = json.loads(command[command.index("--overrides") + 1])
        (c,) = overrides["spec"]["containers"]
        self.assertEqual(c["image"], PG_IMAGE)
        self.assertEqual(c["env"], [
            {"name": "PGUSER", "valueFrom": {"secretKeyRef": {"name": "polaris-db",
                                                              "key": "username"}}},
            {"name": "PGPASSWORD", "valueFrom": {"secretKeyRef": {"name": "polaris-db",
                                                                  "key": "password"}}}])

    def test_check_restore_is_a_command(self):
        self.assertIn("check-restore", gate.__doc__.strip().splitlines()[-1])

    def test_it_reads_the_restores_full_recovery_pods(self):
        argv = gate.recovery_pods_command("catalog-polaris")
        self.assertEqual(argv[:5], ["kubectl", "--context", gate.CONTEXT, "-n",
                                    "catalog-polaris"])
        self.assertIn("cnpg.io/cluster=polaris-pg-restore,cnpg.io/jobRole=full-recovery", argv)

    def test_the_report_counts_the_pods_and_names_how_each_ended(self):
        # Oldest first, whatever order they were read in.
        lines, failed = gate.recovery_report(RECOVERY_PODS)
        self.assertEqual(lines[0], "Full recovery of polaris-pg-restore: 3 pods, 2 failed.")
        self.assertEqual(lines[1:], [
            "  polaris-pg-restore-1-full-recovery-aaaaa: Failed, created 00:01:00, ended "
            "00:01:20; plugin-barman-cloud: terminated Completed (exit 0); full-recovery: "
            "terminated Error (exit 1)",
            "  polaris-pg-restore-1-full-recovery-bbbbb: Failed, created 00:02:00, ended "
            "00:02:20; plugin-barman-cloud: running; full-recovery: terminated Error (exit 1), "
            "2 restarts",
            "  polaris-pg-restore-1-full-recovery-ccccc: Succeeded, created 00:03:00, ended "
            "00:03:20; full-recovery: terminated Completed (exit 0)",
        ])
        self.assertEqual(failed, ["polaris-pg-restore-1-full-recovery-aaaaa",
                                  "polaris-pg-restore-1-full-recovery-bbbbb"])

    def test_no_pods_read_is_said_and_names_none(self):
        self.assertEqual(gate.recovery_report({"items": []}),
                         (["Full recovery of polaris-pg-restore: 0 pods, 0 failed."], []))

    def test_the_restore_is_ready_only_on_its_ready_condition(self):
        def cluster(*conditions):
            return {"status": {"conditions": [{"type": t, "status": s} for t, s in conditions]}}
        self.assertTrue(gate.cluster_ready(cluster(("Ready", "True"))))
        self.assertFalse(gate.cluster_ready(cluster(("Ready", "False"))))
        self.assertFalse(gate.cluster_ready(cluster(("ContinuousArchiving", "True"))))
        self.assertFalse(gate.cluster_ready({}))

    def test_pods_are_remembered_after_they_are_gone_and_each_failure_named_once(self):
        # #127's smoke: CNPG deletes the Job's pods, so a read after Ready found one of them.
        aaaaa, bbbbb, ccccc = sorted(RECOVERY_PODS["items"],
                                     key=lambda p: p["metadata"]["name"])
        running = copy.deepcopy(bbbbb)
        running["status"]["phase"] = "Running"
        seen = {}
        self.assertEqual(gate.remember_pods(seen, {"items": [aaaaa, running]}),
                         [aaaaa["metadata"]["name"]])
        self.assertEqual(gate.remember_pods(seen, {"items": [aaaaa, bbbbb]}),
                         [bbbbb["metadata"]["name"]])
        self.assertEqual(gate.remember_pods(seen, {"items": [ccccc]}), [])
        self.assertEqual(sorted(seen), sorted(p["metadata"]["name"] for p in (aaaaa, bbbbb,
                                                                              ccccc)))
        lines, _ = gate.recovery_report({"items": list(seen.values())})
        self.assertEqual(lines[0], "Full recovery of polaris-pg-restore: 3 pods, 2 failed.")

    def check(self, polls=None, settled=None, unread=(), archived=None):
        """Runs check_restore with every command faked. `polls` lists, per read of the wait,
        whether the restore is Ready and the pods read; once they run out the clock passes
        the timeout. `unread` names the reads of the pods (0-based) that fail. `archived`
        lists what pg_stat_archiver answers on each read, the last answer repeated.
        (problems, notes, events)."""
        if archived is None:
            archived = [ARCHIVED_BEFORE, SWITCHED]
        if polls is None:
            aaaaa, bbbbb, ccccc = sorted(RECOVERY_PODS["items"],
                                         key=lambda p: p["metadata"]["name"])
            polls = [(False, [aaaaa]), (False, [bbbbb]), (True, [ccccc])]
        events, reads = [], {"cluster": 0, "pods": 0, "archived": 0}

        def subprocess_run(argv, **kwargs):
            text = " ".join(argv)
            kind = ("switch" if "pg_switch_wal" in text else
                    "archived" if "pg_stat_archiver" in text else
                    "primary" if f"cluster/{gate.release.POLARIS_PG}" in argv else
                    "cluster" if f"cluster/{gate.RESTORE}" in argv else
                    "delete" if "delete" in argv else "pods" if "get" in argv else
                    "logs" if "logs" in argv else "psql")
            events.append((kind, argv))
            if kind == "primary":
                return subprocess.CompletedProcess(argv, 0, json.dumps(
                    {"status": {"currentPrimary": "polaris-pg-2"}}), "")
            if kind == "switch":
                return subprocess.CompletedProcess(argv, 0, SWITCHED + "\n", "")
            if kind == "archived":
                answer = archived[min(reads["archived"], len(archived) - 1)]
                reads["archived"] += 1
                return subprocess.CompletedProcess(argv, 0, answer + "\n", "")
            if kind == "cluster":
                ready = polls[min(reads["cluster"], len(polls) - 1)][0]
                reads["cluster"] += 1
                return subprocess.CompletedProcess(argv, 0, json.dumps({"status": {
                    "conditions": [{"type": "Ready", "status": str(ready)}]}}), "")
            if kind == "pods":
                n = reads["pods"]
                reads["pods"] += 1
                if n in unread:
                    return subprocess.CompletedProcess(argv, 1, "", "connection refused")
                pods = polls[min(n, len(polls) - 1)][1]
                return subprocess.CompletedProcess(argv, 0, json.dumps({"items": pods}), "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        def settle(run, context, namespace, cluster):
            events.append(("settle", (run, context, namespace, cluster)))
            return settled

        def monotonic():
            # Within the timeout for as many reads as there are polls, past it after.
            return 0 if reads["cluster"] < len(polls) else gate.RESTORE_TIMEOUT + 1

        def run(argv, stdin=None):
            events.append(("apply", stdin and json.loads(stdin)["kind"]))

        with mock.patch.object(gate, "run", side_effect=run), \
                mock.patch.object(gate, "wait_for", return_value=("completed", "")), \
                mock.patch.object(gate, "polaris_pg_values",
                                  return_value={"image": PG_IMAGE, "database": "polaris"}), \
                mock.patch.object(gate.subprocess, "run", side_effect=subprocess_run), \
                mock.patch.object(gate.time, "monotonic", side_effect=monotonic), \
                mock.patch.object(gate.time, "sleep") as sleep, \
                mock.patch.object(gate.release, "settle", side_effect=settle), \
                contextlib.redirect_stdout(io.StringIO()):
            problems, notes = gate.check_restore(RESTORE_ENV)
        for call in sleep.call_args_list:
            self.assertEqual(call.args, (gate.RESTORE_POLL,))
        return problems, notes, events

    # Smoke 38245393066: Ready, then psql refused while CNPG restarted the restored instance.
    def test_the_restore_settles_after_ready_and_before_psql(self):
        problems, notes, events = self.check()
        self.assertEqual(problems, [])
        self.assertEqual([kind for kind, _ in events][-3:], ["settle", "psql", "delete"])
        settle = next(args for kind, args in events if kind == "settle")
        self.assertEqual(settle, (gate.release.call, gate.CONTEXT, "catalog-polaris",
                                  "polaris-pg-restore"))
        self.assertIn("healthy", "\n".join(notes))

    def test_each_failed_pods_logs_are_printed_while_the_restore_waits(self):
        # #86: the logs are read as soon as a pod fails, before CNPG deletes it.
        problems, notes, events = self.check()
        self.assertEqual(problems, [])
        wait = [kind for kind, _ in events][events.index(("apply", "Cluster")) + 1:]
        self.assertEqual(wait[:8], ["cluster", "pods", "logs", "cluster", "pods", "logs",
                                    "cluster", "pods"])
        logs = [argv for kind, argv in events if kind == "logs"]
        self.assertEqual([argv[argv.index("logs") + 1] for argv in logs],
                         ["polaris-pg-restore-1-full-recovery-aaaaa",
                          "polaris-pg-restore-1-full-recovery-bbbbb"])
        for argv in logs:
            self.assertIn("--all-containers", argv)
            self.assertIn("--prefix", argv)
        # Every pod seen in the wait is reported, though no single read held them all.
        self.assertIn("Full recovery of polaris-pg-restore: 3 pods, 2 failed.", notes)

    def test_a_restore_that_never_gets_ready_still_reports_its_pods(self):
        aaaaa = sorted(RECOVERY_PODS["items"], key=lambda p: p["metadata"]["name"])[0]
        problems, notes, events = self.check(polls=[(False, [aaaaa]), (False, [aaaaa])])
        self.assertEqual(problems, ["Cluster polaris-pg-restore did not get Ready from "
                                    "polaris-backup"])
        self.assertEqual([kind for kind, _ in events].count("logs"), 1)
        self.assertNotIn("settle", [kind for kind, _ in events])
        self.assertEqual(events[-1][0], "delete")
        self.assertIn("Full recovery of polaris-pg-restore: 1 pods, 1 failed.", notes)

    def test_a_read_of_the_pods_that_fails_is_counted_and_the_wait_goes_on(self):
        problems, notes, _ = self.check(unread=(1,))
        self.assertEqual(problems, [])
        self.assertIn("Full recovery of polaris-pg-restore: 2 pods, 1 failed.", notes)
        self.assertIn("  1 read of the pods failed: connection refused", notes)

    def test_a_restore_that_does_not_settle_is_a_problem_and_still_deleted(self):
        problems, _, events = self.check(settled="Cluster polaris-pg-restore did not settle")
        self.assertEqual(problems, ["Cluster polaris-pg-restore did not settle"])
        self.assertEqual([kind for kind, _ in events][-2:], ["settle", "delete"])

    # #86: a backup from a standby is consistent only past the WAL archived so far; without a
    # switch, the restore failed until archive_timeout (smoke 38515945485: 4 attempts).
    def test_the_primary_switches_wal_and_it_is_archived_before_the_restore(self):
        problems, notes, events = self.check(archived=[ARCHIVED_BEFORE, ARCHIVED_BEFORE,
                                                       SWITCHED])
        self.assertEqual(problems, [])
        kinds = [kind for kind, _ in events]
        self.assertEqual(kinds[:7], ["apply", "primary", "switch", "archived", "archived",
                                     "archived", "apply"])
        self.assertEqual(events[0][1], "Backup")
        self.assertEqual(events[6][1], "Cluster")
        self.assertIn(f"WAL switched on polaris-pg-2: {SWITCHED} archived before the "
                      "restore.", notes)

    def test_the_switch_and_the_archive_are_read_on_the_primary(self):
        self.assertEqual(gate.wal_switch_command("catalog-polaris", "polaris-pg-2"), [
            "kubectl", "--context", gate.CONTEXT, "-n", "catalog-polaris", "exec",
            "polaris-pg-2", "-c", "postgres", "--", "psql", "-XAtq", "-c",
            "SELECT pg_walfile_name(pg_switch_wal())"])
        self.assertEqual(gate.archived_wal_command("catalog-polaris", "polaris-pg-2")[-1],
                         "SELECT coalesce(last_archived_wal, '') FROM pg_stat_archiver")

    def test_a_segment_counts_as_archived_once_the_archiver_reached_it(self):
        self.assertTrue(gate.wal_archived(SWITCHED, SWITCHED))
        self.assertTrue(gate.wal_archived(SWITCHED, "000000010000000000000009"))
        self.assertFalse(gate.wal_archived(SWITCHED, ARCHIVED_BEFORE))
        self.assertFalse(gate.wal_archived(SWITCHED, ""))
        # A later timeline's segment comes after every segment of an earlier one.
        self.assertTrue(gate.wal_archived(SWITCHED, "000000020000000000000001"))

    def test_a_segment_never_archived_is_a_problem_and_no_restore_is_made(self):
        problems, _, events = self.check(archived=[ARCHIVED_BEFORE])
        self.assertEqual(problems, [f"WAL segment {SWITCHED} from polaris-pg-2 was not "
                                    f"archived within {gate.ARCHIVE_TIMEOUT} s"])
        kinds = [kind for kind, _ in events]
        self.assertNotIn("cluster", kinds)
        self.assertEqual([args for kind, args in events if kind == "apply"], ["Backup"])
        self.assertEqual(kinds.count("archived"), gate.ARCHIVE_TIMEOUT // gate.RESTORE_POLL)


class StandinEndpoints(unittest.TestCase):
    def test_the_sidecar_forwards_to_the_fluent_bit_standin(self):
        for direct in (False, True):
            with self.subTest(direct=direct):
                env = gate.standin_endpoints("standins", direct)
                self.assertEqual(env["POLARIS_LOG_OUTPUT_HOST"],
                                 "main-fluent-bit-standin.standins.svc.cluster.local")
                self.assertEqual(env["POLARIS_LOG_OUTPUT_PORT"], "24224")


class Standins(unittest.TestCase):
    def test_applies_the_list_server_side_then_waits_for_every_deployment(self):
        (apply, stdin), (wait, _) = gate.standins_commands(
            STANDIN_ENV, "the stand-ins list")[:2]
        self.assertEqual(stdin, "the stand-ins list")
        self.assertEqual(apply[:3], ["kubectl", "--context", gate.CONTEXT])
        self.assertIn("--server-side", apply)
        self.assertEqual(apply[-2:], ["-f", "-"])
        self.assertEqual(wait[:5], ["kubectl", "--context", gate.CONTEXT, "-n", "standins"])
        for name in ("seaweedfs", "opa", "recorder", "prometheus", "main-fluent-bit-standin"):
            self.assertIn(f"deployment/{name}", wait)

    def test_then_the_backup_bucket_is_created_in_seaweedfs_and_checked(self):
        # ADR-043 §2: `weed mini -bucket` makes one bucket; the backups get their own.
        (create, stdin), (check, none) = gate.standins_commands(
            STANDIN_ENV, "the stand-ins list")[2:4]
        exec_ = ["kubectl", "--context", gate.CONTEXT, "-n", "standins", "exec"]
        self.assertEqual(create, exec_ + ["-i", "deployment/seaweedfs", "--", "weed", "shell"])
        self.assertEqual(stdin, "s3.bucket.create -name polaris-backup\n")
        self.assertEqual(check, exec_ + ["deployment/seaweedfs", "--", "curl", "-sf",
                                         "-o", "/dev/null",
                                         "http://127.0.0.1:8888/buckets/polaris-backup/"])
        self.assertIsNone(none)

    def test_then_datahubs_backup_bucket_is_created_and_checked_too(self):
        # ADR-066 §3: on the gate the S3 stand-in gets Datahub's bucket as well.
        commands = gate.standins_commands(STANDIN_ENV, "the stand-ins list")
        self.assertEqual(len(commands), 6)
        (create, stdin), (check, none) = commands[4:]
        exec_ = ["kubectl", "--context", gate.CONTEXT, "-n", "standins", "exec"]
        self.assertEqual(create, exec_ + ["-i", "deployment/seaweedfs", "--", "weed", "shell"])
        self.assertEqual(stdin, "s3.bucket.create -name datahub-backup\n")
        self.assertEqual(check, exec_ + ["deployment/seaweedfs", "--", "curl", "-sf",
                                         "-o", "/dev/null",
                                         "http://127.0.0.1:8888/buckets/datahub-backup/"])
        self.assertIsNone(none)

    def test_direct_stand_ins_take_their_cpu_limits_from_the_stress_config(self):
        # ADR-053 §4: k6, OPA and SeaweedFS each run under a CPU limit; tuning values live in
        # gates/stress/config.json (ADR-000 rule 11).
        limits = gate.stress_standin_cpu()
        self.assertEqual(sorted(limits), ["opa", "seaweedfs"])
        for name, cpu in limits.items():
            with self.subTest(name=name):
                self.assertRegex(cpu, r"^[1-9][0-9]*m?$")

    def overlay(self, d, data):
        path = os.path.join(d, "overlay.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return {"STRESS_OVERLAY": path}

    def test_without_an_overlay_the_stress_config_is_config_json(self):
        with open(gate.STRESS_CONFIG, encoding="utf-8") as f:
            self.assertEqual(gate.stress_config({}), json.load(f))

    def test_an_overlay_changes_only_the_tuning_values_it_names(self):
        # ADR-053 §1, §7: Tier D runs Tier A's scenario; only the rates, the step lengths, the
        # hold and the sizes differ.
        with tempfile.TemporaryDirectory() as d:
            config = gate.stress_config(self.overlay(d, {
                "_comment": "Tier D", "step_seconds": 120,
                "profiles": {"mixed": {"start": 250, "step": 250}},
                "hold": {"fraction": 0.8, "seconds": 7200}, "standin_cpu": {"opa": "2"},
                "k6": {"max_vus": 2000}}))
        base = gate.stress_config({})
        self.assertEqual(config["step_seconds"], 120)
        self.assertEqual(config["profiles"]["mixed"], {**base["profiles"]["mixed"],
                                                       "start": 250, "step": 250})
        self.assertEqual(config["profiles"]["commits"], base["profiles"]["commits"])
        self.assertEqual(config["hold"], {**base["hold"], "fraction": 0.8, "seconds": 7200})
        self.assertEqual(config["standin_cpu"], {**base["standin_cpu"], "opa": "2"})
        self.assertEqual(config["k6"], {**base["k6"], "max_vus": 2000})
        self.assertEqual(config["_comment"], base["_comment"])
        for key in ("pass", "seed", "hot", "commit_retries", "etag_ttl_s", "throttle_max"):
            self.assertEqual(config[key], base[key])

    def test_an_overlay_that_touches_the_scenario_is_refused(self):
        for bad in ({"pass": {"error_max": 0.1}}, {"seed": {"batch": 1}},
                    {"profiles": {"mixed": {"shares": {"commit": 1.0}}}},
                    {"profiles": {"new": {"start": 1}}}, {"k6": {"cpu": "4"}},
                    {"standin_cpu": {"polaris": "4"}}, {"hold": {"profile": "commits"}},
                    {"step_seconds": "120"}, {"hold": 1}):
            with self.subTest(overlay=bad), tempfile.TemporaryDirectory() as d:
                with self.assertRaises(ValueError):
                    gate.stress_config(self.overlay(d, bad))

    def test_direct_stand_ins_take_their_cpu_limits_from_the_overlay_too(self):
        with tempfile.TemporaryDirectory() as d:
            limits = gate.stress_standin_cpu(environ=self.overlay(d, {"standin_cpu": {
                "seaweedfs": "3"}}))
        self.assertEqual(limits["seaweedfs"], "3")

    def test_the_stress_config_says_its_numbers_are_placeholders_until_calibration(self):
        with open(gate.STRESS_CONFIG, encoding="utf-8") as f:
            config = json.load(f)
        self.assertIn("calibration", config["_comment"])

    def test_the_recorder_certificate_is_made_here_for_its_service_name(self):
        with tempfile.TemporaryDirectory() as d:
            cert, key = gate.recorder_certificate(STANDIN_ENV, d)
        self.assertIn("BEGIN CERTIFICATE", cert)
        self.assertIn("PRIVATE KEY", key)


class CheckTraffic(unittest.TestCase):
    def test_reads_every_line_the_recorder_wrote(self):
        self.assertEqual(gate.recorder_logs_command(STANDIN_ENV),
                         ["kubectl", "--context", gate.CONTEXT, "-n", "standins", "logs",
                          "deployment/recorder", "--tail=-1"])

    def test_every_expectation_file_is_checked_and_any_failure_fails(self):
        log = "\n".join(json.dumps(r) for r in (
            {"proto": "http", "listener": "opa", "status": 200},
            {"proto": "pg", "event": "connect", "result": "ok"}))
        good = [{"name": "opa answered", "must": "exist", "match": {"listener": "opa"}}]
        bad = [{"name": "no pg", "must": "not_exist", "match": {"proto": "pg"}}]
        problems, lines = gate.check_traffic(log, [good])
        self.assertEqual(problems, [])
        self.assertIn("2 records: 1 HTTP, 1 PostgreSQL", lines[0])
        problems, _ = gate.check_traffic(log, [good, bad])
        self.assertEqual(len(problems), 1)


def upgrading(*times, name="polaris-pg", reason="UpgradingInstance"):
    return {"items": [{"involvedObject": {"kind": "Cluster", "name": name}, "reason": reason,
                       "firstTimestamp": t} for t in times]}


class RestartWindow(unittest.TestCase):
    END = "2026-10-12T10:37:58+00:00"

    def test_from_the_first_upgrading_instance_event_to_the_end_of_the_install(self):
        events = upgrading("2026-10-12T10:34:40Z", "2026-10-12T10:34:00Z")
        self.assertEqual(gate.restart_window(events, self.END),
                         ("2026-10-12T10:34:00Z", self.END))

    def test_no_window_without_a_restart_or_an_upgrade(self):
        self.assertIsNone(gate.restart_window(upgrading(), self.END))
        self.assertIsNone(gate.restart_window(upgrading("2026-10-12T10:34:00Z"), None))

    def test_other_clusters_reasons_and_later_restarts_do_not_count(self):
        self.assertIsNone(gate.restart_window(
            upgrading("2026-10-12T10:34:00Z", name="polaris-pg-restore"), self.END))
        self.assertIsNone(gate.restart_window(
            upgrading("2026-10-12T10:34:00Z", reason="CreatingInstance"), self.END))
        self.assertIsNone(gate.restart_window(upgrading("2026-10-12T10:40:00Z"), self.END))

    def test_the_events_are_read_for_polaris_pg_in_its_namespace(self):
        self.assertEqual(gate.restart_events_command("catalog-polaris"), [
            "kubectl", "--context", gate.CONTEXT, "-n", "catalog-polaris", "get", "events",
            "--field-selector", "involvedObject.name=polaris-pg,reason=UpgradingInstance",
            "-o", "json"])

    def test_check_traffic_names_the_window_and_passes_it_on(self):
        log = json.dumps({"proto": "pg", "event": "statement", "time": "2026-10-12T10:37:06Z",
                          "error": {"sqlstate": "57P01", "severity": "FATAL"}})
        rule = [{"name": "no failed statement", "must": "not_exist",
                 "match": {"event": "statement"},
                 "allow_during_restart": {"error.sqlstate": "57P01"}}]
        problems, lines = gate.check_traffic(log, [rule],
                                             restart=("2026-10-12T10:34:00Z", self.END))
        self.assertEqual(problems, [])
        self.assertIn("polaris-pg restarted during the upgrade from 2026-10-12T10:34:00Z",
                      lines[1])
        problems, _ = gate.check_traffic(log, [rule])
        self.assertEqual(len(problems), 1)

    def test_only_the_rules_kade_named_carry_an_allowance(self):
        # Two on 2026-10-12; the TLS negotiation on 2026-10-13 (nightly smoke 38385377721).
        with open(os.path.join(HERE, "expectations", "standins.json"), encoding="utf-8") as f:
            rules = {e["name"]: e.get("allow_during_restart")
                     for e in json.load(f)["expectations"]}
        self.assertEqual({n: a for n, a in rules.items() if a}, {
            "No PostgreSQL statement failed, but for a table that did not exist yet":
                {"error.sqlstate": "57P01", "error.severity": "FATAL"},
            "No upstream the recorder could not reach": {"listener": "pg"},
            "No PostgreSQL TLS negotiation failed": {"listener": "pg"}})


class CheckStandins(unittest.TestCase):
    CASES = [{"name": "root", "allow": True, "input": {"actor": {"principal": "root"}}},
             {"name": "stranger", "allow": False, "input": {"actor": {"principal": "x"}}}]

    def command(self):
        return gate.check_standins_command(STANDIN_ENV, self.CASES, CURL, name="check-test")

    def test_a_short_lived_curl_pod_runs_in_polaris_namespace(self):
        argv = self.command()
        self.assertEqual(argv[:7], ["kubectl", "--context", gate.CONTEXT, "-n",
                                    "catalog-polaris", "run", "check-test"])
        for flag in ("--rm", "-i", "--restart=Never", f"--image={CURL}"):
            self.assertIn(flag, argv)

    def test_the_s3_credentials_come_from_polaris_secret_never_the_command_line(self):
        argv = self.command()
        overrides = json.loads(argv[argv.index("--overrides") + 1])
        (container,) = overrides["spec"]["containers"]
        self.assertEqual(container["name"], "check-test")
        refs = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in container["env"]}
        self.assertEqual(refs["AWS_ACCESS_KEY_ID"], {"name": "polaris-s3", "key": "accessKeyId"})
        self.assertEqual(refs["AWS_SECRET_ACCESS_KEY"],
                         {"name": "polaris-s3", "key": "secretAccessKey"})

    def test_the_override_merges_into_the_generated_container_instead_of_replacing_it(self):
        # A merge patch (kubectl's default) replaces the containers list whole, so the pod
        # would lose its `sh -s` command and run curl's entrypoint (smoke 38121510918).
        self.assertIn("--override-type=strategic", self.command())

    def test_the_script_reads_stdin_with_the_endpoints_then_each_case(self):
        argv = self.command()
        tail = argv[argv.index("sh"):]
        self.assertEqual(tail[:7], ["sh", "-s", "--", STANDIN_ENV["S3_ENDPOINT"],
                                    "polaris-warehouse", "us-east-1",
                                    STANDIN_ENV["POLARIS_OPA_URL"]])
        self.assertEqual(tail[7:9], ["root", "true"])
        self.assertEqual(json.loads(tail[9]), self.CASES[0]["input"])
        self.assertEqual(tail[10:12], ["stranger", "false"])

    def test_the_database_check_runs_psql_through_the_recorder_as_polaris(self):
        argv = gate.check_db_command(STANDIN_ENV, "pg-image@sha256:" + "d" * 64, "polaris",
                                     name="db-test")
        self.assertEqual(argv[:7], ["kubectl", "--context", gate.CONTEXT, "-n",
                                    "catalog-polaris", "run", "db-test"])
        self.assertIn("--override-type=strategic", argv)
        overrides = json.loads(argv[argv.index("--overrides") + 1])
        refs = {e["name"]: e["valueFrom"]["secretKeyRef"]
                for e in overrides["spec"]["containers"][0]["env"]}
        self.assertEqual(refs, {"PGUSER": {"name": "polaris-db", "key": "username"},
                                "PGPASSWORD": {"name": "polaris-db", "key": "password"}})
        self.assertEqual(argv[-5:], ["sh", "-s", "--", STANDIN_ENV["POLARIS_DB_HOST"],
                                     "polaris"])

    def test_the_database_image_and_name_are_the_charts(self):
        values = gate.polaris_pg_values()
        self.assertRegex(values["image"], r"@sha256:[0-9a-f]{64}$")
        self.assertEqual(values["database"], "polaris")

    def test_the_committed_cases_load(self):
        cases = gate.standins.load_cases(os.path.join(HERE, "opa", "cases.json"))
        self.assertGreater(len(cases), 1)


DATAHUB_IMAGE = "ghcr.io/cloudnative-pg/postgresql:16.15-1@sha256:" + "d" * 64
DATAHUB_VALUES = {"instances": 3, "image": DATAHUB_IMAGE, "podAntiAffinityType": "required",
                  "smartShutdownTimeout": 0}


def datahub_cluster(ready=3, synchronous=None, instances=3, image=DATAHUB_IMAGE):
    spec = {"instances": instances, "imageName": image, "smartShutdownTimeout": 0,
            "affinity": {"podAntiAffinityType": "required"}}
    if synchronous:
        spec["postgresql"] = {"synchronous": synchronous}
    return {"spec": spec, "status": {"readyInstances": ready, "currentPrimary": "datahub-pg-1"}}


def datahub_pods(*nodes):
    return {"items": [{"metadata": {"name": f"datahub-pg-{i + 1}"}, "spec": {"nodeName": n}}
                      for i, n in enumerate(nodes)]}


class CheckDatahubPg(unittest.TestCase):
    """ADR-066 §5: datahub-pg as its values say, asynchronous, three instances on three nodes;
    then WAL archived and an on-demand backup completed through the plugin."""

    def test_reads_its_values_cluster_and_instances_in_datahubs_namespace(self):
        values, cluster, pods = gate.datahub_pg_commands("catalog-datahub")
        self.assertEqual(values, ["helm", "--kube-context", gate.CONTEXT, "-n", "catalog-datahub",
                                  "get", "values", "datahub-postgres", "--all", "-o", "json"])
        self.assertEqual(cluster, ["kubectl", "--context", gate.CONTEXT, "-n", "catalog-datahub",
                                   "get", "cluster/datahub-pg", "-o", "json"])
        self.assertEqual(pods[:6], ["kubectl", "--context", gate.CONTEXT, "-n",
                                    "catalog-datahub", "get"])
        self.assertIn("cnpg.io/cluster=datahub-pg,cnpg.io/podRole=instance", pods)

    def test_three_ready_asynchronous_instances_on_three_nodes_pass(self):
        problems, notes = gate.datahub_pg_problems(
            DATAHUB_VALUES, datahub_cluster(), datahub_pods("a", "b", "c"))
        self.assertEqual(problems, [])
        self.assertEqual(notes, ["Cluster datahub-pg: 3 instances ready on 3 nodes (a, b, c), "
                                 f"image {DATAHUB_IMAGE}, anti-affinity required, replication "
                                 "asynchronous."])

    def test_synchronous_replication_running_is_a_problem(self):
        problems, _ = gate.datahub_pg_problems(
            DATAHUB_VALUES, datahub_cluster(synchronous={"method": "any", "number": 1}),
            datahub_pods("a", "b", "c"))
        self.assertEqual(problems, ["replication: synchronous running, asynchronous in ADR-066"])

    def test_a_value_the_cluster_does_not_run_is_a_problem(self):
        problems, _ = gate.datahub_pg_problems(
            DATAHUB_VALUES, datahub_cluster(instances=2, ready=2), datahub_pods("a", "b"))
        self.assertIn("instances: 2 running, 3 in the values", problems)
        self.assertIn("2 instances ready of 3", problems)

    def test_two_instances_on_one_node_is_a_problem(self):
        problems, _ = gate.datahub_pg_problems(
            DATAHUB_VALUES, datahub_cluster(), datahub_pods("a", "a", "b"))
        self.assertEqual(problems, ["3 instances on 2 nodes: a, b"])

    def test_the_backup_is_of_datahub_pg_in_datahubs_namespace(self):
        backup = gate.backup_manifest({"NS_CATALOG_DATAHUB": "catalog-datahub"},
                                      "datahub-pg-gate-1", "NS_CATALOG_DATAHUB",
                                      gate.release.DATAHUB_PG)
        self.assertEqual(backup["metadata"], {"name": "datahub-pg-gate-1",
                                              "namespace": "catalog-datahub"})
        self.assertEqual(backup["spec"]["cluster"], {"name": "datahub-pg"})
        self.assertEqual(backup["spec"]["method"], "plugin")

    def test_check_datahub_pg_switches_wal_then_backs_up_and_reports_both(self):
        events = []

        def fake_output(argv):
            if "helm" == argv[0]:
                return json.dumps(DATAHUB_VALUES)
            if "cluster/datahub-pg" in argv:
                return json.dumps(datahub_cluster())
            if "pods" in argv:
                return json.dumps(datahub_pods("a", "b", "c"))
            raise AssertionError(argv)

        def fake_switch(kubectl, namespace, cluster, when):
            events.append(("switch", namespace, cluster))
            return None, f"WAL switched on datahub-pg-1: 0001 archived {when}."

        def fake_run(argv, stdin=None):
            events.append(("apply", json.loads(stdin)["spec"]["cluster"]["name"]))

        with mock.patch.object(gate, "output", side_effect=fake_output), \
                mock.patch.object(gate, "switch_wal", side_effect=fake_switch), \
                mock.patch.object(gate, "run", side_effect=fake_run), \
                mock.patch.object(gate, "wait_for", return_value=("completed", "")):
            problems, notes = gate.check_datahub_pg({"NS_CATALOG_DATAHUB": "catalog-datahub"})
        self.assertEqual(problems, [])
        self.assertEqual(events, [("switch", "catalog-datahub", "datahub-pg"),
                                  ("apply", "datahub-pg")])
        self.assertIn("archived through the plugin", notes[1])
        self.assertIn("completed through the plugin", notes[2])

    def test_a_failed_backup_is_a_problem(self):
        with mock.patch.object(gate, "output", side_effect=lambda argv: json.dumps(
                DATAHUB_VALUES if argv[0] == "helm" else datahub_cluster()
                if "cluster/datahub-pg" in argv else datahub_pods("a", "b", "c"))), \
                mock.patch.object(gate, "switch_wal", return_value=(None, "ok")), \
                mock.patch.object(gate, "run"), \
                mock.patch.object(gate, "wait_for", return_value=("failed", "no credentials")):
            problems, _ = gate.check_datahub_pg({"NS_CATALOG_DATAHUB": "catalog-datahub"})
        self.assertEqual(len(problems), 1)
        self.assertIn("no credentials", problems[0])

    def test_it_is_a_command_of_the_gate(self):
        self.assertIn("| check-datahub-pg |", gate.__doc__.strip().splitlines()[-1])



KAFKA_IMAGE = "apache/kafka:4.3.1@sha256:" + "e" * 64
# Stand-ins for Kafka's tools, for KAFKA_CHECK: each logs its arguments; the producer keeps what
# it reads, the consumer prints it back.
FAKE_KAFKA_TOOLS = {
    "kafka-topics.sh": 'echo "topics $*" >> "$FAKE_KAFKA/log"\n',
    "kafka-console-producer.sh": 'echo "producer $*" >> "$FAKE_KAFKA/log"\n'
                                 'cat > "$FAKE_KAFKA/topic"\n',
    # As the real one on the gate (smoke run 39103796549): a line of its own on stdout besides
    # the message.
    "kafka-console-consumer.sh": 'echo "consumer $*" >> "$FAKE_KAFKA/log"\n'
                                 'echo "[2026-10-18 06:29:58,000] WARN a line of the tool"\n'
                                 'cat "$FAKE_KAFKA/topic"\n',
}


def kafka_pods(*pods):
    """A pod list of datahub-kafka: (uid, ready) per pod."""
    return {"items": [{"metadata": {"name": "datahub-kafka-0", "uid": uid},
                       "status": {"conditions": [{"type": "Ready",
                                                  "status": "True" if ready else "False"}]}}
                      for uid, ready in pods]}


class CheckDatahubKafka(unittest.TestCase):
    """ADR-068 §5: from a pod with Kafka's client label, a 4.5 MiB message produced and consumed
    back unchanged; the broker's pod deleted and back ready with the message on its volume."""

    def test_the_check_pod_carries_kafkas_client_label_and_never_the_check_label(self):
        pod = gate.kafka_check_pod("catalog-datahub", KAFKA_IMAGE, "datahub-kafka-check-1")
        self.assertEqual(pod["metadata"]["namespace"], "catalog-datahub")
        labels = pod["metadata"]["labels"]
        self.assertEqual(labels[gate.release.DATAHUB_KAFKA_CLIENT_LABEL], "true")
        self.assertNotIn(gate.network.CHECK_LABEL, labels)
        (container,) = pod["spec"]["containers"]
        # The chart's own image, from its helm release.
        self.assertEqual(container["image"], KAFKA_IMAGE)
        self.assertEqual(container["command"][0], "sleep")
        self.assertEqual(pod["spec"]["restartPolicy"], "Never")
        self.assertFalse(pod["spec"]["enableServiceLinks"])

    def test_the_tools_reach_the_broker_through_its_service(self):
        self.assertEqual(gate.kafka_bootstrap("catalog-datahub"),
                         "datahub-kafka.catalog-datahub.svc.cluster.local:9092")
        self.assertEqual(gate.kafka_exec("catalog-datahub", "p", "write", "t"), [
            "kubectl", "--context", gate.CONTEXT, "-n", "catalog-datahub", "exec", "-i", "p",
            "--", "bash", "-s", "--", "write",
            "datahub-kafka.catalog-datahub.svc.cluster.local:9092", "t"])

    def script(self, *args):
        """Runs KAFKA_CHECK with FAKE_KAFKA_TOOLS; returns (stdouts, the tools' log)."""
        with tempfile.TemporaryDirectory() as fake:
            for name, body in FAKE_KAFKA_TOOLS.items():
                path = os.path.join(fake, name)
                with open(path, "w", encoding="utf-8", newline="\n") as f:
                    f.write("#!/bin/bash\n" + body)
                os.chmod(path, 0o755)
            env = {**os.environ, "KAFKA_BIN": fake, "FAKE_KAFKA": fake}
            outs = [subprocess.run(["bash", "-s", "--", step, "b:9092", "gate-check-1"],
                                   input=gate.KAFKA_CHECK, env=env, capture_output=True,
                                   text=True, check=True).stdout for step in args]
            with open(os.path.join(fake, "log"), encoding="utf-8") as f:
                return outs, f.read().splitlines()

    def test_the_script_sends_4_5_mib_and_reads_the_same_bytes_back(self):
        (write, read), log = self.script("write", "read")
        sent, received = gate.kafka_lines(write)["sent"], gate.kafka_lines(read)["received"]
        self.assertEqual(sent, received)
        size = int(sent[1])
        self.assertGreater(size, 4.5 * 1024 * 1024)
        # Below Datahub's 5 MiB and the broker's message.max.bytes (ADR-068 §2).
        self.assertLess(size, 5 * 1024 * 1024)
        topics, producer, consumer = log
        self.assertIn("--create --topic gate-check-1 --partitions 1 --replication-factor 1",
                      topics)
        # The broker's message.max.bytes is what is proven: the topic sets none of its own.
        self.assertNotIn("max.message.bytes", topics)
        self.assertIn("--producer-property max.request.size=5242880", producer)
        self.assertIn("--from-beginning --max-messages 1", consumer)
        self.assertIn("--bootstrap-server b:9092", consumer)

    def test_the_script_deletes_the_topic(self):
        (out,), log = self.script("delete")
        self.assertEqual(out.strip(), "deleted")
        self.assertEqual(log, ["topics --bootstrap-server b:9092 --delete --topic gate-check-1"])

    def test_the_broker_is_back_when_a_new_pod_is_ready(self):
        back = gate.new_ready_pod({"old"})
        self.assertIsNone(back(kafka_pods()))
        self.assertIsNone(back(kafka_pods(("old", True))))
        self.assertIsNone(back(kafka_pods(("new", False))))
        self.assertEqual(back(kafka_pods(("new", True))), "datahub-kafka-0")

    def fake_cluster(self, sent=("abc", "4718612"), reads=(("abc", "4718612"),) * 2):
        """gate.output answering each call of check_datahub_kafka; records them in order."""
        calls, reads = [], list(reads)

        def fake_output(argv, stdin=None):
            calls.append(argv)
            if argv[0] == "helm":
                return json.dumps({"image": KAFKA_IMAGE})
            if "exec" in argv:
                self.assertEqual(stdin, gate.KAFKA_CHECK)
                step = argv[argv.index("-s") + 2]
                if step == "write":
                    return f"sent {sent[0]} {sent[1]}\n"
                if step == "read":
                    sha, size = reads.pop(0)
                    return f"received {sha} {size}\n"
                return "deleted\n"
            if "get" in argv:
                return json.dumps(kafka_pods(("old", True)))
            return ""
        return calls, fake_output

    def check(self, fake_output, wait=("datahub-kafka-0",)):
        env = {"NS_CATALOG_DATAHUB": "catalog-datahub"}
        with mock.patch.object(gate, "output", side_effect=fake_output), \
                mock.patch.object(gate, "run") as run, \
                mock.patch.object(gate, "wait_for", side_effect=list(wait)) as wait_for, \
                mock.patch.object(gate.subprocess, "run") as cleanup:
            problems, notes = gate.check_datahub_kafka(env)
        return problems, notes, run, wait_for, cleanup

    def test_round_trip_then_restart_then_read_again_then_delete(self):
        calls, fake_output = self.fake_cluster()
        problems, notes, run, wait_for, cleanup = self.check(fake_output)
        self.assertEqual(problems, [])
        # The check pod from the chart's image, in Datahub's namespace.
        (apply_call,) = run.call_args_list
        self.assertIn("apply", apply_call.args[0])
        self.assertEqual(json.loads(apply_call.args[1])["spec"]["containers"][0]["image"],
                         KAFKA_IMAGE)
        steps = [c[c.index("-s") + 2] if "exec" in c else
                 "delete-broker" if "delete" in c else None for c in calls]
        self.assertEqual([s for s in steps if s], ["write", "read", "delete-broker", "read",
                                                    "delete"])
        (delete,) = [c for c in calls if "delete" in c and "exec" not in c]
        self.assertIn("pod/datahub-kafka-0", delete)
        self.assertEqual(wait_for.call_count, 1)
        # The check pod is deleted whatever happened.
        self.assertIn("delete", cleanup.call_args.args[0])
        self.assertEqual(len(notes), 3)
        self.assertIn("4718612 bytes", notes[0])
        self.assertIn("datahub-kafka-0", notes[1])
        self.assertIn("deleted", notes[2])

    def test_a_message_changed_on_the_way_is_a_problem_and_nothing_restarts(self):
        calls, fake_output = self.fake_cluster(reads=(("xyz", "4718612"),))
        problems, _, _, wait_for, cleanup = self.check(fake_output)
        self.assertEqual(len(problems), 1)
        self.assertIn("xyz", problems[0])
        self.assertFalse(any("pod/datahub-kafka-0" in c for c in calls))
        self.assertEqual(wait_for.call_count, 0)
        self.assertTrue(cleanup.called)

    def test_a_message_lost_with_the_restart_is_a_problem(self):
        _, fake_output = self.fake_cluster(reads=(("abc", "4718612"), ("e3b0", "0")))
        problems, _, _, _, _ = self.check(fake_output)
        self.assertEqual(len(problems), 1)
        self.assertIn("after the restart", problems[0])

    def test_it_is_a_command_of_the_gate(self):
        self.assertIn("| check-datahub-kafka |", gate.__doc__.strip().splitlines()[-1])

    def test_the_script_lists_the_topics_one_line_each(self):
        lister = ('echo "topics $*" >> "$FAKE_KAFKA/log"\n'
                  'printf "DataHubUpgradeHistory_v1\\n__consumer_offsets\\n"\n')
        with mock.patch.dict(FAKE_KAFKA_TOOLS, {"kafka-topics.sh": lister}):
            (out,), log = self.script("topics")
        self.assertEqual(log, ["topics --bootstrap-server b:9092 --list"])
        self.assertEqual(out.splitlines(), ["topic DataHubUpgradeHistory_v1",
                                            "topic __consumer_offsets"])
        self.assertEqual(gate.kafka_topics(out), {"DataHubUpgradeHistory_v1",
                                                  "__consumer_offsets"})
        self.assertEqual(gate.kafka_topics("noise\n"), set())



OPENSEARCH_VALUES = {"image": {"repository": "opensearchproject/opensearch",
                               "tag": "2.19.6@sha256:" + "f" * 64}}
OPENSEARCH_IMAGE = "opensearchproject/opensearch:2.19.6@sha256:" + "f" * 64
OPENSEARCH_URL = "http://datahub-opensearch.catalog-datahub.svc.cluster.local:9200"
# A stand-in for curl, for OPENSEARCH_CHECK: logs its arguments and answers as OpenSearch would,
# with the health FAKE_HEALTH and FAKE_HITS hits.
FAKE_CURL = r'''#!/bin/bash
echo "curl $*" >> "$FAKE_OS/log"
case "$*" in
  *http_code*) printf %s "${FAKE_CODE:-200}" ;;
  *_cluster/health*) echo "{\"status\":\"$FAKE_HEALTH\"}" ;;
  *_search*) echo "{\"hits\":{\"total\":{\"value\":$FAKE_HITS},\"hits\":[{\"_source\":{\"check\":\"gate-check-1\"}}]}}" ;;
  *) echo '{"acknowledged":true}' ;;
esac
'''


def opensearch_answer(health="green", hits=1, index="gate-check-1"):
    """OPENSEARCH_CHECK's `read` output, as the check pod prints it."""
    found = [{"_source": {"check": index}}] * hits
    return (f"health {json.dumps({'status': health})}\n"
            f"search {json.dumps({'hits': {'total': {'value': hits}, 'hits': found}})}\n")


def opensearch_pods(*pods):
    """A pod list of datahub-opensearch: (uid, ready) per pod."""
    return {"items": [{"metadata": {"name": "datahub-opensearch-0", "uid": uid},
                       "status": {"conditions": [{"type": "Ready",
                                                  "status": "True" if ready else "False"}]}}
                      for uid, ready in pods]}


class CheckDatahubOpensearch(unittest.TestCase):
    """ADR-070 §5: from a pod with OpenSearch's client label, an index made and a document
    written and found over plain HTTP without credentials; the node's pod deleted and back ready
    with both on its volume; the index deleted."""

    def test_the_check_pod_carries_opensearchs_client_label_and_never_the_check_label(self):
        pod = gate.opensearch_check_pod("catalog-datahub", OPENSEARCH_IMAGE, "c-1")
        self.assertEqual(pod["metadata"]["namespace"], "catalog-datahub")
        labels = pod["metadata"]["labels"]
        self.assertEqual(labels[gate.release.DATAHUB_OPENSEARCH_CLIENT_LABEL], "true")
        self.assertNotIn(gate.release.DATAHUB_KAFKA_CLIENT_LABEL, labels)
        self.assertNotIn(gate.network.CHECK_LABEL, labels)
        (container,) = pod["spec"]["containers"]
        self.assertEqual(container["image"], OPENSEARCH_IMAGE)
        self.assertEqual(container["command"][0], "sleep")
        self.assertEqual(pod["spec"]["restartPolicy"], "Never")

    def test_the_charts_image_comes_from_the_releases_values(self):
        self.assertEqual(gate.opensearch_image(OPENSEARCH_VALUES), OPENSEARCH_IMAGE)

    def test_plain_http_to_the_service_on_9200(self):
        self.assertEqual(gate.opensearch_url("catalog-datahub"), OPENSEARCH_URL)
        self.assertEqual(gate.opensearch_exec("catalog-datahub", "p", "write", "i"), [
            "kubectl", "--context", gate.CONTEXT, "-n", "catalog-datahub", "exec", "-i", "p",
            "--", "bash", "-s", "--", "write", OPENSEARCH_URL, "i"])

    def script(self, step, health="green", hits=1, code="200"):
        """Runs OPENSEARCH_CHECK's `step` with FAKE_CURL; returns (stdout, curl's log)."""
        with tempfile.TemporaryDirectory() as fake:
            path = os.path.join(fake, "curl")
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(FAKE_CURL)
            os.chmod(path, 0o755)
            env = {**os.environ, "CURL": path, "FAKE_OS": fake, "FAKE_HEALTH": health,
                   "FAKE_HITS": str(hits), "FAKE_CODE": code}
            out = subprocess.run(["bash", "-s", "--", step, OPENSEARCH_URL, "gate-check-1"],
                                 input=gate.OPENSEARCH_CHECK, env=env, capture_output=True,
                                 text=True, check=True).stdout
            with open(os.path.join(fake, "log"), encoding="utf-8") as f:
                return out, f.read().splitlines()

    def test_write_makes_one_shard_and_no_replica_waits_for_green_then_writes_one_document(self):
        out, log = self.script("write")
        create, health, doc = log
        self.assertIn(f"-X PUT {OPENSEARCH_URL}/gate-check-1 ", create + " ")
        self.assertIn('"number_of_shards":1', create)
        self.assertIn('"number_of_replicas":0', create)
        self.assertIn(f"{OPENSEARCH_URL}/_cluster/health/gate-check-1?wait_for_status=green",
                      health)
        self.assertIn(f"-X PUT {OPENSEARCH_URL}/gate-check-1/_doc/1?refresh=true", doc)
        self.assertIn('"check":"gate-check-1"', doc)
        self.assertEqual(gate.opensearch_lines(out)["health"], {"status": "green"})

    def test_no_credential_and_no_tls_option_reaches_curl(self):
        # ADR-070 §2: the security plugin off, so plain HTTP answers without a login.
        _, log = self.script("write")
        for line in log:
            with self.subTest(line=line):
                self.assertNotRegex(line, r" (-u|--user|-k|--insecure|--cacert) ")
                self.assertNotIn("https://", line)
                self.assertIn("--fail-with-body", line)

    def test_read_waits_for_green_and_searches_for_the_document(self):
        out, log = self.script("read")
        health, search = log
        self.assertIn("wait_for_status=green", health)
        self.assertIn(f"{OPENSEARCH_URL}/gate-check-1/_search?q=check:gate-check-1", search)
        found = gate.opensearch_lines(out)
        self.assertEqual(found["health"], {"status": "green"})
        self.assertTrue(gate.opensearch_found(found, "gate-check-1"))

    def test_the_script_deletes_the_index(self):
        out, log = self.script("delete")
        self.assertEqual(out.strip(), "deleted")
        (delete,) = log
        self.assertIn(f"-X DELETE {OPENSEARCH_URL}/gate-check-1", delete)

    def test_a_document_is_found_only_once_and_only_when_green(self):
        lines = gate.opensearch_lines
        self.assertTrue(gate.opensearch_found(lines(opensearch_answer()), "gate-check-1"))
        for answer in (opensearch_answer(health="yellow"), opensearch_answer(hits=0),
                       opensearch_answer(hits=2), opensearch_answer(index="other"), ""):
            with self.subTest(answer=answer):
                self.assertFalse(gate.opensearch_found(lines(answer), "gate-check-1"))

    def fake_cluster(self, reads=({},) * 2):
        """gate.output answering each call of check_datahub_opensearch, each read as `reads`
        say (opensearch_answer's arguments) for the index it names; records them in order."""
        calls, reads = [], list(reads)

        def fake_output(argv, stdin=None):
            calls.append(argv)
            if argv[0] == "helm":
                return json.dumps(OPENSEARCH_VALUES)
            if "exec" in argv:
                self.assertEqual(stdin, gate.OPENSEARCH_CHECK)
                step = argv[argv.index("-s") + 2]
                if step == "write":
                    return f"health {json.dumps({'status': 'green'})}\n"
                if step == "read":
                    return opensearch_answer(index=argv[-1], **reads.pop(0))
                return "deleted\n"
            if "get" in argv:
                return json.dumps(opensearch_pods(("old", True)))
            return ""
        return calls, fake_output

    def check(self, fake_output, wait=("datahub-opensearch-0",)):
        env = {"NS_CATALOG_DATAHUB": "catalog-datahub"}
        with mock.patch.object(gate, "output", side_effect=fake_output), \
                mock.patch.object(gate, "run") as run, \
                mock.patch.object(gate, "wait_for", side_effect=list(wait)) as wait_for, \
                mock.patch.object(gate.subprocess, "run") as cleanup:
            problems, notes = gate.check_datahub_opensearch(env)
        return problems, notes, run, wait_for, cleanup

    def test_round_trip_then_restart_then_read_again_then_delete(self):
        calls, fake_output = self.fake_cluster()
        problems, notes, run, wait_for, cleanup = self.check(fake_output)
        self.assertEqual(problems, [])
        (apply_call,) = run.call_args_list
        self.assertIn("apply", apply_call.args[0])
        self.assertEqual(json.loads(apply_call.args[1])["spec"]["containers"][0]["image"],
                         OPENSEARCH_IMAGE)
        steps = [c[c.index("-s") + 2] if "exec" in c else
                 "delete-node" if "delete" in c else None for c in calls]
        self.assertEqual([s for s in steps if s], ["write", "read", "delete-node", "read",
                                                    "delete"])
        (delete,) = [c for c in calls if "delete" in c and "exec" not in c]
        self.assertIn("pod/datahub-opensearch-0", delete)
        (get,) = {tuple(c) for c in calls if "get" in c and "pods" in c}
        self.assertIn("app.kubernetes.io/name=opensearch,"
                      "app.kubernetes.io/instance=datahub-opensearch", get)
        self.assertEqual(wait_for.call_count, 1)
        self.assertEqual(wait_for.call_args.kwargs["timeout"], gate.OPENSEARCH_RESTART_TIMEOUT)
        self.assertIn("delete", cleanup.call_args.args[0])
        self.assertEqual(len(notes), 3)
        self.assertIn("plain HTTP", notes[0])
        self.assertIn("datahub-opensearch-0", notes[1])
        self.assertIn("deleted", notes[2])

    def test_a_document_not_found_is_a_problem_and_nothing_restarts(self):
        calls, fake_output = self.fake_cluster(reads=({"hits": 0},))
        problems, _, _, wait_for, cleanup = self.check(fake_output)
        self.assertEqual(len(problems), 1)
        self.assertIn("gate-check-", problems[0])
        self.assertFalse(any("pod/datahub-opensearch-0" in c for c in calls))
        self.assertEqual(wait_for.call_count, 0)
        self.assertTrue(cleanup.called)

    def test_a_document_lost_with_the_restart_is_a_problem(self):
        _, fake_output = self.fake_cluster(reads=({}, {"hits": 0}))
        problems, _, _, _, _ = self.check(fake_output)
        self.assertEqual(len(problems), 1)
        self.assertIn("after the restart", problems[0])

    def test_it_is_a_command_of_the_gate(self):
        self.assertIn("| check-datahub-opensearch |", gate.__doc__.strip().splitlines()[-1])

    def test_exists_prints_the_http_status_of_the_index_or_alias(self):
        for code in ("200", "404"):
            with self.subTest(code=code):
                out, log = self.script("exists", code=code)
                (status,) = log
                self.assertIn(f"{OPENSEARCH_URL}/gate-check-1", status)
                self.assertNotIn("--fail-with-body", status)
                self.assertEqual(gate.opensearch_lines(out), {"exists": int(code)})


DATAHUB_SECRET = "SystemSecret0123456789abcdefABCD"
DATAHUB_PASSWORD = "RootPassword0123456789abcdefABCD"
PYTHON_IMAGE = "python:3.12.14-slim@sha256:" + "f" * 64


def b64(text):
    import base64
    return base64.b64encode(text.encode()).decode()


def datahub_jobs(*complete):
    """A job list of the two system-update Jobs, each Complete or not."""
    names = ("datahub-system-update", "datahub-system-update-nonblk")
    return {"items": [{"metadata": {"name": name},
                       "status": {"conditions": [{"type": "Complete" if done else "Failed",
                                                  "status": "True"}]}}
                      for name, done in zip(names, complete)]}


class CheckDatahub(unittest.TestCase):
    """ADR-071 §8, ADR-072 §4: system-update done, a dataset through every store, the login."""

    def test_both_system_update_jobs_must_have_completed(self):
        self.assertEqual(gate.system_update_problems(datahub_jobs(True, True)), [])
        self.assertEqual(gate.system_update_problems(datahub_jobs(True, False)), [
            "Job datahub-system-update-nonblk in Datahub's namespace has not completed"])
        self.assertEqual(gate.system_update_problems(datahub_jobs(True)), [
            "Job datahub-system-update-nonblk in Datahub's namespace has not completed"])

    def test_the_root_password_is_the_user_props_line_for_datahub(self):
        self.assertEqual(gate.root_password(f"datahub:{DATAHUB_PASSWORD}\n"), DATAHUB_PASSWORD)
        for text in ("admin:x", "datahub:", "datahub"):
            with self.subTest(text=text):
                self.assertIsNone(gate.root_password(text))

    def test_a_secrets_value_is_decoded_from_its_data(self):
        secret = {"data": {"system_client_secret": b64(DATAHUB_SECRET)}}
        self.assertEqual(gate.secret_value(secret, "system_client_secret"), DATAHUB_SECRET)

    def test_the_urls_are_the_services_upstreams_chart_names(self):
        self.assertEqual(gate.datahub_urls("catalog-datahub"), (
            "http://datahub-datahub-gms.catalog-datahub.svc.cluster.local:8080",
            "http://datahub-datahub-frontend.catalog-datahub.svc.cluster.local:9002"))

    def test_the_check_pod_runs_the_script_from_its_arguments_and_never_the_check_label(self):
        argv = gate.datahub_check_command("catalog-datahub", PYTHON_IMAGE, "c-1",
                                          ["gms", "http://g", "n", "180"],
                                          labels={"lakehouse/datahub-gms-client": "true"})
        with open(gate.DATAHUB_CHECK, encoding="utf-8") as f:
            script = f.read()
        self.assertEqual(argv, [
            "kubectl", "--context", gate.CONTEXT, "-n", "catalog-datahub", "run", "c-1",
            "--labels=lakehouse/datahub-gms-client=true", f"--image={PYTHON_IMAGE}",
            "--restart=Never", "--rm", "-i", "--quiet", "--pod-running-timeout=3m",
            "--command", "--", "python3", "-c", script, "gms", "http://g", "n", "180"])
        self.assertNotIn(gate.CHECK_POD_LABEL, argv)
        unlabelled = gate.datahub_check_command("ns", PYTHON_IMAGE, "c-2", ["login", "u"])
        self.assertFalse([a for a in unlabelled if a.startswith("--labels")])

    def test_ok_lines_are_notes_and_fail_lines_problems(self):
        self.assertEqual(gate.datahub_lines("ok   one\nFAIL two: why\nnoise\n"),
                         (["two: why"], ["one"]))

    def fake_cluster(self, jobs=(True, True), gms_out="ok   GMS answers /health\n",
                     login_out="ok   the frontend signs in datahub\n"):
        """gate.output answering check_datahub's reads, subprocess.run its two pods; both
        record their calls."""
        reads, pods = [], []
        secrets = {"datahub-auth-secrets": {"data": {"system_client_secret":
                                                     b64(DATAHUB_SECRET)}},
                   "datahub-root-user": {"data": {"user.props":
                                                  b64(f"datahub:{DATAHUB_PASSWORD}")}}}

        def fake_output(argv, stdin=None):
            reads.append((argv, stdin))
            if "jobs" in argv:
                return json.dumps(datahub_jobs(*jobs))
            if "secret" in argv:
                return json.dumps(secrets[argv[argv.index("secret") + 1]])
            return ""

        def fake_run(argv, input=None, **kwargs):
            pods.append((argv, input))
            out = gms_out if "gms" in argv else login_out
            return subprocess.CompletedProcess(argv, 1 if "FAIL" in out else 0, out, "")
        return reads, pods, fake_output, fake_run

    def check(self, fake_output, fake_run):
        env = {"NS_CATALOG_DATAHUB": "catalog-datahub",
               "INGRESS_NAMESPACE_LABEL": "lakehouse-gate/caller=ingress"}
        pins = {"standin_images": {"recorder": {"image": PYTHON_IMAGE}}}
        with mock.patch.object(gate, "output", side_effect=fake_output), \
                mock.patch.object(gate.subprocess, "run", side_effect=fake_run):
            return gate.check_datahub(env, pins)

    def test_gms_from_its_client_and_the_login_from_the_ingress_namespace(self):
        reads, pods, fake_output, fake_run = self.fake_cluster()
        problems, notes = self.check(fake_output, fake_run)
        self.assertEqual(problems, [])
        self.assertIn("system-update", notes[0])
        (apply,) = [(argv, stdin) for argv, stdin in reads if "apply" in argv]
        namespace = json.loads(apply[1])
        self.assertEqual(namespace["metadata"], {
            "name": "gate-net-ingress", "labels": {"lakehouse-gate/caller": "ingress"}})
        (gms, gms_in), (login, login_in) = pods
        self.assertEqual(gms[gms.index("-n") + 1], "catalog-datahub")
        self.assertIn("--labels=lakehouse/datahub-gms-client=true", gms)
        self.assertEqual(gms[-4:], [
            "gms", "http://datahub-datahub-gms.catalog-datahub.svc.cluster.local:8080",
            f"gate_check_{os.getpid()}", str(gate.DATAHUB_SEARCH_SECONDS)])
        self.assertEqual(gms_in, DATAHUB_SECRET + "\n")
        self.assertEqual(login[login.index("-n") + 1], "gate-net-ingress")
        self.assertFalse([a for a in login if a.startswith("--labels")])
        self.assertEqual(login[-2:], [
            "login", "http://datahub-datahub-frontend.catalog-datahub.svc.cluster.local:9002"])
        self.assertEqual(login_in, DATAHUB_PASSWORD + "\n")
        for argv, _ in pods + reads:
            self.assertNotIn(DATAHUB_SECRET, " ".join(argv))
            self.assertNotIn(DATAHUB_PASSWORD, " ".join(argv))
        self.assertIn("GMS answers /health", notes)
        self.assertIn("the frontend signs in datahub", notes)

    def test_system_update_not_done_stops_before_any_pod(self):
        _, pods, fake_output, fake_run = self.fake_cluster(jobs=(False, True))
        problems, _ = self.check(fake_output, fake_run)
        self.assertEqual(problems, [
            "Job datahub-system-update in Datahub's namespace has not completed"])
        self.assertEqual(pods, [])

    def test_a_failed_line_from_either_pod_is_a_problem(self):
        _, _, fake_output, fake_run = self.fake_cluster(
            gms_out="ok   GMS answers /health\nFAIL found by search: not within 180 s\n",
            login_out="FAIL the frontend refuses the image's datahub:datahub: HTTP 200\n")
        problems, _ = self.check(fake_output, fake_run)
        self.assertEqual(problems, ["found by search: not within 180 s",
                                    "the frontend refuses the image's datahub:datahub: HTTP 200"])

    def test_a_pod_that_failed_without_a_line_is_a_problem_with_the_secret_masked(self):
        _, _, fake_output, _ = self.fake_cluster()

        def fake_run(argv, input=None, **kwargs):
            return subprocess.CompletedProcess(argv, 1, "", f"error: {DATAHUB_SECRET} timed out")
        problems, _ = self.check(fake_output, fake_run)
        self.assertEqual(len(problems), 2)
        for problem in problems:
            self.assertNotIn(DATAHUB_SECRET, problem)
            self.assertIn("<redacted>", problem)

    def test_it_is_a_command_of_the_gate(self):
        self.assertIn("| check-datahub |", gate.__doc__.strip().splitlines()[-1])


DATAHUB_TOPICS = ("DataHubUpgradeHistory_v1", "MetadataChangeLog_Versioned_v1")
OPENSEARCH_CLAIM = "datahub-opensearch-datahub-opensearch-0"
KAFKA_CLAIM = "data-datahub-kafka-0"


def stateful_pod(name, claim):
    """A StatefulSet pod as kubectl reads it: one claim among its volumes."""
    return {"metadata": {"name": name, "uid": "old"},
            "spec": {"volumes": [{"name": "config", "configMap": {"name": "c"}},
                                 {"name": "data", "persistentVolumeClaim": {"claimName": claim}}]}}


class FakeRecovery:
    """The gate's cluster as check_datahub_recovery sees it, through gate.output,
    gate.wait_for, gate.subprocess.run (check.py's pods, the logs and the clean-up) and
    release.datahub. Datasets are in datahub-pg, and in the index only once the change log or a
    restore put them there. `broken` names one thing to spoil; `events` is what happened, in
    order."""

    def __init__(self, test, broken=None):
        self.test, self.broken, self.events = test, broken, []
        self.index, self.topics = 200, set(DATAHUB_TOPICS) | {"__consumer_offsets"}
        self.claims = {OPENSEARCH_CLAIM: "os-1", KAFKA_CLAIM: "kafka-1"}
        self.datasets, self.indexed, self.pods = set(), set(), []
        # What the timeline reads back: Datahub's Helm revision, its Jobs' times.
        self.revision, self.timeline = 1, []

    def read(self, argv):
        """Helm's history and the Jobs, read for the timeline only (ADR-077 §3)."""
        if self.broken == "unreadable":
            return subprocess.CompletedProcess(argv, 1, "", "connection refused")
        if argv[0] == "helm":
            self.test.assertEqual(argv[argv.index("history") + 1:],
                                  ["datahub", "--max", "1", "-o", "json"])
            return subprocess.CompletedProcess(argv, 0, json.dumps([{
                "revision": self.revision, "status": "deployed",
                "updated": f"2026-10-19T00:0{self.revision}:00.123456789+00:00"}]), "")
        kind = argv[argv.index("get") + 1]
        names = gate.DATAHUB_JOBS if kind == "jobs" else [argv[argv.index("get") + 2]]
        items = [{"metadata": {"name": name}, "status": {
            "startTime": f"2026-10-19T00:0{self.revision}:1{i}Z",
            "completionTime": f"2026-10-19T00:0{self.revision}:2{i}Z"}}
            for i, name in enumerate(names)]
        # Another Job in the namespace stays off the timeline.
        items.append({"metadata": {"name": "other"},
                      "status": {"startTime": "2026-10-19T00:00:00Z"}})
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"items": items} if kind == "jobs" else items[0]), "")

    def output(self, argv, stdin=None):
        if argv[0] == "helm":
            release = argv[argv.index("values") + 1]
            return json.dumps({"image": KAFKA_IMAGE} if release == "datahub-kafka"
                              else OPENSEARCH_VALUES)
        verb = argv[argv.index("-n") + 2]
        if verb == "exec":
            step = argv[argv.index("-s") + 2]
            if step == "exists":
                self.test.assertEqual(stdin, gate.OPENSEARCH_CHECK)
                self.test.assertEqual(argv[-1], "datasetindex_v2")
                return f"exists {self.index}\n"
            self.test.assertEqual((step, stdin), ("topics", gate.KAFKA_CHECK))
            return "".join(f"topic {t}\n" for t in sorted(self.topics))
        if verb == "get":
            kind, name = argv[argv.index("get") + 1:argv.index("get") + 3]
            if kind == "secret":
                return json.dumps({"data": {"system_client_secret": b64(DATAHUB_SECRET)}})
            if kind == "pod":
                claim = OPENSEARCH_CLAIM if "opensearch" in name else KAFKA_CLAIM
                return json.dumps(stateful_pod(name, claim))
            if kind == "pvc":
                return json.dumps({"metadata": {"name": name, "uid": self.claims[name]}})
            return json.dumps({"items": [{"metadata": {"name": "p-0", "uid": "old"}}]})
        if verb == "delete" and argv[argv.index("delete") + 1] == "pvc":
            claim = argv[argv.index("pvc") + 1]
            self.events.append(f"delete claim {claim}")
            if self.broken != "old-claim":
                self.claims[claim] += "-new"
            if claim == OPENSEARCH_CLAIM and self.broken != "index-kept":
                self.index, self.indexed = 404, set()
            if claim == KAFKA_CLAIM and self.broken != "topics-kept":
                self.topics = set()
        elif verb == "delete" and "-l" in argv:
            self.test.assertIn(gate.GMS_POD_LABELS, argv)
            self.events.append("restart gms")
        elif verb == "delete":
            self.events.append(f"delete {argv[argv.index('delete') + 1]}")
        elif verb == "create":
            self.test.assertIn(
                "--from=cronjob/datahub-datahub-restore-indices-job-template", argv)
            self.events.append("restore-indices")
            if self.broken != "restore-fails":
                self.indexed = set(self.datasets)
        return ""

    def wait_for(self, argv, done, what, timeout=None):
        if "job" in argv:
            self.test.assertEqual(timeout, gate.RESTORE_INDICES_TIMEOUT)
            return "failed" if self.broken == "restore-fails" else "succeeded"
        return "p-0"

    def datahub(self, env, run):
        self.test.assertIs(run, gate.release.call)
        self.events.append("install")
        if self.broken == "install":
            return ["helm upgrade datahub in catalog-datahub failed: timed out"], []
        if self.index == 404 and self.broken != "no-index":
            self.index = 200
        if not self.topics and self.broken != "no-topics":
            self.topics = set(DATAHUB_TOPICS)
        self.revision += 1
        return [], ["Step 1: Datahub in catalog-datahub is installed (ADR-071)."]

    def run(self, argv, input=None, **kwargs):
        if "history" in argv or "get" in argv:
            return self.read(argv)
        if "python3" in argv:
            self.pods.append((argv, input))
            step, _, *rest = argv[argv.index("-c") + 2:]
            names = [r for r in rest if not r.isdigit()]
            self.events.append(" ".join([step, *names]))
            lines = []
            for name in names:
                if step == "write":
                    self.datasets.add(name)
                    if self.index == 200 and "MetadataChangeLog_Versioned_v1" in self.topics:
                        self.indexed.add(name)
                    lines.append(f"ok   {name} written")
                elif step == "read":
                    lines.append(("ok  " if name in self.datasets else "FAIL") + f" {name} read")
                elif step == "search":
                    lines.append(("ok  " if name in self.indexed else "FAIL") + f" {name} found")
                else:
                    self.datasets.discard(name)
                    lines.append(f"ok   {name} hard-deleted")
            out = "\n".join(lines) + "\n"
            return subprocess.CompletedProcess(argv, 1 if "FAIL" in out else 0, out, "")
        if "logs" in argv:
            return subprocess.CompletedProcess(argv, 0, "RestoreIndices failed: no route\n", "")
        self.events.append("clean up " + " ".join(a for a in argv if a.startswith(("datahub-",
                                                                                 "job"))))
        return subprocess.CompletedProcess(argv, 0, "", "")


class CheckDatahubRecovery(unittest.TestCase):
    """ADR-071 §8: Datahub's indices rebuilt after OpenSearch's volume is lost, its topics after
    Kafka's; a dataset written before both still read and found, another written after."""

    ENV = {"NS_CATALOG_DATAHUB": "catalog-datahub", "KUBE_CONTEXT": "kind-lakehouse-gate"}
    PINS = {"standin_images": {"recorder": {"image": PYTHON_IMAGE}}}

    def check(self, cluster, env=None):
        with mock.patch.object(gate, "output", side_effect=cluster.output), \
                mock.patch.object(gate, "run") as run, \
                mock.patch.object(gate, "wait_for", side_effect=cluster.wait_for), \
                mock.patch.object(gate.subprocess, "run", side_effect=cluster.run), \
                mock.patch.object(gate.release, "datahub", side_effect=cluster.datahub):
            problems, notes = gate.check_datahub_recovery(env or self.ENV, self.PINS,
                                                          cluster.timeline)
        return problems, notes, run

    def marks(self, cluster):
        return [text for _, text in cluster.timeline]

    def test_each_phase_is_on_the_timeline_in_order(self):
        # ADR-077 §3, from the check's own clock and from Helm and the cluster.
        cluster = FakeRecovery(self)
        problems, _, _ = self.check(cluster)
        self.assertEqual(problems, [])
        first, second, pid = *self.names(), os.getpid()
        topics = "topics DataHubUpgradeHistory_v1, MetadataChangeLog_Versioned_v1"

        def install(revision):
            return ["Datahub's install step started", "Datahub's install step ended",
                    f"helm datahub revision {revision} deployed",
                    "job datahub-system-update started", "job datahub-system-update complete",
                    "job datahub-system-update-nonblk started",
                    "job datahub-system-update-nonblk complete"]

        def lost(claim, pod):
            return [f"claim {claim} and pod {pod} being deleted",
                    f"claim {claim} and pod {pod} deleted", "p-0 ready"]

        def step(*args):
            return [f"{' '.join(args)} started", f"{' '.join(args)} passed"]
        restore = f"job datahub-restore-indices-{pid}"
        self.assertEqual(self.marks(cluster), [
            "check pods created", "check pods ready",
            *step("write", first), *step("search", first),
            "index datasetindex_v2 answers 200",
            *lost(OPENSEARCH_CLAIM, "datahub-opensearch-0"),
            "index datasetindex_v2 answers 404",
            *install(2),
            "index datasetindex_v2 answers 200",
            f"{restore} created", f"{restore} started", f"{restore} complete",
            f"{restore} succeeded",
            *step("search", first),
            f"{topics}: 2 of 2 present",
            *lost(KAFKA_CLAIM, "datahub-kafka-0"),
            f"{topics}: 0 of 2 present",
            *install(3),
            f"{topics}: 2 of 2 present",
            "GMS's pod being deleted", "GMS's pod deleted", "GMS's pod p-0 ready",
            *step("read", first), *step("write", second), *step("search", second),
            "clean-up started", *step("delete", first, second), "clean-up ended"])
        # Helm's and the Jobs' times are theirs, not the check's.
        times = dict((text, at) for at, text in cluster.timeline)
        self.assertEqual(times["helm datahub revision 3 deployed"],
                         "2026-10-19T00:03:00.123456789+00:00")
        self.assertEqual(times["job datahub-system-update-nonblk complete"],
                         "2026-10-19T00:03:21Z")
        self.assertEqual(times[f"{restore} complete"], "2026-10-19T00:02:20Z")

    def test_a_timeline_that_cannot_be_read_is_said_and_never_fails_the_check(self):
        cluster = FakeRecovery(self, broken="unreadable")
        problems, _, _ = self.check(cluster)
        self.assertEqual(problems, [])
        marks = self.marks(cluster)
        for what in ("Datahub's Helm revision", "Datahub's system-update Jobs",
                     f"job datahub-restore-indices-{os.getpid()}"):
            self.assertIn(f"{what} not read (connection refused)", marks)

    def test_a_check_that_stops_still_has_its_timeline_so_far(self):
        cluster = FakeRecovery(self, broken="install")
        problems, _, _ = self.check(cluster)
        self.assertEqual(len(problems), 1)
        marks = self.marks(cluster)
        self.assertEqual(marks[-4:], ["clean-up started",
                                      f"delete {self.names()[0]} started",
                                      f"delete {self.names()[0]} passed", "clean-up ended"])
        self.assertIn("Datahub's install step ended", marks)
        self.assertNotIn("helm datahub revision 2 deployed", marks)

    def test_the_timelines_lines_from_a_recorded_set_of_times(self):
        marks = [("2026-10-19T00:00:40Z", "b"), ("2026-10-19T00:00:05+00:00", "a"),
                 ("2026-10-19T00:03:20.123456789+00:00", "c")]
        self.assertEqual(gate.recovery_timeline(marks, "2026-10-19T00:00:00+00:00"), [
            "Recovery timeline, seconds after check-datahub-recovery started (and since the "
            "line before):",
            "-   5 s (+ 5)  a", "-  40 s (+35)  b", "- 200 s (+160)  c"])
        self.assertEqual(gate.recovery_timeline([], "2026-10-19T00:00:00+00:00"),
                         ["Recovery timeline: nothing recorded after check-datahub-recovery "
                          "started."])
        lines = gate.recovery_timeline([("not a time", "x")], "2026-10-19T00:00:00+00:00")
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("Recovery timeline: not measured ("), lines[0])

    def main(self, outcome):
        def check(env, pins, timeline):
            timeline.append(("2026-10-19T00:00:05Z", "check pods ready"))
            return outcome(timeline)
        with mock.patch.object(gate, "check_datahub_recovery", side_effect=check), \
                mock.patch.object(gate.release, "load_env", return_value=self.ENV), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            try:
                code = gate.main(["gate.py", "check-datahub-recovery"])
            except SystemExit as e:
                code = f"exit {e.code}"
        return code, out.getvalue()

    def test_the_command_prints_its_timeline_after_its_notes_passed_or_failed(self):
        for problems, want in (([], 0), (["Index datasetindex_v2 answers 404"], 1)):
            with self.subTest(problems=problems):
                code, out = self.main(lambda _: (problems, ["a note"]))
                self.assertEqual(code, want)
                self.assertLess(out.index("a note"), out.index("Recovery timeline, seconds"))
                self.assertIn("  check pods ready", out)

    def test_a_check_that_stops_the_gate_prints_its_timeline_first(self):
        def stop(_):
            sys.exit("GMS back ready: not done after 900 s")
        code, out = self.main(stop)
        self.assertEqual(code, "exit GMS back ready: not done after 900 s")
        self.assertIn("Recovery timeline, seconds", out)

    def test_a_timeline_that_cannot_be_read_never_fails_the_command(self):
        def unreadable(timeline):
            timeline.append(("not a time", "x"))
            return [], []
        code, out = self.main(unreadable)
        self.assertEqual(code, 0)
        self.assertIn("Recovery timeline: not measured (", out)

    def names(self):
        pid = os.getpid()
        return f"gate_recovery_a_{pid}", f"gate_recovery_b_{pid}"

    def test_the_claim_is_the_pods_one_persistent_volume_claim(self):
        self.assertEqual(gate.claim_of(stateful_pod("datahub-kafka-0", KAFKA_CLAIM)),
                         KAFKA_CLAIM)
        self.assertIsNone(gate.claim_of({"spec": {"volumes": [{"name": "c", "emptyDir": {}}]}}))

    def test_a_job_is_done_by_its_condition_not_by_a_failed_try(self):
        def job(*conditions, failed=0):
            return {"status": {"failed": failed, "conditions": [
                {"type": t, "status": "True"} for t in conditions]}}
        self.assertEqual(gate.job_finished(job("Complete", failed=1)), "succeeded")
        self.assertEqual(gate.job_finished(job("Failed", failed=7)), "failed")
        self.assertIsNone(gate.job_finished(job(failed=1)))
        self.assertIsNone(gate.job_finished({}))

    def test_gms_pods_are_the_charts_for_release_datahub(self):
        self.assertEqual(gate.GMS_POD_LABELS,
                         "app.kubernetes.io/name=datahub-gms,app.kubernetes.io/instance=datahub")

    def test_both_volumes_lost_and_rebuilt_in_order(self):
        cluster = FakeRecovery(self)
        problems, notes, run = self.check(cluster)
        self.assertEqual(problems, [])
        first, second = self.names()
        self.assertEqual(cluster.events, [
            f"write {first}", f"search {first}",
            f"delete claim {OPENSEARCH_CLAIM}", "delete pod/datahub-opensearch-0", "install",
            "restore-indices", f"clean up job datahub-restore-indices-{os.getpid()}",
            f"search {first}",
            f"delete claim {KAFKA_CLAIM}", "delete pod/datahub-kafka-0", "install",
            "restart gms", f"read {first}", f"write {second}", f"search {second}",
            f"delete {first} {second}",
            f"clean up datahub-kafka-recovery-{os.getpid()} "
            f"datahub-opensearch-recovery-{os.getpid()}"])
        self.assertEqual(cluster.datasets, set())
        # Both check pods from their charts' images, each with its own client label only.
        pods = [json.loads(c.args[1]) for c in run.call_args_list]
        self.assertEqual([p["spec"]["containers"][0]["image"] for p in pods],
                         [KAFKA_IMAGE, OPENSEARCH_IMAGE])
        text = "\n".join(notes)
        for want in (OPENSEARCH_CLAIM, KAFKA_CLAIM, "datasetindex_v2", "restore-indices",
                     "DataHubUpgradeHistory_v1", "GMS"):
            self.assertIn(want, text)

    def test_every_check_pod_is_gmss_client_with_the_secret_on_stdin_only(self):
        cluster = FakeRecovery(self)
        self.check(cluster)
        for argv, stdin in cluster.pods:
            self.assertEqual(argv[argv.index("-n") + 1], "catalog-datahub")
            self.assertIn("--labels=lakehouse/datahub-gms-client=true", argv)
            self.assertNotIn(gate.CHECK_POD_LABEL, argv)
            self.assertIn("http://datahub-datahub-gms.catalog-datahub.svc.cluster.local:8080",
                          argv)
            self.assertNotIn(DATAHUB_SECRET, " ".join(argv))
            self.assertEqual(stdin, DATAHUB_SECRET + "\n")
        searches = [a for a, _ in cluster.pods if a[a.index("-c") + 2] == "search"]
        self.assertEqual({a[-1] for a in searches}, {str(gate.DATAHUB_SEARCH_SECONDS)})

    def test_only_on_the_gates_own_cluster(self):
        cluster = FakeRecovery(self)
        problems, notes, run = self.check(cluster, env={**self.ENV, "KUBE_CONTEXT": "other"})
        self.assertEqual(len(problems), 1)
        self.assertIn("ADR-004", problems[0])
        self.assertEqual((cluster.events, notes, run.call_count), ([], [], 0))

    def assert_stopped_after(self, broken, last, why):
        cluster = FakeRecovery(self, broken=broken)
        problems, _, _ = self.check(cluster)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(why, problems[0])
        first, second = self.names()
        deleted = [e for e in cluster.events if e.startswith(f"delete {first}")]
        self.assertEqual(cluster.events[-3:], [last, *deleted, cluster.events[-1]])
        self.assertTrue(cluster.events[-1].startswith("clean up datahub-kafka-recovery"))
        self.assertEqual(cluster.datasets, set())
        return problems[0]

    def test_a_volume_that_came_back_on_its_old_claim_is_a_problem(self):
        self.assert_stopped_after("old-claim", "delete pod/datahub-opensearch-0", "old claim")

    def test_an_index_kept_after_the_loss_is_a_problem(self):
        self.assert_stopped_after("index-kept", "delete pod/datahub-opensearch-0", "404")

    def test_a_failed_install_is_a_problem(self):
        problem = self.assert_stopped_after("install", "install", "helm upgrade datahub")
        self.assertIn("timed out", problem)

    def test_an_index_system_update_did_not_recreate_is_a_problem(self):
        self.assert_stopped_after("no-index", "install", "datasetindex_v2")

    def test_a_failed_restore_is_a_problem_with_its_log(self):
        problem = self.assert_stopped_after("restore-fails", "restore-indices", "failed")
        self.assertIn("RestoreIndices failed: no route", problem)

    def test_topics_kept_after_the_loss_are_a_problem(self):
        self.assert_stopped_after("topics-kept", "delete pod/datahub-kafka-0",
                                  "DataHubUpgradeHistory_v1")

    def test_topics_system_update_did_not_recreate_are_a_problem(self):
        self.assert_stopped_after("no-topics", "install", "MetadataChangeLog_Versioned_v1")

    def test_it_is_a_command_of_the_gate(self):
        self.assertIn("| check-datahub-recovery |", gate.__doc__.strip().splitlines()[-1])


ROOT_ID, ROOT_SECRET = "rootid0123456789", "RootSecretValue0123456789abcdefg"
INGEST_ID, INGEST_SECRET = "ingestid01234567", "IngestSecretValue0123456789abcde"
INGEST_ENV = {"NS_CATALOG_DATAHUB": "catalog-datahub", "NS_CATALOG_POLARIS": "catalog-polaris",
              "S3_ENDPOINT": "http://recorder.standins.svc.cluster.local:8333",
              "S3_REGION": "us-east-1", "POLARIS_WAREHOUSE_BUCKET": "polaris-warehouse"}
INGEST_LOG = ("catalog gate_ingest_catalog: ingesting\n"
              "Pipeline finished successfully; produced 9 events in 21.3 seconds.\n"
              "catalog gate_ingest_catalog: passed\n")


def ingest_job(state=None):
    conditions = {"succeeded": [{"type": "Complete", "status": "True"}],
                  "failed": [{"type": "Failed", "status": "True"}]}.get(state, [])
    return {"status": {"startTime": "2026-10-19T10:00:00Z",
                       "completionTime": "2026-10-19T10:01:30Z", "conditions": conditions}}


class CheckDatahubIngestion(unittest.TestCase):
    """ADR-076 §6: seed, one ingestion run from the CronJob, what Datahub holds, what
    datahub_ingest may not do, then clean-up."""

    def fake_cluster(self, job_states=("succeeded",), pod_out=None, log=INGEST_LOG):
        """gate.output answering the reads and the Job's creation, subprocess.run the pods and
        the log; every call recorded."""
        reads, pods = [], []
        states = list(job_states)
        secrets = {"polaris-root": {"data": {"clientId": b64(ROOT_ID),
                                             "clientSecret": b64(ROOT_SECRET)}},
                   "datahub-auth-secrets": {"data": {"system_client_secret":
                                                     b64(DATAHUB_SECRET)}},
                   "datahub-polaris-principal": {"data": {"clientId": b64(INGEST_ID),
                                                          "clientSecret": b64(INGEST_SECRET)}}}
        pod_out = dict(pod_out or {})

        def fake_output(argv, stdin=None):
            reads.append((argv, stdin))
            if "secret" in argv:
                return json.dumps(secrets[argv[argv.index("secret") + 1]])
            if argv[5:7] == ["create", "job"]:
                return "job.batch/created\n"
            if argv[5:7] == ["get", "job"]:
                state = states.pop(0) if len(states) > 1 else states[0]
                return json.dumps(ingest_job(state))
            raise AssertionError(f"unexpected {argv}")

        def fake_run(argv, input=None, **kwargs):
            if "logs" in argv:
                pods.append((argv, input))
                return subprocess.CompletedProcess(argv, 0, log, "")
            pods.append((argv, input))
            step = argv[argv.index("-c") + 2]
            out = pod_out.get(step, f"ok   {step} done\n")
            return subprocess.CompletedProcess(argv, 1 if "FAIL" in out else 0, out, "")
        return reads, pods, fake_output, fake_run

    def check(self, fake_output, fake_run, timeout=900):
        pins = {"standin_images": {"recorder": {"image": PYTHON_IMAGE}}}
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds
        with mock.patch.object(gate, "output", side_effect=fake_output), \
                mock.patch.object(gate.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(gate, "INGESTION_TIMEOUT", timeout):
            return gate.check_datahub_ingestion(INGEST_ENV, pins, sleep=sleep,
                                                clock=lambda: clock[0])

    def steps(self, pods):
        return [argv[argv.index("-c") + 2] for argv, _ in pods if "logs" not in argv]

    def test_seed_run_read_refuse_then_clean_in_order(self):
        reads, pods, fake_output, fake_run = self.fake_cluster()
        problems, notes = self.check(fake_output, fake_run)
        self.assertEqual(problems, [])
        self.assertEqual(self.steps(pods), ["seed", "ingested", "refused", "clean"])
        (create,) = [argv for argv, _ in reads if argv[5:7] == ["create", "job"]]
        self.assertEqual(create, [
            "kubectl", "--context", gate.CONTEXT, "-n", "catalog-datahub", "create", "job",
            "--from=cronjob/datahub-datahub-ingestion-cron-polaris",
            f"check-ingest-{os.getpid()}"])
        self.assertIn("catalog gate_ingest_catalog: passed", notes)
        self.assertTrue(any("completed in 90 s" in n for n in notes), notes)

    def test_each_pod_its_namespace_labels_arguments_and_credential(self):
        _, pods, fake_output, fake_run = self.fake_cluster()
        self.check(fake_output, fake_run)
        by_step = {argv[argv.index("-c") + 2]: (argv, stdin) for argv, stdin in pods
                   if "logs" not in argv}
        polaris = "http://polaris.catalog-polaris.svc.cluster.local:8181"
        gms = "http://datahub-datahub-gms.catalog-datahub.svc.cluster.local:8080"
        seed, seed_in = by_step["seed"]
        self.assertEqual(seed[seed.index("-c") + 2:], [
            "seed", polaris, "POLARIS", "gate_ingest_catalog",
            "http://recorder.standins.svc.cluster.local:8333", "us-east-1", "polaris-warehouse"])
        self.assertEqual(seed_in, f"{ROOT_ID}:{ROOT_SECRET}\n")
        for step in ("ingested", "clean"):
            argv, stdin = by_step[step]
            self.assertIn("--labels=lakehouse/datahub-gms-client=true", argv)
            self.assertEqual(argv[argv.index("-c") + 2:], [step, gms, "gate_ingest_catalog"])
            self.assertEqual(stdin, DATAHUB_SECRET + "\n")
        refused, refused_in = by_step["refused"]
        self.assertEqual(refused[refused.index("-c") + 2:],
                         ["refused", polaris, "POLARIS", "gate_ingest_catalog"])
        self.assertFalse([a for a in refused if a.startswith("--labels")])
        flag = refused[refused.index("--overrides") + 1]
        (container,) = json.loads(flag)["spec"]["containers"]
        self.assertEqual(container["name"], refused[refused.index("run") + 1])
        self.assertEqual({e["name"]: e["valueFrom"]["secretKeyRef"] for e in container["env"]}, {
            "CLIENT_ID": {"name": "datahub-polaris-principal", "key": "clientId"},
            "CLIENT_SECRET": {"name": "datahub-polaris-principal", "key": "clientSecret"}})
        self.assertEqual(refused_in.strip(), "")
        for argv, _ in by_step.values():
            self.assertEqual(argv[argv.index("-n") + 1], "catalog-datahub")
            self.assertNotIn(gate.CHECK_POD_LABEL, argv)
            for value in (ROOT_ID, ROOT_SECRET, INGEST_SECRET, DATAHUB_SECRET):
                self.assertNotIn(value, " ".join(argv))

    def test_a_failed_seed_stops_before_the_run(self):
        reads, pods, fake_output, fake_run = self.fake_cluster(
            pod_out={"seed": "FAIL catalog gate_ingest_catalog created: HTTP 500\n"})
        problems, _ = self.check(fake_output, fake_run)
        self.assertEqual(problems, ["catalog gate_ingest_catalog created: HTTP 500"])
        self.assertEqual(self.steps(pods), ["seed"])
        self.assertFalse([a for a, _ in reads if a[5:7] == ["create", "job"]])

    def test_a_failed_run_prints_its_log_skips_the_reads_and_still_cleans(self):
        log = (f"catalog gate_ingest_catalog: ingesting\nerror 403 for {INGEST_ID}\n"
               "catalog gate_ingest_catalog: failed\n")
        _, pods, fake_output, fake_run = self.fake_cluster(job_states=(None, "failed"), log=log)
        problems, _ = self.check(fake_output, fake_run)
        (problem,) = problems
        self.assertIn("Job check-ingest-", problem)
        self.assertIn("failed", problem)
        self.assertIn("error 403 for <redacted>", problem)
        self.assertNotIn(INGEST_ID, problem)
        self.assertEqual(self.steps(pods), ["seed", "refused", "clean"])

    def test_a_run_past_the_time_limit_fails_and_still_cleans(self):
        _, pods, fake_output, fake_run = self.fake_cluster(job_states=(None,))
        problems, _ = self.check(fake_output, fake_run, timeout=30)
        self.assertTrue(any("not finished within 30 s" in p for p in problems), problems)
        self.assertEqual(self.steps(pods)[-1], "clean")

    def test_a_failed_read_or_refusal_is_a_problem_and_clean_still_runs(self):
        _, pods, fake_output, fake_run = self.fake_cluster(pod_out={
            "ingested": "FAIL urn:x ingested with its fields a: HTTP 404\n",
            "refused": "FAIL datahub_ingest creating a namespace refused: HTTP 200\n"})
        problems, _ = self.check(fake_output, fake_run)
        self.assertEqual(problems, ["urn:x ingested with its fields a: HTTP 404",
                                    "datahub_ingest creating a namespace refused: HTTP 200"])
        self.assertEqual(self.steps(pods)[-1], "clean")

    def test_it_is_a_command_of_the_gate(self):
        self.assertIn("| check-datahub-ingestion |", gate.__doc__.strip().splitlines()[-1])
        with mock.patch.object(gate, "check_datahub_ingestion",
                               return_value=([], ["ok"])) as called, \
                mock.patch.object(gate.release, "load_env", return_value=INGEST_ENV), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gate.main(["gate.py", "check-datahub-ingestion"]), 0)
        called.assert_called_once()


if __name__ == "__main__":
    unittest.main()
